"""W2: Winlink sessions and mailbox through Radio Remote, with tests/fake_pat.py as Pat.

Checks:
  - mailbox/read/compose/gateway routes reach Pat; anything off the list is 404
  - compose without a date gets one added (Pat answers 400 without it); Cc
    passes through; compose with an X-Pat-SourcePath header is refused
  - mark read: {"read": true} marks it read, a bare `true` is Pat's 400
  - archive (move) and delete reach Pat; a bad source path, a dotfile MID or
    an encoded traversal in the source path never reach Pat
  - the outbox can't be changed while a session runs (409), other boxes can;
    in process, the guard covers the admit window and connect() waits for a
    mailbox change already under way
  - Pat's own port is 127.0.0.1 only
  - winlink_connect arms the lane, Pat keys through it, the page sees the log,
    the session end, the received count; afterwards the lane is disarmed
  - a bad connect URL (unknown transport) is refused before anything happens
  - Stop aborts a session (Pat /api/disconnect), radio unkeyed, disarmed
  - the page closing mid-session aborts it and unkeys
  - --winlink-session-max cuts a session that runs too long
  - a busy-channel prompt reaches the page and its answer reaches Pat
  - Pat dying is noticed and Pat is started again
  - --no-tx: connect refused

    .venv/bin/python tests/winlink_test.py
"""
import asyncio
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types

import aiohttp
import testkit  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPS = 15000 + random.randrange(1000)
LANE = 16000 + random.randrange(1000)
PATP = 17000 + random.randrange(1000)
WEB = 18000 + random.randrange(1000)
URL = "ardop:///N0DAJ?bw=2000MAX&freq=3588.5"
fails = 0



def lan_address():
    """A non-loopback address of this machine on which a listener bound to
    0.0.0.0 answers, or None. The hostname may resolve to 127.0.0.1 (mac-shack)
    and the default route may be a VPN (unreachable even for 0.0.0.0), so try
    several routes and keep the first one the control can actually reach."""
    cands = []
    try:
        cands += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    for target in ("192.168.1.1", "192.168.1.1", "10.0.0.1", "192.0.2.1"):  # ranch-radio: allow (generic test addresses)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
                u.connect((target, 9))       # routing lookup only; nothing is sent
                cands.append(u.getsockname()[0])
        except OSError:
            pass
    for ip in dict.fromkeys(cands):
        if ip.startswith("127.") or ip == "0.0.0.0":
            continue
        with socket.socket() as ctl:
            ctl.bind(("0.0.0.0", 0))
            ctl.listen()
            try:
                socket.create_connection((ip, ctl.getsockname()[1]), timeout=1).close()
                return ip
            except OSError:
                continue
    return None

def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:56} got {got!r}" + ("" if ok else f" want {want!r}"))


def ptt():
    with socket.create_connection(("127.0.0.1", APPS), timeout=3) as s:
        f = s.makefile("r")
        s.sendall(b"t\n")
        v = f.readline().strip()
        f.close()
        return v


def lane_ptt_refused():
    with socket.create_connection(("127.0.0.1", LANE), timeout=3) as s:
        f = s.makefile("r")
        s.sendall(b"T 1\n")
        v = f.readline().strip()
        if v == "RPRT 0":
            s.sendall(b"T 0\n")
            f.readline()
        f.close()
        return v == "RPRT -11"


