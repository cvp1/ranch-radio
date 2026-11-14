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

    def reply(self, key):
        self.asked.append(key)
        return None


s = Sync()
s.roster.status(U.parse_status(J.status(dial=14_074_000, de="N0CALL", de_grid="FN31")))
for m in ("CQ JA5XYZ PM85", "N0CALL VK9NA -10", "CQ VK2ABC QF56"):
    s.roster.add(U.parse_decode(J.decode(msg=m)))
mv = s.map_view(None)
check("map view: our grid, the band in view", (mv["me"], mv["band"], mv["showing"]), ("FN31", "20m", "20m"))
check("  the band's grids", mv["grids"], {"PM95": "c", "PM97": "w"})
check("  only decodes with a grid", sorted(d["call"] for d in mv["decodes"]), ["JA5XYZ", "VK2ABC"])
check("  each with its need and key", sorted(mv["decodes"][0]) , sorted(["key", "call", "grid", "need", "snr", "cq", "to_me", "entity"]))
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
check("  a click answers through the sync", (s.asked, a.host.notes[-1][0]), ([mv["decodes"][0]["key"]], "info"))
check("  pota off: none in the snapshot", M.Map(Host(s), {"pota": False}).snapshot()["pota"], None)

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
    check("gridLL: FN31's south-west corner", json.loads(b.js("JSON.stringify(rrMapMath.gridLL('FN31'))")), {"lon": -74, "lat": 41, "w": 2, "h": 1})
finally:
    b.close()

print(f"\n{'all ok' if not fails else f'{fails} FAILED'}", flush=True)
_os._exit(1 if fails else 0)
