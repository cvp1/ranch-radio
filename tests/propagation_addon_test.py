"""The Propagation add-on: space weather and the bands heard, beside the band buttons.

  - parsing: flare class from GOES flux, K index labels, the newest NOAA row
    wins whatever the feed's order, InfluxQL results to per-band counts (top
    regions, best SNR, farthest, which receivers heard it; UNKNOWN regions dropped)
  - against stand-in NOAA and InfluxDB servers: the snapshot carries Kp,
    X-ray class and solar flux and the bands heard; an unreachable InfluxDB
    gives "unknown" (no counts, an error), never zero; NOAA failing keeps the
    last values and says so; no skywatch configured never queries one
  - PSKReporter: reception reports to per-band receivers (unique), best SNR,
    farthest (from the two locators) and countries; polled only with a
    callsign set, and an unreachable PSKReporter says so instead of "none"
  - next openings (skywatch_forecast_window): only cells the newest issue
    wrote, consecutive hours merged into one window, soonest first; no
    forecast measurement is no section (not "no openings"); unreachable is
    an error, not empty
  - needed near you (opt-in "near"): reports from receivers in one grid to
    one row per (call, band, mode), newest kept, your own call and
    locator-less or off-band reports dropped, capped; judged per band by a
    needs index (strong levels only, highest first); no index is unknown
    (None), never zero; the strong list matches the Roster page's; the query
    asks PSKReporter by grid over 15 minutes; the two PSKReporter queries
    never run closer than PSK_GAP_S; "near": true takes the station's grid,
    else PSKReporter's locator for you; no grid known, or PSKReporter down,
    is an error with no counts
  - it's in the module catalog and manifest

    python3 tests/propagation_addon_test.py
"""
import asyncio
import json
import sys
import time
from pathlib import Path

from aiohttp import web

import testkit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from addons import propagation as P  # noqa: E402
from ranch_radio import catalog, manifest  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


# ---------------------------------------------------------------- parsing
check("X-ray class from flux (none: no class)",
      [P.xray_class(f) for f in (2.632919517964183e-07, 1.2e-5, 3e-4, 5e-9, None)],
      ["B2.6", "M1.2", "X3.0", "A0.5", None])
check("Kp labels", [P.kp_label(k) for k in (0, 3, 4.3, 5, 7, 9)],
      ["quiet", "unsettled", "active", "storm G1", "storm G3", "storm G5"])

kp_rows = [{"time_tag": "2026-10-29T23:50:00", "kp_index": 2, "estimated_kp": 2.33, "kp": "2M"},
           {"time_tag": "2026-10-29T23:49:00", "kp_index": 1, "estimated_kp": 1.0, "kp": "1Z"}]
check("the newest K index row wins, whatever the order", P.parse_kp(kp_rows)["kp"], 2.3)
check("  with its time", P.parse_kp(kp_rows)["ts"], 1793317800)
xr = [{"time_tag": "2026-10-29T23:50:00Z", "flux": 2.6e-7, "energy": "0.1-0.8nm"},
      {"time_tag": "2026-10-29T23:51:00Z", "flux": 9e-9, "energy": "0.05-0.4nm"},
      {"time_tag": "2026-10-29T23:45:00Z", "flux": 3e-6, "energy": "0.1-0.8nm"}]
check("X-ray: only the 0.1-0.8 nm channel, newest", P.parse_xray(xr)["class"], "B2.6")
f107 = [{"time_tag": "2026-09-18T17:00:00", "flux": 96.0}, {"time_tag": "2026-10-29T22:00:00", "flux": 90.0}]
check("solar flux: the newest", P.parse_f107(f107)["sfi"], 90)
check("empty feeds parse to None", (P.parse_kp([]), P.parse_xray(None), P.parse_f107([{}])), (None, None, None))