class Page:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.events = []
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.run(self._open())

    def run(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _open(self):
        self.sess = aiohttp.ClientSession()
        self.ws = await self.sess.ws_connect(f"http://127.0.0.1:{WEB}/ws")
        json.loads((await self.ws.receive()).data)
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                m = json.loads(msg.data)
                if m.get("t") in ("winlink", "notice"):
                    self.events.append(m)

    def send(self, m):
        self.run(self.ws.send_str(json.dumps(m)))

    def wait(self, pred, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            for e in list(self.events):
                if pred(e):
                    return e
            time.sleep(0.1)
        return None

    def http(self, method, path, **kw):
        async def go():
            async with self.sess.request(method, f"http://127.0.0.1:{WEB}{path}", **kw) as r:
                return r.status, await r.text()
        return self.run(go())

    def close(self):
        async def _c():
            await self.ws.close()
            await self.sess.close()
        self.run(_c())
        time.sleep(0.5)


def ended(e):
    return e.get("t") == "winlink" and e.get("session") == "ended"


def fake_log(path):
    try:
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]
    except OSError:
        return []


def recs(path, route, **match):
    """The fake's log lines for one route, matching every key=value given."""
    return [r for r in fake_log(path) if r.get("route") == route
            and all(r.get(k) == v for k, v in match.items())]


def reached(path, needle):
    """Requests that reached the fake whose path or source header contains needle."""
    return [r for r in recs(path, "req")
            if needle in r.get("path", "") or needle in (r.get("headers") or {}).get("X-Pat-SourcePath", "")]


def mids(page, box):
    st, body = page.http("GET", f"/winlink/api/mailbox/{box}")
    return sorted(m["MID"] for m in json.loads(body)) if st == 200 else f"HTTP {st}"


def start(env_extra=None, *extra):
    logf = tempfile.NamedTemporaryFile(prefix="fakepat-", suffix=".jsonl", delete=False).name
    env = dict(os.environ, FAKE_PAT_RIGCTL=str(LANE), FAKE_PAT_LOG=logf, **(env_extra or {}))
    state = os.path.join(tempfile.mkdtemp(prefix="wltest-"), "addons.json")   # never the user's own
    p = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB),
         "--rigctl-port", str(APPS), "--winlink-rigctl-port", str(LANE),
         "--winlink-pat", testkit.fake_pat_cmd(), "--winlink-pat-port", str(PATP),
         "--addons-state", state, *extra],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            for port in (WEB, LANE, PATP):
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            time.sleep(0.5)          # Radio Remote's websocket to Pat
            return p, logf
        except OSError:
            time.sleep(0.1)
    p.kill()
    print(p.stdout.read())
    raise SystemExit("server did not start")


def stop(p):
    p.terminate()
    try:
        return p.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired:
        p.kill()
        return p.communicate()[0]


def show(out):
    for line in out.splitlines():
        if "winlink" in line.lower() or "pat" in line.lower() or "Traceback" in line:
            print("   log:", line[:200])


