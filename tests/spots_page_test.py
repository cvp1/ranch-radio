"""Spot labels on the radio scope (UX plan PR 4a, 2026-11-19): display only.

The simulated IC-7300 (server.py --sim) behind station.py, with the Roster
and Map add-ons on (no logbook sync, POTA fetching off). The add-ons' data is
then given to the station page as its socket would deliver it, raw: Roster
rows as the Roster's view makes them (each with the dial it arrived on) and
the Map's POTA spots as the Map keeps them (khz is the API's string). No
network. A real browser on the station page's radio tab reads the scope in
the radio page's frame. Nothing transmits. Checks:

  feed     a decode sits at row dial + df, only on a radio on the row's band
           (a 40m row is absent on 20m; a row with no dial is not placed);
           a POTA "14074" sits at 14,074,000 Hz; a khz that is not wholly a
           decimal ("14,070", "14074abc", "1e4", "") is dropped, and one
           outside the radio's range; one inside the range but off the scope
           is culled
  labels   the expected number of labels is drawn (and that check fails on
           an empty list: a control); each single label's x is its
           frequency's position in the span; neighbours within 40 px collapse
           to one ("FT8 ×3 · 2 new") whose tick is its most needed member
  hover    the label's tip shows; unlocked, a click on a label tunes the
           radio through the station's tuneFocus (UX plan PR 4c, 2026-11-19;
           in 4a it sent nothing): a POTA CW spot sets its frequency and CW, a
           cluster of decodes the dial its best was decoded on and no mode,
           and the radio page itself sends no `set`; control: a click on the bare scope
           does send a `set` (so the recorder can see one)
  band     the radio changing band changes its spots
  off      the Roster switched off takes its labels away
  cap      more than 200 spots keep the 200 most needed
  themes   all six themes give the need colours (no fallback)

    python3 tests/spots_page_test.py [screenshot-dir]
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
TMP = tempfile.mkdtemp(prefix="spots-page-")

fp = testkit.free_port
WEB, SHARED, CDP = fp(), fp(), fp()
IC = {"web": fp(), "rigctl": fp(), "lane": fp()}
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:70} got {got!r}" + ("" if ok else f" want {want!r}"), flush=True)


def answers(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


def stop(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def rigctl(cmd):
    """One rigctl command to the simulator (the test's, never the page's)."""
    with socket.create_connection(("127.0.0.1", IC["rigctl"]), timeout=3) as c:
        c.sendall(cmd.encode() + b"\n")
        return c.makefile("r").readline().strip()


def rig_freq():
    return int(rigctl("f"))


def row(call, need, dial, df, band="20m", mode="FT8"):
    """A roster row as the Roster's view makes it, with its arrival dial
    (named without its module: a module name here is an affected.py edge
    this test does not have)."""
    return {"key": f"1|0|{df}|CQ {call} AA00", "answer": "cq", "call": call, "grid": "AA00", "cq": True, "cq_word": "",
            "to_me": False, "to": "", "snr": -5, "dt": 0.1, "df": df, "message": f"CQ {call} AA00", "age": 10,
            "need": need, "reasons": [need] if need else [], "entity": "", "cont": "", "lotw": False, "km": None,
            "bearing": None, "band": band, "mode": mode, "dial": dial}


# One decode heard with JTDX at 14.090, three at 14.080, one on 40m, one before any dial.
ROWS = [row("N9ABC", None, 14_090_000, 1000),
        row("JA1ABC", "dxcc", 14_080_000, 1200), row("VK2ABC", "grid", 14_080_000, 1400),
        row("K1ABC", None, 14_080_000, 1600),
        row("K6ABC", "dxcc", 7_074_000, 1500, band="40m"),
        row("K0ABC", "dxcc", None, 1500)]
ROSTER = {"t": "addon", "addon": "roster", "roster": {"band": "20m", "mode": "FT8", "dial": 14_080_000,
                                                       "rows": ROWS, "needs": None}}

# POTA spots as the Map add-on keeps them (khz is the API's string).
POTA = [("K2ABC", "14074"), ("K3ABC", "14062.5"), ("K5ABC", "7062"),                 # kept (7062: culled off 20m's scope)
        ("K4ABC", "14,070"), ("K8ABC", "14074abc"), ("K9ABC", "1e4"), ("KA1ABC", ""),  # not a decimal: dropped
        ("K7ABC", "144174")]                                                          # past the IC-7300's 74.8 MHz
MAP = {"t": "addon", "addon": "map", "pota_age": 30, "pota_error": None,
       "pota": [{"call": c, "ref": "US-0001", "name": "Test Park", "mode": "CW", "khz": k, "band": "", "grid": "FN31",
                 "lat": 41.0, "lon": -73.0} for c, k in POTA]}

with open(os.path.join(TMP, "station.json"), "w") as f:
    json.dump({"name": "Test Station", "radios": [{"id": "ic7300", "name": "IC-7300", **IC}],
               "addons": {"roster": {}, "map": {"pota": False}},          # nothing fetched: the spots are delivered
               "addons_state": os.path.join(TMP, "addons.json")}, f)

FRAME = "document.querySelector('#view-radio iframe').contentWindow"


def deliver(m):
    """A message as the station's socket delivers it (station.js's own handler)."""
    b.js(f"ws.onmessage({{ data: {json.dumps(json.dumps(m))} }})")


def F(expr):
    """Evaluate expr in the radio page's frame (same origin, /r/<id>/)."""
    return b.js(f"(() => {{ const w = {FRAME}; return w ? w.eval({json.dumps(expr)}) : null; }})()")