BY_SOURCE = {"results": [{"statement_id": 0, "series": [
    {"name": "skywatch_decode", "tags": {"band": "20m", "source": "jtdx"},
     "columns": ["time", "n", "best", "far"], "values": [[0, 120, 5.0, 9100.4]]},
    {"name": "skywatch_decode", "tags": {"band": "20m", "source": "skimmer"},
     "columns": ["time", "n", "best", "far"], "values": [[0, 30, 12.0, 4000.0]]},
    {"name": "skywatch_decode", "tags": {"band": "17m", "source": "skimmer"},
     "columns": ["time", "n", "best", "far"], "values": [[0, 4, -18.0, None]]},
]}]}
BY_REGION = {"results": [{"statement_id": 0, "series": [
    {"tags": {"band": "20m", "region": "JA"}, "columns": ["time", "count"], "values": [[0, 40]]},
    {"tags": {"band": "20m", "region": "NA_EAST"}, "columns": ["time", "count"], "values": [[0, 90]]},
    {"tags": {"band": "20m", "region": "UNKNOWN"}, "columns": ["time", "count"], "values": [[0, 20]]},
    {"tags": {"band": "17m", "region": "EU"}, "columns": ["time", "count"], "values": [[0, 4]]},
]}]}
heard = P.parse_heard(BY_SOURCE, BY_REGION)
check("20m: counts summed across receivers", heard["20m"]["n"], 150)
check("  best SNR is the best of either", heard["20m"]["best_snr"], 12)
check("  farthest is the farthest of either", heard["20m"]["max_km"], 9100)
check("  which receivers heard it", heard["20m"]["sources"], {"jtdx": 120, "skimmer": 30})
check("  regions busiest first, UNKNOWN dropped", heard["20m"]["regions"], [["NA_EAST", 90], ["JA", 40]])
check("17m: no distance stays None", heard["17m"]["max_km"], None)
check("a band with no rows is absent (no data), not zero", "40m" in heard, False)
check("an empty result is no bands", P.parse_heard({"results": [{"statement_id": 0}]}, {}), {})

PSK_XML = """<?xml version="1.0"?>
<receptionReports >
  <receptionReport receiverCallsign="JG0CQK" receiverLocator="PM97ms" senderCallsign="N0CALL" senderLocator="FN31pr" frequency="28075548" flowStartSeconds="1793317588" mode="FT8" receiverDXCC="Japan" sNR="-16" />
  <receptionReport receiverCallsign="JK1AKC" receiverLocator="QM05ft" senderCallsign="N0CALL" senderLocator="FN31pr" frequency="28075532" flowStartSeconds="1793317559" mode="FT8" receiverDXCC="Japan" sNR="0" />
  <receptionReport receiverCallsign="JK1AKC" receiverLocator="QM05ft" senderCallsign="N0CALL" senderLocator="FN31pr" frequency="28075533" flowStartSeconds="1793317500" mode="FT8" receiverDXCC="Japan" sNR="-3" />
  <receptionReport receiverCallsign="KL2R" receiverLocator="BP64KU" senderCallsign="N0CALL" senderLocator="FN31pr" frequency="28075533" flowStartSeconds="1793317545" mode="FT8" receiverDXCC="Alaska" sNR="-16" />
  <receptionReport receiverCallsign="K6ABC" receiverLocator="CM87" senderCallsign="N0CALL" senderLocator="FN31pr" frequency="14075000" flowStartSeconds="1793317545" mode="FT8" receiverDXCC="United States" sNR="" />
  <receptionReport receiverCallsign="X1X" receiverLocator="" senderCallsign="N0CALL" senderLocator="FN31pr" frequency="9999" mode="FT8" />
</receptionReports>"""
psk = P.parse_psk(PSK_XML)
check("PSKReporter: bands from frequency (an off-band report dropped)", sorted(psk), ["10m", "20m"])
check("  receivers are unique stations, spots every report", (psk["10m"]["receivers"], psk["10m"]["spots"]), (3, 4))
check("  best SNR", psk["10m"]["best_snr"], 0)
check("  countries busiest first", psk["10m"]["countries"], [["Japan", 2], ["Alaska", 1]])
check("  farthest from the two locators (FN31 to QM05 is ~10,900 km)", 10500 < psk["10m"]["max_km"] < 11300, True)
check("  an empty SNR is skipped, not zero", psk["20m"]["best_snr"], None)
rx = P.psk_receivers(PSK_XML)
check("PSKReporter, station by station (the Map tab): one row per station and band, newest first",
      [(r["call"], r["band"], r["snr"], r["t"]) for r in rx],
      [("JG0CQK", "10m", -16, 1793317588), ("JK1AKC", "10m", 0, 1793317559), ("KL2R", "10m", -16, 1793317545), ("K6ABC", "20m", None, 1793317545)])