# --- routes and a normal session ---
server, flog = start({"FAKE_PAT_TX_S": "1.5"}, "--winlink-session-max", "6")
try:
    page = Page()
    st, body = page.http("GET", "/winlink/api/mailbox/in")
    check("GET inbox", (st, json.loads(body)[0]["MID"]), (200, "ABC123"))
    st, body = page.http("GET", "/winlink/api/mailbox/in/ABC123")
    check("GET message body", (st, json.loads(body)["Body"].strip()), (200, "Hello from the test."))
    check("GET unknown box -> 404", page.http("GET", "/winlink/api/mailbox/bogus")[0], 404)
    check("config route not proxied -> 404", page.http("GET", "/winlink/api/config")[0], 404)
    check("connect route not proxied -> 404", page.http("GET", f"/winlink/api/connect?url={URL}")[0], 404)
    st, body = page.http("GET", "/winlink/api/rmslist?mode=ardop&band=80m")
    check("GET gateway list", (st, json.loads(body)[0]["callsign"]), (200, "N0DAJ"))

    form = aiohttp.FormData()
    form.add_field("to", "N0DAJ")
    form.add_field("subject", "hi")
    form.add_field("body", "test body")
    form.add_field("files", b"attached", filename="a.txt")
    st, body = page.http("POST", "/winlink/api/mailbox/out", data=form)
    check("compose -> 201", st, 201)
    comp = [r for r in fake_log(flog) if r["route"] == "compose"]
    check("compose: date added for Pat", bool(comp and comp[-1]["fields"].get("date")), True)
    check("compose: attachment passed", comp and [(f["name"], f["size"]) for f in comp[-1]["files"]],
          [("a.txt", 8)])

    # A2 (before A1, so its "no read line" is clean): a bare `true` is Pat's 400
    st, body = page.http("POST", "/winlink/api/mailbox/in/ABC123/read", data="true",
                         headers={"Content-Type": "application/json"})
    check("A2 mark read with body `true` -> 400 through the proxy", st, 400)
    check("A2   the request reached the fake", len(reached(flog, "/api/mailbox/in/ABC123/read")) >= 1, True)
    check("A2   and earned no read record", recs(flog, "read"), [])
    # A1: {"read": true} marks it read
    st, body = page.http("POST", "/winlink/api/mailbox/in/ABC123/read", data='{"read": true}',
                         headers={"Content-Type": "application/json"})
    check("A1 mark read {\"read\": true} -> 200", st, 200)
    check("A1   fake recorded read: true", [r["read"] for r in recs(flog, "read", mid="ABC123")], [True])
    st, body = page.http("GET", "/winlink/api/mailbox/in")
    check("A1   inbox shows ABC123 read", [m["Unread"] for m in json.loads(body) if m["MID"] == "ABC123"], [False])

    # A3: archive = move with X-Pat-SourcePath
    st, _ = page.http("POST", "/winlink/api/mailbox/archive",
                      headers={"X-Pat-SourcePath": "/api/mailbox/in/DEF456"})
    check("A3 archive DEF456 from in -> 200", st, 200)
    check("A3   gone from in", "DEF456" in mids(page, "in"), False)
    check("A3   listed in archive", mids(page, "archive"), ["DEF456"])
    check("A3   fake recorded the move", [(r["src"], r["mid"], r["dest"]) for r in recs(flog, "move")],
          [("in", "DEF456", "archive")])

    # A4: bad source paths; compose with the header; dest in from archive
    st, _ = page.http("POST", "/winlink/api/mailbox/archive", headers={"X-Pat-SourcePath": "/api/mailbox/bogus/X"})
    check("A4 move from an unknown box -> 400", st, 400)
    st, _ = page.http("POST", "/winlink/api/mailbox/archive")
    check("A4 move without X-Pat-SourcePath -> 400", st, 400)
    n_comp = len(recs(flog, "compose"))
    form = aiohttp.FormData()
    for k, v in (("to", "N0DAJ"), ("subject", "not a move"), ("body", "b")):
        form.add_field(k, v)
    st, body = page.http("POST", "/winlink/api/mailbox/out", data=form,
                         headers={"X-Pat-SourcePath": "/api/mailbox/in/ABC123"})
    check("A4 POST to out with X-Pat-SourcePath -> 400 use the move route",
          (st, body.strip(), len(recs(flog, "compose")) - n_comp), (400, "use the move route", 0))
    st, _ = page.http("POST", "/winlink/api/mailbox/in", headers={"X-Pat-SourcePath": "/api/mailbox/archive/DEF456"})
    check("A4 move DEF456 archive -> in -> 200 (allowed)", (st, "DEF456" in mids(page, "in")), (200, True))

    # A6: a dotfile MID never reaches Pat (yarl normalises . and .. segments
    # before they get to {mid}, so those would prove nothing)
    st_d, _ = page.http("DELETE", "/winlink/api/mailbox/in/.hidden")
    st_g, _ = page.http("GET", "/winlink/api/mailbox/in/.hidden")
    check("A6 DELETE and GET in/.hidden -> 404", (st_d, st_g), (404, 404))
    check("A6   no request for .hidden reached the fake", reached(flog, ".hidden"), [])
    # A6b: an encoded traversal in the move source (in/X/../../out/OUT0 to Pat)
    st, _ = page.http("POST", "/winlink/api/mailbox/archive",
                      headers={"X-Pat-SourcePath": "/api/mailbox/in/X%2F..%2F..%2Fout%2FOUT0"})
    check("A6b move source in/X%2F..%2F..%2Fout%2FOUT0 -> 400", st, 400)
    check("A6b   no request for it reached the fake", reached(flog, "X%2F"), [])

    lan_ip = lan_address()
    check("a LAN address that reaches a 0.0.0.0 listener (control)", lan_ip is not None, True)
    if lan_ip is not None:              # no usable address: the control above already failed
        try:
            socket.create_connection((lan_ip, PATP), timeout=1).close()
            lan = True
        except OSError:
            lan = False
        check(f"Pat's port not on the LAN address ({lan_ip})", lan, False)

    page.send({"t": "winlink_connect", "url": "serial-tnc:///N0DAJ"})
    e = page.wait(lambda e: e.get("t") == "notice" and "transport" in e.get("msg", "").lower(), 5)
    check("unsupported transport refused", e is not None, True)
    page.send({"t": "winlink_connect", "url": "ardop:///N0DAJ?freq=14400"})
    e = page.wait(lambda e: e.get("t") == "notice" and "band" in e.get("msg", "").lower(), 5)
    check("off-band frequency refused", e is not None, True)
    check("refused URL: lane still disarmed", lane_ptt_refused(), True)

    page.events.clear()
    page.send({"t": "winlink_connect", "url": URL})
    e = page.wait(lambda e: e.get("t") == "winlink" and e.get("armed") is True, 5)
    check("connect: armed", e is not None, True)
    e = page.wait(lambda e: e.get("t") == "winlink" and "Connecting to" in str(e.get("log", "")), 5)
    check("connect: Pat's log relayed", e is not None, True)
    time.sleep(0.7)
    check("connect: radio keyed through the lane", ptt(), "1")
    e = page.wait(ended, 15)
    check("session ended ok, 1 received", e and (e.get("ok"), e.get("received")), (True, 1))
    check("session ended: the composed message counted sent, none left",
          e and (e.get("sent"), e.get("unsent")), (1, 0))
    st, body = page.http("GET", "/winlink/api/mailbox/sent")
    check("the message is in sent/", (st, len(json.loads(body))), (200, 1))
    check("after session: radio unkeyed", ptt(), "0")
    check("after session: lane disarmed", lane_ptt_refused(), True)
    check("Pat got the URL", [r["url"] for r in fake_log(flog) if r["route"] == "connect"][-1:], [URL])
    n = page.wait(lambda e: e.get("t") == "winlink" and "notify" in e, 2)
    check("new-message notification relayed", n is not None, True)

    # A5: delete a message in sent/ (the one the session just sent)
    sent_mid = (mids(page, "sent") or ["?"])[0]
    st, _ = page.http("DELETE", f"/winlink/api/mailbox/sent/{sent_mid}")
    check("A5 DELETE sent/<mid> -> 200", st, 200)
    check("A5   gone from sent", mids(page, "sent"), [])
    check("A5   fake recorded the delete", [(r["box"], r["mid"]) for r in recs(flog, "delete")], [("sent", sent_mid)])
    # A8: Cc passes through compose
    form = aiohttp.FormData()
    for k, v in (("to", "N0DAJ"), ("cc", "K7ABC N0XYZ"), ("subject", "cc"), ("body", "b")):
        form.add_field(k, v)
    st, _ = page.http("POST", "/winlink/api/mailbox/out", data=form)
    check("A8 compose with cc -> 201, fake got cc",
          (st, (recs(flog, "compose") or [{}])[-1].get("fields", {}).get("cc")), (201, "K7ABC N0XYZ"))

    # Stop aborts
    page.events.clear()
    os.environ["FAKE_PAT_TX_S"] = "1.5"
    page.send({"t": "winlink_connect", "url": URL})
    page.wait(lambda e: e.get("t") == "winlink" and e.get("armed") is True, 5)
    time.sleep(0.8)
    page.send({"t": "winlink_stop"})
    e = page.wait(ended, 10)
    check("stop: session ended not ok", e and e.get("ok"), False)
    check("stop: nothing counted sent", e and e.get("sent"), 0)
    check("stop: Pat was told to disconnect",
          any(r["route"] == "disconnect" for r in fake_log(flog)), True)
    check("stop: radio unkeyed", ptt(), "0")
    check("stop: lane disarmed", lane_ptt_refused(), True)

    # second connect while one runs is refused
    page.events.clear()
    page.send({"t": "winlink_connect", "url": URL})
    page.wait(lambda e: e.get("t") == "winlink" and e.get("armed") is True, 5)
    page.send({"t": "winlink_connect", "url": URL})
    e = page.wait(lambda e: e.get("t") == "notice" and "already" in e.get("msg", "").lower(), 3)
    check("second connect while one runs refused", e is not None, True)
    page.wait(ended, 10)

    # page closing mid-session
    page.events.clear()
    page.send({"t": "winlink_connect", "url": URL})
    page.wait(lambda e: e.get("t") == "winlink" and e.get("armed") is True, 5)
    time.sleep(0.6)
    before = len([r for r in fake_log(flog) if r["route"] == "disconnect"])
    page.close()
    time.sleep(1.5)
    check("page closed: Pat told to disconnect",
          len([r for r in fake_log(flog) if r["route"] == "disconnect"]) > before, True)
    check("page closed: radio unkeyed", ptt(), "0")
    check("page closed: lane disarmed", lane_ptt_refused(), True)

    # Pat dies and comes back
    pid = [r["pid"] for r in fake_log(flog) if r["route"] == "start"][-1]
    os.kill(pid, 9)
    time.sleep(4)
    page = Page()
    st, _ = page.http("GET", "/winlink/api/mailbox/in")
    check("Pat restarted after dying", (st, len([r for r in fake_log(flog) if r["route"] == "start"])), (200, 2))
    page.close()
