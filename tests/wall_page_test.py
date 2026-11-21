"""The wall (UX plan PR 5a, 2026-11-19): the station page's read-only kiosk
view, #/wall or ?wall, in headless Chromium at 1920x1080.

One simulated IC-7300 behind station.py. The Roster and Map add-ons' page
scripts are the real ones; their server side is not run: the test hands the
registry their list and snapshots as the station's hello would. The browser
reaches no network (every https URL is blocked). Nothing transmits: the
simulator has no callsign, and only STOP TX is pressed.

A recorder, installed in every document before its scripts run (the station
page and any radio frame), logs each message any WebSocket sends: the
station's socket, the station page's socket to each radio, and a frame's own.
A command is any message but the frames' ping.

  1. ?wall lands on #/wall: no radio frame exists; every view, the top bar and
     the toasts are inert; the rig bar is not, but its radios and Cancel are;
     STOP TX and Exit are visible and take input; the Map is shown without its
     head or power switch, its night side drawn though map.layers says off, and
     map.layers is not written; the clock, band strip and the focus radio's
     spectrum (MiniScope, drawn from the station's scope frames) show
  2. Space, the arrows and Enter, and real clicks on a band cell, the Map, the
     spectrum and the bar's frequency, send no command; the radio's frequency
     and TX, read on a fresh socket, are unchanged. Tab reaches Exit first;
     Enter on it leaves: #/home, ?wall gone from the URL, the wake lock
     released, and the station page itself sent no command
  3. Positive control: with the radio's tab open, unlocked, and its frame
     holding the keyboard, an arrow key does reach the frame (the recorder
     sees its set:freq), and a station send() is seen on the station socket.
     Then #/wall: the frame is not visible, the keyboard is no longer its,
     and Space, the arrows and Enter send nothing from any document; Escape
     leaves the wall and sends nothing
  4. A real click on the wall's STOP TX sends tx_stop on the station page's
     socket to the radio and latches it: config.tx_stopped on a fresh socket
  5. The wake lock (stubbed: proves the call site only) is asked for on
     entry and released on exit; refused, the wall says so and still shows

    python3 tests/wall_page_test.py [screenshot-dir]
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import aiohttp

os.environ["RANCH_RADIO_HOSTS"] = ""     # never this machine's own host files
os.environ["RANCH_RADIO_OFFLINE"] = "1"
os.environ.pop("CC_OBS_TOKENS_FILE", None)

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
TMP = tempfile.mkdtemp(prefix="wall-page-")

fp = testkit.free_port
WEB, SHARED, CDP = fp(), fp(), fp()
IC = {"web": fp(), "rigctl": fp(), "lane": fp()}
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:72} got {got!r}" + ("" if ok else f" want {want!r}"), flush=True)


def _answers(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


def radio_hello(port):
    """A fresh socket to the radio's own server: its hello (state and config)."""
    async def go():
        async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
            while True:
                m = await ws.receive(timeout=5)
                if m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data).get("t") == "hello":
                    return json.loads(m.data)
    return asyncio.run(go())


def rig_now():
    h = radio_hello(IC["web"])
    return {"freq": h["state"].get("freq"), "tx": bool(h["state"].get("tx")), "stopped": h["config"].get("tx_stopped")}


cfg = {"name": "Test Station", "radios": [{"id": "ic7300", "name": "IC-7300", **IC}],
       "addons_state": os.path.join(TMP, "addons.json")}
cfg_path = os.path.join(TMP, "station.json")
with open(cfg_path, "w") as f:
    json.dump(cfg, f)

ROW = {"answer": "cq", "cq": True, "cq_word": None, "to_me": False, "to": None, "snr": -10, "dt": 0.1, "df": 1500,
       "age": 0, "need": "dxcc", "reasons": ["dxcc"], "lotw": False, "band": "20m", "mode": "FT8"}
ROSTER = {"band": "20m", "mode": "FT8", "dial": 14074000, "needs": {"qsos": 0, "dxcc_worked": 0, "dxcc_confirmed": 0,
          "grids_worked": 0, "grids_confirmed": 0, "lotw_users": 0},
          "rows": [{**ROW, "key": "JTDX|1|1500|CQ JA5XYZ PM95", "call": "JA5XYZ", "grid": "PM95", "message": "CQ JA5XYZ PM95",
                    "entity": "Japan", "cont": "AS", "km": 10853, "bearing": 334}]}
