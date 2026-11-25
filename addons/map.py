"""Map add-on: who you hear, who hears you, and what the station is doing
right now, on one map.

- **You hear**: every station JTDX decoded with a grid, kept for an hour
  (the roster itself forgets after two minutes), coloured by need; a live one
  can be answered from its card, as on the Roster tab.
- **Hears you**: the stations that reported your callsign to PSKReporter, one
  by one at their own locators, with the SNR they gave you and when. They come
  from the Propagation add-on's poll (its "pskreporter": {"call"}): one query
  serves both tabs, and PSKReporter asks for no more than one every five
  minutes, so this is minutes old by nature and every station carries its age.
- **Two-way**: a station in both lists on the same band; you can work it.
- **On the air**: what JTDX's Status says (transmitting, listening, calling
  whom) and what each radio's own server says (keyed or not, frequency, mode),
  pushed the moment either changes. Nothing here keys or tunes a radio.
- The grids you have worked and confirmed (on the band JTDX is on, or all),
  your own grid, the gray line, and POTA activators spotted on the band.

The grids, decodes and JTDX's state come from the logbook sync (logsync.py:
its ledger, roster and rig_view); the radios from the station's watches
(station.rigs_view). POTA spots are fetched from api.pota.app every
POTA_EVERY_S while the tab is on (a public, unauthenticated feed; nothing of
ours is sent); a failed fetch keeps the last spots and says so. The map's
background is the operator's pick (static/basemaps.js), fetched by the browser
as on the Mesh map.

Options (the host file's addons block):

    "map": {"enabled": true}
    "map": {"enabled": true, "pota": false}      no POTA spots
    "map": {"enabled": true, "history": "~/path/to/two-way.html"}

**History** ("history": a file path): a page of your own that covers longer than
the hour this map keeps (e.g. every medium you hear and that hears you, built
daily elsewhere), shown on the tab's History view and served at /map/history.
It is whatever that file holds, often inline script and text heard over the
air, so it is never served as part of the station: the response carries its
own policy, a CSP sandbox WITHOUT allow-same-origin. The browser runs it in an
opaque origin: no station cookies, no station API (a request from it says
"Origin: null", which webguard refuses), no forms, no fetches; images (map
tiles) and links out only; framed only by the station. The file is read on
each request (rebuilt daily, nothing cached), at most HISTORY_MAX_BYTES.
"""
import asyncio
import logging
import os
import time

from aiohttp import web

from . import Addon

