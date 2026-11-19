"""Listen in the station's rig bar (UX plan PR 1c, 2026-11-19), in headless Chromium.

Split from rigbar_page_test.py: Listen needs radios with (simulated) audio and
a hardware-scope rig beside an audio-scope one; that test's radios have none.

Two simulated radios behind station.py, both with the simulator's audio: an
IC-7300 (hardware scope) and a G90 (no scope: its page draws one from the
receive audio, over /audio). A fresh browser profile, no autoplay override;
clicks are real (CDP Input.dispatchMouseEvent). Nothing transmits: "PTT" below
is a second client keying the simulator. Checks:
  L1. the focus radio's frame is made early and hidden on #/home; without a
      gesture listen(true) resolves false (the control: this browser enforces
      autoplay); a click on the bar's speaker leaves the frame's audio running
      and its /audio open
  L2. a tab change does not stop it; the radio's own Listen agrees
  L3. a second click stops it and closes /audio
  L4. rapid toggles end in the last state asked for (on after three, off
      after three more)
  L5. a click before the frame is ready resolves false: a toast, the radio's
      tab; once the frame is ready, a click works
  L6. an audio-scope rig, hidden and not listening, holds no /audio socket
      and its loops are stopped; shown, both come back; hidden and listening,
      the socket stays
  L7. another client's PTT and Winlink session are untouched by the frame
      loading, listening and unloading; the frame sends the radio nothing but
      its ping (positive controls: that client leaving unkeys and disarms)

    python3 tests/rigbar_listen_page_test.py [screenshot-dir]
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

os.environ["RANCH_RADIO_HOSTS"] = ""     # never this machine's own host files
os.environ["RANCH_RADIO_OFFLINE"] = "1"
os.environ.pop("CC_OBS_TOKENS_FILE", None)

import aiohttp  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from cdp_browser import Browser  # noqa: E402
import testkit  # noqa: E402

CHROME = (os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
          or next((p for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                               "/Applications/Chromium.app/Contents/MacOS/Chromium") if os.path.exists(p)), None))
if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")
SHOTS = sys.argv[1] if len(sys.argv) > 1 else None
TMP = tempfile.mkdtemp(prefix="rigbar-listen-")

fp = testkit.free_port
WEB, SHARED, CDP = fp(), fp(), fp()
IC = {"web": fp(), "rigctl": fp(), "lane": fp()}
G90 = {"web": fp(), "rigctl": fp(), "lane": fp()}
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:64} got {got!r}" + ("" if ok else f" want {want!r}"))


def _answers(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


def radio(rig, p):
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--rig", rig, "--host", "127.0.0.1",
         "--listen", str(p["web"]), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(p["rigctl"]),
         "--winlink-rigctl-port", str(p["lane"]), "--addons-state", ""],
        cwd=ROOT, stdout=open(os.path.join(TMP, f"{rig}.log"), "w"), stderr=subprocess.STDOUT)
    assert testkit.wait_for(lambda: _answers(p["web"]), 60, 0.1), f"{rig} did not start"
    return proc


def stop(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def hello(port):
    """A fresh socket's hello from a radio's own server: (tx, Winlink armed)."""
    async def go():
        async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
            while True:
                m = await ws.receive(timeout=5)
                if m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data).get("t") == "hello":
                    h = json.loads(m.data)
                    return bool(h["state"].get("tx")), bool(h["winlink"]["armed"])
    return asyncio.run(go())


class Other:
    """A second page on a radio's own server (its own thread and loop): it
    holds PTT (with keep-alives) or a Winlink session while the station's
    frame comes and goes."""

    def __init__(self, port):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.msgs, self.ka = [], None
        self._run(self._open(port))
        assert testkit.wait_for(lambda: any(m.get("t") == "hello" for m in self.msgs), 10), "no hello"

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(15)

    async def _open(self, port):
        self.s = aiohttp.ClientSession()
        self.ws = await self.s.ws_connect(f"http://127.0.0.1:{port}/ws")
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for m in self.ws:
            if m.type == aiohttp.WSMsgType.TEXT:
                self.msgs.append(json.loads(m.data))

    async def _send(self, d):
        await self.ws.send_str(json.dumps(d))

    def send(self, **d):
        self._run(self._send(d))

    def hold_ptt(self):
        async def ka():
            while True:
                await self._send({"t": "set", "k": "tx", "v": True})
                await asyncio.sleep(0.4)
        self.ka = asyncio.run_coroutine_threadsafe(ka(), self.loop)

    def release_ptt(self):
        if self.ka:
            self.ka.cancel()
            self.ka = None
        self.send(t="set", k="tx", v=False)

    def lost(self, since):
        """What, after message `since`, says this client lost what it held."""
        out = []
        for m in self.msgs[since:]:
            if m.get("t") == "state" and m.get("d", {}).get("tx") is False:
                out.append("unkeyed")
            if m.get("t") == "winlink" and m.get("armed") is False:
                out.append("disarmed")
        return out

    def close(self):
        async def go():
            if self.ka:
                self.ka.cancel()
            await self.ws.close()
            await self.s.close()
        self._run(go())
        self.loop.call_soon_threadsafe(self.loop.stop)


cfg = {"name": "Test Station",
       "radios": [{"id": "ic7300", "name": "IC-7300", **IC}, {"id": "g90", "name": "G90", **G90}],
       "addons_state": os.path.join(TMP, "addons.json")}
cfg_path = os.path.join(TMP, "station.json")
with open(cfg_path, "w") as f:
    json.dump(cfg, f)

FREQ_RE = "/^\\d+\\.\\d{3}\\.\\d{3}$/"
RIG = "document.querySelector('#rig-bar .rb-rig[data-id=\"{}\"]')"
BTN = "#rig-bar .rb-rig.focus .rb-listen"
SHOWN = "((e) => !!e && !e.closest('[hidden]') && e.offsetParent !== null)"
TOASTS = "[...document.querySelectorAll('#toasts .toast')].map((t) => t.textContent)"


def frame_js(rid, expr):
    """`expr` evaluated in radio `rid`'s frame (its own globals: audio, loops), or null."""
    return b.js(f"(() => {{ const w = document.querySelector('#view-radio iframe[src*=\"r/{rid}/\"]')?.contentWindow; "
                f"try {{ return w && w.rrFrame ? w.eval({json.dumps(expr)}) : null; }} catch (e) {{ return 'JS error: ' + e; }} }})()")