MAP = {"map": {"me": "FN31", "band": "20m", "showing": "20m", "grids": {}, "decodes": []}, "pota": []}
ADDONS = [{"name": n, "label": n.title(), "title": n.title(), "group": "tools", "css": True, "on": True, "error": None}
          for n in ("roster", "map")]

# Before any page script: the socket recorder (every document) and, on the
# station page, a stubbed wake lock (localStorage wall-test-wake=refuse makes
# it refuse).
RECORDER = r"""(() => {
  let top_ = window; try { top_ = window.top; void top_.document; } catch { top_ = window; }
  const doc = top_ === window ? "station" : "frame";
  const log = () => (top_.__wsLog ||= []);
  const o = WebSocket.prototype.send;
  WebSocket.prototype.send = function (d) {
    let t = typeof d === "string" ? "text" : "binary";
    try { const m = JSON.parse(d); t = m.t + (m.k ? ":" + m.k : "") + (m.do ? ":" + m.do : ""); } catch {}
    log().push({ doc, url: String(this.url).replace(/^.*?\/\/[^/]+/, "").replace(/\?.*$/, ""), t });
    return o.call(this, d);
  };
  if (doc !== "station") return;
  window.__wake = [];
  let mode = "ok"; try { mode = localStorage.getItem("wall-test-wake") || "ok"; } catch {}
  Object.defineProperty(navigator, "wakeLock", { configurable: true, value: { request: async (type) => {
    window.__wake.push("request:" + type);
    if (mode === "refuse") throw new DOMException("not allowed here", "NotAllowedError");
    const s = new EventTarget();
    s.released = false;
    s.release = async () => { if (!s.released) { s.released = true; window.__wake.push("release"); s.dispatchEvent(new Event("release")); } };
    return s;
  } } });
})();"""
CMDS = "(window.__wsLog || []).filter((e) => e.t !== 'ping')"
SHOWN = "((e) => !!e && !e.closest('[hidden]') && getComputedStyle(e).display !== 'none' && e.getClientRects().length > 0 && e.getBoundingClientRect().width > 0)"
FRAME = "document.querySelector('#view-radio iframe[src*=\"r/ic7300/\"]')"
KEYS = {"Tab": ("Tab", 9, ""), "Enter": ("Enter", 13, "\r"), "Space": (" ", 32, " "), "Escape": ("Escape", 27, ""),
        "ArrowUp": ("ArrowUp", 38, ""), "ArrowDown": ("ArrowDown", 40, ""), "ArrowLeft": ("ArrowLeft", 37, ""),
        "ArrowRight": ("ArrowRight", 39, "")}


def press(name):
    """A real key press (trusted) to whatever document has the keyboard."""
    key, vk, text = KEYS[name]
    code = "Space" if name == "Space" else key
    down = {"type": "keyDown" if text else "rawKeyDown", "key": key, "code": code, "windowsVirtualKeyCode": vk}
    if text:
        down["text"] = text
    b.call("Input.dispatchKeyEvent", **down)
    b.call("Input.dispatchKeyEvent", type="keyUp", key=key, code=code, windowsVirtualKeyCode=vk)


def click_at(x, y):
    for t in ("mousePressed", "mouseReleased"):
        b.call("Input.dispatchMouseEvent", type=t, x=x, y=y, button="left", clickCount=1)


def middle(sel):
    return b.js(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); if (!e) return null; "
                "const r = e.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; })()")


def click(sel):
    p = middle(sel)
    assert p, f"nothing to click at {sel}"
    click_at(*p)


def frame_js(expr):
    return b.js(f"(() => {{ const w = {FRAME}?.contentWindow; "
                f"try {{ return w && w.rrFrame ? w.eval({json.dumps(expr)}) : null; }} catch (e) {{ return 'JS error: ' + e; }} }})()")


def shot(name):
    if SHOTS:
        b.screenshot(os.path.join(SHOTS, f"wall-{name}.png"))


def wall_ready():
    return b.wait("location.hash === '#/wall' && document.body.classList.contains('wall') && "
                  f"{SHOWN}(document.querySelector('#view-wall')) && {SHOWN}(document.querySelector('#view-addon-map .rm-wrap'))", 15)


def inject_addons():
    b.js("addons.hello(%s, %s)" % (json.dumps(ADDONS), json.dumps({"roster": {"roster": ROSTER}, "map": MAP})))


def quiet():
    # A quiet window before checking that nothing was sent (a minimum, not a guess at a wait).
    time.sleep(1.5)


