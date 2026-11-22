"""auth readonly-lan: a page without the operator's session watches and
presses STOP TX, and nothing else; through the station's proxy, as a
browser reaches a radio (review plan PR 4, panel 2026-11-21). Simulated
radio (server.py --sim) and fake TNC only: no RF.

  - read-only, through /r/<id>/ws: hello says so; a QSY and a key-up are
    refused and the radio does not move; STOP TX works
  - an operator keys; a read-only page's STOP TX unkeys it and latches; the
    read-only page cannot resume; the operator can
  - /auth/login: a wrong token 401; the right one sets rr_op (HttpOnly,
    SameSite=Strict); with it the same proxied socket operates; a tampered
    or expired cookie is read-only
  - a session that ends while its page holds PTT: unkeyed, read-only
  - the station's own socket refuses Winlink/JS8/Modules from a read-only
    page; its mutating HTTP is 403; the hub's transmit Off is let through,
    On is not
  - fail closed: a token file others can read, or none, stops the server
  - the token is in no rendered file or command line (only its path)

    python3 tests/auth_roles_test.py
"""
import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
import aiohttp  # noqa: E402
import testkit  # noqa: E402
import tnc_hub_test as T  # noqa: E402

fails = 0
TOKEN = "correct-horse-battery-1"
TMP = tempfile.mkdtemp(prefix="authroles-")
TOKEN_FILE = os.path.join(TMP, "token")
with open(TOKEN_FILE, "w") as f:
    f.write(TOKEN + "\n")
os.chmod(TOKEN_FILE, 0o600)
AUTH = ("--auth-mode", "readonly-lan", "--auth-token-file", TOKEN_FILE)


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:64} got {got!r}" + ("" if ok else f" want {want!r}"))


def cookie(exp):
    return f"{exp}.{hmac.new(TOKEN.encode(), f'operator|{exp}'.encode(), hashlib.sha256).hexdigest()}"


def http(method, port, path, body=None, cookie_v=None, headers=None):
    h = {"content-type": "application/json", "X-Ranch-Radio": "1", "Origin": f"http://127.0.0.1:{port}", **(headers or {})}
    if cookie_v:
        h["Cookie"] = f"rr_op={cookie_v}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def health(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as r:
            return json.load(r)
    except OSError:
        return {}


class Page:
    """A page's control socket (through the station's proxy unless direct),
    read all the time on its own thread, as a browser does (the proxy's
    heartbeat closes a socket nobody reads)."""

    def __init__(self, url, cookie_v=None, headers=None):
        self.url, self.msgs = url, []
        self.h = {"Cookie": f"rr_op={cookie_v}"} if cookie_v else {}
        self.h.update(headers or {})
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coro, timeout=15):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _open(self):
        self.s = aiohttp.ClientSession()
        self.ws = await self.s.ws_connect(self.url, headers=self.h, heartbeat=2)
        while True:
            m = json.loads((await self.ws.receive()).data)
            if m.get("t") == "hello":
                break
        self.reader = asyncio.ensure_future(self._read())
        return m

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                self.msgs.append(json.loads(msg.data))

    def open(self):
        self.hello = self.run(self._open())
        return self

    def send(self, m, pred=lambda d: False, timeout=3):
        n = len(self.msgs)
        self.run(self.ws.send_str(json.dumps(m)))
        end = time.time() + timeout
        while time.time() < end:
            for d in self.msgs[n:]:
                if pred(d):
                    return d
            time.sleep(0.05)
        return None

    def close(self):
        async def c():
            await self.ws.close()
            await self.s.close()
        try:
            self.run(c())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)


def notice(d):
    return d.get("t") == "notice" and "Read-only" in str(d.get("text", d.get("msg", "")))


procs = []


def spawn(argv, log):
    p = subprocess.Popen(argv, cwd=ROOT, stdout=open(os.path.join(TMP, log), "w"), stderr=subprocess.STDOUT)
    procs.append(p)
    return p