log = logging.getLogger("addons.map")
POTA_URL = "https://api.pota.app/spot/activator"
POTA_EVERY_S = 60
POTA_MAX_BYTES = 2 << 20
HEARD_KEEP_S = 3600              # a station you heard stays on the map this long
HEARD_MAX = 500                  # and at most this many (the oldest go first)
TICK_S = 20                      # how often a new PSKReporter answer is looked for
RIG_PUSH_S = 0.1                 # what is on the air goes to the pages at most this often
HISTORY_MAX_BYTES = 30 << 20     # the History page is read whole; past this it is refused, not truncated
# The History page's own policy (see the docstring): sandboxed without allow-same-origin, so its
# inline script runs in an opaque origin that can reach nothing of the station's.
HISTORY_CSP = "; ".join((
    "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox",
    "default-src 'none'",
    "script-src 'unsafe-inline'",
    "style-src 'unsafe-inline'",
    "img-src data: https:",
    "connect-src 'none'",
    "form-action 'none'",
    "base-uri 'none'",
    "frame-ancestors 'self'",
))
NO_PSK = ('To see who hears you, switch on the Propagation add-on and give it your callsign '
          '("pskreporter": {"call": "N0CALL"}): the stations come from PSKReporter.')
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
    title = "Who you hear, who hears you and what is on the air; worked grids, gray line, POTA"
    group = "tools"
    css = True

    def __init__(self, host, opts):
        super().__init__(host, opts)
        self.want_pota = opts.get("pota", True) is not False
        h = opts.get("history")
        self.history = os.path.expanduser(h) if isinstance(h, str) and h else None
        self.pota, self.pota_at, self.pota_error = [], 0.0, None
        self.showing = None             # None: the current band; "all"; or a band name
        self.heard = {}                 # (call, band) -> the station as last heard (HEARD_KEEP_S)
        self.clock = time.time
        self._psk_sent = None           # the PSKReporter answer the pages have: (checked, error)
        self._rig_handle = None         # a push of what is on the air, waiting RIG_PUSH_S (push_rig)
        self._tasks = []

    async def start(self):
        self._tasks = [asyncio.ensure_future(self._tick_loop())]
        if self.want_pota and os.environ.get("RANCH_RADIO_OFFLINE") != "1":   # tests: never the POTA API
            self._tasks.append(asyncio.ensure_future(self._pota_loop()))

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._rig_handle is not None:
            self._rig_handle.cancel()
            self._rig_handle = None
        self.heard = {}

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
                if self._sync() is not None:
                    self.emit(self._pota_part(self._sync().map_view(self.showing)["showing"]))
                await asyncio.sleep(POTA_EVERY_S)

    async def _tick_loop(self):
        """A new PSKReporter answer (the Propagation add-on polls it) goes to
        the pages once; nothing is sent while it hasn't changed."""
        while True:
            await asyncio.sleep(TICK_S)
            try:
                self.push_hears()
            except Exception as e:               # a broken neighbour must not stop the map
                log.warning("map: who hears you: %s", type(e).__name__)

    def _sync(self):
        return getattr(self.host, "logsync", None)

    # --- who hears you: the Propagation add-on's PSKReporter stations

    def hears_me(self):
        """(dict | None, why): {call, window_min, every_s, receivers, error,
        checked}, or None and what to set up."""
        reg = getattr(self.host, "addons", None)
        prop = reg.get("propagation") if reg and reg.is_on("propagation") else None
        hb = prop.heard_by() if prop is not None and hasattr(prop, "heard_by") else None
        return (hb, None) if hb else (None, NO_PSK)

    def _hears_part(self):
        hb, why = self.hears_me()
        return {"now": self.clock(), "hears_me": hb, "hears_me_why": why}

    def push_hears(self):
        hb, _ = self.hears_me()
        stamp = (hb["checked"], hb["error"]) if hb else None
        if stamp != self._psk_sent and self._sync() is not None:
            self._psk_sent = stamp
            self.emit(self._hears_part())

    # --- who you hear: the roster's decodes, remembered for an hour

    def _remember(self, decodes):
        now = self.clock()
        for d in decodes:
            k = (d["call"], d.get("band") or "")
            old = self.heard.get(k)
            new = old is None or old["key"] != d["key"]
            self.heard[k] = {"call": d["call"], "grid": d["grid"], "band": d.get("band") or "", "mode": d.get("mode") or "",
                             "snr": d["snr"], "best": max(d["snr"], old["best"]) if old else d["snr"],
                             "t": round(now - (d.get("age") or 0)), "n": (old["n"] if old else 0) + (1 if new else 0),
                             "need": d.get("need"), "entity": d.get("entity") or "", "km": d.get("km"),
                             "bearing": d.get("bearing"), "key": d["key"]}
        for k in [k for k, h in self.heard.items() if now - h["t"] > HEARD_KEEP_S]:
            del self.heard[k]
        if len(self.heard) > HEARD_MAX:
            for k in sorted(self.heard, key=lambda k: self.heard[k]["t"])[:len(self.heard) - HEARD_MAX]:
                del self.heard[k]

    def _heard_list(self):
        return [{k: v for k, v in h.items() if k != "key"}
                for h in sorted(self.heard.values(), key=lambda h: -h["t"])]

    # --- what the pages get, whole (a page opening) and in parts (as each thing changes)

    def _map_part(self):
        mv = self._sync().map_view(self.showing)
        self._remember(mv["decodes"])
        return {"now": self.clock(), "map": mv, "heard": self._heard_list()}

    def _rig_part(self):
        ls, view = self._sync(), getattr(self.host, "rigs_view", None)
        return {"now": self.clock(), "rig": ls.rig_view() if ls is not None and hasattr(ls, "rig_view") else {"alive": False},
                "radios": view() if view else []}

    def _pota_part(self, band):
        return {"pota": [s for s in self.pota if band == "all" or s["band"] == band] if self.want_pota else None,
                "pota_age": round(time.time() - self.pota_at) if self.pota_at else None, "pota_error": self.pota_error}

    def _history_part(self):
        """Whether the History view has a page to show, and when it was built."""
        if not self.history:
            return {"history": None}
        try:
            st = os.stat(self.history)
        except OSError:
            return {"history": {"ok": False, "why": "the History page isn't there yet"}}
        return {"history": {"ok": True, "built": int(st.st_mtime)}}

    def snapshot(self):
        if self._sync() is None:
            return {"error": "the logbook sync isn't running on this station, so there's no map data",
                    **self._history_part()}
        part = self._map_part()
        return {**part, **self._rig_part(), **self._hears_part(), **self._pota_part(part["map"]["showing"]),
                **self._history_part()}

    def http(self, app, authorised):
        async def history(request):
            if not self.history:
                return web.json_response({"error": "no History page is set (the host file's addons.map.history)"},
                                         status=404)
            try:
                size = os.path.getsize(self.history)
                if size > HISTORY_MAX_BYTES:
                    return web.json_response({"error": f"the History page is {size >> 20} MB, over the "
                                                       f"{HISTORY_MAX_BYTES >> 20} MB this serves"}, status=413)
                with open(self.history, "rb") as f:
                    body = f.read(HISTORY_MAX_BYTES + 1)
            except OSError:
                return web.json_response({"error": "the History page isn't there yet"}, status=404)
            return web.Response(body=body, content_type="text/html", charset="utf-8",
                                headers={"Content-Security-Policy": HISTORY_CSP, "Cache-Control": "no-store"})

        app.router.add_get("/map/history", history)

    def push(self):
        """New decodes on the roster (station.roster_changed)."""
        if self._sync() is not None:
            self.emit(self._map_part())

    def push_rig(self):
        """A radio or JTDX changed what it is doing (station.rig_changed).
        One push per RIG_PUSH_S: a VFO being turned changes the frequency many
        times a second, and keying is not late at a tenth of one."""
        if self._sync() is None or self._rig_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:                 # no loop (a test calling it directly): now
            self.emit(self._rig_part())
            return

        def fire():
            self._rig_handle = None
            if self._sync() is not None:
                self.emit(self._rig_part())
        self._rig_handle = loop.call_later(RIG_PUSH_S, fire)

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
            why = ls.reply(str(m.get("key", "")), done=lambda ok, msg: self.notice(f"Map: {msg}", "info" if ok else "warn", ws))
            if why:
                self.notice(f"Map: {why}", "warn", ws)
        else:
            await super().command(ws, verb, m)


ADDON = Map
