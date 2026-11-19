"""The Operate view (UX plan PR 2b, 2026-11-19): the focus radio, the Roster
and the Map side by side on the station page, in headless Chromium.

Two simulated radios behind station.py: an IC-7300 with the simulator's audio
and a G90. The Roster and Map add-ons' page scripts are the real ones; their
server side is not run: the test hands the registry the add-ons' list and
snapshots as the station's hello would, and keeps the add-on messages the page
sends (it forwards nothing for them). The browser reaches no network: every
https URL (the map's tiles) is blocked. Nothing transmits. Checks:
  1. at 1600x1000 the three panes are visible at once, do not overlap, each
     at least 320 px wide; the radio frame's box is its pane's; the band
     strip is over them; the bar's STOP TX and the frame's own STOP TX are
     both visible, the frame's with its connection state; Controls (Levels,
     the TX panel, Filter/AGC) is closed and opens and closes, and while the
     page is armed those stay shown with Controls closed
  2. the frame is not reloaded: a marker on its window survives entering and
     leaving; the audio started from the bar's Listen keeps running; the
     frame sends its radio nothing but its ping meanwhile
  3. a row click in the Roster pane still sends do: "reply"
  4. with two radios the pane shows the focus radio; focusing the other in
     the bar swaps the frames (and the pane mode with them)
  5. with the Map switched off, two panes share the width; on again, three
  6. resized live from 1600 to 1200, the radio on the left and the Roster
     over the Map; at 900 the tab is gone and the route lands on #/home
  7. after leaving, the Roster view's box is .st-main's and the Map is its
     full-width height again, its station list as it was (map.list unwritten)
  S. the frame's STOP TX is clickable in the pane: a real click there latches
     the radio and its banner (with Resume) shows in the pane

    python3 tests/operate_page_test.py [screenshot-dir]
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile

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
TMP = tempfile.mkdtemp(prefix="operate-page-")

fp = testkit.free_port
WEB, SHARED, CDP = fp(), fp(), fp()
IC = {"web": fp(), "rigctl": fp(), "lane": fp()}
G90 = {"web": fp(), "rigctl": fp(), "lane": fp()}
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:70} got {got!r}" + ("" if ok else f" want {want!r}"), flush=True)


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
    return proc


def stop(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


cfg = {"name": "Test Station",
       "radios": [{"id": "ic7300", "name": "IC-7300", **IC}, {"id": "g90", "name": "G90", **G90}],
       "addons_state": os.path.join(TMP, "addons.json")}
cfg_path = os.path.join(TMP, "station.json")
with open(cfg_path, "w") as f:
    json.dump(cfg, f)

# A roster as the logbook sync sends it (fake calls), and a map snapshot.
ROW = {"answer": "cq", "cq": True, "cq_word": None, "to_me": False, "to": None, "snr": -10, "dt": 0.1, "df": 1500,
       "age": 0, "need": "dxcc", "reasons": ["dxcc", "call_band_mode"], "lotw": False, "band": "20m", "mode": "FT8"}
ROSTER = {"band": "20m", "mode": "FT8", "dial": 14074000, "needs": {"qsos": 0, "dxcc_worked": 0, "dxcc_confirmed": 0,
          "grids_worked": 0, "grids_confirmed": 0, "lotw_users": 0},
          "rows": [{**ROW, "key": "JTDX|1|1500|CQ JA5XYZ PM95", "call": "JA5XYZ", "grid": "PM95", "message": "CQ JA5XYZ PM95",
                    "entity": "Japan", "cont": "AS", "km": 10853, "bearing": 334},
                   {**ROW, "key": "JTDX|1|900|CQ VK2ABC QF56", "call": "VK2ABC", "grid": "QF56", "message": "CQ VK2ABC QF56",
                    "entity": "Australia", "cont": "OC", "km": 16077, "bearing": 268}]}
MAP = {"map": {"me": "FN31", "band": "20m", "showing": "20m", "grids": {}, "decodes": []}, "pota": []}
ADDONS = [{"name": n, "label": n.title(), "title": n.title(), "group": "tools", "css": True, "on": True, "error": None}
          for n in ("roster", "map")]

FRAME = "document.querySelector('#view-radio iframe[src*=\"r/{}/\"]')"
SHOWN = "((e) => !!e && !e.closest('[hidden]') && getComputedStyle(e).display !== 'none' && e.getBoundingClientRect().width > 0)"
BOX = "((e) => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.width, r.height].map(Math.round); })"
PANES = (f"(() => {{ const v = (s) => document.querySelector(s); return {{radio: {BOX}(v('#view-radio')), "
         f"roster: {SHOWN}(v('#view-addon-roster')) ? {BOX}(v('#view-addon-roster')) : null, "
         f"map: {SHOWN}(v('#view-addon-map')) ? {BOX}(v('#view-addon-map')) : null, main: {BOX}(v('.st-main'))}}; }})()")


def frame_js(rid, expr):
    """`expr` evaluated in radio `rid`'s frame (its own globals), or null."""
    return b.js(f"(() => {{ const w = {FRAME.format(rid)}?.contentWindow; "
                f"try {{ return w && w.rrFrame ? w.eval({json.dumps(expr)}) : null; }} catch (e) {{ return 'JS error: ' + e; }} }})()")


