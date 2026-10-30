"""Propagation add-on: what the bands are doing, beside the band buttons.

Two sources, each optional:

- **Space weather** from NOAA's Space Weather Prediction Center (public JSON,
  no key): the planetary K index (1-minute estimate), GOES X-ray flux (flare
  class) and the 10.7 cm solar flux. On by default; "swpc": false turns it off.
- **What the station heard** from a skywatch InfluxDB (the decode telemetry a
  skimmer and WSJT-X/JTDX write): per band over the last hour, how many FT8 /
  WSPR decodes, the best SNR, the farthest one and the regions heard. Off
  unless the host file names the database.
- **Next openings** from the same InfluxDB, when it has skywatch's
  forecast (measurement skywatch_forecast_window): the band and region
  windows forecast open in the next hours. Only cells the newest forecast
  issue wrote count, so an opening a later forecast withdrew never shows.
  No forecast measurement: no section, not "no openings".
- **Who hears you** from PSKReporter (public, no key): per band over the last
  hour, how many stations reported your callsign, the best SNR, the farthest
  and their countries. Off unless the host file gives "pskreporter": {"call"}.
  Polled every 10 minutes (PSKReporter asks for no more than every 5).

Options (the host file's addons block):

    "propagation": {"enabled": true,
                    "skywatch": {"url": "http://192.168.1.20:8086", "db": "skywatch"},
                    "pskreporter": {"call": "N0CALL"},
                    "window_min": 60,
                    "forecast_h": 12}

A band with no decodes is shown as "no data", never as closed: a receiver
only hears the band it is tuned to, so silence usually means nobody listened.
The page script (static/addons/propagation.js) badges the radio page's band
buttons and draws the Propagation tab. Nothing here keys or tunes a radio.
"""

import asyncio
import logging
import math
import re
import time
import xml.etree.ElementTree as ET

import aiohttp

from . import Addon

log = logging.getLogger("addon.propagation")

SWPC = "https://services.swpc.noaa.gov"
URL_KP = SWPC + "/json/planetary_k_index_1m.json"
URL_XRAY = SWPC + "/json/goes/primary/xrays-6-hour.json"
URL_F107 = SWPC + "/json/f107_cm_flux.json"

TICK_S = 60                 # skywatch is read every tick
SWPC_EVERY_S = 300          # NOAA every 5 minutes (its K index updates each minute)
KP_STALE_S = 30 * 60        # older than this, the K index is shown as stale
WINDOW_MIN = (5, 360)       # the "heard" window's allowed range, minutes
MEASUREMENT = "skywatch_decode"
FORECAST = "skywatch_forecast_window"
FORECAST_EVERY_S = 600      # skywatch issues one forecast a day; 10 minutes is plenty
FORECAST_H = (1, 27)        # the "next openings" horizon's allowed range, hours
FORECAST_MAX = 16           # windows sent to the page
URL_PSK = "https://retrieve.pskreporter.info/query"
PSK_EVERY_S = 600
CALL = re.compile(r"^[A-Z0-9/]{3,15}$")
# HF/6m band edges (Hz) for naming a reported frequency.
BAND_EDGES = [("160m", 1.8e6, 2.0e6), ("80m", 3.5e6, 4.0e6), ("60m", 5.25e6, 5.45e6), ("40m", 7.0e6, 7.3e6),
              ("30m", 10.1e6, 10.15e6), ("20m", 14.0e6, 14.35e6), ("17m", 18.068e6, 18.168e6),
              ("15m", 21.0e6, 21.45e6), ("12m", 24.89e6, 24.99e6), ("10m", 28.0e6, 29.7e6), ("6m", 50e6, 54e6)]


# --------------------------------------------------------------------- parsing

def xray_class(flux):
    """GOES 0.1-0.8 nm flux (W/m^2) -> flare class, e.g. 2.6e-7 -> "B2.6"."""
    if flux is None or flux <= 0:
        return None
    for letter, base in (("X", 1e-4), ("M", 1e-5), ("C", 1e-6), ("B", 1e-7), ("A", 1e-8)):
        if flux >= base or letter == "A":
            return f"{letter}{flux / base:.1f}"