finally:
    show(stop(server))

# --- session cap and busy prompt ---
server, flog = start({"FAKE_PAT_TX_S": "30"}, "--winlink-session-max", "4")
try:
    page = Page()
    page.send({"t": "winlink_connect", "url": URL})
    e = page.wait(ended, 20)
    check("--winlink-session-max 4 cuts a long session", e and e.get("ok"), False)
    check("cap: dirty disconnect sent",
          any(r["route"] == "disconnect" and r["dirty"] == "true" for r in fake_log(flog)), True)
    check("cap: radio unkeyed", ptt(), "0")
    page.close()
finally:
    show(stop(server))

server, flog = start({"FAKE_PAT_BUSY": "1"})
try:
    page = Page()
    page.send({"t": "winlink_connect", "url": URL})
    e = page.wait(lambda e: e.get("t") == "winlink" and (e.get("prompt") or {}).get("kind") == "busy-channel", 5)
    check("busy-channel prompt reaches the page", e is not None, True)
    page.send({"t": "winlink_prompt", "id": "busy1", "value": "abort"})
    e = page.wait(ended, 10)
    check("answer reaches Pat, session ends", (e and e.get("ok"),
          [r.get("value") for r in fake_log(flog) if r["route"] == "prompt_answer"]), (False, ["abort"]))
    check("busy abort: never keyed", any(r["route"] == "ptt_on" for r in fake_log(flog)), False)
    page.close()
