"""The Map tab (addons/map.py, static/addons/map.js, logsync's map_view and
the needs index's grid_map): server side with fakes, and the page's sun
math in a real browser. No test reaches the internet (POTA is never fetched:
the add-on's loop isn't started; tiles are never asked for).

  grids      worked vs confirmed, on one band or all
  map view   our grid, the band showing, grids, and only decodes with a grid;
             "all" shows every band's grids
  pota       spots -> band from kHz, unplaceable ones dropped, fields capped;
             the snapshot shows only the band in view
  add-on     without the sync it says why; "show" changes the band; a click
             answers through the sync
  you hear   a station stays on the map after the roster forgets it, with
             its best SNR and how often it was decoded; an hour old it goes;
             the oldest go first past the cap
  hears you  from the Propagation add-on's PSKReporter stations when it is on
             and has a callsign, else what to set up; pushed once per answer
  parts      new decodes send the map and who you hear; a radio or JTDX
             changing sends only what is on the air, at most ten times a
             second, and the page redraws only your marker and the panel
  paths      a locator's centre (4 and 6 characters); distance and bearing;
             a great circle is one line across the date line
  background NASA's Blue Marble by default with its credit, city lights on
             the night side; past a background's deepest zoom its tiles are
             drawn larger; a pick changes tiles, credit and the night's look;
             the wheel: a sideways swipe pans and a momentum tail does
             nothing (they used to zoom out), a notch or a pinch is a level
  the page   two-way needs both directions on one band; the window and the
             layer switches take stations off; a first look frames them all
  sun        the subsolar point at the June and December solstices; night and
             day at reference places and times (NOAA); the night polygon covers
             the winter pole

    python3 tests/map_page_test.py
"""
import os as _os
_os.environ["RANCH_RADIO_OFFLINE"] = "1"

import asyncio  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import testkit  # noqa: E402
from cdp_browser import Browser  # noqa: E402

import logsync_cty as C  # noqa: E402
import logsync_needs as N  # noqa: E402
from addons import map as M  # noqa: E402

MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME = (_os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
          or (MAC_CHROME if _os.path.exists(MAC_CHROME) else None))
if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"), flush=True)


# ================================================================ grids
n = N.Needs(C.Cty((ROOT / "tests/fixtures/cty-sample.csv").read_text()))
n.add("JA1ABC", "20m", "FT8", "", "<GRIDSQUARE:4>PM95", True)
n.add("JA2ABC", "40m", "FT8", "", "<GRIDSQUARE:4>PM96", False)
n.add("JA3ABC", "20m", "FT8", "", "<GRIDSQUARE:6>PM97ab", False)
n.add("JA4ABC", "40m", "FT8", "", "<GRIDSQUARE:4>PM95", False)
check("grid_map: all bands, confirmed wins", n.grid_map(), {"PM95": "c", "PM96": "w", "PM97": "w"})
check("  on 40m: PM95 worked there, not confirmed there", n.grid_map("40m"), {"PM95": "w", "PM96": "w"})
check("  on 20m", n.grid_map("20m"), {"PM95": "c", "PM97": "w"})

# ================================================================ map view (logsync), with the roster filled by hand
import logsync_roster as R  # noqa: E402
import logsync_udp as U  # noqa: E402
import fake_jtdx as J  # noqa: E402


class Sync:
    """What addons/map.py needs from logsync: map_view (the roster's) and reply."""

    def __init__(self):
        self.roster = R.Roster(needs=n)
        self.asked = []

    def map_view(self, band=None):
        return self.roster.map_view(band)

    def reply(self, key, done=None):
        self.asked.append(key)
        done and done(True, "JTDX is calling JA5XYZ.")
        return None


s = Sync()
s.roster.status(U.parse_status(J.status(dial=14_074_000, de="N0CALL", de_grid="FN31")))
for m in ("CQ JA5XYZ PM85", "N0CALL VK9NA -10", "CQ VK2ABC QF56"):
    s.roster.add(U.parse_decode(J.decode(msg=m)))