def frame_wait(rid, expr, timeout=10):
    return testkit.wait_for(lambda: frame_js(rid, expr) is True, timeout, 0.1)


def click(sel):
    """A real click (trusted, a user gesture) at the middle of `sel`."""
    x, y = b.js(f"(() => {{ const r = document.querySelector({json.dumps(sel)}).getBoundingClientRect(); "
                f"return [r.x + r.width / 2, r.y + r.height / 2]; }})()")
    for t in ("mousePressed", "mouseReleased"):
        b.call("Input.dispatchMouseEvent", type=t, x=x, y=y, button="left", clickCount=1)


def settled(rid, want):
    """The bar and the frame agree on `want`, with no call out."""
    return b.wait(f"(() => {{ const e = document.querySelector('{BTN}'); return !!e && !e.classList.contains('busy') && "
                  f"e.getAttribute('aria-pressed') === '{str(want).lower()}' && e.classList.contains('on') === {str(want).lower()}; }})()", 10) \
        and frame_wait(rid, f"rrFrame.listening() === {str(want).lower()}")


def shot(name):
    if SHOTS:
        b.screenshot(os.path.join(SHOTS, f"rigbar-listen-{name}.png"))


def home_ready(rid):
    b.wait(f"{FREQ_RE}.test({RIG.format(rid)}?.querySelector('.rb-f')?.textContent || '') && "
           f"{SHOWN}(document.querySelector('{BTN}'))", 30)
    return frame_wait(rid, "typeof rrFrame === 'object' && !!CFG.name", 20)


# Records what a frame sends on any socket, by message type (its pings included).
SENDS = ("(() => { if (window.__sent) return; window.__sent = []; const o = WebSocket.prototype.send; "
         "WebSocket.prototype.send = function (d) { let t = typeof d === 'string' ? 'text' : 'binary'; "
         "try { const m = JSON.parse(d); t = m.t + (m.k ? ':' + m.k : ''); } catch {} window.__sent.push(t); "
         "return o.call(this, d); }; return true; })()")

ic = radio("ic7300", IC)
g90 = radio("g90", G90)
front = subprocess.Popen([sys.executable, "station.py", "--config", cfg_path, "--listen", str(WEB),
                          "--rigctl-port", str(SHARED)], cwd=ROOT,
                         stdout=open(os.path.join(TMP, "station.log"), "w"), stderr=subprocess.STDOUT)
