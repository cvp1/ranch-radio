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
  - it's in the module catalog and manifest

    python3 tests/propagation_addon_test.py
"""
import asyncio
import json
import socket
import sys
from pathlib import Path

from aiohttp import web

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
check("flux 2.63e-7 is B2.6", P.xray_class(2.632919517964183e-07), "B2.6")
check("flux 1.2e-5 is M1.2", P.xray_class(1.2e-5), "M1.2")
check("flux 3e-4 is X3.0", P.xray_class(3e-4), "X3.0")
check("flux 5e-9 is A0.5", P.xray_class(5e-9), "A0.5")
check("no flux, no class", P.xray_class(None), None)
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
check("in the module catalog as an add-on", catalog.by_name().get("propagation", {}).get("kind"), "addon")
check("its manifest block is addons/propagation", manifest.MANIFESTS["propagation"]["block"], ("addons", "propagation"))
check("both OSes have a template", [manifest.template("propagation", o) for o in ("macos", "linux")],
      [{"enabled": True}, {"enabled": True}])


# ---------------------------------------------------------------- against stand-ins
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


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


async def pskr(request):
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
    port = free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    base = f"http://127.0.0.1:{port}"
    P.URL_KP, P.URL_XRAY, P.URL_F107 = f"{base}/noaa/kp", f"{base}/noaa/xray", f"{base}/noaa/f107"
    P.URL_PSK = f"{base}/psk"

    host = Host()
    a = P.Propagation(host, {"skywatch": {"url": base, "db": "skywatch"}, "window_min": 45,
                             "pskreporter": {"call": "n0call"}})
    await a.start()
    await asyncio.sleep(1.0)
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
    a.sw_url = "http://127.0.0.1:9"          # nothing listens there
    noaa_up["on"] = False
    a._last_swpc = 0
    host.sent.clear()
    await a.stop()
    a.sw_url = "http://127.0.0.1:9"
    await a.start()
    await asyncio.sleep(1.5)
    s = a.snapshot()
    check("InfluxDB down: no counts", s["bands"], {})
    check("  and says why", s["heard_error"], "skywatch not reachable")
    check("NOAA down: says so", s["space_error"], "NOAA not reachable")
    check("PSKReporter down: unknown, not none", (s["psk"], s["psk_error"]), ({}, "PSKReporter not reachable"))
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
    check("no callsign: PSKReporter never asked", (s["pskreporter"], len(psk_seen)), ("", 2))
    check("a callsign that isn't one is ignored", P.Propagation(Host(), {"pskreporter": {"call": "x; rm"}}).psk_call, "")
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

    c = P.Propagation(Host(), {"swpc": False, "window_min": 99999})
    check('"swpc": false turns NOAA off', c.snapshot()["swpc"], False)
    check("the window is clamped", c.window_min, 360)
    await runner.cleanup()


asyncio.run(main())
print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
