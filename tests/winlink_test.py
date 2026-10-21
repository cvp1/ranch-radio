"""W2: Winlink sessions and mailbox through Radio Remote, with tests/fake_pat.py as Pat.

Checks:
  - mailbox/read/compose/gateway routes reach Pat; anything off the list is 404
  - compose without a date gets one added (Pat answers 400 without it)
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

import aiohttp

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


def start(env_extra=None, *extra):
    logf = tempfile.NamedTemporaryFile(prefix="fakepat-", suffix=".jsonl", delete=False).name
    env = dict(os.environ, FAKE_PAT_RIGCTL=str(LANE), FAKE_PAT_LOG=logf, **(env_extra or {}))
    p = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB),
         "--rigctl-port", str(APPS), "--winlink-rigctl-port", str(LANE),
         "--winlink-pat", f"{sys.executable} tests/fake_pat.py", "--winlink-pat-port", str(PATP),
         *extra],
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
    check("compose: attachment passed", comp and comp[-1]["files"], ["a.txt"])

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
    check("Pat's QSX answered ok (no Pat error)",
          [(r["hz"], r["reply"]) for r in fake_log(flog) if r["route"] == "qsx"], [(10136000, "RPRT 0")])
    time.sleep(3)
    check("after session: radio stays on the session frequency", freq(), 3588500)
    check("after session: only the session's band tuned", tunes(page), ["ATU tuning for 80 m (band change)"])
    page.close()
finally:
    out = stop(server)
lane = [l for l in out.splitlines() if "winlink lane" in l and "set_freq" in l]
check("lane log names the QSY and the ignored QSX",
      [("3588500 Hz applied" in l, "10136000 Hz ignored" in l) for l in lane], [(True, False), (False, True)])
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