mv = s.map_view(None)
check("map view: our grid, the band in view", (mv["me"], mv["band"], mv["showing"]), ("FN31", "20m", "20m"))
check("  the band's grids", mv["grids"], {"PM95": "c", "PM97": "w"})
check("  only decodes with a grid", sorted(d["call"] for d in mv["decodes"]), ["JA5XYZ", "VK2ABC"])
check("  each with its need, key, distance and age", sorted(mv["decodes"][0]), sorted(R.MAP_KEYS))
check("  (distance and bearing from our grid)", sorted((d["call"], d["km"] > 10000, d["band"]) for d in mv["decodes"]),
      [("JA5XYZ", True, "20m"), ("VK2ABC", True, "20m")])
check("  'all' shows every band's grids", s.map_view("all")["grids"], {"PM95": "c", "PM96": "w", "PM97": "w"})

# ================================================================ POTA
spots = M.pota_spots([
    {"activator": "K1ABC", "frequency": "14074", "mode": "FT8", "reference": "US-0001", "name": "A Park", "grid4": "FN42", "latitude": 42.3, "longitude": -71.1},
    {"activator": "K2ABC", "frequency": "7074", "mode": "FT8", "reference": "US-0002", "name": "B", "grid4": "FN20", "latitude": 40.1, "longitude": -74.2},
    {"activator": "K3ABC", "frequency": "14074", "mode": "FT8", "reference": "US-0003"},                       # no position
    {"activator": "K4ABC" * 10, "frequency": "abc", "latitude": 1, "longitude": 2, "name": "x" * 500}])
check("pota: band from kHz, unplaceable dropped", [(p["call"][:5], p["band"]) for p in spots], [("K1ABC", "20m"), ("K2ABC", "40m"), ("K4ABC", "")])
check("  fields capped", (len(spots[2]["call"]), len(spots[2]["name"])), (16, 60))
check("  not a list: nothing", M.pota_spots({"error": "x"}), [])
check("band_khz edges", (M.band_khz("14350"), M.band_khz("14351"), M.band_khz(None)), ("20m", "", ""))


class Host:
    def __init__(self, ls):
        self.logsync, self.notes, self.sent = ls, [], []

    def notice(self, msg, level="info", ws=None):
        self.notes.append((level, msg))

    def _broadcast(self, payload):
        self.sent.append(payload)

    async def _send(self, ws, payload):
        self.sent.append(payload)


check("add-on: without the sync, the snapshot says why", "isn't running" in M.Map(Host(None), {}).snapshot()["error"], True)
a = M.Map(Host(s), {"enabled": True})
a.pota = spots
snap = a.snapshot()
check("  the snapshot: the map, and POTA on the band in view only", ([p["call"] for p in snap["pota"]], snap["map"]["showing"]), (["K1ABC"], "20m"))
loop = asyncio.new_event_loop()
loop.run_until_complete(a.command(object(), "show", {"band": "all"}))
check("  'show all': every band's spots and grids", (len(a.snapshot()["pota"]), len(a.snapshot()["map"]["grids"])), (3, 3))
loop.run_until_complete(a.command(object(), "show", {"band": "40m"}))
check("  'show 40m'", ([p["call"] for p in a.snapshot()["pota"]], a.snapshot()["map"]["grids"]), (["K2ABC"], {"PM95": "w", "PM96": "w"}))
loop.run_until_complete(a.command(None, "reply", {"key": mv["decodes"][0]["key"], "call": "JA5XYZ"}))
check("  a click answers through the sync; JTDX's outcome reaches the page", (s.asked, a.host.notes[-1]),
      ([mv["decodes"][0]["key"]], ("info", "Map: JTDX is calling JA5XYZ.")))
check("  pota off: none in the snapshot", M.Map(Host(s), {"pota": False}).snapshot()["pota"], None)

# ================================================================ who you hear, remembered; who hears you; the parts
import json as _json  # noqa: E402
import time as _time  # noqa: E402

s2 = Sync()
s2.roster.status(U.parse_status(J.status(dial=14_074_000, de="N0CALL", de_grid="FN31")))
h = Host(s2)
m2 = M.Map(h, {"pota": False})
clock = [1_000_000.0]
m2.clock = lambda: clock[0]
rclock = [100.0]
s2.roster.clock = lambda: rclock[0]
s2.roster.add(U.parse_decode(J.decode(msg="CQ JA5XYZ PM85", snr=-15, t_ms=1000)))
m2.push()
sent = _json.loads(h.sent[-1])
check("new decodes: the map and who you hear, nothing else", sorted(k for k in sent if k not in ("t", "addon")), ["heard", "map", "now"])
check("  heard: the station, its grid, band and SNR", [(x["call"], x["grid"], x["band"], x["snr"], x["best"], x["n"]) for x in sent["heard"]],
      [("JA5XYZ", "PM85", "20m", -15, -15, 1)])