def frame_wait(rid, expr, timeout=10):
    return testkit.wait_for(lambda: frame_js(rid, expr) is True, timeout, 0.1)


def click_at(x, y):
    """A real click (trusted, a user gesture) at page coordinates."""
    for t in ("mousePressed", "mouseReleased"):
        b.call("Input.dispatchMouseEvent", type=t, x=x, y=y, button="left", clickCount=1)


def click(sel):
    x, y = b.js(f"(() => {{ const r = document.querySelector({json.dumps(sel)}).getBoundingClientRect(); "
                f"return [r.x + r.width / 2, r.y + r.height / 2]; }})()")
    click_at(x, y)


def frame_point(rid, sel):
    """The middle of `sel` in radio `rid`'s frame, in the station page's coordinates."""
    return b.js(f"(() => {{ const f = {FRAME.format(rid)}, e = f.contentDocument.querySelector({json.dumps(sel)}); "
                "const a = f.getBoundingClientRect(), r = e.getBoundingClientRect(); "
                "return [a.x + r.x + r.width / 2, a.y + r.y + r.height / 2]; })()")


def shot(name):
    if SHOTS:
        b.screenshot(os.path.join(SHOTS, f"operate-{name}.png"))


def overlap(a, c):
    return max(0, min(a[0] + a[2], c[0] + c[2]) - max(a[0], c[0])) * max(0, min(a[1] + a[3], c[1] + c[3]) - max(a[1], c[1]))