check("  at its own locator, with the distance and country", (rx[1]["grid"], round(rx[1]["lat"], 1), round(rx[1]["lon"], 1), 10500 < rx[1]["km"] < 11300, rx[1]["country"]),
      ("QM05ft", 35.8, 140.5, True, "Japan"))
check("  no locator or off-band: dropped; capped", ("X1X" in [r["call"] for r in rx], len(P.psk_receivers(PSK_XML, cap=2))), (False, 2))
check("  your own locator, as the reports give it", (P.psk_locator(PSK_XML), P.psk_locator("<receptionReports/>")), ("FN31pr", ""))
check("locator centre: FN31 (lat, lon)", tuple(round(v, 2) for v in P.grid_latlon("FN31")), (41.5, -73.0))
check("a bad locator is None", (P.grid_latlon(""), P.grid_latlon("ZZ99")), (None, None))
check("band names from Hz", (P.band_of(7_074_000), P.band_of(50_313_000), P.band_of(9_999)), ("40m", "6m", None))

# ---------------------------------------------------------------- next openings
ISSUED = 1793380063
H0 = 1793383200                      # an hour boundary


def fc_open(rows):
    """InfluxQL (epoch s) for open cells: rows of (band, region, [(t, prob, issued)])."""
    return {"results": [{"statement_id": 0, "series": [
        {"name": "skywatch_forecast_window", "tags": {"band": b, "region": r},
         "columns": ["time", "prob", "clim_level", "issued_ts"],
         "values": [[t, p, 1.0, i] for t, p, i in cells]} for b, r, cells in rows]}]}


OPEN = fc_open([
    ("17m", "NA_EAST", [(H0, 0.66, ISSUED), (H0 + 3600, 0.71, ISSUED), (H0 + 7200, 0.55, ISSUED)]),
    ("30m", "NA_EAST", [(H0 + 7200, 0.91, ISSUED)]),
    ("40m", "EU", [(H0 + 3600, 0.8, ISSUED - 86400)]),          # yesterday's issue: withdrawn
    ("20m", "JA", [(H0 + 3600, 0.6, ISSUED), (H0 + 3 * 3600, 0.62, ISSUED)]),   # a gap: two windows
])
fw = P.parse_forecast(OPEN, ISSUED)
check("forecast: consecutive hours are one window, peak kept",
      fw[0], {"band": "17m", "region": "NA_EAST", "start": H0, "end": H0 + 3 * 3600, "peak": 0.71})
check("  a cell an older issue wrote is dropped", any(w["band"] == "40m" for w in fw), False)
check("  a gap makes two windows", [(w["start"], w["end"]) for w in fw if w["band"] == "20m"],
      [(H0 + 3600, H0 + 7200), (H0 + 3 * 3600, H0 + 4 * 3600)])
check("  soonest first", [w["start"] for w in fw], sorted(w["start"] for w in fw))
check("  capped", len(P.parse_forecast(fc_open([(f"{i}m", "EU", [(H0, 0.6, ISSUED)]) for i in range(40)]), ISSUED)),
      P.FORECAST_MAX)
check("  nothing open is no windows", P.parse_forecast({"results": [{"statement_id": 0}]}, ISSUED), [])

# ---------------------------------------------------------------- catalog
check("an add-on in the catalog, at addons/propagation, enabled on both OSes",
      (catalog.by_name().get("propagation", {}).get("kind"), manifest.MANIFESTS["propagation"]["block"],
       [manifest.template("propagation", o) for o in ("macos", "linux")]),
      ("addon", ("addons", "propagation"), [{"enabled": True}, {"enabled": True}]))