ic = subprocess.Popen(
    [sys.executable, "server.py", "--sim", "--rig", "ic7300", "--host", "127.0.0.1",
     "--listen", str(IC["web"]), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(IC["rigctl"]),
     "--winlink-rigctl-port", str(IC["lane"]), "--addons-state", ""],
    cwd=ROOT, stdout=open(os.path.join(TMP, "ic7300.log"), "w"), stderr=subprocess.STDOUT)
front = subprocess.Popen([sys.executable, "station.py", "--config", cfg_path, "--listen", str(WEB),
                          "--rigctl-port", str(SHARED)], cwd=ROOT,
                         stdout=open(os.path.join(TMP, "station.log"), "w"), stderr=subprocess.STDOUT)
b = Browser(CHROME, CDP, size="1920,1080")
# The test reads a radio frame's globals with w.eval(); the pages' policy
# (webguard.CSP, no unsafe-eval) refuses that, as it should. Bypass it
# for this test only; station_page_test and xss_render_page_test run the
# pages under it.
b.call("Page.setBypassCSP", enabled=True)
started = time.monotonic()
try:
    for n, port in (("ic7300", IC["web"]), ("station", WEB)):
        assert testkit.wait_for(lambda: _answers(port), 60, 0.1), f"{n} did not start"
    b.call("Network.enable")
    b.call("Network.setBlockedURLs", urls=["https://*"])
    b.call("Emulation.setDeviceMetricsOverride", width=1920, height=1080, deviceScaleFactor=1, mobile=False)
    b.call("Page.addScriptToEvaluateOnNewDocument", source=RECORDER)
    b.goto(f"http://127.0.0.1:{WEB}/#/home")
    b.js("localStorage.setItem('rr-focus', 'ic7300'); store.set('map.layers', {night: false}); store.set('locked', true)")
    layers0 = b.js("localStorage.getItem('ic7300.map.layers')")

    # 1. ?wall.
    b.goto(f"http://127.0.0.1:{WEB}/?r=1&wall")
    assert b.wait("/^\\d+\\.\\d{3}\\.\\d{3}$/.test(document.querySelector('#rig-bar .rb-rig.focus .rb-f')?.textContent || '')", 30), \
        "the rig bar never showed the focus radio"
    inject_addons()
    check("1. ?wall lands on #/wall with the Map beside the wall", bool(wall_ready()), True)
    check("  the URL keeps ?wall while it is the wall", b.js("new URLSearchParams(location.search).has('wall')"), True)
    check("  no radio frame exists", b.js("document.querySelectorAll('iframe').length"), 0)
    check("  every view, the top bar and the toasts are inert",
          b.js("[...document.querySelectorAll('.st-main > .st-view, .topbar, #toasts')].every((e) => e.inert)"), True)
    check("  the top bar is not shown", b.js(f"{SHOWN}(document.querySelector('.topbar'))"), False)
    check("  the rig bar is not inert; its radios and Cancel are",
          b.js("(() => { const bar = document.querySelector('#rig-bar'); return [bar.inert, "
               "[...bar.querySelectorAll('.rb-rig')].every((e) => e.inert), bar.querySelector('.rb-stop-cancel').inert, "
               "bar.querySelector('.rb-stop').inert]; })()"), [False, True, True, False])
    check("  STOP TX and Exit are visible and not inert",
          b.js(f"[{SHOWN}(document.querySelector('#rig-bar .rb-stop-btn')), {SHOWN}(document.querySelector('#wall-exit')), "
               "!!document.querySelector('#rig-bar .rb-stop-btn').closest('[inert]'), !!document.querySelector('#wall-exit').closest('[inert]')]"),
          [True, True, False, False])
    check("  STOP TX and Exit are each the top element at their middle",
          b.js("['#rig-bar .rb-stop-btn', '#wall-exit'].map((s) => { const e = document.querySelector(s), r = e.getBoundingClientRect(); "
               "return document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)?.closest(s) === e; })"), [True, True])
    check("  the Map's head and power switch are not rendered; its controls neither",
          b.js("['#view-addon-map .addon-head', '#view-addon-map .addon-power', '#view-addon-map .rm-bar', "
               "'#view-addon-map .rm-ctl', '#view-addon-map .rm-list'].map((s) => document.querySelector(s)?.getClientRects().length ?? 0)"),
          [0, 0, 0, 0, 0])
    check("  the night side is drawn though map.layers says off",
          bool(b.wait("document.querySelectorAll('#view-addon-map .rm-nightp').length > 0", 10)), True)
    check("  map.layers is not written", b.js("localStorage.getItem('ic7300.map.layers')"), layers0)
    check("  the focus radio's spectrum draws in the wall (its MiniScope)",
          bool(b.wait("(() => { const c = document.querySelector('#wall-scope canvas.mc-scope'); "
                      "return !!c && c === mission.scopes.ic7300?.el && c.width > 0 && c.getBoundingClientRect().height > 100; })()", 20)), True)
    check("  the UTC clock and the date show",
          bool(b.wait("document.querySelector('#wall-utc').textContent.slice(0, 5) === new Date().toISOString().slice(11, 16) && "
                      "!!document.querySelector('#wall-date').textContent", 5)), True)
    check("  the band strip shows its cells", bool(b.wait("document.querySelectorAll('#wall-bands .bs-cell').length > 0", 10)), True)
    check("5. the wake lock is asked for on entry", b.wait("JSON.stringify(window.__wake)", 5), '["request:screen"]')
    check("  the footer says the screen is kept awake",
          bool(b.wait("document.querySelector('#wall-foot').textContent.includes('kept awake')", 5)), True)
    shot("1920x1080")

    # 2. Keys and clicks on the wall, no frame.
    before = rig_now()
    b.js("window.__wsLog = []; document.activeElement?.blur()")
    for k in ("Space", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Enter"):
        press(k)
    # Before any click: a click moves where Tab starts from.
    press("Tab")
    check("  Tab reaches Exit first", b.js("document.activeElement?.id"), "wall-exit")
    b.js("document.activeElement?.blur()")
    for sel in ("#wall-bands .bs-cell", "#view-addon-map .rm-wrap", "#wall-scope", "#rig-bar .rb-rig.focus .rb-f"):
        click(sel)
    quiet()
    check("2. Space, arrows, Enter and clicks (band cell, Map, scope, the bar's frequency) send nothing",
          b.js(CMDS), [])
    check("  still the wall; no frequency entry opened; still no frame",
          b.js("[location.hash, document.querySelector('#rig-bar .rb-entry').hidden, document.querySelectorAll('iframe').length]"),
          ["#/wall", True, 0])
    check("  the radio's frequency and TX unchanged (a fresh socket)",
          {k: v for k, v in rig_now().items() if k != "stopped"}, {k: v for k, v in before.items() if k != "stopped"})
    b.js("document.querySelector('#wall-exit').focus()")
    press("Enter")
    check("  Enter on Exit leaves: #/home, ?wall gone from the URL",
          b.wait("location.hash === '#/home' && !document.body.classList.contains('wall') && "
                 "JSON.stringify([location.hash, location.search])", 5), json.dumps(["#/home", "?r=1"], separators=(",", ":")))
    check("  the wake lock released on exit", b.wait("window.__wake.length === 2 && JSON.stringify(window.__wake)", 5),
          '["request:screen","release"]')
    quiet()
    check("  the station page sent no command (Exit's effect: the focus frame is built)",
          b.js(f"{CMDS}.filter((e) => e.doc === 'station')"), [])
    print("     the frame's own messages after Exit:", sorted({e["t"] for e in b.js("window.__wsLog") if e["doc"] == "frame"}))
    check("  nothing inert after leaving",
          b.js("[...document.querySelectorAll('.st-main > .st-view, .topbar, #toasts, #rig-bar *')].some((e) => e.inert)"), False)

    # 3. Positive control, then the wall over a frame that had the keyboard.
    b.js("location.hash = '#/ic7300'")
    assert testkit.wait_for(lambda: frame_js("typeof rrFrame === 'object' && !!S.freq") is True, 20, 0.1), "the frame never loaded"
    b.js("store.set('locked', false)")
    assert testkit.wait_for(lambda: frame_js("locked === false") is True, 5, 0.1), "the frame did not see the unlock"
    b.js(f"window.__wsLog = []; {FRAME}.contentWindow.focus()")
    check("3. control: the frame has the keyboard", b.js(f"document.activeElement === {FRAME}"), True)
    press("ArrowUp")
    check("  control: an arrow there reaches the frame (its set:freq is recorded)",
          bool(b.wait("window.__wsLog.some((e) => e.doc === 'frame' && e.t === 'set:freq')", 5)), True)
    b.js("send({t: 'addon', addon: 'map', do: 'show', band: '20m'})")
    check("  control: a station send() is recorded on the station socket",
          bool(b.wait("window.__wsLog.some((e) => e.doc === 'station' && e.url === '/ws' && e.t === 'addon:show')", 5)), True)
    time.sleep(0.5)                       # the retune's answer, before the baseline
    before = rig_now()
    b.js("location.hash = '#/wall'")
    check("  #/wall from the radio's tab", bool(wall_ready()), True)
    check("  the frame is not visible; the keyboard is no longer the frame's",
          b.js(f"[[...document.querySelectorAll('iframe')].some((f) => {SHOWN}(f)), document.activeElement === {FRAME}]"),
          [False, False])
    b.js("window.__wsLog = []")
    for k in ("Space", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Enter"):
        press(k)
    quiet()
    # The frame pings every 2 s: wait for one, so its recorder is shown live.
    alive = bool(b.wait("window.__wsLog.some((e) => e.doc === 'frame' && e.t === 'ping')", 6))
    log = b.js("window.__wsLog")
    check("  Space, arrows and Enter send nothing from any document", [e for e in log if e["t"] != "ping"], [])
    check("  the frame is alive meanwhile (its pings are recorded)", alive, True)
    check("  the radio's frequency and TX unchanged (a fresh socket)",
          {k: v for k, v in rig_now().items() if k != "stopped"}, {k: v for k, v in before.items() if k != "stopped"})
    b.js("window.__wsLog = []")
    press("Escape")
    check("  Escape leaves the wall", bool(b.wait("location.hash === '#/home' && !document.body.classList.contains('wall')", 5)), True)
    quiet()
    check("  and sends nothing", b.js(CMDS), [])

    if SHOTS:                                     # the README's shot: a fresh wall (an empty ticker), a full Map
        sys.path[:0] = [ROOT, os.path.join(ROOT, "tools")]
        import readme_fixture as F
        b.goto(f"http://127.0.0.1:{WEB}/?wall")
        b.wait("!!document.querySelector('#rig-bar .rb-rig.focus')", 30)
        inject_addons()
        wall_ready()
        b.js("addons.event({t: 'addon', addon: 'roster', on: true, snapshot: %s})" % json.dumps({"roster": F.ROSTER}))
        b.js("addons.event({t: 'addon', addon: 'map', on: true, snapshot: %s})" % json.dumps(F.MAP))
        b.js("wall.note('IC-7300 to 20m')")
        time.sleep(15)                            # the reload emptied the waterfall: let it fill
        shot("populated")

    # 4. STOP TX on the wall.
    b.js("store.set('locked', true); location.hash = '#/wall'")
    wall_ready()
    check("4. not latched before", rig_now()["stopped"], None)
    b.js("window.__wsLog = []")
    click("#rig-bar .rb-stop-btn")
    check("  a real click on the wall's STOP TX sends tx_stop to the radio (station page's socket)",
          bool(b.wait("window.__wsLog.some((e) => e.doc === 'station' && e.url === '/r/ic7300/ws' && e.t === 'tx_stop')", 5)), True)
    check("  the radio latches: config.tx_stopped on a fresh socket",
          testkit.wait_for(lambda: rig_now()["stopped"] is not None, 10, 0.2), True)
    check("  the bar says TX STOPPED",
          bool(b.wait("document.querySelector('#rig-bar .rb-stop-state')?.textContent === 'TX STOPPED'", 10)), True)
    check("  the ticker has it", bool(b.wait("document.querySelector('#wall-ticker').textContent.includes('STOP TX latched')", 5)), True)
    shot("stopped")

    # 5. A refused wake lock.
    b.js("localStorage.setItem('wall-test-wake', 'refuse')")
    b.goto(f"http://127.0.0.1:{WEB}/?wall")
    b.wait("!!document.querySelector('#rig-bar .rb-rig.focus')", 30)
    inject_addons()
    check("5. refused: the wall still shows", bool(wall_ready()), True)
    check("  and says the screen may sleep",
          bool(b.wait("window.__wake[0] === 'request:screen' && document.querySelector('#wall-foot').textContent.includes('refused')", 5)), True)
    b.js("localStorage.removeItem('wall-test-wake')")
    print(f"     {time.monotonic() - started:.0f} s")
finally:
    b.close()
    for p in (front, ic):
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()

print("FAIL" if fails else "PASS", f"({fails} failed)" if fails else "")
sys.exit(1 if fails else 0)