def resize(w, h=1000):
    b.call("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=False)
    b.wait(f"innerWidth === {w}", 5)


def operate_ready(three=True):
    return b.wait(f"location.hash === '#/operate' && (() => {{ const p = {PANES}; return !!p.roster "
                  f"{'&& !!p.map' if three else ''} && {SHOWN}({FRAME.format('ic7300')}); }})()", 15)


# Records what a frame sends on any socket, by message type (its pings included).
SENDS = ("(() => { if (window.__sent) return true; window.__sent = []; const o = WebSocket.prototype.send; "
         "WebSocket.prototype.send = function (d) { let t = typeof d === 'string' ? 'text' : 'binary'; "
         "try { const m = JSON.parse(d); t = m.t + (m.k ? ':' + m.k : ''); } catch {} window.__sent.push(t); "
         "return o.call(this, d); }; return true; })()")

ic = radio("ic7300", IC)
g90 = radio("g90", G90)
front = subprocess.Popen([sys.executable, "station.py", "--config", cfg_path, "--listen", str(WEB),
                          "--rigctl-port", str(SHARED)], cwd=ROOT,
                         stdout=open(os.path.join(TMP, "station.log"), "w"), stderr=subprocess.STDOUT)
b = Browser(CHROME, CDP, size="1600,1000")
try:
    for n, port in (("ic7300", IC["web"]), ("g90", G90["web"]), ("station", WEB)):
        assert testkit.wait_for(lambda: _answers(port), 60, 0.1), f"{n} did not start"
    b.call("Network.enable")
    b.call("Network.setBlockedURLs", urls=["https://*"])       # no tiles, no network
    resize(1600)
    b.goto(f"http://127.0.0.1:{WEB}/#/home")
    b.js("localStorage.setItem('rr-focus', 'ic7300')")
    b.goto(f"http://127.0.0.1:{WEB}/?r=1#/home")
    assert b.wait("/^\\d+\\.\\d{3}\\.\\d{3}$/.test(document.querySelector('#rig-bar .rb-rig.focus .rb-f')?.textContent || '')", 30), \
        "the rig bar never showed the focus radio"
    assert frame_wait("ic7300", "typeof rrFrame === 'object' && !!CFG.name", 20), "the focus radio's frame never loaded"
    check("no Operate tab before the Roster or Map is on", b.js("!!document.querySelector('#st-nav a[href=\"#/operate\"]')"), False)
    b.js("""(() => {
      window.__addonSent = [];
      const real = window.send;
      window.send = (m) => { if (m && m.t === 'addon') { window.__addonSent.push(m); return; } real(m); };
      addons.hello(%s, %s);
    })()""" % (json.dumps(ADDONS), json.dumps({"roster": {"roster": ROSTER}, "map": MAP})))
    b.wait("document.querySelectorAll('#view-addon-roster .rs-row').length === 2 && !!document.querySelector('#view-addon-map .rm-wrap')", 15)
    check("the Operate tab follows Mission Control",
          b.js("[...document.querySelectorAll('#st-nav > a')].slice(0, 2).map((a) => a.getAttribute('href'))"),
          ["#/home", "#/operate"])

    # 2 (before). Listen from the bar, and a marker on the frame's window.
    frame_js("ic7300", "window.__opMarker = 'kept'")
    click("#rig-bar .rb-rig.focus .rb-listen")
    listening = frame_wait("ic7300", "rrFrame.listening() && audio.ctx.state === 'running'", 10)
    check("2. the bar's Listen runs the frame's audio (before Operate)", listening, True)
    mapList = b.js("localStorage.getItem('ic7300.map.list')")
    frame_js("ic7300", SENDS)

    # 1. Three panes at 1600x1000.
    b.js("location.hash = '#/operate'")
    check("1. #/operate shows the radio, the Roster and the Map", bool(operate_ready()), True)
    b.wait(f"{SHOWN}(document.querySelector('#view-addon-map .rm-tile')) || true", 3)
    p = b.js(PANES)
    boxes = [p["radio"], p["roster"], p["map"]]
    check("  each pane at least 320 px wide", [bx[2] >= 320 for bx in boxes], [True] * 3)
    check("  no two overlap", [overlap(boxes[i], boxes[j]) for i, j in ((0, 1), (0, 2), (1, 2))], [0, 0, 0])
    check("  left to right: radio, Roster, Map, on one row",
          (boxes[0][0] < boxes[1][0] < boxes[2][0], boxes[0][1] == boxes[1][1] == boxes[2][1]), (True, True))
    check("  widths about 36/26/38 of the row",
          [round(bx[2] / sum(x[2] for x in boxes) * 100) for bx in boxes], [36, 26, 38])
    check("  the radio frame's box is its pane's", b.js(f"{BOX}({FRAME.format('ic7300')})"), p["radio"])
    check("  the band strip is over the panes",
          b.js(f"(() => {{ const s = document.querySelector('#op-bands'); return {SHOWN}(s) && "
               "s.querySelectorAll('.bs-cell').length > 0 && s.getBoundingClientRect().bottom <= "
               "document.querySelector('#view-radio').getBoundingClientRect().top; })()"), True)
    check("  the frame is in pane mode; the other frames are not",
          (frame_js("ic7300", "document.documentElement.classList.contains('pane')"),
           b.js("!!document.querySelector('#view-radio iframe[src*=\"r/g90/\"]')")), (True, False))
    vis = ("(() => { const e = document.querySelector('%s'); if (!e) return null; const r = e.getBoundingClientRect(); "
           "return !e.closest('[hidden]') && getComputedStyle(e).display !== 'none' && getComputedStyle(e).visibility !== 'hidden' "
           "&& r.width > 0 && r.left >= 0 && r.right <= innerWidth + 0.5 && r.top >= 0 && r.bottom <= innerHeight + 0.5; })()")
    check("  STOP TX: the bar's and the frame's own both visible",
          (b.js(vis % "#rig-bar .rb-stop-btn"), frame_js("ic7300", vis % "#tx-stop")), (True, True))
    check("  the frame's STOP TX is the top element at its middle, in the frame and on the page",
          (frame_js("ic7300", "(() => { const r = document.querySelector('#tx-stop').getBoundingClientRect(); "
                              "return document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)?.id; })()"),
           b.js("(() => { const [x, y] = %s; return document.elementFromPoint(x, y) === %s; })()"
                % (json.dumps(frame_point("ic7300", "#tx-stop")), FRAME.format("ic7300")))), ("tx-stop", True))
    check("  the frame's top row keeps its connection state",
          (frame_js("ic7300", vis % "#conn"), frame_js("ic7300", "document.querySelector('#conn b').textContent")),
          (True, "Connected"))
    hid = "(() => [...document.querySelectorAll('.levels, .txpanel, .bandmode .row-2')].map((e) => getComputedStyle(e).display === 'none'))()"
    check("  Controls closed: Levels, the TX panel, Filter/AGC out of the way",
          (frame_js("ic7300", hid), frame_js("ic7300", "document.querySelector('#pane-controls').getAttribute('aria-expanded')")),
          ([True, True, True], "false"))
    fill = frame_js("ic7300", "(() => { const g = document.querySelector('.grid'), s = document.querySelector('.scope').getBoundingClientRect(), "
                              "m = document.querySelector('.bandmode').getBoundingClientRect(); "
                              "return [Math.round(s.height), g.scrollHeight - g.clientHeight, Math.round(innerHeight - m.bottom)]; })()")
    check("  the scope fills what the rest leaves (over 300 px; nothing to scroll; the chips at the bottom)",
          [fill[0] > 300, fill[1] <= 1, 0 <= fill[2] <= 12] if isinstance(fill, list) else fill, [True, True, True])
    print("     scope height, overflow, gap under the chips:", fill)
    shot("1600")
    click_at(*frame_point("ic7300", "#pane-controls"))
    check("  a click on Controls shows them, all in reach",
          (frame_wait("ic7300", "document.documentElement.classList.contains('pane-open')", 5), frame_js("ic7300", hid)),
          (True, [False, False, False]))
    shot("1600-controls")
    click_at(*frame_point("ic7300", "#pane-controls"))
    check("  and a second closes them", frame_wait("ic7300", f"!document.documentElement.classList.contains('pane-open') && {hid}.every(Boolean)", 5), True)
    # Armed (the page's own switch state, set here in the DOM only: nothing is
    # sent), the TX panel and Levels stay shown with Controls closed.
    frame_js("ic7300", "document.querySelector('#btn-arm').setAttribute('aria-checked', 'true')")
    check("  armed: Levels and the TX panel shown though Controls is closed", frame_js("ic7300", hid), [False, False, False])
    frame_js("ic7300", "document.querySelector('#btn-arm').setAttribute('aria-checked', 'false')")
    check("  disarmed: out of the way again", frame_js("ic7300", hid), [True, True, True])

    # 3. A Roster row click in the pane.
    click("#view-addon-roster .rs-row[data-call='VK2ABC'] td")
    check("3. a row click in the Roster pane sends do: \"reply\"",
          b.wait("window.__addonSent.find((m) => m.addon === 'roster' && m.do === 'reply')?.call", 5), "VK2ABC")

    # 4. The other radio takes focus: the frames swap.
    click("#rig-bar .rb-rig[data-id='g90'] .rb-f")
    swapped = b.wait(f"{SHOWN}({FRAME.format('g90')}) && !{SHOWN}({FRAME.format('ic7300')})", 10)
    check("4. focusing the G90 in the bar puts its frame in the pane", bool(swapped), True)
    check("  its box is the pane's; it is in pane mode, the IC-7300's frame not",
          (b.js(f"{BOX}({FRAME.format('g90')})") == b.js(PANES)["radio"],
           frame_wait("g90", "document.documentElement.classList.contains('pane')", 15),
           frame_wait("ic7300", "!document.documentElement.classList.contains('pane')", 5)), (True, True, True))
    click("#rig-bar .rb-rig[data-id='ic7300'] .rb-f")
    check("  focusing the IC-7300 again swaps back",
          bool(b.wait(f"{SHOWN}({FRAME.format('ic7300')}) && !{SHOWN}({FRAME.format('g90')})", 10)), True)

    # 5. The Map switched off, then on.
    b.js("addons.event({t: 'addon', addon: 'map', on: false})")
    two = b.wait(f"(() => {{ const p = {PANES}; return !p.map && !!p.roster && "
                 "Math.abs(p.roster[0] + p.roster[2] - (p.main[0] + p.main[2])) <= 13; })()", 5)
    p = b.js(PANES)
    check("5. Map off: two panes share the width", (bool(two), overlap(p["radio"], p["roster"]), p["radio"][2] > p["roster"][2]),
          (True, 0, True))
    shot("1600-no-map")
    b.js("addons.event({t: 'addon', addon: 'map', on: true, snapshot: %s})" % json.dumps(MAP))
    check("  on again: three", bool(operate_ready()), True)

    # 6. Resized live.
    resize(1200)
    two_col = b.wait(f"(() => {{ const p = {PANES}; return !!p.map && p.roster[0] === p.map[0] && p.radio[0] < p.roster[0] && "
                     "p.roster[1] + p.roster[3] <= p.map[1] && p.radio[1] === p.roster[1] && "
                     "Math.abs(p.radio[1] + p.radio[3] - (p.map[1] + p.map[3])) <= 1; })()", 5)
    p = b.js(PANES)
    check("6. at 1200: the radio left, the Roster over the Map", (bool(two_col), b.js("location.hash")),
          (True, "#/operate"))
    check("  the frame's box is still its pane's; each pane at least 320 px wide",
          (b.js(f"{BOX}({FRAME.format('ic7300')})") == p["radio"], [bx[2] >= 320 for bx in (p["radio"], p["roster"], p["map"])]),
          (True, [True] * 3))
    shot("1200")
    resize(900)
    check("  at 900: the route lands on #/home and the tab is gone",
          (b.wait("location.hash === '#/home' && location.hash", 5),
           b.wait("!document.querySelector('#st-nav a[href=\"#/operate\"]')", 5)), ("#/home", True))
    resize(1600)
    check("  back at 1600 the tab is back", bool(b.wait("!!document.querySelector('#st-nav a[href=\"#/operate\"]')", 5)), True)

    # 2 and 7. Left Operate (at 900): the same frame, still listening; the
    # add-ons' own views as they were.
    check("2. the frame was not reloaded: its marker survived entering and leaving",
          frame_js("ic7300", "window.__opMarker"), "kept")
    check("  its audio still runs and still arrives",
          (frame_js("ic7300", "rrFrame.listening() && audio.ctx.state"), frame_wait("ic7300", "audio.level > 0", 5)),
          ("running", True))
    sent = frame_js("ic7300", "window.__sent")
    check("  the frame sent its radio nothing but its ping meanwhile",
          sorted(set(sent) - {"ping"}) if isinstance(sent, list) else sent, [])
    b.js("location.hash = '#/roster'")
    b.wait(f"{SHOWN}(document.querySelector('#view-addon-roster'))", 5)
    check("7. the Roster view's box is .st-main's again",
          b.js(f"{BOX}(document.querySelector('#view-addon-roster'))"), b.js(f"{BOX}(document.querySelector('.st-main'))"))
    b.js("location.hash = '#/map'")
    full = b.wait(f"{SHOWN}(document.querySelector('#view-addon-map .rm-wrap')) && "
                  "Math.abs(document.querySelector('#view-addon-map .rm-wrap').getBoundingClientRect().height - Math.max(440, innerHeight - 210)) < 1", 5)
    check("  the Map is its full-width height again (100vh - 210 px)", bool(full), True)
    check("  its list open again, and map.list never written",
          (b.js("!document.querySelector('#view-addon-map .rm-list').classList.contains('shut')"),
           b.js("document.querySelector('#view-addon-map .addon-body').classList.contains('rm-pane')"),
           b.js("localStorage.getItem('ic7300.map.list')")), (True, False, mapList))
    check("  no layout left on .st-main",
          b.js("[document.querySelector('.st-main').className, document.querySelectorAll('[data-pane]').length]"), ["st-main", 0])

    # S. The frame's STOP TX, clicked where it shows in the pane.
    b.js("location.hash = '#/operate'")
    operate_ready()
    click_at(*frame_point("ic7300", "#tx-stop"))
    check("S. a real click on the frame's STOP TX in the pane latches the radio",
          frame_wait("ic7300", "document.body.classList.contains('tx-stopped')", 10), True)
    check("  its banner, with Resume, shows in the pane",
          frame_js("ic7300", vis % "#tx-resume"), True)
    check("  the bar says so too", bool(b.wait("document.querySelector('#rig-bar .rb-rig[data-id=\"ic7300\"] .rb-tag')?.textContent === 'STOPPED'", 10)), True)
    shot("1600-stopped")
finally:
    b.close()
    stop(front)
    stop(ic)
    stop(g90)
    if fails:
        for n in ("ic7300", "g90", "station"):
            lp = os.path.join(TMP, f"{n}.log")
            if os.path.exists(lp):
                print(f"--- {n}.log\n" + open(lp).read()[-1200:])
    shutil.rmtree(TMP, ignore_errors=True)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
