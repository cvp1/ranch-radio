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
    check("  the focus frequency is display: it opens the tab",
          b.js(f"{G}.querySelector('.rb-f').tagName + ' ' + {G}.querySelector('.rb-f').getAttribute('href')"), "A #/g90")
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

    # 1, again on a phone.
    b.call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=2, mobile=True)
    b.wait("innerWidth === 390")
    for v in ("home", "g90", "winlink", "mesh", "propagation", "modules"):
        bar_on(v, 390)
    b.js("location.hash = '#/home'")
    b.wait(f"{FREQ_RE}.test({TEXT}({G}, '.rb-f'))")
    shot("home-390")
finally:
    b.close()
    stop(front)
    stop(g90)
    stop(qmx)
    shutil.rmtree(TMP, ignore_errors=True)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