rclock[0] += 30
clock[0] += 30
s2.roster.add(U.parse_decode(J.decode(msg="CQ JA5XYZ PM85", snr=-9, t_ms=31000)))
m2.push()
m2.push()                                        # the same decode pushed again is not a second one
rclock[0] += 30
clock[0] += 30
s2.roster.add(U.parse_decode(J.decode(msg="CQ JA5XYZ PM85", snr=-20, t_ms=61000)))
m2.push()
hd = m2.snapshot()["heard"][0]
check("  decoded again: the latest SNR, the best, the count", (hd["snr"], hd["best"], hd["n"]), (-20, -9, 3))
rclock[0] += 600                                 # the roster forgets after two minutes
clock[0] += 600
snap2 = m2.snapshot()
check("  ten minutes on: gone from the roster, still on the map, at the time it was heard",
      (snap2["map"]["decodes"], [(x["call"], round(clock[0] - x["t"])) for x in snap2["heard"]]), ([], [("JA5XYZ", 600)]))
clock[0] += M.HEARD_KEEP_S
check("  an hour after it was last heard it goes", m2.snapshot()["heard"], [])
M.HEARD_MAX, keep_max = 3, M.HEARD_MAX
for i, call in enumerate(("K1AAA", "K2AAA", "K3AAA", "K4AAA", "K5AAA")):
    rclock[0] += 1
    clock[0] += 1
    s2.roster.add(U.parse_decode(J.decode(msg=f"CQ {call} FN42", t_ms=90000 + i)))
    m2.push()
check("  past the cap the oldest go first", [x["call"] for x in m2.snapshot()["heard"]], ["K5AAA", "K4AAA", "K3AAA"])
M.HEARD_MAX = keep_max
check("who hears you: no Propagation add-on, the snapshot says what to set up",
      (snap2["hears_me"], "pskreporter" in snap2["hears_me_why"]), (None, True))


class Prop:
    def __init__(self):
        self.answer = {"call": "N0CALL", "window_min": 60, "every_s": 600, "locator": "FN31pr", "error": None, "checked": 111,
                       "receivers": [{"call": "JK1AKC", "grid": "QM05ft", "lat": 35.2, "lon": 140.4, "band": "20m", "mode": "FT8",
                                      "snr": -9, "t": 999_000, "km": 10900, "country": "Japan"}]}

    def heard_by(self):
        return self.answer


class Reg:
    def __init__(self, on=True):
        self.on, self.prop = on, Prop()

    def get(self, name):
        return self.prop if name == "propagation" else None

    def is_on(self, name):
        return self.on and name == "propagation"


h.addons = Reg()
h.rigs_view = lambda: [{"id": "ic7300", "name": "IC-7300", "present": True, "connected": True, "tx": True, "freq": 14_074_000,
                        "mode": "USB", "data": True}]
s2.rig_view = lambda: {"alive": True, "transmitting": True, "dx_call": "JA5XYZ"}
snap3 = m2.snapshot()
check("  with it on and a callsign: its stations, and no hint", (snap3["hears_me"]["receivers"][0]["call"], snap3["hears_me_why"]), ("JK1AKC", None))
check("  the snapshot is whole: the map, who you hear, who hears you, what is on the air, POTA",
      sorted(snap3), sorted(["now", "map", "heard", "rig", "radios", "hears_me", "hears_me_why", "pota", "pota_age", "pota_error"]))
h.sent.clear()
m2.push_hears()
m2.push_hears()
check("  a PSKReporter answer is pushed once, on its own", [sorted(k for k in _json.loads(x) if k not in ("t", "addon")) for x in h.sent],
      [["hears_me", "hears_me_why", "now"]])
h.addons.prop.answer = dict(h.addons.prop.answer, checked=222)
m2.push_hears()
check("  the next answer is pushed", len(h.sent), 2)
h.addons.on = False
check("  Propagation switched off: none, and the hint", m2.hears_me()[0], None)
h.sent.clear()
m2.push_rig()
part = _json.loads(h.sent[-1])
check("a radio or JTDX changes: only what is on the air is sent",
      (sorted(k for k in part if k not in ("t", "addon")), part["rig"]["transmitting"], part["radios"][0]["tx"]), (["now", "radios", "rig"], True, True))