def kp_label(kp):
    if kp is None:
        return None
    if kp >= 5:
        return f"storm G{min(5, int(kp) - 4)}"
    if kp >= 4:
        return "active"
    if kp >= 3:
        return "unsettled"
    return "quiet"


def _ts(tag):
    """NOAA time_tag ("2026-10-29T23:50:00" or with Z) -> epoch seconds, UTC."""
    import calendar
    tag = (tag or "").rstrip("Z")[:19]
    try:
        return calendar.timegm(time.strptime(tag, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def parse_kp(rows):
    rows = [r for r in rows or [] if isinstance(r, dict) and r.get("time_tag")]
    if not rows:
        return None
    r = max(rows, key=lambda r: r["time_tag"])
    kp = r.get("estimated_kp", r.get("kp_index"))
    if kp is None:
        return None
    kp = round(float(kp), 1)
    return {"kp": kp, "label": kp_label(kp), "ts": _ts(r["time_tag"])}


def parse_xray(rows):
    rows = [r for r in rows or [] if isinstance(r, dict) and r.get("energy") == "0.1-0.8nm"
            and r.get("flux") is not None and r.get("time_tag")]
    if not rows:
        return None
    r = max(rows, key=lambda r: r["time_tag"])
    return {"flux": r["flux"], "class": xray_class(r["flux"]), "ts": _ts(r["time_tag"])}


def parse_f107(rows):
    rows = [r for r in rows or [] if isinstance(r, dict) and r.get("flux") is not None and r.get("time_tag")]
    if not rows:
        return None
    r = max(rows, key=lambda r: r["time_tag"])
    return {"sfi": round(float(r["flux"])), "ts": _ts(r["time_tag"])}


def _series(result):
    """InfluxQL JSON -> [(tags, columns, values)], tolerating empty results."""
    out = []
    for res in (result or {}).get("results") or []:
        for s in res.get("series") or []:
            out.append((s.get("tags") or {}, s.get("columns") or [], s.get("values") or []))
    return out


def parse_heard(by_source, by_region):
    """Two InfluxQL results -> {band: {n, best_snr, max_km, sources, regions}}.

    by_source: count/max(snr)/max(dist_km) GROUP BY band, source
    by_region: count(snr) GROUP BY band, region
    """
    bands = {}
    for tags, cols, vals in _series(by_source):
        band = tags.get("band")
        if not band or not vals:
            continue
        row = dict(zip(cols, vals[0]))
        n = int(row.get("n") or 0)
        if n <= 0:
            continue
        b = bands.setdefault(band, {"n": 0, "best_snr": None, "max_km": None, "sources": {}, "regions": []})
        b["n"] += n
        b["sources"][tags.get("source") or "?"] = n
        best, far = row.get("best"), row.get("far")
        if best is not None and (b["best_snr"] is None or best > b["best_snr"]):
            b["best_snr"] = int(round(best))
        if far is not None and (b["max_km"] is None or far > b["max_km"]):
            b["max_km"] = int(round(far))
    regions = {}
    for tags, cols, vals in _series(by_region):
        band, region = tags.get("band"), tags.get("region")
        if not band or not region or region.upper() == "UNKNOWN" or not vals:
            continue
        n = int(dict(zip(cols, vals[0])).get("count") or 0)
        if n > 0:
            regions.setdefault(band, []).append((region, n))
    for band, rs in regions.items():
        if band in bands:
            bands[band]["regions"] = [[r, n] for r, n in sorted(rs, key=lambda x: -x[1])[:8]]
    return bands


def _series_all(res):
    """(tags, columns, values) for every series of every statement."""
    for st in (res or {}).get("results") or []:
        for se in st.get("series") or []:
            yield se.get("tags") or {}, se.get("columns") or [], se.get("values") or []


def parse_forecast(res, issued):
    """Open forecast cells (InfluxQL, epoch seconds, GROUP BY band, region)
    -> windows [{band, region, start, end, peak}], soonest first.

    Only cells the newest issue wrote count (issued_ts == issued): Influx
    keeps a cell an older issue wrote until a newer one overwrites it, and a
    cell that dropped out of the trust gate is never overwritten. Consecutive
    hours of one band and region are one window; end is exclusive."""
    out = []
    for tags, cols, vals in _series_all(res):
        band, region = tags.get("band"), tags.get("region")
        if not band or not region:
            continue
        hours = []
        for v in vals:
            row = dict(zip(cols, v))
            t, p, iss = row.get("time"), row.get("prob"), row.get("issued_ts")
            if t is None or p is None or iss is None or abs(float(iss) - float(issued)) > 0.5:
                continue
            hours.append((int(t), float(p)))
        hours.sort()
        for t, p in hours:
            if out and out[-1]["band"] == band and out[-1]["region"] == region and out[-1]["end"] == t:
                out[-1]["end"] = t + 3600
                out[-1]["peak"] = max(out[-1]["peak"], round(p, 2))
            else:
                out.append({"band": band, "region": region, "start": t, "end": t + 3600, "peak": round(p, 2)})
    out.sort(key=lambda w: (w["start"], -w["peak"]))
    return out[:FORECAST_MAX]


def band_of(hz):
    for name, lo, hi in BAND_EDGES:
        if lo <= hz <= hi:
            return name
    return None


def grid_latlon(g):
    """Maidenhead locator (4, 6 or 8 characters) -> (lat, lon) of its centre, or None."""
    g = (g or "").strip()
    if len(g) < 4 or not re.match(r"^[A-Ra-r]{2}\d{2}", g):
        return None
    g = g[:2].upper() + g[2:4] + g[4:6].lower() + g[6:8]
    lon = (ord(g[0]) - 65) * 20 - 180 + int(g[2]) * 2
    lat = (ord(g[1]) - 65) * 10 - 90 + int(g[3])
    dlon, dlat = 2.0, 1.0
    if len(g) >= 6 and re.match(r"^[a-x]{2}$", g[4:6]):
        dlon, dlat = 2 / 24, 1 / 24
        lon += (ord(g[4]) - 97) * dlon
        lat += (ord(g[5]) - 97) * dlat
        if len(g) >= 8 and g[6:8].isdigit():
            dlon, dlat = dlon / 10, dlat / 10
            lon += int(g[6]) * dlon
            lat += int(g[7]) * dlat
    return lat + dlat / 2, lon + dlon / 2


def km_between(a, b):
    p1, l1, p2, l2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    c = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(l2 - l1)
    return 6371.0 * math.acos(max(-1.0, min(1.0, c)))


def parse_psk(xml_text):
    """PSKReporter receptionReports XML -> {band: {spots, receivers, best_snr, max_km, countries}}."""
    root = ET.fromstring(xml_text)
    bands, rx = {}, {}
    for r in root.iter("receptionReport"):
        try:
            hz = float(r.get("frequency") or 0)
        except ValueError:
            continue
        band = band_of(hz)
        if not band:
            continue
        b = bands.setdefault(band, {"spots": 0, "receivers": 0, "best_snr": None, "max_km": None, "countries": []})
        b["spots"] += 1
        rx.setdefault(band, {})
        call = r.get("receiverCallsign") or "?"
        country = r.get("receiverDXCC") or ""
        rx[band][call] = country
        snr = r.get("sNR")
        if snr not in (None, "") and re.fullmatch(r"-?\d+", snr):
            snr = int(snr)
            if b["best_snr"] is None or snr > b["best_snr"]:
                b["best_snr"] = snr
        a, t = grid_latlon(r.get("senderLocator")), grid_latlon(r.get("receiverLocator"))
        if a and t:
            km = int(round(km_between(a, t)))
            if b["max_km"] is None or km > b["max_km"]:
                b["max_km"] = km
    for band, calls in rx.items():
        bands[band]["receivers"] = len(calls)
        counts = {}
        for country in calls.values():
            if country:
                counts[country] = counts.get(country, 0) + 1
        bands[band]["countries"] = [[c, n] for c, n in sorted(counts.items(), key=lambda x: -x[1])[:4]]
    return bands


# --------------------------------------------------------------------- add-on

class Propagation(Addon):
    name = "propagation"
    label = "Propagation"
    title = "Space weather and what the bands are doing (heard in the last hour)"
    css = True

    def __init__(self, host, opts):
        super().__init__(host, opts)
        self.use_swpc = opts.get("swpc", True) is not False
        sw = opts.get("skywatch") or {}
        self.sw_url = str(sw.get("url") or "").rstrip("/")
        self.sw_db = str(sw.get("db") or "skywatch")
        try:
            w = int(opts.get("window_min", 60))
        except (TypeError, ValueError):
            w = 60
        self.window_min = max(WINDOW_MIN[0], min(WINDOW_MIN[1], w))
        try:
            h = int(opts.get("forecast_h", 12))
        except (TypeError, ValueError):
            h = 12
        self.forecast_h = max(FORECAST_H[0], min(FORECAST_H[1], h))
        self._last_forecast = 0.0
        call = str((opts.get("pskreporter") or {}).get("call") or "").strip().upper()
        self.psk_call = call if CALL.match(call) else ""
        self._last_psk = 0.0
        self._session = None
        self._task = None
        self._last_swpc = 0.0
        self._reset()

    def _reset(self):
        self.space = {"kp": None, "xray": None, "f107": None, "error": None, "checked": None}
        self.heard = {"bands": {}, "error": None, "checked": None}
        self.psk = {"bands": {}, "error": None, "checked": None}
        self.forecast = {"windows": [], "issued": None, "error": None, "checked": None}

    async def start(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15, sock_connect=5),
                                              headers={"User-Agent": "ranch-radio propagation add-on"})
        self._last_swpc = 0.0
        self._last_psk = 0.0
        self._last_forecast = 0.0
        self._task = asyncio.create_task(self._run(), name="propagation-poll")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._session:
            await self._session.close()
            self._session = None
        self._reset()

    async def _get_json(self, url, params=None):
        async with self._session.get(url, params=params) as r:
            if r.status != 200:
                raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status,
                                                  message=f"HTTP {r.status}")
            return await r.json(content_type=None)

    async def _poll_swpc(self):
        new = dict(self.space)
        try:
            kp, xr, f107 = await asyncio.gather(self._get_json(URL_KP), self._get_json(URL_XRAY),
                                                self._get_json(URL_F107))
            new.update(kp=parse_kp(kp), xray=parse_xray(xr), f107=parse_f107(f107), error=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, TypeError) as e:
            log.info("NOAA space weather: %s", e)
            new["error"] = "NOAA not reachable"     # keep the last good values, marked by their age
        new["checked"] = int(time.time())
        return new

    async def _poll_skywatch(self):
        w = f"time > now() - {self.window_min}m"
        q_src = (f'SELECT count(snr) AS n, max(snr) AS best, max(dist_km) AS far FROM "{MEASUREMENT}" '
                 f"WHERE {w} GROUP BY band, source")
        q_reg = f'SELECT count(snr) FROM "{MEASUREMENT}" WHERE {w} GROUP BY band, region'
        url = f"{self.sw_url}/query"
        try:
            src, reg = await asyncio.gather(self._get_json(url, {"db": self.sw_db, "q": q_src}),
                                            self._get_json(url, {"db": self.sw_db, "q": q_reg}))
            for res in (src, reg):
                err = next((x.get("error") for x in res.get("results") or [] if x.get("error")), None)
                if err:
                    raise ValueError(err)
            return {"bands": parse_heard(src, reg), "error": None, "checked": int(time.time())}
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, TypeError, AttributeError) as e:
            log.info("skywatch %s: %s", self.sw_url, e)
            # Unknown is not zero: drop the counts rather than show stale ones as current.
            return {"bands": {}, "error": "skywatch not reachable", "checked": int(time.time())}

    async def _poll_forecast(self):
        # The current hour too: its cell started up to 59 minutes ago.
        w = f"time >= now() - 1h AND time < now() + {self.forecast_h}h"
        q_new = f'SELECT max(issued_ts) FROM "{FORECAST}" WHERE {w}'
        q_open = f'SELECT prob, clim_level, issued_ts FROM "{FORECAST}" WHERE open = 1 AND {w} GROUP BY band, region'
        url = f"{self.sw_url}/query"
        try:
            new, opened = await asyncio.gather(
                self._get_json(url, {"db": self.sw_db, "q": q_new, "epoch": "s"}),
                self._get_json(url, {"db": self.sw_db, "q": q_open, "epoch": "s"}))
            for res in (new, opened):
                err = next((x.get("error") for x in res.get("results") or [] if x.get("error")), None)
                if err:
                    raise ValueError(err)
            issued = next((v[0][1] for _, c, v in _series_all(new) if v and v[0][1] is not None), None)
            windows = parse_forecast(opened, issued) if issued else []
            return {"windows": windows, "issued": int(issued) if issued else None, "error": None,
                    "checked": int(time.time())}
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, TypeError, AttributeError) as e:
            log.info("skywatch forecast %s: %s", self.sw_url, e)
            return {"windows": [], "issued": None, "error": "skywatch not reachable", "checked": int(time.time())}

    async def _poll_psk(self):
        params = {"senderCallsign": self.psk_call, "flowStartSeconds": -60 * self.window_min,
                  "rronly": 1, "noactive": 1}
        try:
            async with self._session.get(URL_PSK, params=params) as r:
                if r.status != 200:
                    raise ValueError(f"HTTP {r.status}")
                text = await r.text()
            return {"bands": parse_psk(text), "error": None, "checked": int(time.time())}
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, ET.ParseError) as e:
            log.info("PSKReporter: %s", e)
            return {"bands": {}, "error": "PSKReporter not reachable", "checked": int(time.time())}

    async def _run(self):
        while True:
            before = self.snapshot()
            now = time.time()
            if self.use_swpc and now - self._last_swpc >= SWPC_EVERY_S:
                self.space = await self._poll_swpc()
                self._last_swpc = now
            if self.sw_url:
                self.heard = await self._poll_skywatch()
            if self.sw_url and now - self._last_forecast >= FORECAST_EVERY_S:
                self.forecast = await self._poll_forecast()
                self._last_forecast = now
            if self.psk_call and now - self._last_psk >= PSK_EVERY_S:
                self.psk = await self._poll_psk()
                self._last_psk = now
            after = self.snapshot()
            if _changed(before, after):
                self.emit({"state": after})
            await asyncio.sleep(TICK_S)

    def snapshot(self):
        now = time.time()
        kp = self.space.get("kp")
        return {
            "swpc": self.use_swpc,
            "skywatch": bool(self.sw_url),
            "window_min": self.window_min,
            "kp": kp,
            "kp_stale": bool(kp and kp.get("ts") and now - kp["ts"] > KP_STALE_S),
            "xray": self.space.get("xray"),
            "f107": self.space.get("f107"),
            "space_error": self.space.get("error"),
            "bands": self.heard.get("bands") or {},
            "heard_error": self.heard.get("error"),
            "heard_checked": self.heard.get("checked"),
            "pskreporter": self.psk_call,
            "psk": self.psk.get("bands") or {},
            "psk_error": self.psk.get("error"),
            "psk_checked": self.psk.get("checked"),
            "forecast_h": self.forecast_h,
            "forecast": self.forecast.get("windows") or [],
            "forecast_issued": self.forecast.get("issued"),
            "forecast_error": self.forecast.get("error"),
        }


def _changed(a, b):
    """Edge-triggered: say it again only when something a page shows changed."""
    keys = ("kp", "kp_stale", "xray", "f107", "space_error", "bands", "heard_error", "psk", "psk_error",
            "forecast", "forecast_issued", "forecast_error")
    return any(a.get(k) != b.get(k) for k in keys)


ADDON = Propagation