RW, RR, RL, WEB, SR = (testkit.free_port() for _ in range(5))
try:
    # ---------------------------------------------------------------- fail closed
    loose = os.path.join(TMP, "loose")
    with open(loose, "w") as f:
        f.write(TOKEN)
    os.chmod(loose, 0o644)
    for what, tf in (("a token file others can read", loose), ("no token file", os.path.join(TMP, "none"))):
        r = subprocess.run([sys.executable, "server.py", "--sim", "--rig", "g90", "--no-audio", "--host", "127.0.0.1",
                            "--listen", str(testkit.free_port()), "--rigctl-port", str(testkit.free_port()),
                            "--auth-mode", "readonly-lan", "--auth-token-file", tf],
                           cwd=ROOT, capture_output=True, text=True, timeout=60)
        check(f"fail closed: {what} stops the radio server", (r.returncode != 0, "fail closed" in r.stdout + r.stderr),
              (True, True))

    # ---------------------------------------------------------------- the station in front of a radio
    spawn([sys.executable, "server.py", "--sim", "--rig", "g90", "--no-audio", "--host", "127.0.0.1",
           "--listen", str(RW), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(RR),
           "--winlink-rigctl-port", str(RL), "--addons-state", "", *AUTH], "radio.log")
    cfg = {"name": "Test Station", "radios": [{"id": "g90", "name": "G90", "web": RW, "rigctl": RR, "lane": RL}],
           "addons_state": os.path.join(TMP, "addons.json"),
           "auth": {"mode": "readonly-lan", "token_file": TOKEN_FILE}}
    with open(os.path.join(TMP, "station.json"), "w") as f:
        json.dump(cfg, f)
    spawn([sys.executable, "station.py", "--config", os.path.join(TMP, "station.json"), "--host", "127.0.0.1",
           "--listen", str(WEB), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(SR)], "station.log")
    check("the radio is up", testkit.wait_for(lambda: health(RW).get("rig_connected"), 60, 0.2), True)
    check("the station is up", testkit.wait_for(lambda: health(WEB).get("role") == "station", 60, 0.2), True)
    PROXY = f"http://127.0.0.1:{WEB}/r/g90/ws"
    freq0 = health(RW) and Page(f"http://127.0.0.1:{RW}/ws", headers={"Authorization": f"Bearer {TOKEN}"}).open()
    f0 = freq0.hello["state"]["freq"]
    check("Bearer (the station's own lane and watch): operator", freq0.hello.get("role"), "operator")
    freq0.close()

    ro = Page(PROXY).open()
    check("read-only through the proxy: hello says so", ro.hello.get("role"), "readonly")
    check("  a QSY is refused, said", bool(ro.send({"t": "set", "k": "freq", "v": f0 + 5000}, notice)), True)
    time.sleep(0.5)
    op = Page(PROXY, cookie(int(time.time()) + 600)).open()
    check("  and the radio did not move", op.hello["state"]["freq"], f0)
    check("  a key-up is refused", bool(ro.send({"t": "set", "k": "tx", "v": True}, notice)), True)
    check("  and the radio is not keyed", health(RW).get("tx"), False)

    check("operator (cookie, through the proxy): hello says so", op.hello.get("role"), "operator")
    jtdx = socket.create_connection(("127.0.0.1", RR), timeout=5)    # held: a closed client's PTT drops
    jtdx.sendall(b"T 1\n")
    jtdx.recv(64)
    check("  the operator's app keys over rigctl (the simulator)", testkit.wait_for(lambda: health(RW).get("tx"), 5, 0.1), True)
    ro.send({"t": "tx_stop"}, lambda d: d.get("t") == "tx_stop" and d.get("stopped"), 5)
    check("read-only STOP TX unkeys the operator's transmission", testkit.wait_for(lambda: health(RW).get("tx") is False, 5, 0.1),
          True)
    check("  and latches", health(RW).get("tx_stopped"), True)
    jtdx.sendall(b"T 1\n")
    jtdx.recv(64)
    check("  latched: the app cannot key again", (time.sleep(0.5), health(RW).get("tx"))[1], False)
    jtdx.close()
    check("  read-only cannot resume", bool(ro.send({"t": "tx_resume"}, notice)), True)
    check("  still latched", health(RW).get("tx_stopped"), True)
    op.send({"t": "tx_resume"}, lambda d: d.get("t") == "tx_stop" and d.get("stopped") is None, 5)
    check("  the operator can", testkit.wait_for(lambda: health(RW).get("tx_stopped") is False, 5, 0.1), True)
    ro.close()
    op.close()

    code, hd, _ = http("POST", WEB, "/auth/login", {"token": "wrong-token-xx"})
    check("login: a wrong token is 401", code, 401)
    code, hd, _ = http("POST", WEB, "/auth/login", {"token": TOKEN})
    sc = hd.get("Set-Cookie", "") if hd else ""
    check("login: the right one sets rr_op", (code, sc.startswith("rr_op=")), (200, True))
    check("  HttpOnly, SameSite=Strict", ("HttpOnly" in sc, "SameSite=Strict" in sc), (True, True))
    issued = sc.split(";")[0].split("=", 1)[1]
    p = Page(PROXY, issued).open()
    check("  that cookie operates through the proxy", p.hello.get("role"), "operator")
    p.close()
    bad = issued[:-1] + ("0" if issued[-1] != "0" else "1")
    for what, c in (("a tampered cookie", bad), ("an expired cookie", cookie(int(time.time()) - 5))):
        pg = Page(PROXY, c).open()
        check(f"{what} is read-only", pg.hello.get("role"), "readonly")
        pg.close()

    short = Page(PROXY, cookie(int(time.time()) + 4)).open()
    held = threading.Event()

    def keep_alive():                 # the page holds PTT: a key-down every 0.5 s (the radio's dead-man is 2 s)
        while not held.is_set():
            try:
                short.run(short.ws.send_str(json.dumps({"t": "set", "k": "tx", "v": True, "mic": False})), 2)
            except Exception:         # noqa: BLE001 - the socket may be closing at the end
                return
            time.sleep(0.5)
    threading.Thread(target=keep_alive, daemon=True).start()
    keyed = testkit.wait_for(lambda: health(RW).get("tx"), 5, 0.1)
    check("a session about to end, its page holding PTT: keyed", keyed, True)
    if not keyed:
        print("   notices:", [d for d in short.msgs if d.get("t") == "notice"][-3:])
    check("  at its end the radio unkeys, keep-alives and all",
          testkit.wait_for(lambda: health(RW).get("tx") is False, 8, 0.1) and (time.sleep(1.5), health(RW).get("tx"))[1] is False, True)
    held.set()
    check("  and the page is read-only", bool(short.send({"t": "set", "k": "tx", "v": True}, notice)), True)
    short.close()

    st = Page(f"http://127.0.0.1:{WEB}/ws").open()
    check("the station's socket: read-only", st.hello.get("role"), "readonly")
    for t in ("winlink_connect", "js8", "module_apply", "mesh_send"):
        check(f"  refuses {t}", bool(st.send({"t": t}, notice)), True)
    st.close()
    code, _, body = http("POST", WEB, "/packet/api/tx", {"enabled": True})
    check("station HTTP: transmit On from read-only is 403", (code, b"read-only" in body), (403, True))
    code, _, body = http("POST", WEB, "/packet/api/tx", {"enabled": False})
    check("  Off passes the guard (no packet hub here: not the read-only 403)", b"read-only" in body, False)

    import shutil
    chrome = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
    if chrome:
        from cdp_browser import Browser
        b = Browser(chrome, testkit.free_port())
        try:
            b.goto(f"http://127.0.0.1:{WEB}/")
            CHIP = "document.querySelector('#rr-auth')?.textContent"
            check("page: the station shows Read-only · Unlock", b.wait(CHIP, 15), "Read-only · Unlock")
            b.js(f"window.prompt = () => {json.dumps(TOKEN)}")
            b.js("window.__rr_old = 1; document.querySelector('#rr-auth').click()")
            b.wait("!window.__rr_old && document.readyState === 'complete'", 15)
            check("  the token unlocks it: Operator · Lock", b.wait(f"({CHIP}) === 'Operator · Lock' && 'yes'", 15), "yes")
            check("  the radio's frame operates too (its socket says operator)",
                  b.wait("fetch('r/g90/auth/whoami').then((r) => r.json()).then((w) => w.role)", 10), "operator")
        finally:
            b.close()
    else:
        print("skip  page checks: no chromium (set CHROME=)")

    fake = T.FakeKissRadio().start()
    hub = T.HubProc(fake, tx=False, extra=AUTH)
    try:
        code, _, body = http("POST", hub.http, "/api/tx", {"enabled": True})
        check("hub: transmit On from read-only is 403", (code, b"read-only" in body), (403, True))
        code, _, body = http("POST", hub.http, "/api/tx", {"enabled": False})
        check("hub: Off is let through", (code, b"read-only" in body), (200, False))
        check("  and transmit stays off", fake.transmitted(), [])
    finally:
        hub.stop()
        fake.stop()

    from ranch_radio import render
    h = json.load(open(os.path.join(ROOT, "examples/hosts/linux-station.json")))
    h["auth"] = {"mode": "readonly-lan", "token_file": "~/.config/ranch-radio/token"}
    arts = render.artifacts(h)
    text = "\n".join(a.text() for a in arts)
    check("render: the radio gets the mode and the file's path", "--auth-mode readonly-lan" in text and "/.config/ranch-radio/token" in text,
          True)
    check("  the station.json carries it", any(a.fmt == "json" and (a.content or {}).get("auth", {}).get("mode") == "readonly-lan"
                                                 for a in arts), True)
    check("  the token itself is nowhere", TOKEN in text, False)
finally:
    for p in procs:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