def FW(expr, timeout=10):
    """Wait for expr to hold in the radio page's frame."""
    return b.wait(f"(() => {{ const w = document.querySelector('#view-radio iframe')?.contentWindow; "
                  f"return !!w && !!w.rrFrame && w.eval({json.dumps(expr)}); }})()", timeout)


def shot(name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        b.screenshot(os.path.join(SHOTS, f"spots-{name}.png"))


GEOM = "JSON.stringify({start: scope.start, end: scope.end, w: scope.spec.clientWidth, boxes: scope.spotBoxes, " \
       "spots: scope.spots.map((s) => [s.label, s.f, s.kind]), hidden: scope.spotHidden})"


def labels_ok(boxes, want_n):
    """The labels check: an empty list fails it, whatever else holds."""
    return bool(boxes) and len(boxes) == want_n


ic = subprocess.Popen(
    [sys.executable, "server.py", "--sim", "--rig", "ic7300", "--no-audio", "--host", "127.0.0.1",
     "--listen", str(IC["web"]), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(IC["rigctl"]),
     "--winlink-rigctl-port", str(IC["lane"])],
    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
front = None
b = None
try:
    assert testkit.wait_for(lambda: answers(IC["web"]), 60, 0.1), "radio did not start"
    front = subprocess.Popen([sys.executable, "station.py", "--config", os.path.join(TMP, "station.json"),
                              "--listen", str(WEB), "--rigctl-port", str(SHARED)], cwd=ROOT,
                             env=dict(os.environ, HOME=TMP), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert testkit.wait_for(lambda: answers(WEB), 60, 0.1), "station did not start"
    b = Browser(CHROME, CDP, size="1400,900")
    # The test reads a radio frame's globals with w.eval(); the pages' policy
    # (webguard.CSP, no unsafe-eval) refuses that, as it should. Bypass it
    # for this test only; station_page_test and xss_render_page_test run the
    # pages under it.
    b.call("Page.setBypassCSP", enabled=True)
    b.goto(f"http://127.0.0.1:{WEB}/#/ic7300")
    b.wait("document.querySelector('#conn b')?.textContent === 'Connected'")
    check("page: the radio's frame draws its scope", bool(FW("scope.n > 0", 30)), True)
    start_f = rig_freq()
    check("  on 20m (the simulator's start)", 14_000_000 <= start_f <= 14_350_000, True)
    deliver(ROSTER)
    deliver(MAP)
    check("  the delivered spots reach it as labels", bool(FW("scope.spotBoxes.length === 4")), True)

    g = json.loads(F(GEOM))
    spots = {s[0]: s[1] for s in g["spots"]}
    check("feed: the spots the station gave this radio (20m decodes, POTA in range)",
          sorted(spots), sorted(["N9ABC", "JA1ABC", "VK2ABC", "K1ABC", "K2ABC", "K3ABC", "K5ABC"]))
    check("  a decode sits at its row's dial + df (Hz)", (spots.get("N9ABC"), spots.get("JA1ABC")), (14_091_000, 14_081_200))
    check("  a POTA \"14074\" sits at 14,074,000 Hz; \"14062.5\" at 14,062,500", (spots.get("K2ABC"), spots.get("K3ABC")),
          (14_074_000, 14_062_500))
    check("  the 40m row is absent on a 20m radio", "K6ABC" in spots, False)
    check("  a row with no dial is not placed", "K0ABC" in spots, False)
    check("  malformed khz dropped (\"14,070\", \"14074abc\", \"1e4\", \"\")",
          [c for c in ("K4ABC", "K8ABC", "K9ABC", "KA1ABC") if c in spots], [])
    check("  past the radio's range dropped (144.174 MHz on an IC-7300)", "K7ABC" in spots, False)
    check("  the span holds the expected spots", all(g["start"] <= f <= g["end"] for f in (14_062_500, 14_091_000)), True)

    boxes = g["boxes"]
    by = {x["label"]: x for x in boxes}
    check("labels: the expected number is drawn (4: three singles and a cluster)", labels_ok(boxes, 4), True)
    check("  control: the same check fails on an empty list", labels_ok([], 0), False)
    check("  culled: the 7.062 POTA spot is outside the span, so not drawn",
          any(x["f"] == 7_062_000 for x in boxes), False)

    def x_of(f):
        return (f - g["start"]) / (g["end"] - g["start"]) * g["w"]
    singles = {"N9ABC": 14_091_000, "K2ABC": 14_074_000, "K3ABC": 14_062_500}
    check("  each single label's x is its frequency's position (within 0.5 px)",
          {c: c in by and abs(by[c]["x"] - x_of(f)) <= 0.5 for c, f in singles.items()}, {c: True for c in singles})
    cl = [x for x in boxes if x["n"] > 1]
    check("  three FT8 decodes within 40 px collapse to one label (two with a need)",
          [(x["n"], x["label"]) for x in cl], [(3, "FT8 ×3 · 2 new")])
    check("  its tick points at its most needed member (JA1ABC, new DXCC)",
          bool(cl) and cl[0]["f"] == 14_081_200 and abs(cl[0]["x"] - x_of(cl[0]["f"])) <= 0.5, True)
    shot("1-labels")

    # ---- hover and click (real mouse events); unlocked, so a bare click would tune
    F("setLocked(false)")
    check("  the radio page is unlocked (a bare click would tune)", F("locked"), False)
    rec = "(() => { if (!window.__sent) { window.__sent = []; const o = WebSocket.prototype.send; " \
          "WebSocket.prototype.send = function (d) { window.__sent.push(String(d)); return o.call(this, d); }; } " \
          "window.__sent.length = 0; return true; })()"
    b.js(rec)                  # the station page's own sockets
    F(rec)                     # the radio page's

    def page_xy(box):
        r = json.loads(b.js("JSON.stringify((() => { const f = document.querySelector('#view-radio iframe'); "
                            "const a = f.getBoundingClientRect(); const c = f.contentWindow.eval('scope.spec.getBoundingClientRect()'); "
                            "return [a.left + c.left, a.top + c.top]; })())"))
        return r[0] + box["left"] + box["w"] / 2, r[1] + box["top"] + box["h"] / 2

    def mouse(kind, x, y):
        b.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left" if kind != "mouseMoved" else "none",
               clickCount=1 if kind != "mouseMoved" else 0)

    def click(x, y):
        mouse("mouseMoved", x, y)
        mouse("mousePressed", x, y)
        mouse("mouseReleased", x, y)

    def sets():
        return [m for m in json.loads(b.js("JSON.stringify(window.__sent || [])")) + json.loads(F("JSON.stringify(window.__sent || [])"))
                if json.loads(m).get("t") == "set"]

    k2 = by["K2ABC"]
    mouse("mouseMoved", *page_xy(k2))
    check("hover: the label's tip shows", bool(b.wait(f"{FRAME}.document.querySelector('#scope-body').title.includes('US-0001')", 5)), True)
    # PR 4c (2026-11-19) changes this check on purpose: a click on a label now
    # tunes the focus radio through the station's tuneFocus (in 4a it sent
    # nothing). The radio page's own sockets still send no `set` for it.
    def page_sets(where):
        return [json.loads(m).get("k") for m in json.loads(where("JSON.stringify(window.__sent || [])"))
                if json.loads(m).get("t") == "set"]
    click(*page_xy(k2))
    check("click on a single label (POTA CW 14074): the radio reads 14,074,000 Hz, CW",
          testkit.wait_for(lambda: (rig_freq(), rigctl("m")) == (14_074_000, "CW"), 10, 0.2), True)
    check("  the radio page sent no `set` itself", page_sets(F), [])
    b.js(rec)
    click(*page_xy(cl[0] if cl else k2))
    check("click on the cluster: its best decode's dial, 14,080,000 Hz (not dial + df)",
          testkit.wait_for(lambda: rig_freq() == 14_080_000, 10, 0.2), True)
    testkit.wait_for(lambda: False, 0.8)          # a quiet window: no mode may follow
    check("  the station sent set freq only (no mode); the radio page nothing",
          (page_sets(b.js), page_sets(F)), (["freq"], []))
    shot("2-hover")
    # Positive control: the same recorder sees a bare-scope click's `set` (lower down, away from the labels).
    x0, y0 = page_xy({"left": x_of(14_055_000), "w": 0, "top": 150, "h": 0})
    click(x0, y0)
    check("  control: a click on the bare scope does send a `set`", testkit.wait_for(lambda: len(sets()) > 0, 5), True)
    F("setLocked(true)")
    # The radio page ignores a freq state within 700 ms of its own edit
    # (app.js localEdit): let that pass, or the retune below reads as stale.
    FW("performance.now() - (localEdit.freq ?? -1e9) > 800", 5)

    # ---- the radio changes band: its spots follow. Decodes follow the band
    # (row band == radio band); POTA spots follow the radio's range, and the
    # scope culls those off the span.
    check("band: the test retunes the simulator to 7.074 (rigctl)", rigctl("F 7074000"), "RPRT 0")
    check("  on 40m the 40m decode and the 40m POTA spot are drawn, nothing from 20m",
          bool(FW("scope.spotBoxes.map((s) => s.label).sort().join(',') === 'K5ABC,K6ABC'")), True)
    check("  the only decode the 40m radio gets is the 40m row",
          json.loads(F("JSON.stringify(scope.spots.filter((s) => s.kind !== 'POTA').map((s) => s.label))")), ["K6ABC"])
    k6 = json.loads(F("JSON.stringify(scope.spots.find((s) => s.label === 'K6ABC'))"))
    check("  the 40m decode sits at its own dial + df", k6 and k6["f"], 7_075_500)

    # ---- the Roster switched off: its labels go, POTA's stay
    deliver({"t": "addon", "addon": "roster", "on": False})
    check("off: the Roster switched off takes its labels away",
          bool(FW("scope.spotBoxes.map((s) => s.label).join(',') === 'K5ABC'")), True)

    # ---- a dense cluster (screenshot) and the cap
    F("""rrFrame.spots(Array.from({length: 23}, (_, i) => ({f: 7_074_000 + 300 + i * 50, label: 'N9A' + String.fromCharCode(65 + i),
         kind: 'FT8', need: i < 4 ? 'grid' : null, tip: 'fixture ' + i})).concat([{f: 7_065_000, label: 'K2ABC', kind: 'POTA', need: null, tip: 'park'}]))""")
    FW("scope.spotBoxes.some((b) => b.n > 1) && scope.spotBoxes.length === 2", 5)
    dense = json.loads(F("JSON.stringify(scope.spotBoxes.map((b) => b.label))"))
    check("dense: 23 decodes collapse to one label beside the single", sorted(dense), ["FT8 ×23 · 4 new", "K2ABC"])
    shot("3-cluster")
    F("""rrFrame.spots(Array.from({length: 300}, (_, i) => ({f: 7_000_000 + i * 1000, label: 'N' + i, kind: 'FT8',
         need: i % 3 === 0 ? 'dxcc' : null})).concat([{f: NaN, label: 'X'}, {f: 7e6, label: ''}]))""")
    cap = json.loads(F("JSON.stringify([scope.spots.length, scope.spots.filter((s) => s.need === 'dxcc').length])"))
    check("cap: 300 spots keep 200, every one of the 100 most needed among them", cap, [200, 100])
    F("rrFrame.spots([])")
    check("  an empty list clears the labels", bool(FW("scope.spotBoxes.length === 0", 5)), True)

    # ---- every theme sets the need colours (no fallback)
    VARS = ["dxcc", "band", "mode", "grid", "unconf", "call", "none"]
    th = json.loads(F("""JSON.stringify(['classic', 'midnight', 'amber', 'sandstone', 'aurora', 'contrast'].map((t) => {
        document.documentElement.dataset.theme = t;
        const cs = getComputedStyle(document.documentElement);
        return [t, [...%s.map((v) => '--need-' + v + '-rgb'), '--spot-pota-rgb']
          .every((v) => cs.getPropertyValue(v).split(',').map(Number).filter(Number.isFinite).length === 3),
          cs.getPropertyValue('--need-dxcc-rgb').trim()];
      }))""" % json.dumps(VARS)))
    check("themes: all six set all eight spot colours", [t[:2] for t in th], [[t[0], True] for t in th])
    check("  and they differ by theme", len({t[2] for t in th}), 6)
finally:
    if b:
        b.close()
    if front:
        stop(front)
    stop(ic)
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'all ok' if not fails else f'{fails} FAILED'}", flush=True)
sys.exit(1 if fails else 0)
