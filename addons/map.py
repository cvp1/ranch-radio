"""Map add-on: the grids you have worked and confirmed (on the band JTDX is
on, or all), who JTDX is hearing now at their grids (coloured by need, click
to answer, as on the Roster tab), your own grid, the gray line, and POTA
activators spotted on the current band.

The grids and decodes come from the logbook sync (logsync.py: its ledger and
its roster). POTA spots are fetched from api.pota.app every POTA_EVERY_S
while the tab is on (a public, unauthenticated feed; nothing of ours is sent);
a failed fetch keeps the last spots and says so. The map tiles are
OpenStreetMap's, fetched by the browser as on the Mesh map.

Options (the host file's addons block):

    "map": {"enabled": true}
    "map": {"enabled": true, "pota": false}      no POTA spots
"""
import asyncio
import logging
import time

from . import Addon

log = logging.getLogger("addons.map")
POTA_URL = "https://api.pota.app/spot/activator"
POTA_EVERY_S = 60
POTA_MAX_BYTES = 2 << 20
BANDS_KHZ = (("160m", 1800, 2000), ("80m", 3500, 4000), ("60m", 5250, 5450), ("40m", 7000, 7300), ("30m", 10100, 10150),
             ("20m", 14000, 14350), ("17m", 18068, 18168), ("15m", 21000, 21450), ("12m", 24890, 24990),
             ("10m", 28000, 29700), ("6m", 50000, 54000), ("2m", 144000, 148000))


def band_khz(khz):
    try:
        f = float(khz)
    except (TypeError, ValueError):
        return ""
    return next((b for b, lo, hi in BANDS_KHZ if lo <= f <= hi), "")


def pota_spots(items):
    """api.pota.app activator spots -> the few fields the map draws."""
    out = []
    for s in items if isinstance(items, list) else []:
        try:
            lat, lon = float(s["latitude"]), float(s["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"call": str(s.get("activator", ""))[:16], "ref": str(s.get("reference", ""))[:12],
                    "name": str(s.get("name") or "")[:60], "mode": str(s.get("mode", ""))[:8],
                    "khz": str(s.get("frequency", ""))[:10], "band": band_khz(s.get("frequency")),
                    "grid": str(s.get("grid4") or "")[:4], "lat": round(lat, 3), "lon": round(lon, 3)})
    return out


class Map(Addon):
    name = "map"
    label = "Map"
    title = "Worked and confirmed grids, who JTDX hears, gray line, POTA"
    group = "tools"
    css = True

    def __init__(self, host, opts):
        super().__init__(host, opts)
        self.want_pota = opts.get("pota", True) is not False
        self.pota, self.pota_at, self.pota_error = [], 0.0, None
        self.showing = None             # None: the current band; "all"; or a band name
        self._task = None

    async def start(self):
        if self.want_pota:
            self._task = asyncio.ensure_future(self._pota_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _pota_loop(self):
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20),
                                         headers={"User-Agent": "RanchRadio-map/1"}) as http:
            while True:
                try:
                    async with http.get(POTA_URL) as r:
                        if r.status != 200:
                            raise ValueError(f"HTTP {r.status}")
                        raw = await r.content.read(POTA_MAX_BYTES + 1)
                    if len(raw) > POTA_MAX_BYTES:
                        raise ValueError("too large")
                    import json
                    self.pota, self.pota_at, self.pota_error = pota_spots(json.loads(raw)), time.time(), None
                except Exception as e:           # keep the last spots; say why
                    self.pota_error = f"POTA spots: {type(e).__name__}"
                    log.warning("map: %s", self.pota_error)
                self.push()
                await asyncio.sleep(POTA_EVERY_S)

    def _sync(self):
        return getattr(self.host, "logsync", None)

    def snapshot(self):
        ls = self._sync()
        if ls is None:
            return {"error": "the logbook sync isn't running on this station, so there's no map data"}
        mv = ls.map_view(self.showing)
        b = mv["showing"]
        return {"map": mv, "pota": [s for s in self.pota if b == "all" or s["band"] == b] if self.want_pota else None,
                "pota_age": round(time.time() - self.pota_at) if self.pota_at else None, "pota_error": self.pota_error}

    def push(self):
        if self._sync() is not None:
            self.emit(self.snapshot())

    async def command(self, ws, verb, m):
        ls = self._sync()
        if verb == "show":
            b = str(m.get("band") or "")
            self.showing = None if b in ("", "current") else b[:6]
            self.emit(self.snapshot(), ws)
        elif verb == "reply":
            if ls is None:
                self.notice("The logbook sync isn't running, so the map can't answer.", "warn", ws)
                return
            why = ls.reply(str(m.get("key", "")))
            self.notice(f"Map: {why}" if why else f"Asked JTDX to answer {m.get('call') or 'that station'}.",
                        "warn" if why else "info", ws)
        else:
            await super().command(ws, verb, m)


ADDON = Map