# ---------------------------------------------------------------- needed near you
NEAR_XML = """<?xml version="1.0"?>
<receptionReports>
  <receptionReport receiverCallsign="K7AAA" receiverLocator="FN31ab" senderCallsign="JA1ZZZ" senderLocator="PM95" frequency="21074500" flowStartSeconds="1793317500" mode="FT8" sNR="-12" />
  <receptionReport receiverCallsign="K7BBB" receiverLocator="FN31cd" senderCallsign="JA1ZZZ" senderLocator="PM95" frequency="21074600" flowStartSeconds="1793317560" mode="FT8" sNR="-5" />
  <receptionReport receiverCallsign="K7AAA" receiverLocator="FN31ab" senderCallsign="VK2YYY" senderLocator="QF56" frequency="21074900" flowStartSeconds="1793317440" mode="FT8" sNR="-18" />
  <receptionReport receiverCallsign="K7AAA" receiverLocator="FN31ab" senderCallsign="W1XXX" senderLocator="FN42" frequency="21075100" flowStartSeconds="1793317430" mode="FT8" sNR="3" />
  <receptionReport receiverCallsign="K7AAA" receiverLocator="FN31ab" senderCallsign="N0CALL" senderLocator="FN31" frequency="21075200" flowStartSeconds="1793317430" mode="FT8" sNR="9" />
  <receptionReport receiverCallsign="K7AAA" receiverLocator="FN31ab" senderCallsign="G4WWW" senderLocator="IO91" frequency="14074000" flowStartSeconds="1793317400" mode="FT4" sNR="" />
  <receptionReport receiverCallsign="K7AAA" receiverLocator="FN31ab" senderCallsign="F5VVV" senderLocator="" frequency="14074100" flowStartSeconds="1793317400" mode="FT8" sNR="-1" />
  <receptionReport receiverCallsign="K7AAA" receiverLocator="FN31ab" senderCallsign="EA3UUU" senderLocator="JN11" frequency="9999" flowStartSeconds="1793317400" mode="FT8" sNR="-1" />
</receptionReports>"""
near = P.parse_near(NEAR_XML, own="N0CALL")
check("near: one row per (call, band, mode); own call, no locator, off-band dropped",
      sorted((r["call"], r["band"]) for r in near),
      [("G4WWW", "20m"), ("JA1ZZZ", "15m"), ("VK2YYY", "15m"), ("W1XXX", "15m")])
check("  the newest report of a station is kept (time and SNR)",
      [(r["t"], r["snr"]) for r in near if r["call"] == "JA1ZZZ"], [(1793317560, -5)])
check("  newest first, an empty SNR is None, capped",
      (near[0]["call"], [r["snr"] for r in near if r["call"] == "G4WWW"], len(P.parse_near(NEAR_XML, cap=2))),
      ("JA1ZZZ", [None], 2))


class FakeNeeds:
    """need(call, grid, band, mode) -> (level, reasons), as logsync_needs.Needs gives it."""
    LV = {"JA1ZZZ": "dxcc_band", "VK2YYY": "dxcc", "W1XXX": "call_band_mode", "G4WWW": None}

    def __init__(self):
        self.asked = []

    def need(self, call, grid, band, mode, submode=""):
        self.asked.append((call, grid, band, mode))
        lv = self.LV.get(call)
        return lv, [lv] if lv else []


fn = FakeNeeds()
v = P.near_view(near, fn)
check("near_view: per band, distinct calls heard and the strong ones counted",
      {b: (x["heard"], x["strong"]) for b, x in v.items()}, {"15m": (3, 2), "20m": (1, 0)})
