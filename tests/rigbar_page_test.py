"""The station's rig bar (UX plan PR 1a, 2026-11-19), driven in headless Chromium.

Two simulated radios (G90, QMX) behind station.py, with fake Pat, a fake
mesh bridge, the Propagation add-on (no network) and a scratch host file (the
Modules tab). Nothing transmits: "keyed" is a simulator keyed over rigctl.
Checks:
  1. the bar shows every radio on Mission Control, a radio's tab, Winlink,
     Mesh, an add-on tab and Modules, at 1500 px and at 390 px (inside the
     screen); one focus radio in full (frequency, mode, band, meter), the
     other compact; a click on the compact one focuses it, and it sticks
     (localStorage "rr-focus") across a reload
  2. it follows a change made at the radio (the simulator's front panel):
     frequency, mode with -D, band
  3. a keyed radio shows ON AIR, watts and Release TX; Release unkeys it
  4. a radio whose server stops shows offline; restarted, it comes back
  5. a notice from a radio's server (its transmit time limit, 2 s on the
     QMX here) shows as a toast on the station page, named for the radio
STOP TX in the bar (UX plan PR 1b, 2026-11-19); the latch is read from a fresh
socket to each radio, never from the page:
  S1. a press latches both; the frame's PTT is disabled; TX STOPPED, and
      again after a reload
  S8. latched while the radio still transmits (keyed at its front panel):
      LATCHED BUT STILL KEYED
  S5. Resume on one radio from a second page: it shows resumed, not re-latched
  S7. the bar's STOP TX never reaches one radio; that radio's own page's STOP
      TX does, and the bar shows it
  S2. one radio down at the press: the other latches, the bar names the one
      not reached; back up, it latches without another press
  S4. the same with a reload while the intent is pending
  S9. a latched radio's server is replaced: the bar leaves TX STOPPED, says
      so, and sends nothing
  S6. past the TTL (shortened by rr-stop-ttl-s) a pending press shows as not
      delivered and is never sent
  S3. the socket drops after the send, before the reply: unknown, then the
      new hello settles it
  S10. no press, a radio down at page load: it comes up, nothing is sent

    python3 tests/rigbar_page_test.py [screenshot-dir]
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
TMP = tempfile.mkdtemp(prefix="rigbar-page-")
REPO, HOME = os.path.join(TMP, "repo"), os.path.join(TMP, "home")
os.makedirs(os.path.join(REPO, "hosts"))
os.makedirs(HOME)

fp = testkit.free_port
WEB, SHARED, LANE, PAT, CDP = fp(), fp(), fp(), fp(), fp()
ARDOP = testkit.pair_port()
G90 = {"web": fp(), "rigctl": fp(), "lane": fp()}
QMX = {"web": fp(), "rigctl": fp(), "lane": fp()}
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:64} got {got!r}" + ("" if ok else f" want {want!r}"))


def wait_port(port, timeout=60):         # a ceiling; returns once the port answers
    return testkit.wait_for(lambda: _answers(port), timeout, 0.1)


def _answers(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


def radio(rig, p, *extra):
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--rig", rig, "--no-audio", "--host", "127.0.0.1",
         "--listen", str(p["web"]), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(p["rigctl"]),
         "--winlink-rigctl-port", str(p["lane"]), *extra],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert wait_port(p["web"]), f"{rig} did not start"
    return proc


def stop(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def ws_call(port, send=None):
    """A fresh socket to a radio's own server (a second page): its hello's
    tx_stopped, after sending `send` if given."""
    async def go():
        async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
            while True:
                m = await ws.receive(timeout=5)
                if m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data).get("t") == "hello":
                    hello = json.loads(m.data)
                    break
            if send:
                await ws.send_str(json.dumps(send))
                await asyncio.sleep(0.3)
            return hello["config"].get("tx_stopped")
    return asyncio.run(go())


def latched(p):
    return ws_call(p["web"]) is not None


def resume(p):
    ws_call(p["web"], {"t": "tx_resume"})
    assert testkit.wait_for(lambda: not latched(p), 5), "resume did not take"


def panel(port, d):
    """The simulator's front panel (server.py's sim_panel hook): a change made
    at the radio, past every app path."""
    async def go():
        async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
            await ws.receive(timeout=5)
            await ws.send_str(json.dumps({"t": "sim_panel", "d": d}))
            await asyncio.sleep(0.2)
    asyncio.run(go())


with open(os.path.join(REPO, "hosts", "test.json"), "w") as f:
    json.dump({"host": "test", "os": "linux", "repo": "~/ranch-radio", "python": sys.executable,
               "radios": [], "station": {"https": False}}, f)
cfg = {
    "name": "Test Station",
    "host_file": "test",                 # the Modules tab, on the scratch repo above
    "radios": [{"id": "g90", "name": "G90", **G90}, {"id": "qmx", "name": "QMX", **QMX}],
    "winlink": {"pat": testkit.fake_pat_cmd(), "pat_port": PAT, "lane_port": LANE,
                "ardopcf": f"{sys.executable} tests/fake_ardopcf.py", "ardop_port": ARDOP,
                "ardop_logs": os.path.join(TMP, "ardop")},
    "mesh": {"port": "/dev/null", "python": sys.executable, "bridge": "tests/fake_mesh_bridge.py", "log": ""},
    "addons": {"propagation": {"swpc": False}},   # an add-on tab that asks nobody for anything
    "addons_state": os.path.join(TMP, "addons.json"),
}
cfg_path = os.path.join(TMP, "station.json")
with open(cfg_path, "w") as f:
    json.dump(cfg, f)

RIG = "document.querySelector('#rig-bar .rb-rig[data-id=\"{}\"]')"
G, Q = RIG.format("g90"), RIG.format("qmx")
TEXT = "((e, s) => e?.querySelector(s)?.textContent || '')"
SHOWN = "((e) => !!e && !e.closest('[hidden]') && e.offsetParent !== null && getComputedStyle(e).display !== 'none')"
FREQ_RE = "/^\\d+\\.\\d{3}\\.\\d{3}$/"


def shot(name):
    if SHOTS:
        b.screenshot(os.path.join(SHOTS, f"rigbar-{name}.png"))


def bar_on(view, width):
    """The bar on one view: shown, both radios, the focus one in full, inside the screen."""
    b.js(f"location.hash = '#/{view}'")
    # The view itself must be open: show() quietly falls back to home for a route it doesn't know.
    pane = {"g90": "#view-radio", "propagation": "#view-addon-propagation"}.get(view, f"#view-{view}")
    ok = b.wait(f"!document.querySelector('{pane}')?.hidden && {SHOWN}(document.querySelector('#rig-bar')) && "
                f"{FREQ_RE}.test({TEXT}({G}, '.rb-f')) && {FREQ_RE}.test({TEXT}({Q}, '.rb-f')) && 'ok'", 20)
    r = b.js("(() => { const e = document.querySelector('#rig-bar'), r = e.getBoundingClientRect(); "
             "return [r.left >= 0 && r.right <= innerWidth + 0.5, e.scrollWidth <= e.clientWidth + 1, "
             "[...e.querySelectorAll('.rb-rig')].every((x) => x.getBoundingClientRect().right <= innerWidth + 0.5)]; })()")
    full = b.js(f"{SHOWN}(document.querySelector('#rig-bar .rb-rig.focus .rb-meter')) && "
                f"/^S\\d/.test({TEXT}(document.querySelector('#rig-bar .rb-rig.focus'), '.rb-lvl'))")
    check(f"1. {width} px, #/{view}: the bar, both radios, focus in full, on screen",
          (ok, r, full), ("ok", [True, True, True], True))
    # PR 1b: the bar's STOP TX on every view, inside the screen.
    check(f"  {width} px, #/{view}: the bar's STOP TX is shown, on screen",
          b.js(f"(() => {{ const e = document.querySelector('#rig-bar .rb-stop-btn'); "
               f"return {SHOWN}(e) && e.getBoundingClientRect().right <= innerWidth + 0.5; }})()"), True)


g90 = radio("g90", G90)
qmx = radio("qmx", QMX, "--tx-timeout", "2")
front = subprocess.Popen([sys.executable, "station.py", "--config", cfg_path, "--listen", str(WEB),
                          "--rigctl-port", str(SHARED)], cwd=ROOT,
                         env=dict(os.environ, FAKE_PAT_RIGCTL=str(LANE), RANCH_RADIO_REPO=REPO, RANCH_RADIO_HOME=HOME),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
b = Browser(CHROME, CDP, size="1500,1000")
try:
    assert wait_port(WEB), "station did not start"
    b.goto(f"http://127.0.0.1:{WEB}/#/home")
    b.wait(f"{FREQ_RE}.test({TEXT}({G}, '.rb-f')) && {FREQ_RE}.test({TEXT}({Q}, '.rb-f'))", 30)

    # Focus (UX plan 5.1): by default the radio on the shared port; one radio in full.
    check("the bar names both radios, in the station's order",
          b.js("[...document.querySelectorAll('#rig-bar .rb-rig .rb-name b')].map((e) => e.textContent)"), ["G90", "QMX"])
    check("  one focus radio", b.js("document.querySelectorAll('#rig-bar .rb-rig.focus').length"), 1)
    b.js("localStorage.removeItem('rr-focus')")
    # Whichever is compact now, click it, then make the G90 the focus for what follows.
    other = "qmx" if b.js(f"{G}.classList.contains('focus')") else "g90"
    b.js(f"{RIG.format(other)}.click()")
    check("  a click on the compact radio focuses it",
          bool(b.wait(f"{RIG.format(other)}?.classList.contains('focus')", 5)), True)
    if other == "qmx":
        b.js(f"{G}.click()")
    b.wait(f"{G}?.classList.contains('focus') && !{Q}.classList.contains('focus')", 5)
    check("  and it is remembered", b.js("localStorage.getItem('rr-focus')"), "g90")
    check("  the compact radio: name and frequency, no meter",
          (b.js(f"{SHOWN}({Q}.querySelector('.rb-f'))"), b.js(f"{SHOWN}({Q}.querySelector('.rb-meter'))")), (True, False))
    check("  a radio's name opens its tab", b.js(f"{G}.querySelector('a.rb-name').getAttribute('href')"), "#/g90")
    # PR 1d (2026-11-19): the focus frequency is a typed entry (tune_path_page_test).
    check("  the focus frequency is the typed entry's button, beside the padlock",
          b.js(f"{G}.querySelector('.rb-f').tagName + ' ' + !!{G}.querySelector('.rb-lock')"), "BUTTON true")
    b.goto(f"http://127.0.0.1:{WEB}/#/home")
    check("  the focus survives a reload", bool(b.wait(f"{G}?.classList.contains('focus') && "
          f"{FREQ_RE}.test({TEXT}({G}, '.rb-f'))", 20)), True)

    # 1. Every view, at a desk's width.
    for v in ("home", "g90", "winlink", "mesh", "propagation", "modules"):
        bar_on(v, 1500)
    b.js("location.hash = '#/home'")
    shot("home-1500")

    # 2. A change at the radio's own front panel.
    b.js("location.hash = '#/winlink'")
    panel(G90["web"], {"freq": 7_074_000, "mode": "USB", "data": True})
    check("2. follows the radio's front panel: frequency, mode -D, band",
          b.wait(f"{TEXT}({G}, '.rb-f') === '7.074.000' && [{TEXT}({G}, '.rb-f'), {TEXT}({G}, '.rb-mode'), "
                 f"{TEXT}({G}, '.rb-band')]", 10), ["7.074.000", "USB-D", "40 m"])
    panel(QMX["web"], {"freq": 14_062_000})
    check("  the compact radio follows too", b.wait(f"{TEXT}({Q}, '.rb-f') === '14.062.000'", 10), True)

    # 3. Keyed (the G90's simulator, over rigctl): ON AIR, watts, Release TX.
    with socket.create_connection(("127.0.0.1", G90["rigctl"]), timeout=3) as c:
        f = c.makefile("r")
        c.sendall(b"T 1\n")
        f.readline()
        check("3. keyed: ON AIR, Release TX, watts",
              bool(b.wait(f"{G}?.classList.contains('tx') && {SHOWN}({G}.querySelector('.rb-air')) && "
                          f"{SHOWN}({G}.querySelector('.rb-release')) && /^\\d+ W$/.test({TEXT}({G}, '.rb-lvl'))", 5)), True)
        check("  its dot is red", b.js(f"{G}.querySelector('.st-dot').classList.contains('tx')"), True)
        check("  the other radio is not on the air", b.js(f"{SHOWN}({Q}.querySelector('.rb-air'))"), False)
        shot("tx-1500")
        b.js(f"{G}.querySelector('.rb-release').click()")
        check("  Release TX: the bar says it is off the air",
              bool(b.wait(f"!{G}.classList.contains('tx') && !{SHOWN}({G}.querySelector('.rb-release'))", 5)), True)
        c.sendall(b"t\n")
        check("  and the radio is unkeyed", f.readline().strip(), "0")

    # 5. A notice from the radio's server: the QMX's 2 s transmit time limit.
    with socket.create_connection(("127.0.0.1", QMX["rigctl"]), timeout=3) as c:
        f = c.makefile("r")
        c.sendall(b"T 1\n")
        f.readline()
        check("5. the compact radio keyed: ON AIR and Release TX all the same",
              bool(b.wait(f"{Q}?.classList.contains('tx') && {SHOWN}({Q}.querySelector('.rb-release'))", 5)), True)
        check("  its server's notice is a toast, named for the radio",
              bool(b.wait("[...document.querySelectorAll('#toasts .toast')].some((t) => "
                          "t.textContent.startsWith('QMX: Transmitter released'))", 10)), True)
        check("  and the bar follows the unkey", bool(b.wait(f"!{Q}.classList.contains('tx')", 5)), True)

    # 4. The QMX's server stops: offline; back: live again.
    stop(qmx)
    check("4. a radio whose server stops shows offline",
          b.wait(f"{Q}?.classList.contains('off') && {TEXT}({Q}, '.rb-f')", 15), "offline")
    check("  its name no longer links to a tab", b.js(f"!!{Q}.querySelector('a.rb-name')"), False)
    qmx = radio("qmx", QMX, "--tx-timeout", "2")
    check("  restarted, it comes back", bool(b.wait(f"{Q} && !{Q}.classList.contains('off') && "
                                                     f"{FREQ_RE}.test({TEXT}({Q}, '.rb-f'))", 20)), True)

    # ---------- STOP TX in the bar (UX plan PR 1b, 2026-11-19)
    STATE = "document.querySelector('#rig-bar .rb-stop-state')"
    ST_TEXT = f"({STATE}.hidden ? '' : {STATE}.textContent)"
    PRESS = "document.querySelector('#rig-bar .rb-stop-btn').click()"
    INTENTS = "JSON.parse(localStorage.getItem('rr-stop-intent') || '{}')"
    # Counts the tx_stop messages this page sends, per radio (the station's
    # document only; the frames have their own WebSocket).
    COUNT = ("(() => { if (window.__stops) return; window.__stops = {}; const o = WebSocket.prototype.send; "
             "WebSocket.prototype.send = function (d) { const id = (this.url.match(/\\/r\\/([^/]+)\\/ws/) || [])[1]; "
             "if (String(d).includes('tx_stop')) { window.__stops[id] = (window.__stops[id] || 0) + 1; "
             "if (window.__drop?.[id]) return; } const r = o.call(this, d); "
             "if (window.__dropAfter?.[id] && String(d).includes('tx_stop')) { window.__dropAfter[id] = false; this.close(); } "
             "return r; }; })()")

    def chip(rid):
        return b.js(f"{RIG.format(rid)}?.dataset.stop || ''")

    def reload_home():
        # A real reload: goto to the same URL with another hash would be a
        # same-document navigation, and the page would keep its memory.
        b.js("window.__reloaded = 1; location.hash = '#/home'; location.reload()")
        b.wait("!window.__reloaded && document.readyState === 'complete'", 20)
        b.wait(f"{FREQ_RE}.test({TEXT}({G}, '.rb-f'))", 20)
        b.js(COUNT)

    def qmx_down():
        global qmx
        stop(qmx)
        b.wait(f"{Q}?.classList.contains('off')", 15)

    def qmx_up():
        global qmx
        qmx = radio("qmx", QMX, "--tx-timeout", "2")
        b.wait(f"{Q} && !{Q}.classList.contains('off') && {FREQ_RE}.test({TEXT}({Q}, '.rb-f'))", 20)

    def quiet_then_nothing_sent(name, rid, p):
        time.sleep(3)                    # a quiet window: past a reconnect (2 s), nothing may be sent
        check(name, (b.js(f"window.__stops['{rid}'] || 0"), latched(p)), (0, False))

    b.js("localStorage.removeItem('rr-stop-intent')")
    b.js(COUNT)
    check("S. before a press: neither radio latched, the bar says nothing",
          (latched(G90), latched(QMX), b.js(ST_TEXT)), (False, False, ""))
    check("  the button is labelled for all HF radios",
          b.js("document.querySelector('#rig-bar .rb-stop-btn').textContent"), "STOP TX all HF")

    # S1. A press: both latch; the frame's PTT is disabled; TX STOPPED, after a reload too.
    b.js(PRESS)
    check("S1. press: both radios latched (fresh sockets)",
          testkit.wait_for(lambda: latched(G90) and latched(QMX), 10), True)
    check("  the bar reads TX STOPPED", b.wait(f"{ST_TEXT} === 'TX STOPPED' && {STATE}.dataset.state", 10), "all")
    check("  each radio's chip reads STOPPED and opens its tab",
          b.js(f"[{G}, {Q}].map((e) => e.querySelector('.rb-tag').textContent + ' ' + e.querySelector('.rb-tag').getAttribute('href'))"),
          ["STOPPED #/g90", "STOPPED #/qmx"])
    check("  the intents are done", b.js(f"Object.keys({INTENTS}).length"), 0)
    shot("stop-1500")
    b.js("location.hash = '#/g90'")
    FRAME = "document.querySelector('#view-radio iframe[src*=\"r/g90/\"]')?.contentDocument"
    check("  the G90's frame: PTT disabled, its own STOP TX still there",
          b.wait(f"{FRAME}?.querySelector('#ptt')?.disabled && {FRAME}.querySelector('#tx-stop')?.offsetParent !== null "
                 f"&& {FRAME}.querySelector('#tx-stop').textContent", 20), "TX STOPPED")
    shot("stop-radio-tab-1500")
    reload_home()
    check("  a reload: TX STOPPED", b.wait(f"{ST_TEXT} === 'TX STOPPED'", 10), True)

    # S8. Latched, and the radio still reports transmitting (keyed at its front panel).
    panel(G90["web"], {"tx": True})
    check("S8. latched and still keyed: its own red state, ON AIR stays",
          b.wait(f"{STATE}.dataset.state === 'keyed' && {G}.classList.contains('tx') && "
                 f"[{ST_TEXT}.includes('G90: LATCHED BUT STILL KEYED: check the radio'), {ST_TEXT}.includes('TX STOPPED'), "
                 f"{SHOWN}({G}.querySelector('.rb-air'))]", 10), [True, False, True])
    check("  its chip says so", (chip("g90"), b.js(f"{TEXT}({G}, '.rb-tag')")), ("keyed", "STILL KEYED"))
    shot("stop-keyed-1500")
    panel(G90["web"], {"tx": False})
    b.wait(f"{ST_TEXT} === 'TX STOPPED'", 10)

    # S5. Resume on the G90 from a second page: shown as resumed, not re-latched.
    b.js("window.__stops = {}")
    resume(G90)
    check("S5. resumed elsewhere: the bar shows it, not TX STOPPED",
          b.wait(f"{ST_TEXT}.includes('G90 resumed') && !{ST_TEXT}.includes('TX STOPPED') && 'ok'", 10), "ok")
    quiet_then_nothing_sent("  and this page does not re-latch it", "g90", G90)
    resume(QMX)
    b.wait(f"{ST_TEXT} === ''", 10)

    # S7. The bar's STOP TX never reaches the G90; its own page's STOP TX does.
    b.js("location.hash = '#/g90'")
    b.wait(f"{FRAME}?.querySelector('#conn b')?.textContent === 'Connected' && !{FRAME}.querySelector('#tx-stop-banner').offsetParent", 20)
    b.js("window.__drop = {g90: true}")
    b.js(PRESS)
    check("S7. the bar's message to the G90 is lost: QMX latched, G90 not reached",
          (testkit.wait_for(lambda: latched(QMX), 10), latched(G90),
           bool(b.wait(f"{ST_TEXT}.includes('G90 not reached') && {ST_TEXT}.includes('QMX stopped')", 10))), (True, False, True))
    b.js(f"{FRAME}.querySelector('#tx-stop').click()")
    check("  the frame's own STOP TX latches it", testkit.wait_for(lambda: latched(G90), 10), True)
    check("  and the bar then shows it", b.wait(f"{ST_TEXT} === 'TX STOPPED' && {STATE}.dataset.state", 10), "all")
    b.js("window.__drop = {}; location.hash = '#/home'")
    resume(G90)
    resume(QMX)

    # S2. The QMX is down at the press.
    qmx_down()
    b.js(PRESS)
    check("S2. one radio down at the press: the other latches",
          (testkit.wait_for(lambda: latched(G90), 10), bool(b.wait(f"{ST_TEXT}.includes('QMX not reached (offline)')", 10))),
          (True, True))
    # Wait for the bar to draw the G90's own latch too: the check above only
    # waited for the QMX's "not reached", and on a slow runner the G90's
    # tx_stop reply could still be on its way (CI, 2026-11-19). The wait still
    # fails if the bar never reaches "partial, G90 stopped".
    b.wait(f"{STATE}.dataset.state === 'partial' && {ST_TEXT}.includes('G90 stopped')", 10)
    check("  the bar names the one not reached, not TX STOPPED",
          (b.js(f"{STATE}.dataset.state"), b.js(f"{ST_TEXT}.includes('TX STOPPED')"), b.js(f"{ST_TEXT}.includes('G90 stopped')")),
          ("partial", False, True))
    check("  a Cancel is offered for the pending press", b.js(f"{SHOWN}(document.querySelector('#rig-bar .rb-stop-cancel'))"), True)
    qmx_up()
    check("  back up, the QMX latches without another press", testkit.wait_for(lambda: latched(QMX), 15), True)
    check("  the bar reads TX STOPPED", b.wait(f"{ST_TEXT} === 'TX STOPPED'", 10), True)
    resume(G90)
    resume(QMX)

    # S4. The same, with a reload while the QMX's intent is pending.
    qmx_down()
    b.js(PRESS)
    b.wait(f"{ST_TEXT}.includes('QMX not reached (offline)')", 10)
    reload_home()
    check("S4. reload while pending: the intent survives",
          (b.js(f"'qmx' in {INTENTS}"), bool(b.wait(f"{ST_TEXT}.includes('QMX not reached (offline)')", 10))), (True, True))
    qmx_up()
    check("  and completes when the radio is back", testkit.wait_for(lambda: latched(QMX), 15), True)
    check("  TX STOPPED, intents done",
          (bool(b.wait(f"{ST_TEXT} === 'TX STOPPED'", 10)), b.js(f"Object.keys({INTENTS}).length")), (True, 0))

    # S9. The latched QMX's server is replaced: its latch is gone with it.
    b.js("window.__stops = {}")
    stop(qmx)
    check("  (a latched radio that drops reads unknown or offline, not stopped)",
          bool(b.wait(f"/QMX (unknown|offline)/.test({ST_TEXT}) && !{ST_TEXT}.includes('TX STOPPED')", 10)), True)
    qmx_up()
    check("S9. replaced: the bar leaves TX STOPPED and says the QMX is no longer stopped",
          bool(b.wait(f"{ST_TEXT}.includes('QMX no longer stopped') && !{ST_TEXT}.includes('TX STOPPED')", 10)), True)
    quiet_then_nothing_sent("  and the page sends it nothing", "qmx", QMX)
    b.js("document.querySelector('#rig-bar .rb-stop-cancel').click()")
    check("  Dismiss clears the note", b.wait(f"{ST_TEXT}.includes('G90 stopped') && !{ST_TEXT}.includes('QMX no longer')", 5), True)
    resume(G90)
    b.wait(f"{ST_TEXT} === ''", 10)

    # S6. Past the TTL (4 s here), a pending press is not delivered and never sent.
    b.js("localStorage.setItem('rr-stop-ttl-s', '4')")
    qmx_down()
    b.js(PRESS)
    check("S6. past the TTL: the QMX's press shows as not delivered, loudly",
          b.wait(f"{ST_TEXT}.includes('QMX not delivered') && document.querySelector('#rig-bar .rb-stop').classList.contains('bad') "
                 f"&& {Q}.dataset.stop", 15), "expired")
    b.js("window.__stops = {}")
    qmx_up()
    quiet_then_nothing_sent("  the radio comes back: nothing is sent, it stays unlatched", "qmx", QMX)
    check("  still shown as not delivered", b.js(f"{ST_TEXT}.includes('QMX not delivered')"), True)
    b.js("document.querySelector('#rig-bar .rb-stop-cancel').click(); localStorage.removeItem('rr-stop-ttl-s')")
    check("  Dismiss clears it", b.wait(f"!{ST_TEXT}.includes('not delivered') && Object.keys({INTENTS}).length === 0", 5), True)
    resume(G90)

    # S3. The G90's socket drops after the send and before the reply.
    b.wait(f"{ST_TEXT} === ''", 10)
    b.js("window.__dropAfter = {g90: true}")
    b.js(PRESS)
    check("S3. dropped before the reply: the G90 reads unknown",
          bool(b.wait(f"{ST_TEXT}.includes('G90 not reached (unknown)')", 5)), True)
    check("  the new socket's hello settles it: TX STOPPED", b.wait(f"{ST_TEXT} === 'TX STOPPED'", 15), True)
    check("  both latched (fresh sockets)", (latched(G90), latched(QMX)), (True, True))
    resume(G90)
    resume(QMX)

    # S10. No press, the QMX down at page load: it comes up and nothing is sent.
    b.wait(f"{ST_TEXT} === ''", 10)
    b.js("localStorage.removeItem('rr-stop-intent')")
    qmx_down()
    reload_home()
    qmx_up()
    quiet_then_nothing_sent("S10. no press, a radio down at load: nothing sent when it connects", "qmx", QMX)
    check("  the bar does not read TX STOPPED", b.js(ST_TEXT), "")

    # 1, again on a phone.
    b.call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=2, mobile=True)
    b.wait("innerWidth === 390")
    for v in ("home", "g90", "winlink", "mesh", "propagation", "modules"):
        bar_on(v, 390)
    b.js("location.hash = '#/home'")
    b.wait(f"{FREQ_RE}.test({TEXT}({G}, '.rb-f'))")
    shot("home-390")
    # The bar's STOP TX on a phone: pressed, it fits and reads TX STOPPED.
    b.js(PRESS)
    check("S. 390 px: a press reads TX STOPPED, the bar inside the screen",
          (bool(b.wait(f"{ST_TEXT} === 'TX STOPPED'", 10)),
           b.js("(() => { const e = document.querySelector('#rig-bar'); return e.scrollWidth <= e.clientWidth + 1 && "
                "document.querySelector('#rig-bar .rb-stop').getBoundingClientRect().right <= innerWidth + 0.5; })()")),
          (True, True))
    shot("stop-390")
    resume(G90)
    resume(QMX)
finally:
    b.close()
    stop(front)
    stop(g90)
    stop(qmx)
    shutil.rmtree(TMP, ignore_errors=True)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