b = Browser(CHROME, CDP, size="1500,1000")
try:
    assert testkit.wait_for(lambda: _answers(WEB), 60, 0.1), "station did not start"
    b.goto(f"http://127.0.0.1:{WEB}/#/home")
    b.js("localStorage.setItem('rr-focus', 'ic7300')")
    b.goto(f"http://127.0.0.1:{WEB}/?r=1#/home")
    ready = home_ready("ic7300")

    # L1. The prepared frame, hidden; a real click starts its audio.
    check("L1. the focus radio's frame is made early, hidden, ready",
          (ready, b.js("(() => { const f = document.querySelector('#view-radio iframe[src*=\"r/ic7300/\"]'); "
                       "return [!!f, !!f && f.offsetParent === null]; })()"),
           b.js("!!document.querySelector('#view-radio iframe[src*=\"r/g90/\"]')")),
          (True, [True, True], False))
    check("  the hidden frame's loops are stopped", frame_wait("ic7300", "loops.paused === true"), True)
    check("  the speaker is on the focus radio only",
          (b.js(f"{SHOWN}(document.querySelector('{BTN}'))"), b.js("document.querySelectorAll('#rig-bar .rb-listen').length")),
          (True, 1))
    # The negative control, before any click (activation is sticky per
    # document): with no gesture the browser keeps the context suspended, so
    # listen(true) gives up after 1.5 s, cleans up and resolves false.
    check("  control: listen(true) with no gesture resolves false, cleaned up",
          (frame_js("ic7300", "rrFrame.listen(true)"), frame_js("ic7300", "audio.ctx === null && audio.ws === null"),
           b.wait(f"document.querySelector('{BTN}').getAttribute('aria-pressed')", 5)), (False, True, "false"))
    click(BTN)
    check("L1. a click on #/home: the frame's audio runs, its /audio is open",
          (settled("ic7300", True), frame_js("ic7300", "audio.ctx?.state"), frame_wait("ic7300", "audio.ws?.readyState === 1")),
          (True, "running", True))
    check("  and audio arrives on it", frame_wait("ic7300", "audio.level > 0", 10), True)
    check("  the bar shows it pressed", b.js(f"document.querySelector('{BTN}').getAttribute('aria-pressed')"), "true")
    shot("on-1500")

    # L2. A tab change does not stop it; the radio's own page agrees.
    b.js("location.hash = '#/g90'")
    b.wait("!document.querySelector('#view-radio').hidden", 10)
    b.js("location.hash = '#/ic7300'")
    check("L2. a tab change: still listening; its own Listen reads Listening",
          (frame_wait("ic7300", "document.querySelector('#btn-listen span').textContent === 'Listening'"),
           frame_js("ic7300", "rrFrame.listening() && loops.paused === false")), (True, True))
    b.js("location.hash = '#/home'")
    check("  back on #/home: still listening, the frame hidden again",
          (settled("ic7300", True), frame_wait("ic7300", "loops.paused === true")), (True, True))

    # L3. A second click stops it.
    click(BTN)
    check("L3. a second click stops it and closes /audio",
          (settled("ic7300", False), frame_wait("ic7300", "audio.ctx === null && audio.ws === null")), (True, True))

    # L4. Rapid toggles: three clicks end on, then two more end off.
    for _ in range(3):
        click(BTN)
    check("L4. three rapid clicks end listening", settled("ic7300", True), True)
    for _ in range(3):
        click(BTN)
    check("  three more end not listening, /audio closed",
          (settled("ic7300", False), frame_wait("ic7300", "audio.ws === null")), (True, True))

    # L6. The G90 (an audio-scope rig): shown, its spectrum socket is open...
    b.js("location.hash = '#/g90'")
    check("L6. G90 shown: its spectrum socket is open, loops run (the control)",
          (frame_wait("g90", "audio.wantSpectrum === true && audio.ws?.readyState === 1", 20),
           frame_js("g90", "loops.paused")), (True, False))
    b.js("location.hash = '#/home'")
    check("  hidden and not listening: no /audio socket, loops stopped",
          frame_wait("g90", "audio.ws === null && audio.ctx === null && loops.paused === true"), True)
    time.sleep(2.5)                      # a quiet window: past the socket's 1.5 s reconnect
    check("  and it stays closed", frame_js("g90", "audio.ws === null"), True)
    b.js(f"{RIG.format('g90')}.click()")    # focus the G90: its speaker in the bar
    b.wait(f"{RIG.format('g90')}?.classList.contains('focus') && {SHOWN}(document.querySelector('{BTN}'))", 10)
    click(BTN)
    check("  hidden and listening: the socket is open",
          (settled("g90", True), frame_wait("g90", "audio.ws?.readyState === 1")), (True, True))
    click(BTN)
    check("  stopped while hidden: closed again", (settled("g90", False), frame_wait("g90", "audio.ws === null")), (True, True))
    b.js(f"{RIG.format('ic7300')}.click()")
    b.wait(f"{RIG.format('ic7300')}?.classList.contains('focus')", 10)

    # L7. Another client holds the IC-7300's PTT, then a Winlink session.
    other = Other(IC["web"])
    other.hold_ptt()
    check("L7. another client keys the IC-7300 (simulator)", testkit.wait_for(lambda: hello(IC["web"])[0], 10), True)
    mark = len(other.msgs)
    b.goto(f"http://127.0.0.1:{WEB}/?r=2#/home")      # the frame unloads and loads again
    home_ready("ic7300")
    frame_js("ic7300", SENDS)
    click(BTN)
    settled("ic7300", True)
    click(BTN)
    settled("ic7300", False)
    # The recorder must see the frame's own ping, or an empty list proves nothing.
    frame_wait("ic7300", "window.__sent.includes('ping')", 5)
    sent = frame_js("ic7300", "window.__sent")
    b.goto(f"http://127.0.0.1:{WEB}/?r=3#/home")      # and unloads again
    home_ready("ic7300")
    check("  frame loaded, listened, unloaded: still keyed by the other client",
          (hello(IC["web"])[0], other.lost(mark)), (True, []))
    check("  the frame sent the radio nothing but its ping",
          (sorted(set(sent) - {"ping"}), "ping" in sent) if isinstance(sent, list) else sent, ([], True))
    other.release_ptt()
    testkit.wait_for(lambda: not hello(IC["web"])[0], 10)

    other.send(t="winlink_arm", v=True)
    check("  another client arms Winlink", testkit.wait_for(lambda: hello(IC["web"])[1], 10), True)
    mark = len(other.msgs)
    b.goto(f"http://127.0.0.1:{WEB}/?r=4#/home")
    home_ready("ic7300")
    click(BTN)
    settled("ic7300", True)
    click(BTN)
    settled("ic7300", False)
    b.goto(f"http://127.0.0.1:{WEB}/?r=5#/home")
    home_ready("ic7300")
    check("  frame loaded, listened, unloaded: the session is still armed",
          (hello(IC["web"])[1], other.lost(mark)), (True, []))
    other.close()
    check("  positive control: the other client leaving disarms it",
          testkit.wait_for(lambda: not hello(IC["web"])[1], 10), True)
    other = Other(IC["web"])
    other.hold_ptt()
    testkit.wait_for(lambda: hello(IC["web"])[0], 10)
    other.close()
    check("  positive control: the other client leaving unkeys",
          testkit.wait_for(lambda: not hello(IC["web"])[0], 10), True)

    # L5. A click before the frame is ready: its app.js is held at the network.
    b.call("Fetch.enable", patterns=[{"urlPattern": "*/r/ic7300/static/app.js*", "requestStage": "Request"}])

    async def reload_and_hold():
        b.n += 1
        await b.ws.send_str(json.dumps({"id": b.n, "method": "Page.navigate",
                                        "params": {"url": f"http://127.0.0.1:{WEB}/?r=6#/home"}}))
        while True:
            m = json.loads((await b.ws.receive(timeout=20)).data)
            if m.get("method") == "Fetch.requestPaused":
                return m["params"]["requestId"]
    held = b.loop.run_until_complete(reload_and_hold())
    b.wait(f"{FREQ_RE}.test({RIG.format('ic7300')}?.querySelector('.rb-f')?.textContent || '') && "
           f"{SHOWN}(document.querySelector('{BTN}'))", 20)
    check("L5. frame not ready yet (app.js held): no rrFrame",
          b.js("!document.querySelector('#view-radio iframe[src*=\"r/ic7300/\"]')?.contentWindow?.rrFrame"), True)
    click(BTN)
    check("  a click: the toast, and the radio's tab opens",
          (bool(b.wait(f"{TOASTS}.includes('Open the radio tab and press Listen once')", 5)),
           b.wait("location.hash === '#/ic7300' && location.hash", 5)), (True, "#/ic7300"))
    b.call("Fetch.continueRequest", requestId=held)
    b.call("Fetch.disable")
    b.js("location.hash = '#/home'")
    home_ready("ic7300")
    click(BTN)
    check("  ready: a click now listens", settled("ic7300", True), True)

    # The bar with Listen on, on a desk and on a phone.
    shot("on-home-1500")
    b.call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=2, mobile=True)
    b.wait("innerWidth === 390")
    check("390 px: the speaker, pressed, inside the screen with STOP TX",
          b.js(f"(() => {{ const e = document.querySelector('{BTN}'), s = document.querySelector('#rig-bar .rb-stop-btn'), "
               f"bar = document.querySelector('#rig-bar'); return {SHOWN}(e) && e.getAttribute('aria-pressed') === 'true' && "
               f"e.getBoundingClientRect().right <= innerWidth + 0.5 && {SHOWN}(s) && "
               f"s.getBoundingClientRect().right <= innerWidth + 0.5 && bar.scrollWidth <= bar.clientWidth + 1; }})()"), True)
    shot("on-390")
    click(BTN)
    settled("ic7300", False)
finally:
    b.close()
    stop(front)
    stop(ic)
    stop(g90)
    if fails:
        for n in ("ic7300", "g90", "station"):
            p = os.path.join(TMP, f"{n}.log")
            if os.path.exists(p):
                print(f"--- {n}.log\n" + open(p).read()[-1200:])
    shutil.rmtree(TMP, ignore_errors=True)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