check("  a weak need (new call on this band) is heard, not counted", "call_band_mode" in v["15m"]["levels"], False)
check("  the top stations: highest need first", [x["call"] for x in v["15m"]["top"]], ["VK2YYY", "JA1ZZZ"])
check("  per-level counts", v["15m"]["levels"], {"dxcc": 1, "dxcc_band": 1})
check("  the index is asked with the report's grid, band and mode", ("G4WWW", "IO91", "20m", "FT4") in fn.asked, True)
check("  no needs index: unknown (None), not zero", P.near_view(near, None), None)
check("  no rows with an index: no bands", P.near_view([], FakeNeeds()), {})

# Read as text, not imported: the logbook sync's modules are not this test's
# dependency (tests/affected.py: a needs change must not pick this test).
import re as _re  # noqa: E402
js = (ROOT / "static" / "addons" / "roster.js").read_text()
js_list = lambda name: tuple(_re.findall(r'"([a-z_]+)"', _re.search(rf"const {name} = \[([^\]]*)\]", js).group(1)))
check("the strong levels are the Roster page's, in its rank order",
      (js_list("STRONG"), tuple(x for x in js_list("RANK") if x in P.STRONG)), (P.STRONG, P.STRONG))
check('"near": a grid is kept, true is the station\'s own, junk is off',
      [P.Propagation(None, {"pskreporter": {"near": n}}).near for n in ("fn31pr", True, "x; rm", False, None, 1)],
      ["FN31", True, "", "", "", ""])


# ---------------------------------------------------------------- against stand-ins
async def until(addon, pred, timeout=10):
    """Return once pred(snapshot) holds (the poll loop has got that far)."""
    end = time.monotonic() + timeout
    while not pred(addon.snapshot()) and time.monotonic() < end:
        await asyncio.sleep(0.05)


class Host:
    def __init__(self):
        self.sent = []

    def _broadcast(self, payload):
        self.sent.append(json.loads(payload))

    async def _send(self, ws, payload):
        self.sent.append(json.loads(payload))

    def notice(self, msg, level="info", ws=None):
        pass


queries = []
noaa_up = {"on": True}


async def noaa(request):
    if not noaa_up["on"]:
        return web.Response(status=503)
    name = request.match_info["name"]
    body = {"kp": kp_rows, "xray": xr, "f107": f107}[name]
    return web.json_response(body)


psk_seen = []


near_seen = []


async def pskr(request):
    if request.query.get("modify") == "grid":
        near_seen.append((time.monotonic(), dict(request.query)))
        if not noaa_up["on"]:
            return web.Response(status=503)
        return web.Response(text=NEAR_XML, content_type="text/xml")
    psk_seen.append(dict(request.query))
    if not noaa_up["on"]:
        return web.Response(status=503)
    return web.Response(text=PSK_XML, content_type="text/xml")


forecast = {"on": True}


async def influx(request):
    q = request.query.get("q", "")
    queries.append((request.query.get("db"), q))
    if "skywatch_forecast_window" in q:
        if not forecast["on"]:                       # no such measurement: empty results
            return web.json_response({"results": [{"statement_id": 0}]})
        if "max(issued_ts)" in q:
            return web.json_response({"results": [{"statement_id": 0, "series": [
                {"name": "skywatch_forecast_window", "columns": ["time", "max"], "values": [[0, ISSUED]]}]}]})
        return web.json_response(OPEN)
    return web.json_response(BY_REGION if "GROUP BY band, region" in q else BY_SOURCE)