async def burst():
    h.sent.clear()
    for _ in range(20):
        m2.push_rig()
    at_once = len(h.sent)
    await asyncio.sleep(M.RIG_PUSH_S + 0.2)
    return at_once, len(h.sent)


check("  a VFO being turned (twenty changes in a row): one push, a tenth of a second on", loop.run_until_complete(burst()), (0, 1))

# ================================================================ the sun, in a real browser
b = Browser(CHROME, testkit.free_port(), size="400,300")
try:
    b.goto("about:blank")
    b.js("window.addons = {register() {}}; window.esc = (s) => String(s); window.send = () => {};")
    b.js((ROOT / "static/addons/map.js").read_text())
    ss = b.js("JSON.stringify([rrMapMath.subsolar(new Date(Date.UTC(2026, 5, 21, 12))), rrMapMath.subsolar(new Date(Date.UTC(2026, 11, 21, 12)))])")
    import json
    jun, dec = json.loads(ss)
    check("sun: June solstice, 12:00 UTC: subsolar ~23.4N, ~0.4E (NOAA)", (round(jun["lat"]), abs(jun["lon"]) < 1.5), (23, True))
    check("  December solstice: ~23.4S, near 0", (round(dec["lat"]), abs(dec["lon"]) < 2.5), (-23, True))
    places = [("New York at 08:00 local", 40.7, -74.0, True), ("Tokyo at 21:00 local", 35.7, 139.7, False),
              ("Honolulu at 02:00 local", 21.3, -157.8, False), ("London at 13:00 local", 51.5, -0.1, True),
              ("Sydney at 22:00 local", -33.9, 151.2, False), ("Cape Town at 14:00 local", -33.9, 18.4, True)]
    got = json.loads(b.js("JSON.stringify(" + json.dumps(places) + ".map(([n, la, lo]) => !rrMapMath.isNight(new Date(Date.UTC(2026, 5, 21, 12)), la, lo)))"))
    for (name, *_, want), g in zip(places, got):
        check(f"  21 June 12:00 UTC: {name} is {'day' if want else 'night'}", g, want)
    poly = json.loads(b.js("JSON.stringify(rrMapMath.night(new Date(Date.UTC(2026, 5, 21, 12))))"))
    check("  the night polygon closes on the south pole in June", (poly[0][1], poly[-1][1]), (-90, -90))
    eq = json.loads(b.js("JSON.stringify(rrMapMath.night(new Date(Date.UTC(2026, 2, 20, 14, 46))).slice(1, -1).map(p => p[1]))"))
    check("  at the March equinox the line is nearly a meridian (no lat stuck at the equator)", max(abs(x) for x in eq) > 80, True)
    mm = json.loads(b.js("""JSON.stringify({c4: rrMapMath.gridCentre('FN31'), c6: rrMapMath.gridCentre('FN31pr'), bad: rrMapMath.gridCentre('ZZ99'),
      ny_tokyo: rrMapMath.path({lat: 40.7, lon: -74}, {lat: 35.7, lon: 139.7}),
      arc: rrMapMath.arc({lat: 40.7, lon: -74}, {lat: 35.7, lon: 139.7}, 16),
      east: rrMapMath.arc({lat: 40.7, lon: -74}, {lat: 51.5, lon: -0.1}, 8)})"""))
    check("paths: a square's centre, a subsquare's centre, not a locator", (mm["c4"], [round(mm["c6"]["lat"], 3), round(mm["c6"]["lon"], 3)], mm["bad"]),
          ({"lat": 41.5, "lon": -73}, [41.729, -72.708], None))
    check("  New York to Tokyo: ~10,850 km, setting out north-north-west (~333°)",
          (10700 < mm["ny_tokyo"]["km"] < 11000, 328 < mm["ny_tokyo"]["bearing"] < 338), (True, True))
    lons = [p[1] for p in mm["arc"]]
    check("  drawn as one line: longitude runs on west past -180 (Tokyo at -220), no jump",
          (round(lons[-1]), max(abs(a - c) for a, c in zip(lons, lons[1:])) < 60, max(p[0] for p in mm["arc"]) > 60), (-220, True, True))
    check("  and east to London stays as it is", round(mm["east"][-1][1]), 0)
    check("gridLL: FN31's south-west corner", json.loads(b.js("JSON.stringify(rrMapMath.gridLL('FN31'))")), {"lon": -74, "lat": 41, "w": 2, "h": 1})
    check("  a 6-character grid (as JTDX gives our own) is its square", json.loads(b.js("JSON.stringify(rrMapMath.gridLL('FN31pr'))")),
          {"lon": -74, "lat": 41, "w": 2, "h": 1})
    # The tab mounted while hidden, then shown: it must draw at its real size, centred on our grid.
    b.goto("about:blank")                       # a fresh page: map.js declares rrMapMath once per page
    b.js("""(() => {
      window.esc = (s) => String(s); window.send = () => {};
      window.__impl = null; window.addons = {register(n, impl) { window.__impl = impl; }};
      const s = document.createElement('style'); s.textContent = '.rm-wrap{position:relative;height:300px;overflow:hidden}.rm-tile{position:absolute;width:256px;height:256px}';
      document.head.appendChild(s);
      const host = document.createElement('div'); host.id = 'mapbox'; host.style.display = 'none'; host.style.width = '800px';
      document.body.appendChild(host);
    })()""")
    b.js((ROOT / "static/basemaps.js").read_text() + "; window.basemaps = basemaps;")
    b.js((ROOT / "static/addons/map.js").read_text())
    b.js("""__impl.mount(document.getElementById('mapbox'));
      __impl.hello({map: {me: 'FN31pr', band: '15m', showing: '15m', grids: {FN31: 'c', FN42: 'w'}, decodes: []}, pota: []});
      document.getElementById('mapbox').style.display = 'block';""")
    b.wait("document.querySelectorAll('#mapbox .rm-tile').length > 2", 5)
    shown = json.loads(b.js("""JSON.stringify({tiles: document.querySelectorAll('#mapbox .rm-tile').length,
      me: !!document.querySelector('#mapbox .rm-mep'),
      mePos: (() => { const c = document.querySelector('#mapbox .rm-mep'); const r = c && c.getBoundingClientRect();
                      const w = document.querySelector('#mapbox .rm-wrap').getBoundingClientRect();
                      return r ? [Math.round(r.x + r.width / 2 - w.x), Math.round(r.y + r.height / 2 - w.y)] : null; })()})"""))
    check("map hidden then shown: tiles fill the view", shown["tiles"] > 2, True)
    check("  our grid (6 characters) is marked, at the centre", (shown["me"], [abs(shown["mePos"][0] - 400) < 3, abs(shown["mePos"][1] - 150) < 3] if shown["mePos"] else None),
          (True, [True, True]))
    # ---- the background (basemaps.js)
    BGJS = """JSON.stringify({host: new URL(document.querySelector('#mapbox .bm-tile').src).host, attr: document.querySelector('#mapbox .bm-host + .bm-host ~ a, #mapbox a').textContent,
      lights: document.querySelectorAll('#mapbox [data-basemap="lights"] .bm-tile').length > 0 && !document.querySelector('#mapbox [data-basemap="lights"]').hidden,
      clip: document.querySelector('#mapbox [data-basemap="lights"]').style.clipPath.startsWith('polygon('),
      lit: !!document.querySelector('#mapbox polygon.lit')})"""
    check("background: NASA's Blue Marble by default, credited; the night side is the Earth's city lights, cut along the terminator",
          json.loads(b.js(BGJS)), {"host": "gibs.earthdata.nasa.gov", "attr": "NASA GIBS", "lights": True, "clip": True, "lit": True})
    check("  a menu to pick another", json.loads(b.js("JSON.stringify([...document.querySelectorAll('#mapbox .bm-pick option')].map((o) => o.value))")),
          ["earth", "terrain", "topo", "imagery", "streets", "dark"])
    for _ in range(7):
        b.js("document.querySelector('#mapbox [data-z=\"1\"]').click()")
    deep = json.loads(b.js("""JSON.stringify((() => { const i = document.querySelector('#mapbox [data-basemap="earth"] .bm-tile');
      return {level: i.src.split('GoogleMapsCompatible_Level8/')[1].split('/')[0], px: parseInt(i.style.width, 10)}; })())"""))
    check("  past its deepest zoom (8) the map still zooms: the deepest tiles, drawn larger (zoom 10: four times)", deep, {"level": "8", "px": 1025})
    for _ in range(7):
        b.js("document.querySelector('#mapbox [data-z=\"-1\"]').click()")
    b.js("(() => { const s = document.querySelector('#mapbox .bm-pick'); s.value = 'streets'; s.dispatchEvent(new Event('change')); })()")
    check("  Streets picked: OpenStreetMap's tiles and credit, and the night side is shaded again",
          json.loads(b.js(BGJS)), {"host": "tile.openstreetmap.org", "attr": "© OpenStreetMap", "lights": False, "clip": True, "lit": False})
    b.js("(() => { const s = document.querySelector('#mapbox .bm-pick'); s.value = 'earth'; s.dispatchEvent(new Event('change')); })()")
    # ---- the wheel (basemaps.wheel, 2026-11-19: the map "would not hold a zoom and kept zooming out")
    LEVEL = "Number(document.querySelector('#mapbox [data-basemap=\"earth\"] .bm-tile').src.split('GoogleMapsCompatible_Level8/')[1].split('/')[0])"
    VIEW = "document.querySelector('#mapbox .rm-world').getAttribute('transform')"
    mid = json.loads(b.js("JSON.stringify((() => { const r = document.querySelector('#mapbox .rm-wrap').getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; })())"))

    def wheel(dx, dy, n=1, ctrl=False):
        for _ in range(n):
            b.call("Input.dispatchMouseEvent", type="mouseWheel", x=mid[0], y=mid[1], deltaX=dx, deltaY=dy, modifiers=2 if ctrl else 0)
            _time.sleep(0.03)
        _time.sleep(0.3)                               # a gesture ends: the next one starts from nothing

    for _ in range(2):
        b.js("document.querySelector('#mapbox [data-z=\"1\"]').click()")
    z0, v0 = b.js(LEVEL), b.js(VIEW)
    wheel(-40, 0, 5)
    check("  five sideways trackpad swipes: the zoom holds", b.js(LEVEL), z0)
    check("    and the map pans", b.js(VIEW) != v0, True)
    wheel(0, 2, 6)
    check("  a trackpad's momentum tail (six small ticks): the zoom holds", b.js(LEVEL), z0)
    wheel(0, -100)
    check("  one mouse-wheel notch in: one level", b.js(LEVEL), z0 + 1)
    wheel(0, 100)
    check("  one notch out: back", b.js(LEVEL), z0)
    wheel(0, -8, 4, ctrl=True)
    check("  a pinch out: in a level", b.js(LEVEL), z0 + 1)
    for _ in range(3):
        b.js("document.querySelector('#mapbox [data-z=\"-1\"]').click()")
    # ---- the page's own working out, from a snapshot handed to it
    now = 2_000_000
    snap_js = {"now": now, "map": {"me": "FN31pr", "band": "20m", "showing": "all", "grids": {}, "decodes": [
        {"key": "k1", "answer": "cq", "call": "JA1ABC", "grid": "PM95", "need": "dxcc", "reasons": [], "snr": -12, "cq": True, "to_me": False,
         "entity": "Japan", "km": 10800, "bearing": 334, "age": 5, "band": "20m", "mode": "FT8", "message": "CQ JA1ABC PM95", "lotw": True}]},
        "heard": [{"call": "JA1ABC", "grid": "PM95", "band": "20m", "mode": "FT8", "snr": -12, "best": -12, "t": now - 5, "n": 1, "need": "dxcc", "entity": "Japan"},
                  {"call": "VK2ABC", "grid": "QF56", "band": "20m", "mode": "FT8", "snr": -19, "best": -19, "t": now - 1500, "n": 2, "need": None, "entity": "Australia"},
                  {"call": "K6ABC", "grid": "CM87", "band": "40m", "mode": "FT8", "snr": 1, "best": 1, "t": now - 60, "n": 1, "need": None, "entity": "United States"}],
        "rig": {"alive": True, "transmitting": False, "tx_enabled": False, "decoding": False, "dx_call": "", "dx_grid": "", "mode": "FT8", "dial": 14074000, "band": "20m"},
        "radios": [],
        "hears_me": {"call": "N0CALL", "window_min": 60, "every_s": 600, "locator": "FN31pr", "error": None, "checked": now - 100, "receivers": [
            {"call": "JA1ABC", "grid": "PM95vq", "lat": 35.7, "lon": 139.8, "band": "40m", "mode": "FT8", "snr": -3, "t": now - 30, "km": 10800, "country": "Japan"},
            {"call": "JA1ABC", "grid": "PM95vq", "lat": 35.7, "lon": 139.8, "band": "20m", "mode": "FT8", "snr": -17, "t": now - 300, "km": 10800, "country": "Japan"},
            {"call": "K6ABC", "grid": "CM87", "lat": 37.5, "lon": -123, "band": "20m", "mode": "FT8", "snr": -8, "t": now - 200, "km": 4100, "country": "United States"},
            {"call": "G4ABC", "grid": "IO91", "lat": 51.5, "lon": -1, "band": "20m", "mode": "FT8", "snr": -20, "t": now - 2400, "km": 5500, "country": "England"}]},
        "hears_me_why": None, "pota": None}
    b.js("document.getElementById('mapbox').style.height = '300px'; __impl.hello(" + json.dumps(snap_js) + ")")
    b.wait("rrMap.stations().length === 4", 5)
    st = json.loads(b.js("JSON.stringify(Object.fromEntries(rrMap.stations().map((r) => [r.call, [r.kind, r.rx && r.rx.band, r.tx && r.tx.band, r.tx && r.tx.snr]])))"))
    check("page: every band showing, two-way is the report on the band you hear it on (not the newer one elsewhere)",
          st["JA1ABC"], ["both", "20m", "20m", -17])
    check("  heard on 40m and reported on 20m is not two-way", st["K6ABC"], ["rx", "40m", "20m", -8])
    check("  the others: heard only, reported only", (st["VK2ABC"][0], st["G4ABC"][0]), ("rx", "tx"))
    view = json.loads(b.js("""JSON.stringify((() => { const w = document.querySelector('#mapbox .rm-wrap').getBoundingClientRect();
      const inside = [...document.querySelectorAll('#mapbox .rm-s')].every((g) => { const r = g.getBoundingClientRect(); return r.x >= w.x && r.x <= w.right && r.y >= w.y && r.y <= w.bottom; });
      return {n: document.querySelectorAll('#mapbox .rm-s').length, inside}; })())"""))
    check("  the first stations are framed: all four on the map at once", view, {"n": 4, "inside": True})
    b.js("(() => { const s = document.querySelector('#mapbox .rm-win'); s.value = '15'; s.dispatchEvent(new Event('change')); })()")
    check("  a 15 minute window: what was heard or reported longer ago is off",
          sorted(json.loads(b.js("JSON.stringify(rrMap.stations().map((r) => r.call))"))), ["JA1ABC", "K6ABC"])
    b.js("(() => { const s = document.querySelector('#mapbox .rm-win'); s.value = '60'; s.dispatchEvent(new Event('change')); })()")
    b.js("document.querySelector('#mapbox [data-layer=\"heard\"]').click()")
    check("  'Hears you' off: only what you hear is drawn, and no amber path",
          (b.js("document.querySelectorAll('#mapbox .rm-s').length"), b.js("document.querySelectorAll('#mapbox .rm-p-tx').length")), (3, 0))
    b.js("document.querySelector('#mapbox [data-layer=\"heard\"]').click(); document.querySelector('#mapbox [data-layer=\"paths\"]').click()")
    check("  'Paths' off: the stations stay, the lines go", (b.js("document.querySelectorAll('#mapbox .rm-s').length"), b.js("document.querySelectorAll('#mapbox .rm-p').length")), (4, 0))
    b.js("document.querySelector('#mapbox .rm-s').__same = true")
    b.js("__impl.event({rig: Object.assign({}, " + json.dumps(snap_js["rig"]) + ", {transmitting: true, dx_call: 'ZL1ABC', dx_grid: 'RF73'}), radios: [], now: " + str(now) + "})")
    check("  transmitting to a station not on the map: the beam runs to the grid JTDX has for it",
          (b.js("document.querySelectorAll('#mapbox .rm-beamp').length"), b.js("document.querySelector('#mapbox .rm-beamt').textContent")), (1, "→ ZL1ABC"))
    check("  the nav: transmitting", b.js("__impl.tab().dot"), "tx")
    check("  what is on the air changing does not redraw the stations", b.js("document.querySelector('#mapbox .rm-s').__same"), True)
finally:
    b.close()

print(f"\n{'all ok' if not fails else f'{fails} FAILED'}", flush=True)
_os._exit(1 if fails else 0)