finally:
    show(stop(server))

# --- Pat puts the dial back after a session: the radio stays put, no tune ---
# Live 2026-10-15: Pat's QSX (revert) reaches the lane ~1 s BEFORE /api/connect
# returns, i.e. while the session is still armed; the band change it caused
# fired an ATU tune carrier after every session.
server, flog = start({"FAKE_PAT_QSY": "1", "FAKE_PAT_TX_S": "1"}, "--auto-tune")
try:
    def freq():
        with socket.create_connection(("127.0.0.1", APPS), timeout=3) as s:
            f = s.makefile("r")
            s.sendall(b"f\n")
            v = int(f.readline().strip())
            f.close()
            return v

    def tunes(page):
        return [e["msg"] for e in page.events
                if e.get("t") == "notice" and "ATU tuning" in e.get("msg", "")]

    time.sleep(4)                                  # past the auto-tune arming window
    page = Page()
    with socket.create_connection(("127.0.0.1", APPS), timeout=3) as s:
        f = s.makefile("r")
        s.sendall(b"F 10136000\n")                # an app left the radio on 30 m
        f.readline()
        f.close()
    time.sleep(6)                                  # its 30 m tune (2 s settle + 2.5 s) ends
    page.events.clear()
    page.send({"t": "winlink_connect", "url": URL})
    e = page.wait(ended, 15)
    check("QSY session ended ok", e and e.get("ok"), True)
    check("Pat's QSY reached the radio",
          [(r["hz"], r["reply"]) for r in fake_log(flog) if r["route"] == "qsy"], [(3588500, "RPRT 0")])
    # The server puts the radio on the session's frequency before Pat dials
    # (hold Pat until the tune is done, 2026-11-07), so the frequency Pat
    # reads to put back afterwards is the session's own: its QSX is a no-op,
    # never a band change. (Before, it reverted toward 30 m and was ignored.)
    check("Pat's QSX answered ok (no Pat error), back to where it already is",
          [(r["hz"], r["reply"]) for r in fake_log(flog) if r["route"] == "qsx"], [(3588500, "RPRT 0")])
    time.sleep(3)
    check("after session: radio stays on the session frequency", freq(), 3588500)
    check("after session: only the session's band tuned", tunes(page), ["ATU tuning for 80 m (band change)"])
    page.close()
