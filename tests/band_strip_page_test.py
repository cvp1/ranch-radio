"""Mission Control's band strip (static/bands.js, UX plan PR 3a, 2026-11-19),
driven in headless Chromium.

Two simulated radios (G90: 0.5-30 MHz, QMX: 1.8-54 MHz) behind station.py,
with the Propagation add-on asking nobody for anything ("swpc": false, no
skywatch, no PSKReporter) and the Roster add-on (no logbook sync). The
add-ons' states are then given to the page as the station's socket would
deliver them, raw as addons/propagation.py and the Roster's own view make them
(peak 0.72, region NA_EAST: never a pre-formatted string). No network. The
strip's clock is fixed (bandStrip.now) so hours are known. Checks:

  place     under the clock row on Mission Control, once
  bands     one cell per band <= 54 MHz the focus radio covers; a click on
            the other radio in the rig bar changes the set (6 m)
  unknown   each fact's unknown states: not set up (the server's own
            snapshot), waiting (no first answer), unavailable (each source's
            error), none (no decodes: "may not have been monitored"; no
            forecast measurement: not set up)
  facts     heard here "1.1k decodes" with "last 60 min"; hears you a tick
            with the receiver count; forecast "NA east, now to 05Z, peak
            72%" with the issue's age, a second path counted, never merged
  later     a window starting after the current hour is not the chance now
  stale     PSKReporter failing: the last tick stays, marked stale; a band
            with no last count is unknown
  hour      the clock crosses the hour with no event: the strip redraws; the
            later window is now open, a window ending on the hour has closed
  needs     the needs index unavailable: unknown, not zero; then only fresh
            rows on the roster's band and mode, in the strong levels, count
            (a row from the previous band, another mode, or stale does not);
            the Roster card uses the same count
  display   a click on every cell sends nothing and leaves the radio's
            frequency as it was (read back over rigctl)
  near      needed near you (R2): not set up, waiting for the needs index
            (unknown, not zero), PSKReporter failing, then a count on a
            band JTDX is not on, its tooltip naming the stations and needs,
            a band heard with nothing needed, a band nobody reported, and
            the source named once under the cells; the Roster line stays
            on the Roster's own band
  off       Propagation switched off: the strip says its facts are unknown

    python3 tests/band_strip_page_test.py [screenshot-dir]
"""
import calendar
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
TMP = tempfile.mkdtemp(prefix="band-strip-page-")

fp = testkit.free_port
WEB, SHARED, CDP = fp(), fp(), fp()
G90 = {"web": fp(), "rigctl": fp(), "lane": fp()}
QMX = {"web": fp(), "rigctl": fp(), "lane": fp()}
fails = 0

HF_G90 = ["160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m"]
HF_QMX = HF_G90 + ["6m"]

# The strip's clock: half past 03Z on a fixed day, so the hours in the text are known.
H = calendar.timegm((2026, 11, 19, 3, 0, 0))
T = H + 1800


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:66} got {got!r}" + ("" if ok else f" want {want!r}"), flush=True)


def answers(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


def radio(rig, p):
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--rig", rig, "--no-audio", "--host", "127.0.0.1",
         "--listen", str(p["web"]), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(p["rigctl"]),
         "--winlink-rigctl-port", str(p["lane"])],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert testkit.wait_for(lambda: answers(p["web"]), 60, 0.1), f"{rig} did not start"
    return proc


