"""A phone as the station's remote (UX plan PR 5b, 2026-11-19), in headless
Chromium at 390x844 against a simulated radio with the fake Mesh node, and
the station in front of it:

  station page, 700 px and under
  - a bottom tab bar: Home, Radio, Messages, Mesh, More, inside the screen,
    clear of the iPhone home indicator (safe-area inset)
  - the header is one row; the rig bar is the second row, its STOP TX on screen
  - Radio opens the focus radio; More opens the rest
  radio page, 860 px and under
  - order: VFO, meters, the TX panel (Listen, PTT), scope, band and mode, levels
  - at 390x844 the frequency, the S-meter and Listen are on screen without
    scrolling
  Mesh, 390x844
  - the header's stats and buttons fold into one menu; the first message
    starts at or above 260 px (425 before this PR)

    python3 tests/phone_remote_page_test.py [screenshot-dir]
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from cdp_browser import Browser  # noqa: E402
from fake_mesh_bridge import FAKE_KEY  # noqa: E402
import testkit  # noqa: E402

CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PHONE_SHOTS")
WEB, RIGCTL, LANE, ST, CDP = (testkit.free_port() for _ in range(5))
TMP = tempfile.mkdtemp(prefix="phone-remote-")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


def wait_port(port, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def shot(name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        b.screenshot(os.path.join(SHOTS, f"phone-{name}.png"))


def phone():
    b.call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=2, mobile=True)


# An element's box, whether it is shown, and whether it lies inside the
# 390x844 screen without scrolling (the document's own scroll counts too).
ONSCREEN = ("((s, doc = document) => { const e = doc.querySelector(s); if (!e) return null; "
            "const r = e.getBoundingClientRect(), st = getComputedStyle(e); "
            "return {top: Math.round(r.top), bottom: Math.round(r.bottom), left: Math.round(r.left), right: Math.round(r.right), "
            "shown: st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && !e.closest('[hidden]'), "
            "inside: r.top >= 0 && r.left >= 0 && r.bottom <= 844 && r.right <= 390, "
            "scrolled: (doc.scrollingElement || doc.documentElement).scrollTop}; })")

key_file = os.path.join(TMP, "key")
with open(key_file, "w") as f:
    f.write(FAKE_KEY)
adds = os.path.join(TMP, "addons.json")
with open(adds, "w") as f:
    json.dump({"mesh": {"port": "/dev/null", "python": sys.executable, "bridge": "tests/fake_mesh_bridge.py",
                        "keys": [f"HomeNet={key_file}"], "log": ""}}, f)
radio = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--host", "127.0.0.1",
                          "--listen", str(WEB), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(RIGCTL),
                          "--winlink-rigctl-port", str(LANE), "--addons-state", adds, "--addons", adds],
                         cwd=ROOT, env=dict(os.environ, FAKE_MESH_LOG=os.path.join(TMP, "mesh.jsonl")),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
cfg = {"name": "Test Station",
       "radios": [{"id": "ic7300", "name": "IC-7300", "web": WEB, "rigctl": RIGCTL, "lane": LANE,
                   "audio_in": "in", "audio_out": "out"}],
       "mesh": {"port": "/dev/null", "python": sys.executable, "bridge": "tests/fake_mesh_bridge.py", "log": ""},
       "addons_state": os.path.join(TMP, "station-addons.json")}
cfg_path = os.path.join(TMP, "station.json")
with open(cfg_path, "w") as f:
    json.dump(cfg, f)
station = subprocess.Popen([sys.executable, "station.py", "--config", cfg_path, "--listen", str(ST),
                            "--rigctl-port", "0"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
b = None
try:
    assert wait_port(WEB) and wait_port(ST), "radio or station did not start"
    b = Browser(CHROME, CDP, size="390,844")
    phone()

    # ---------------------------------------------------------------- radio page
    print("\n# the radio's own page at 390x844", flush=True)
    b.goto(f"http://127.0.0.1:{WEB}/")
    b.wait("document.querySelector('#conn b')?.textContent === 'Connected' && matchMedia('(width: 390px)').matches", 20)
    b.wait("/\\d/.test(document.querySelector('#freq')?.textContent || '')", 10)
    order = b.js("[...document.querySelectorAll('main.grid > section.panel')].filter((e) => !e.hidden && "
                 "getComputedStyle(e).display !== 'none').map((e) => [e.classList[1], Math.round(e.getBoundingClientRect().top)])"
                 ".sort((a, c) => a[1] - c[1]).map((x) => x[0])")
    check("order: VFO, meters, TX panel, scope, band and mode, levels",
          [x for x in order if x in ("vfo", "meters", "txpanel", "scope", "bandmode", "levels")],
          ["vfo", "meters", "txpanel", "scope", "bandmode", "levels"])
    for sel, name in (("#freq", "the frequency"), ("#smeter", "the S-meter"), ("#btn-listen", "Listen")):
        r = b.js(f"{ONSCREEN}({json.dumps(sel)})") or {}
        check(f"  {name} is on screen without scrolling", (r.get("shown"), r.get("inside"), r.get("scrolled")), (True, True, 0))
    check("  STOP TX is on screen", (b.js(f"{ONSCREEN}('#tx-stop')") or {}).get("inside"), True)
    shot("radio")

    # ---------------------------------------------------------------- Mesh
    print("\n# Mesh on the radio's page at 390x844", flush=True)
    b.goto(f"http://127.0.0.1:{WEB}/?v=mesh#/mesh")
    b.wait("!!document.querySelector('#mesh-tabs [data-conv=c1]')", 20)
    b.js("document.querySelector('#mesh-tabs [data-conv=c1]')?.click()")
    # Where the messages start on screen: the list's own top, with the page
    # itself unscrolled (the list scrolls to its newest message, so the first
    # bubble's position says nothing about the layout).
    b.wait("document.querySelector('#mesh-list .chat-bubble')?.getBoundingClientRect().height > 0", 15)
    first = b.js("(() => { const se = document.scrollingElement; se.scrollTop = 0; "
                 "const r = document.querySelector('#mesh-list').getBoundingClientRect(); "
                 "return [Math.round(r.top), se.scrollTop]; })()")
    check("the messages start at or above 260 px, page unscrolled (was 425)",
          (first[0] <= 260, first[1]), (True, 0))
    print("  measured: messages start at", first[0], "px", flush=True)
    menu = b.js("(() => { const m = document.querySelector('#mesh .mesh-more'); return !!m && getComputedStyle(m).display !== 'none'; })()")
    check("  the header's stats and buttons are in one menu", menu, True)
    shot("mesh")

    # ---------------------------------------------------------------- station page
    print("\n# the station page at 390x844", flush=True)
    b.goto(f"http://127.0.0.1:{ST}/")
    b.wait("document.querySelector('#conn b')?.textContent === 'Connected' && matchMedia('(width: 390px)').matches", 20)
    bar = b.wait("(() => { const n = document.querySelector('#st-bottom'); if (!n || getComputedStyle(n).display === 'none') return null; "
                 "return [...n.querySelectorAll('.st-btab')].map((a) => a.firstChild.textContent.trim()); })()", 10)
    check("a bottom tab bar: Shack, Radio, Messages, Mesh, More", bar, ["Shack", "Radio", "Messages", "Mesh", "More"])
    r = b.js(f"{ONSCREEN}('#st-bottom')") or {}
    check("  pinned to the bottom, inside the screen", (r.get("inside"), r.get("bottom")), (True, 844))
    check("  it leaves room for the home indicator (safe-area padding)",
          "safe-area-inset-bottom" in (b.js("[...document.styleSheets].flatMap((s) => { try { return [...s.cssRules]; } "
                                            "catch (e) { return []; } }).map((r) => r.cssText).join(' ')") or ""), True)
    head = b.js("(() => { const t = document.querySelector('.topbar').getBoundingClientRect(), "
                "rb = document.querySelector('#rig-bar').getBoundingClientRect(); "
                "return [Math.round(t.bottom), Math.round(rb.top), Math.round(rb.bottom)]; })()")
    check("  the header is one row; the rig bar is the second", (head[0] <= 64, head[1] >= head[0] - 1), (True, True))
    check("  the rig bar's STOP TX is on screen", (b.js(f"{ONSCREEN}('#rig-bar .rb-stop-btn')") or {}).get("inside"), True)
    check("  the top nav gives way to the bottom bar", b.js("getComputedStyle(document.querySelector('#st-nav')).display"), "none")
    b.js("location.hash = '#/home'")
    b.wait("location.hash === '#/home'")
    b.js("[...document.querySelectorAll('#st-bottom a')].find((a) => a.textContent.trim() === 'Radio').click()")
    check("Radio opens the focus radio", bool(b.wait("location.hash === '#/ic7300'", 10)), True)
    b.js("[...document.querySelectorAll('#st-bottom a')].find((a) => a.textContent.trim() === 'More').click()")
    more = b.wait("(() => { const m = document.querySelector('#st-more'); return m && !m.hidden && "
                  "[...m.querySelectorAll('a')].map((a) => a.textContent.trim()); })()", 5)
    check("More opens the rest of the station's tabs", bool(more) and "Shack" in more, True)
    shot("station")
finally:
    for p in (station, radio):
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
    if b:
        b.close()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'FAIL' if fails else 'PASS'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