finally:
    out = stop(server)
lane = [l for l in out.splitlines() if "winlink lane" in l and "set_freq" in l]
check("lane log: Pat's QSY and its no-op QSX, both on the session frequency, never 30 m",
      [("3588500 Hz applied" in l, "10136000" in l) for l in lane], [(True, False), (True, False)])
for l in [x for x in out.splitlines() if "refused" in x or "tun" in x] + lane:
    print("   log:", l[:200])

# --- what a session SENT: Pat's outbox at arm vs sent/ at the end ---
def compose(page, subject):
    form = aiohttp.FormData()
    for k, v in (("to", "N0DAJ"), ("subject", subject), ("body", "b")):
        form.add_field(k, v)
    return page.http("POST", "/winlink/api/mailbox/out", data=form)[0]


server, flog = start({"FAKE_PAT_SEND_N": "1"})
try:
    page = Page()
    check("two messages queued", (compose(page, "one"), compose(page, "two")), (201, 201))
    page.send({"t": "winlink_connect", "url": URL})
    e = page.wait(ended, 15)
    check("gateway took one of two: sent 1, 1 left in the outbox",
          e and (e.get("ok"), e.get("sent"), e.get("unsent")), (True, 1, 1))
    page.events.clear()
    page.send({"t": "winlink_connect", "url": URL})
    e = page.wait(ended, 15)
    check("next session sends the leftover", e and (e.get("sent"), e.get("unsent")), (1, 0))
    page.events.clear()
    page.send({"t": "winlink_connect", "url": URL})
    e = page.wait(ended, 15)
    check("empty outbox: sent 0 (a count, not unknown)", e and (e.get("sent"), e.get("unsent")), (0, 0))
    page.close()
finally:
    out = stop(server)
check("log line reports sent and received",
      any("session ended ok=True sent=1 received=1" in l for l in out.splitlines()), True)

server, flog = start({"FAKE_PAT_BROKEN_BOX": "sent"})
try:
    page = Page()
    compose(page, "one")
    page.send({"t": "winlink_connect", "url": URL})
    e = page.wait(ended, 15)
    check("sent/ unreadable: session still ok, sent unknown (None), not 0",
          e and (e.get("ok"), "sent" in e, e.get("sent"), e.get("received")), (True, True, None, 1))
    page.close()
finally:
    stop(server)

# --- A7: the outbox is Pat's while a session runs ---
server, flog = start({"FAKE_PAT_TX_S": "30"})
try:
    page = Page()
    check("A7 one message queued", compose(page, "queued"), 201)
    page.send({"t": "winlink_connect", "url": URL})
    e = page.wait(lambda e: e.get("t") == "winlink" and e.get("session") == "started", 10)
    check("A7 session started", e is not None, True)
    st, body = page.http("DELETE", "/winlink/api/mailbox/out/OUT0")
    check("A7 during the session: DELETE out/OUT0 -> 409", st, 409)
    check("A7   still listed in out", mids(page, "out"), ["OUT0"])
    st, _ = page.http("POST", "/winlink/api/mailbox/archive", headers={"X-Pat-SourcePath": "/api/mailbox/out/OUT0"})
    check("A7   move from out -> 409", st, 409)
    check("A7   neither reached the fake", reached(flog, "OUT0"), [])
    st, _ = page.http("DELETE", "/winlink/api/mailbox/in/ABC123")
    check("A7   DELETE in/ABC123 (another box) -> 200", st, 200)
    page.send({"t": "winlink_stop"})
    e = page.wait(ended, 20)
    check("A7 session ended", e is not None, True)
    st, _ = page.http("DELETE", "/winlink/api/mailbox/out/OUT0")
    check("A7 after the session: DELETE out/OUT0 -> 200", (st, mids(page, "out")), (200, []))
    page.close()