def stop(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def rig_freq(p):
    """The simulator's frequency in Hz, over its rigctl port (read only)."""
    with socket.create_connection(("127.0.0.1", p["rigctl"]), timeout=3) as c:
        c.sendall(b"f\n")
        return int(c.makefile("r").readline().strip())


# ---- the add-ons' states, raw, as addons/propagation.py snapshot() makes them
def prop(**over):
    s = {"swpc": False, "skywatch": True, "window_min": 60, "kp": None, "kp_stale": False, "xray": None,
         "f107": None, "space_error": None, "bands": {}, "heard_error": None, "heard_checked": None,
         "pskreporter": "N0CALL", "psk": {}, "psk_error": None, "psk_checked": None, "forecast_h": 12,
         "forecast": [], "forecast_issued": None, "forecast_error": None}
    s.update(over)
    return s


WINDOWS = [
    # one path open now on 20m to 05Z (end exclusive), its best hour 72%
    {"band": "20m", "region": "NA_EAST", "start": H - 3600, "end": H + 2 * 3600, "peak": 0.72},
    # a second 20m path, open now, closing on the hour
    {"band": "20m", "region": "EU", "start": H, "end": H + 3600, "peak": 0.55},
    # 15m opens next hour, with a higher peak than anything open now
    {"band": "15m", "region": "SA", "start": H + 3600, "end": H + 3 * 3600, "peak": 0.9},
]
GOOD = prop(bands={"20m": {"n": 1100, "best_snr": 9, "max_km": 9800, "sources": {"jtdx": 1100},
                           "regions": [["EU", 700], ["NA_EAST", 400]]}},
            heard_checked=T - 30,
            psk={"20m": {"spots": 6, "receivers": 4, "best_snr": -6, "max_km": 8800, "countries": [["Germany", 2]]}},
            psk_checked=T - 60, forecast=WINDOWS, forecast_issued=T - 15 * 3600)


def row(call, need, band="20m", mode="FT8", age=10):
    """A roster row as the Roster's view() makes it (named without its module:
    a module name here is an affected.py edge this test does not have)."""
    return {"key": f"1|0|500|CQ {call} AA00", "answer": "cq", "call": call, "grid": "AA00", "cq": True, "cq_word": "",
            "to_me": False, "to": "", "snr": -5, "dt": 0.1, "df": 500, "message": f"CQ {call} AA00", "age": age,
            "need": need, "reasons": [need] if need else [], "entity": "", "cont": "", "lotw": False, "km": None,
            "bearing": None, "band": band, "mode": mode}


SUMMARY = {"qsos": 10, "dxcc_worked": 5, "dxcc_confirmed": 3, "grids_worked": 8, "grids_confirmed": 4, "lotw_users": 0}
ROWS = [row("N9ABC", "dxcc"), row("N9ABD", "grid_band"), row("N9ABE", "dxcc_unconfirmed"),
        row("N9ABF", "call_band_mode"),
        row("N9ABG", "dxcc", band="40m"),                 # kept from the previous band
        row("N9ABH", "dxcc", mode="FT4"),                 # another mode
        row("N9ABJ", "dxcc_band", age=200)]               # no longer fresh

with open(os.path.join(TMP, "station.json"), "w") as f:
    json.dump({"name": "Test Station",
               "radios": [{"id": "g90", "name": "G90", **G90}, {"id": "qmx", "name": "QMX", **QMX}],
               "addons": {"propagation": {"swpc": False}, "roster": {}},
               "addons_state": os.path.join(TMP, "addons.json")}, f)

CELLS = "[...document.querySelectorAll('#mc-bands .bs-cell')]"
CELL = "document.querySelector('#mc-bands .bs-cell[data-band=\"{}\"]')"
NOTES = "[...document.querySelectorAll('#mc-bands .bs-notes span')].map((e) => e.textContent)"


def deliver(m):
    """A message as the station's socket delivers it (station.js's own handler)."""
    b.js(f"ws.onmessage({{ data: {json.dumps(json.dumps(m))} }})")


def facts(band):
    """{fact: [state, text, title]} for one band's cell."""
    return b.js(f"""(() => {{ const c = {CELL.format(band)}; if (!c) return null; const o = {{}};
      for (const k of ['heard', 'you', 'fc', 'near']) {{ const e = c.querySelector('.bs-' + k);
        o[k] = [e.dataset.state, e.textContent.trim(), e.title]; }}
      const n = c.querySelector('.bs-needs'); if (n) o.needs = [n.dataset.state, n.textContent.trim(), n.title];
      return o; }})()""")


def states(fact):
    return b.js(f"[...new Set({CELLS}.map((c) => c.querySelector('.bs-{fact}').dataset.state))]")


def shot(name):
    if SHOTS:
        b.screenshot(os.path.join(SHOTS, f"band-strip-{name}.png"))


g90 = radio("g90", G90)
qmx = radio("qmx", QMX)
front = subprocess.Popen([sys.executable, "station.py", "--config", os.path.join(TMP, "station.json"),
                          "--listen", str(WEB), "--rigctl-port", str(SHARED)], cwd=ROOT,
                         env=dict(os.environ, HOME=TMP), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
b = Browser(CHROME, CDP, size="1500,1000")
try:
    assert testkit.wait_for(lambda: answers(WEB), 60, 0.1), "station did not start"
    b.goto(f"http://127.0.0.1:{WEB}/#/home")
    b.wait("document.querySelector('#conn b')?.textContent === 'Connected'")
    b.wait("!!document.querySelector('#rig-bar .rb-rig.focus')")

    print("# place")
    check("the strip is the clock row's next sibling, on Mission Control",
          b.wait("!document.querySelector('#mc-bands').hidden && "
                 "[document.querySelector('#view-home .mc-band').nextElementSibling.id, "
                 "document.querySelector('#mc-bands').closest('.st-view').id]"), ["mc-bands", "view-home"])
    check("  and nowhere else", b.js("document.querySelectorAll('.bs-cells').length"), 1)

    print("\n# bands: the focus radio's")
    focus = b.js("document.querySelector('#rig-bar .rb-rig.focus').dataset.id")
    other = "qmx" if focus == "g90" else "g90"
    want = {"g90": HF_G90, "qmx": HF_QMX}
    check(f"the focus radio ({focus}): its bands at or below 54 MHz",
          b.wait(f"{CELLS}.length === {len(want[focus])} && {CELLS}.map((c) => c.dataset.band)"), want[focus])
    b.js(f"document.querySelector('#rig-bar .rb-rig[data-id=\"{other}\"]').click()")
    check(f"focus on the {other}: its bands",
          b.wait(f"{CELLS}.length === {len(want[other])} && {CELLS}.map((c) => c.dataset.band)"), want[other])
    b.js("document.querySelector('#rig-bar .rb-rig[data-id=\"qmx\"]')?.click()")    # end on the QMX: 6 m too
    b.wait(f"{CELLS}.length === 11")

    print("\n# unknown: not set up (the server's own snapshot: no skywatch, no callsign)")
    check("each fact not set up", [states("heard"), states("you"), states("fc"), states("near")],
          [["off"], ["off"], ["off"], ["off"]])
    check("  said once, under the cells",
          [any("Heard here: not set up" in n for n in b.js(NOTES)), any("Hears you: not set up" in n for n in b.js(NOTES)),
           any("Forecast: not set up" in n for n in b.js(NOTES))], [True, True, True])
    check("  no needs line (no logbook sync, no roster)", b.js("document.querySelectorAll('.bs-needs').length"), 0)

    b.js(f"window.__T = {T}; bandStrip.now = () => window.__T")
    print("\n# unknown: waiting for each source's first answer")
    deliver({"t": "addon", "addon": "propagation", "state": prop()})
    b.wait(f"{CELL.format('20m')}.querySelector('.bs-heard').dataset.state === 'wait'")
    check("each fact waiting", [states("heard"), states("you"), states("fc")], [["wait"], ["wait"], ["wait"]])

    print("\n# unknown: each source failing")
    deliver({"t": "addon", "addon": "propagation",
             "state": prop(heard_error="skywatch not reachable", heard_checked=T, psk_error="PSKReporter not reachable",
                           psk_checked=T, forecast_error="skywatch not reachable")})
    b.wait(f"{CELL.format('20m')}.querySelector('.bs-heard').dataset.state === 'err'")
    check("each fact unavailable", [states("heard"), states("you"), states("fc")], [["err"], ["err"], ["err"]])
    check("  heard here: unknown, not zero",
          any(n == "skywatch not reachable: what this station heard is unknown, not zero" for n in b.js(NOTES)), True)
    check("  who hears you: unknown", any(n.startswith("PSKReporter not reachable: who hears you is unknown") for n in b.js(NOTES)), True)
    check("  the forecast: unknown", any(n == "skywatch not reachable: the forecast is unknown" for n in b.js(NOTES)), True)
    check("  a cell shows no count, no tick, no forecast",
          [facts("20m")["heard"][1], facts("20m")["you"][1], "%" in facts("20m")["fc"][1]], ["heard: ?", "", False])

    print("\n# none: skywatch answers, nothing heard, no forecast measurement")
    deliver({"t": "addon", "addon": "propagation", "state": prop(heard_checked=T, psk_checked=T)})
    b.wait(f"{CELL.format('20m')}.querySelector('.bs-heard').dataset.state === 'none'")
    f40 = facts("40m")
    check("no decodes: may not have been monitored",
          [f40["heard"][0], f40["heard"][1], f40["heard"][2]],
          ["none", "no decodes", "40m: no decodes in the last 60 min: may not have been monitored"])
    check("  nobody reported you: no tick", [f40["you"][0], f40["you"][1]], ["none", ""])
    check("  no forecast measurement: not set up, not 'nothing open'",
          [states("fc"), any("no forecast measurement" in n for n in b.js(NOTES))], [["off"], True])

    print("\n# facts")
    deliver({"t": "addon", "addon": "propagation", "state": GOOD})
    b.wait(f"{CELL.format('20m')}.querySelector('.bs-heard').dataset.state === 'ok'")
    f20 = facts("20m")
    check("heard here: the count and its window",
          [f20["heard"][1], f20["heard"][2]], ["1.1k decodes", "20m: 1.1k decodes, last 60 min"])
    check("  the window named once", any(n == "Heard here: decodes, last 60 min" for n in b.js(NOTES)), True)
    check("hears you: a tick with the receiver count", [f20["you"][0], f20["you"][1]], ["ok", "✓ 4"])
    check("  its title names the source and window",
          f20["you"][2], "4 stations reported N0CALL on 20m in the last 60 min (PSKReporter)")
    check("forecast: region, until, peak as a rounded percent (0.72 -> 72%)",
          f20["fc"][1], "NA east, now to 05Z, peak 72% +1 path")
    check("  the second path is its own line, never merged",
          f20["fc"][2].split("\n")[1:3], ["NA east, now to 05Z, peak 72%", "Europe, now to 04Z, peak 55%"])
    check("  peak is said to be the best hour, not the chance now",
          "not the chance now" in f20["fc"][2], True)
    check("  the issue's age", any("issued 15 h ago" in n for n in b.js(NOTES)), True)
    f15 = facts("15m")
    check("later: a window from the next hour is not the chance now",
          [f15["fc"][0], "%" in f15["fc"][1], "90" in f15["fc"][1]], ["none", False, False])
    check("  it says when it starts", f15["fc"][1], "next: S America 04Z")
    check("a band no window covers: no forecast text", facts("40m")["fc"][:2], ["none", ""])
    shot("1500")

    print("\n# stale: PSKReporter failing after a good answer")
    deliver({"t": "addon", "addon": "propagation", "state": dict(GOOD, psk={}, psk_error="PSKReporter not reachable",
                                                                    psk_checked=T)})
    b.wait(f"{CELL.format('20m')}.querySelector('.bs-you').dataset.state === 'stale'")
    f20 = facts("20m")
    check("the last tick stays, marked stale", [f20["you"][0], f20["you"][1]], ["stale", "✓ 4"])
    check("  its title says why and how old", f20["you"][2].startswith("PSKReporter not reachable: stale."), True)
    check("  a band with no last count: unknown", facts("15m")["you"][:2], ["err", ""])
    check("  heard here and the forecast unaffected", [f20["heard"][0], f20["fc"][0]], ["ok", "ok"])

    print("\n# near: needed near you, on every band (R2)")
    NEAR = dict(GOOD, near=True, near_grid="FN31", near_window_min=15, near_checked=T - 30, near_bands=None, near_error=None)
    deliver({"t": "addon", "addon": "propagation", "state": NEAR})
    b.wait(f"{CELL.format('15m')}.querySelector('.bs-near').dataset.state === 'wait'")
    check("no needs index yet: unknown, not zero", facts("15m")["near"][:2], ["wait", "need: ?"])
    check("  said once", any(n.startswith("The needs index is not ready") for n in b.js(NOTES)), True)
    deliver({"t": "addon", "addon": "propagation", "state": dict(NEAR, near_error="PSKReporter not reachable")})
    b.wait(f"{CELL.format('15m')}.querySelector('.bs-near').dataset.state === 'err'")
    check("PSKReporter failing: unknown on every band", [states("near"), facts("15m")["near"][1]], [["err"], "need: ?"])
    check("  said once", any(n == "PSKReporter not reachable: who you need is unknown" for n in b.js(NOTES)), True)
    top = [{"call": "VK2YYY", "need": "dxcc", "grid": "QF56", "mode": "FT8", "snr": -18},
           {"call": "JA1ZZZ", "need": "dxcc_band", "grid": "PM95", "mode": "FT8", "snr": -5}]
    deliver({"t": "addon", "addon": "propagation", "state": dict(NEAR, near_bands={
        "15m": {"heard": 3, "strong": 7, "levels": {"dxcc": 1, "dxcc_band": 6}, "top": top},
        "20m": {"heard": 5, "strong": 0, "levels": {}, "top": []}})})
    b.wait(f"{CELL.format('15m')}.querySelector('.bs-near').dataset.state === 'ok'")
    f15, f20, f40 = facts("15m"), facts("20m"), facts("40m")
    check("a band JTDX is not on: the count of stations you need", f15["near"][:2], ["ok", "7 need"])
    check("  its tooltip names them, their need, and the rest",
          f15["near"][2].split("\n"),
          ["7 stations you need, of 3 decoded near FN31 on 15m in the last 15 min (PSKReporter):",
           "VK2YYY (QF56, FT8 -18 dB): new DXCC", "JA1ZZZ (PM95, FT8 -5 dB): new DXCC on this band", "+5 more"])
    check("a band heard with nothing you need: no count, the title says so",
          [f20["near"][0], f20["near"][1], f20["near"][2].endswith("none you need for DXCC or a grid")], ["none", "", True])
    check("a band nobody reported: none, not a zero", [f40["near"][0], f40["near"][1]], ["none", ""])
    check("  the source named once", any(n == "Need: stations decoded near FN31, last 15 min (PSKReporter), new DXCC or grid"
                                          for n in b.js(NOTES)), True)
    check("  the other facts unaffected", [f15["fc"][1], f20["you"][0]], ["next: S America 04Z", "ok"])
    shot("near")
    deliver({"t": "addon", "addon": "propagation", "state": GOOD})
    b.wait(f"{CELL.format('15m')}.querySelector('.bs-near').dataset.state === 'off'")

    print("\n# hour: the clock crosses 04Z with no event")
    # Off Mission Control nothing else redraws it (its S-meter and 30 s
    # redraws run only on the view), so only the strip's own hour check can.
    b.js("location.hash = '#/propagation'")
    b.wait("location.hash === '#/propagation' && document.querySelector('#view-home').hidden")
    # A redraw queued just before leaving (homeChanged, up to 330 ms) still
    # runs: let it, so it cannot be what crosses the hour. A quiet window.
    time.sleep(1)
    b.js(f"window.__T = {H + 3600}")
    check("the later window is now open (redrawn on the hour, no event)",
          b.wait(f"{CELL.format('15m')}.querySelector('.bs-fc').dataset.state === 'ok' && "
                 f"{CELL.format('15m')}.querySelector('.bs-fc').textContent.trim()", timeout=20),
          "S America, now to 06Z, peak 90%")
    check("  end is exclusive: the 20m path ending at 04Z has closed",
          facts("20m")["fc"][1], "NA east, now to 05Z, peak 72%")
    b.js("location.hash = '#/home'")
    b.wait("!document.querySelector('#view-home').hidden")

    print("\n# needs: the roster's strong needs on its band")
    deliver({"t": "addon", "addon": "roster", "roster": {"band": "20m", "mode": "FT8", "dial": 14_074_000,
                                                        "rows": ROWS, "needs": None}})
    check("needs index unavailable: unknown, not zero",
          b.wait(f"{CELL.format('20m')}.querySelector('.bs-needs')?.textContent"), "strong needs: unknown")
    check("  only on the decoding band", b.js("document.querySelectorAll('.bs-needs').length"), 1)
    deliver({"t": "addon", "addon": "roster", "roster": {"band": "20m", "mode": "FT8", "dial": 14_074_000,
                                                        "rows": ROWS, "needs": SUMMARY}})
    check("fresh rows on 20m FT8 in the strong levels; unconfirmed apart",
          b.wait(f"/strong needs ·/.test({CELL.format('20m')}.querySelector('.bs-needs')?.textContent) && "
                 f"{CELL.format('20m')}.querySelector('.bs-needs').textContent"),
          "2 recent stations with strong needs · 1 unconfirmed")
    check("  new calls counted apart, in the title", "1 new call" in facts("20m")["needs"][2], True)
    check("  the Roster card uses the same count",
          b.wait("[...document.querySelectorAll('.mc-card')].find((c) => c.querySelector('b').textContent === 'Roster')"
                 "?.querySelector('.mc-tag')?.textContent"), "2 strong needs")
    shot("1500-needs")

    print("\n# display only")
    before = rig_freq(QMX), rig_freq(G90)
    b.js("window.__sent = []; const s = WebSocket.prototype.send; "
         "WebSocket.prototype.send = function (d) { window.__sent.push(String(d)); return s.call(this, d); }")
    b.js(f"{CELLS}.forEach((c) => {{ c.click(); c.querySelectorAll('*').forEach((e) => e.click()); }})")
    time.sleep(0.5)                    # a quiet window: checking that nothing was sent
    check("a click on every cell sends nothing", b.js("window.__sent"), [])
    check("  the radios' frequencies as they were (rigctl)", (rig_freq(QMX), rig_freq(G90)), before)
    check("  Mission Control stays open", b.js("location.hash"), "#/home")

    print("\n# 390 px")
    b.call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=2, mobile=True)
    b.wait("innerWidth === 390")
    check("one row that scrolls sideways, inside the screen",
          b.js("(() => { const s = document.querySelector('#mc-bands .bs-cells'), r = s.getBoundingClientRect(),"
               f" tops = new Set({CELLS}.map((c) => Math.round(c.getBoundingClientRect().top)));"
               " return [tops.size, s.scrollWidth > s.clientWidth, r.left >= 0 && r.right <= 390]; })()"),
          [1, True, True])
    shot("390")
    b.call("Emulation.clearDeviceMetricsOverride")

    print("\n# off")
    deliver({"t": "addon", "addon": "propagation", "on": False})
    check("Propagation switched off: its facts unknown",
          b.wait(f"({NOTES}).find((n) => n.startsWith('Propagation is off')) || null"),
          "Propagation is off: heard here, hears you and the forecast are unknown")
    check("  no fact shown in any cell", [states("heard"), states("you"), states("fc")], [["off"], ["off"], ["off"]])
finally:
    b.close()
    stop(front)
    stop(g90)
    stop(qmx)
    shutil.rmtree(TMP, ignore_errors=True)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