async def main():
    app = web.Application()
    app.router.add_get("/noaa/{name}", noaa)
    app.router.add_get("/query", influx)
    app.router.add_get("/psk", pskr)
    runner = web.AppRunner(app)
    await runner.setup()
    port = testkit.free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    base = f"http://127.0.0.1:{port}"
    P.URL_KP, P.URL_XRAY, P.URL_F107 = f"{base}/noaa/kp", f"{base}/noaa/xray", f"{base}/noaa/f107"
    P.URL_PSK = f"{base}/psk"

    host = Host()
    a = P.Propagation(host, {"skywatch": {"url": base, "db": "skywatch"}, "window_min": 45,
                             "pskreporter": {"call": "n0call"}})
    await a.start()
    await until(a, lambda s: s["psk_checked"])          # PSKReporter is the loop's last poll
    s = a.snapshot()
    check("snapshot: Kp from NOAA", (s["kp"]["kp"], s["kp"]["label"]), (2.3, "quiet"))
    check("snapshot: X-ray class and solar flux", (s["xray"]["class"], s["f107"]["sfi"]), ("B2.6", 90))
    check("snapshot: the bands heard", sorted(s["bands"]), ["17m", "20m"])
    check("the window is the host file's", s["window_min"], 45)
    check("PSKReporter: who heard you, per band", (s["pskreporter"], sorted(s["psk"])), ("N0CALL", ["10m", "20m"]))
    check("  asked for the callsign over the window",
          (psk_seen[0].get("senderCallsign"), psk_seen[0].get("flowStartSeconds")), ("N0CALL", "-2700"))
    check("InfluxDB asked for the skywatch db over the window",
          all(db == "skywatch" and ("now() - 45m" in q or "skywatch_forecast_window" in q) for db, q in queries), True)
    check("next openings: from the newest issue", (s["forecast_issued"], [w["band"] for w in s["forecast"]]),
          (ISSUED, ["17m", "20m", "30m", "20m"]))
    check("  over the host file's horizon, asked in epoch seconds",
          any("skywatch_forecast_window" in q and "now() + 12h" in q for _, q in queries), True)
    check("the page was told (one state message)", [m.get("addon") for m in host.sent if "state" in m][:1], ["propagation"])

    # InfluxDB goes away: unknown, not zero.
    noaa_up["on"] = False
    host.sent.clear()
    await a.stop()
    a.sw_url = "http://127.0.0.1:9"          # nothing listens there
    await a.start()
    await until(a, lambda s: s["psk_error"])
    s = a.snapshot()
    check("InfluxDB down: no counts", s["bands"], {})
    check("  and says why", s["heard_error"], "skywatch not reachable")
    check("NOAA down: says so", s["space_error"], "NOAA not reachable")
    check("PSKReporter down: unknown, not none", (s["psk"], s["psk_error"]), ({}, "PSKReporter not reachable"))
    check("  a restart forgot the stations too", a.heard_by()["receivers"], [])
    a.psk = {"bands": {"10m": {}}, "receivers": P.psk_receivers(PSK_XML), "locator": "FN31pr", "error": None, "checked": 1}
    a.psk = await a._poll_psk()                  # an answer was held, and the next poll fails
    hb = a.heard_by()
    check("  a failed poll after a good one: the counts go, the Map tab's stations stay (each has its own time), with the error",
          (a.psk["bands"], len(hb["receivers"]), hb["locator"], hb["error"], hb["call"], hb["every_s"]),
          ({}, 4, "FN31pr", "PSKReporter not reachable", "N0CALL", 600))
    check("InfluxDB down: the forecast is unknown, not empty", (s["forecast"], s["forecast_error"]),
          ([], "skywatch not reachable"))
    await a.stop()

    # NOAA failing after a good read keeps the last values.
    noaa_up["on"] = True
    b = P.Propagation(Host(), {})
    await b.start()
    await asyncio.sleep(1.0)
    noaa_up["on"] = False
    b.space = await b._poll_swpc()
    s = b.snapshot()
    check("NOAA failing later keeps the last Kp", s["kp"]["kp"], 2.3)
    check("  and flags it", s["space_error"], "NOAA not reachable")
    n_before = len(queries)
    check("no skywatch configured: nothing heard, no error", (s["skywatch"], s["bands"], s["heard_error"]), (False, {}, None))
    check("  and no InfluxDB query was made", len(queries), n_before)
    check("no callsign: PSKReporter never asked (twice by the first add-on's loop, once by hand above)",
          (s["pskreporter"], len(psk_seen)), ("", 3))
    check("a callsign that isn't one is ignored", P.Propagation(Host(), {"pskreporter": {"call": "x; rm"}}).psk_call, "")
    check("  and there is nobody to draw", P.Propagation(Host(), {}).heard_by(), None)
    await b.stop()

    # A skywatch with no forecast measurement: no openings section at all.
    forecast["on"] = False
    noaa_up["on"] = True
    d = P.Propagation(Host(), {"swpc": False, "skywatch": {"url": base, "db": "skywatch"}})
    await d.start()
    await asyncio.sleep(1.0)
    s = d.snapshot()
    check("no forecast measurement: no issue, no windows, no error",
          (s["forecast_issued"], s["forecast"], s["forecast_error"]), (None, [], None))
    await d.stop()
    check("the horizon is clamped", P.Propagation(Host(), {"forecast_h": 999}).forecast_h, 27)

    # Needed near you, against the stand-in PSKReporter.
    noaa_up["on"] = True
    tick, gap = P.TICK_S, P.PSK_GAP_S
    P.TICK_S = 0.05
    h = Host()
    h.logsync = type("LS", (), {"needs": FakeNeeds()})()
    h.identity = lambda: {"call": "N0CALL", "grid": "FN31pr"}
    e = P.Propagation(h, {"swpc": False, "pskreporter": {"call": "N0CALL", "near": True}})
    n_psk = len(psk_seen)
    await e.start()
    await asyncio.sleep(1.0)
    check("the two PSKReporter queries are PSK_GAP_S apart: only 'who hears you' in the first 5 minutes",
          (len(psk_seen) - n_psk, len(near_seen), e.snapshot()["near_checked"]), (1, 0, None))
    await e.stop()
    P.PSK_GAP_S = 0.3
    t0 = time.monotonic()
    await e.start()
    await until(e, lambda s: s["near_checked"])
    s = e.snapshot()
    t_near, q = near_seen[-1]
    check("  then the near query, no sooner than the gap", t_near - t0 >= 0.3, True)
    check("near: asked by the station's own grid (JTDX's), 15 minutes, reports only",
          {k: q.get(k) for k in ("receiverCallsign", "modify", "flowStartSeconds", "rronly", "rptlimit")},
          {"receiverCallsign": "FN31", "modify": "grid", "flowStartSeconds": "-900", "rronly": "1", "rptlimit": "2000"})
    check("  the snapshot: grid, window, per-band strong counts, no error",
          (s["near"], s["near_grid"], s["near_window_min"], {b: x["strong"] for b, x in s["near_bands"].items()}, s["near_error"]),
          (True, "FN31", 15, {"15m": 2, "20m": 0}, None))
    h.logsync.needs = None
    check("  the needs index going away: unknown (None), the rows kept", (e.snapshot()["near_bands"], len(e.nearby["rows"])), (None, 4))
    h.logsync.needs = FakeNeeds()
    noaa_up["on"] = False
    e.nearby = await e._poll_near()
    check("  PSKReporter down: no counts, says why", (e.snapshot()["near_bands"], e.snapshot()["near_error"]),
          (None, "PSKReporter not reachable"))
    await e.stop()
    noaa_up["on"] = True
    h.identity = lambda: {"call": "N0CALL", "grid": None}
    check("  no grid from JTDX: PSKReporter's locator for you", (e.psk.update(locator="EM12ab"), e.near_grid())[1], "EM12")
    e.psk["locator"] = ""
    g = await e._poll_near()
    check("  no grid at all: an error, nothing asked", (g["rows"], g["error"], len(near_seen)),
          ([], "this station's grid is not known yet", len(near_seen)))
    f = P.Propagation(Host(), {"swpc": False, "pskreporter": {"near": "fn31"}})
    check("  a grid in the host file is used as given, with no callsign needed", (f.near_grid(), f.snapshot()["near"]), ("FN31", True))
    check("  off by default", (P.Propagation(Host(), {}).snapshot()["near"], P.Propagation(Host(), {}).near_needs()), (False, None))
    P.TICK_S, P.PSK_GAP_S = tick, gap

    c = P.Propagation(Host(), {"swpc": False, "window_min": 99999})
    check('"swpc": false turns NOAA off', c.snapshot()["swpc"], False)
    check("the window is clamped", c.window_min, 360)
    await runner.cleanup()


asyncio.run(main())
print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