finally:
    show(stop(server))


# --- A7b: in process, the guard covers the admit window, under a lock ---
def a7b():
    sys.path.insert(0, ROOT)
    import winlink as W
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    class Station:
        def notice(self, *a, **k):
            pass

        def _send(self, *a):
            pass

        def _broadcast(self, *a):
            pass

        def band_of(self, hz):
            return "80m"

    async def go():
        res = {}
        args = types.SimpleNamespace(winlink_pat_port=1, winlink_pat="pat", winlink_session_max=60)
        eng = W.PatEngine(Station(), args)
        eng.running = True
        calls = []

        async def fake_pass(method, path, json_out=False, **kw):
            calls.append(method)
            return web.Response(text='"OK"')

        eng._pass = fake_pass
        app = web.Application()
        eng.routes(app, lambda r: True)
        src = {"X-Pat-SourcePath": "/api/mailbox/out/OUT0"}
        async def probe(req):                     # (status, how many times Pat was called)
            n = len(calls)
            return (await req).status, len(calls) - n

        async with TestClient(TestServer(app)) as c:
            eng._admitting = True
            res["admitting: delete out"] = await probe(c.delete("/winlink/api/mailbox/out/OUT0"))
            res["admitting: move from out"] = await probe(c.post("/winlink/api/mailbox/archive", headers=src))
            eng._admitting = False
            # No Status from Pat yet: its state is unknown, which counts as
            # busy (A4). Then Pat says it is idle, as its WebSocket would.
            res["unknown: delete out"] = await probe(c.delete("/winlink/api/mailbox/out/OUT0"))
            eng.pat_ws, eng._status_live = object(), True
            eng.status = {"connected": False, "dialing": False, "active_listeners": [], "remote_addr": ""}
            res["idle: delete out"] = await probe(c.delete("/winlink/api/mailbox/out/OUT0"))
            res["idle: move from out"] = await probe(c.post("/winlink/api/mailbox/archive", headers=src))
        try:
            got = asyncio.Event()

            async def fake_admit(ws, url, via, peer=False):
                got.set()

            eng._admit = fake_admit
            await eng._outbox_lock.acquire()      # a mailbox change past its check, mid-call
            t = asyncio.create_task(eng.connect(None, "telnet://N9ABC:CMSTelnet@cms.winlink.org:8772/wl2k"))
            try:
                await asyncio.wait_for(got.wait(), 0.3)   # quiet window: it must NOT get there
                early = True
            except TimeoutError:
                early = False
            admitting = eng._admitting
            eng._outbox_lock.release()
            try:
                await asyncio.wait_for(got.wait(), 3)
                late = True
            except TimeoutError:
                late = False
            await asyncio.wait_for(t, 3)
            res["connect waits for the lock"] = (early, admitting, late)
        except Exception as e:                    # noqa: BLE001 - a failed check, not an abort
            res["connect waits for the lock"] = f"error: {type(e).__name__}: {e}"
        return res

    try:
        return asyncio.run(go())
    except Exception as e:                        # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


r = a7b()
check("A7b _admitting: delete out -> 409, Pat not called", r.get("admitting: delete out"), (409, 0))
check("A7b _admitting: move from out -> 409, Pat not called", r.get("admitting: move from out"), (409, 0))
check("A7b Pat's state unknown: delete out -> 409, Pat not called", r.get("unknown: delete out"), (409, 0))
check("A7b idle: delete out passes through", r.get("idle: delete out"), (200, 1))
check("A7b idle: move from out passes through", r.get("idle: move from out"), (200, 1))
check("A7b connect() waits for a held outbox lock (early, admitting, after)",
      r.get("connect waits for the lock"), (False, True, True))

server, flog = start({}, "--no-tx")
try:
    page = Page()
    page.send({"t": "winlink_connect", "url": URL})
    e = page.wait(lambda e: e.get("t") == "notice" and e.get("level") == "warn", 5)
    check("--no-tx: connect refused", e is not None, True)
    check("--no-tx: Pat never asked to connect",
          any(r["route"] == "connect" for r in fake_log(flog)), False)
    page.close()
finally:
    stop(server)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
