"""Imaging add-on: pictures off the air. SSTV (slow-scan TV: hams, and the
ISS's ARISS events) and HF radiofax (the Coast Guard's and other weather
services' charts), decoded from one radio's receive audio.

Receive only. It listens to the radio's /audio exactly as a page does
(ws://127.0.0.1:<web>/audio on the radio's own server, with the station's
token) and never sends anything back: no microphone audio, no CAT, no
tuning. Tune the radio yourself: SSTV on 14.230 USB (20 m) or 7.171 LSB
(40 m), 145.800 FM for the ISS; fax on USB with the dial 1.9 kHz below the
published carrier (Pt. Reyes NMC 8682.0 -> 8680.1 USB). While the radio
transmits, nothing is decoded.

A picture whose VIS header never came through (weak, or joined late) is
still found: every 20 s the SSTV decoder folds the last picture's length of
audio at each mode's line period on a worker thread (sstv.find_headerless)
and saves what passes as <...>-noheader.png. Off with "sstv_scan": false.

Decoding is sstv.py and wefax.py (pure, numpy), fed by one
picture.Discriminator. SSTV pictures start at their VIS header and are
slant-corrected from their sync pulses. A fax chart starts at the start
tone and is lined up from its phasing lines (its slant: `clock_ppm`); joined
mid-chart, the panel's "Start fax" begins one now (its left edge lands
where it lands). Stop saves what came in so far.

Pictures are PNGs in <state dir>/imaging/ (beside addons.json), named
<UTC time>-<sstv|fax>-<mode>-<kHz>k-<status>.png, newest first in the panel,
the newest `keep` kept (older ones are deleted as new ones arrive). The
picture coming in is shown as it arrives (live-sstv.png / live-fax.png).
Served at /imaging/img/<name> behind the station's token.

While on, the radio's audio capture runs all the time (a page listening does
the same). On Linux give the radio a pipewire: audio device so JTDX can
share the sound card; macOS's CoreAudio shares it anyway.

Needs numpy in the station's Python (`ranch-radio doctor --fix` installs
it). Without it the add-on shows "not loaded" with that reason, and the
station runs on.

Options (the host file's addons block):

    "imaging": {"enabled": true,
                "radio": "ic7300",        # which radio to listen to (default: the first)
                "sstv": true, "fax": true, # which decoders run
                "sstv_modes": ["pd120", "martin1"],   # default: all
                "sstv_scan": true,        # also find pictures whose VIS header never came through
                "fax_lpm": 120, "fax_ioc": 576,
                "clock_ppm": 0,           # the sound card's clock error, for fax (SSTV measures its own)
                "keep": 100}              # pictures kept
"""
import asyncio
import datetime
import logging
import os
import re

import aiohttp
from aiohttp import web

from . import Addon

log = logging.getLogger("addons.imaging")

CHUNK_BYTES = 3200                    # 100 ms of 16 kHz s16le: one decode step
RETRY_S = 5
MIN_ROWS = {"sstv": 16, "fax": 20}    # fewer than this is a false start, not a picture
LIVE = {"sstv": "live-sstv.png", "fax": "live-fax.png"}
NAME = re.compile(r"^(?:live-(?:sstv|fax)|\d{8}T\d{6}Z-(?:sstv|fax)-[a-z0-9]+-(?:\d+k|nok)-[a-z]+)\.png$")
LIST_MAX = 60


class Imaging(Addon):
    name = "imaging"
    label = "Imaging"
    title = "Pictures off the air: SSTV and weather fax from a radio's receive audio"
    group = "tools"
    css = True

    def __init__(self, host, opts):
        super().__init__(host, opts)
        try:
            import picture
            import sstv
            import wefax
        except ImportError as e:
            raise ValueError(f"needs numpy in the station's Python ({e}); ranch-radio doctor --fix installs it") from e
        self._picture, self._sstv, self._wefax = picture, sstv, wefax
        o = dict(opts)
        self.radio_id = o.pop("radio", None)
        if self.radio_id is not None and not isinstance(self.radio_id, str):
            raise ValueError("radio: a radio's id, as a string")
        ids = [r.id for r in getattr(host, "radios", [])]
        if self.radio_id is not None and ids and self.radio_id not in ids:
            raise ValueError(f"radio: {self.radio_id!r} is not one of this station's radios ({', '.join(ids)})")
        self.want = {"sstv": o.pop("sstv", True), "fax": o.pop("fax", True)}
        for k, v in self.want.items():
            if not isinstance(v, bool):
                raise ValueError(f"{k}: true or false")
        if not any(self.want.values()):
            raise ValueError("sstv, fax: at least one must be on")
        self.scan = o.pop("sstv_scan", True)
        if not isinstance(self.scan, bool):
            raise ValueError("sstv_scan: true or false")
        self.modes = o.pop("sstv_modes", None)
        if self.modes is not None and (not isinstance(self.modes, list) or not self.modes
                                       or any(m not in sstv.BY_NAME for m in self.modes)):
            raise ValueError(f"sstv_modes: a list of {', '.join(sstv.BY_NAME)}")
        self.fax_opts = {"lpm": o.pop("fax_lpm", 120), "ioc": o.pop("fax_ioc", 576),
                         "clock_ppm": o.pop("clock_ppm", 0)}
        for k in ("lpm", "ioc"):
            if not isinstance(self.fax_opts[k], int) or isinstance(self.fax_opts[k], bool):
                raise ValueError(f"fax_{k}: a whole number")
        if not isinstance(self.fax_opts["clock_ppm"], (int, float)) or isinstance(self.fax_opts["clock_ppm"], bool):
            raise ValueError("clock_ppm: a number")
        self.keep = o.pop("keep", 100)
        if not isinstance(self.keep, int) or isinstance(self.keep, bool) or not 1 <= self.keep <= 1000:
            raise ValueError("keep: 1..1000 pictures")
        if o:
            raise ValueError(f"{', '.join(sorted(o))}: not an Imaging option")
        try:
            wefax.FaxDecoder(**self.fax_opts)          # its own checks, named
        except ValueError as e:
            raise ValueError(str(e).replace("fax lpm", "fax_lpm").replace("fax ioc", "fax_ioc")) from e
        self._task = None
        self._scan_task = None
        self.error = None
        self.listening = False
        self.live = {}                     # kind -> {"name", "v", "mode", "label", "lines", "total"}
        self._new_decoders()

    def _new_decoders(self):
        self.disc = self._picture.Discriminator()
        self.dec = {}
        if self.want["sstv"]:
            self.dec["sstv"] = self._sstv.SstvDecoder(self.modes, scan=self.scan)
        if self.want["fax"]:
            self.dec["fax"] = self._wefax.FaxDecoder(**self.fax_opts)
        self._pending = bytearray()

    # --- where things are

    @property
    def folder(self):
        base = self.state_dir or os.path.expanduser("~/.local/state/radio-remote")
        return os.path.join(base, "imaging")

    def _radio(self):
        radios = list(getattr(self.host, "radios", []))
        if self.radio_id:
            return next((r for r in radios if r.id == self.radio_id), None)
        return radios[0] if radios else None

    def _rig(self, r):
        w = getattr(self.host, "watches", {}).get(r.id) if r is not None else None
        return getattr(w, "rig", {}) or {}

    # --- on / off

    async def start(self):
        self._new_decoders()
        self.live = {}
        self._task = asyncio.ensure_future(self._run())

    async def stop(self):
        st, self._scan_task = self._scan_task, None
        if st is not None:
            st.cancel()
        t, self._task = self._task, None
        if t is not None:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):     # noqa: BLE001 (stopping must not raise)
                pass
        self.listening = False
        self.live = {}

    def degraded(self):
        return [f"imaging: {self.error}"] if self.error else []

    async def _run(self):
        while True:
            r = self._radio()
            try:
                if r is None:
                    raise ConnectionError("this station has no radio to listen to")
                if not r.present:
                    raise ConnectionError(f"{r.name} isn't up")
                url = f"http://127.0.0.1:{r.web}/audio"
                if getattr(self.host, "token", None):
                    url += f"?token={self.host.token}"
                auth = getattr(self.host, "auth", None)
                headers = auth.bearer_headers() if auth is not None else {}
                async with aiohttp.ClientSession() as s, \
                        s.ws_connect(url, heartbeat=5, headers=headers, max_msg_size=1 << 16) as ws:
                    self._set(None, True)
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            await self._audio(msg.data, r)
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                            break
                raise ConnectionError(f"{r.name}'s audio closed")
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, ConnectionError, OSError, asyncio.TimeoutError) as e:
                self._set(str(e) or type(e).__name__, False)
            except Exception as e:                               # noqa: BLE001 (a decoder bug must not kill the loop silently)
                log.exception("imaging: %s", e)
                self._set(f"decoder error: {type(e).__name__}: {e}", False)
                self._new_decoders()
            await asyncio.sleep(RETRY_S)

    def _set(self, error, listening):
        if (error, listening) != (self.error, self.listening):
            self.error, self.listening = error, listening
            self.emit({"event": "status", "error": error, "listening": listening})

    # --- audio in, pictures out

    async def _audio(self, data, r):
        np = self._picture.np
        self._pending += data
        if len(self._pending) < CHUNK_BYTES:
            return
        n = len(self._pending) // 2 * 2
        pcm = np.frombuffer(bytes(self._pending[:n]), dtype="<i2")
        del self._pending[:n]
        if self._rig(r).get("tx"):
            return                     # our own transmission is nobody's picture
        f = self.disc.feed(pcm)
        for kind, dec in self.dec.items():
            for ev in dec.feed(f):
                await self._event(kind, ev, r)
        d = self.dec.get("sstv")
        if d is not None and self._scan_task is None and d.scan_due():
            self._scan_task = asyncio.ensure_future(self._scan(d, d.scan_snapshot(), r))

    async def _scan(self, d, snap, r):
        """The headerless scan, on a worker thread (0.5..0.9 s each, every
        20 s, measured 2026-11-26), then its finds back on the loop."""
        try:
            found = await asyncio.to_thread(self._sstv.find_headerless, *snap)
            if d is self.dec.get("sstv"):                     # not replaced while it ran
                for ev in d.scan_apply(found, snap[2]):
                    await self._event("sstv", ev, r)
        except asyncio.CancelledError:
            raise
        except Exception as e:                                # noqa: BLE001 (a scan bug must not stop the decoders)
            log.exception("imaging: headerless scan: %s", e)
        finally:
            self._scan_task = None

    async def _event(self, kind, ev, r):
        e = ev["event"]
        if e == "start":
            self.live[kind] = {"name": None, "v": 0, "mode": ev["mode"], "label": ev["label"],
                               "lines": 0, "total": ev.get("lines")}
            label = ev["label"] if ev.get("header", True) else f"{ev['label']} (found without its header)"
            self.emit({"event": "start", "kind": kind, "label": label})
        elif e == "progress":
            live = self.live.setdefault(kind, {"name": None, "v": 0, "mode": ev["mode"], "label": "", "total": None})
            live["lines"] = ev["lines"]
            img = self.dec[kind].image()
            if img is not None:
                await asyncio.to_thread(self._write, LIVE[kind], img)
                live["name"], live["v"] = LIVE[kind], live["v"] + 1
            self.emit({"event": "progress", "kind": kind, "live": live})
        elif e == "done":
            self.live.pop(kind, None)
            await asyncio.to_thread(self._unlink, LIVE[kind])
            if ev["lines"] < MIN_ROWS[kind]:
                self.emit({"event": "dropped", "kind": kind, "label": ev["label"], "lines": ev["lines"]})
                return
            freq = self._rig(r).get("freq")
            khz = f"{int(freq) // 1000}k" if isinstance(freq, (int, float)) and freq > 0 else "nok"
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            name = f"{stamp}-{kind}-{ev['mode']}-{khz}-{ev['status']}.png"
            await asyncio.to_thread(self._write, name, ev["image"])
            await asyncio.to_thread(self._prune)
            self.emit({"event": "saved", "kind": kind, "image": self._describe(name), "label": ev["label"],
                       "clock_ppm": ev.get("clock_ppm")})

    def emit(self, d, ws=None):
        """Every event carries the decoders' states (the panel's lines)."""
        super().emit({**d, "decoders": {k: dec.status() for k, dec in self.dec.items()}}, ws)

    def _write(self, name, img):
        os.makedirs(self.folder, exist_ok=True)
        tmp = os.path.join(self.folder, f".{name}.tmp")
        with open(tmp, "wb") as fh:
            fh.write(self._picture.png(img))
        os.replace(tmp, os.path.join(self.folder, name))

    def _unlink(self, name):
        try:
            os.unlink(os.path.join(self.folder, name))
        except FileNotFoundError:
            pass

    def _saved(self):
        try:
            names = [n for n in os.listdir(self.folder) if NAME.match(n) and not n.startswith("live-")]
        except FileNotFoundError:
            return []
        return sorted(names, reverse=True)

    def _prune(self):
        for n in self._saved()[self.keep:]:
            self._unlink(n)

    @staticmethod
    def _describe(name):
        stamp, kind, mode, khz, status = name[:-4].split("-")
        at = datetime.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
        return {"name": name, "kind": kind, "mode": mode, "khz": None if khz == "nok" else int(khz[:-1]),
                "status": status, "at": at.isoformat().replace("+00:00", "Z")}

    # --- the page

    def snapshot(self):
        r = self._radio()
        return {"radio": r.name if r is not None else None, "listening": self.listening, "error": self.error,
                "decoders": {k: d.status() for k, d in self.dec.items()}, "live": self.live,
                "images": [self._describe(n) for n in self._saved()[:LIST_MAX]]}

    async def command(self, ws, verb, m):
        if verb == "fax_start":
            if "fax" not in self.dec:
                self.notice("Imaging: the fax decoder is off in the host file.", "warn", ws)
                return
            for ev in self.dec["fax"].start_now():
                await self._event("fax", ev, self._radio())
            return
        if verb == "stop":
            kind = m.get("kind")
            if kind not in self.dec:
                self.notice("Imaging: stop what? (sstv or fax)", "warn", ws)
                return
            for ev in self.dec[kind].abort() or []:
                await self._event(kind, ev, self._radio())
            return
        await super().command(ws, verb, m)

    def http(self, app, authorised):
        async def img(request):
            if not authorised(request):
                return web.json_response({"error": "token required"}, status=401)
            name = request.match_info["name"]
            if not NAME.match(name):
                return web.json_response({"error": "no such picture"}, status=404)
            path = os.path.join(self.folder, name)
            if not os.path.isfile(path):
                return web.json_response({"error": "no such picture"}, status=404)
            return web.FileResponse(path, headers={"Cache-Control": "no-store", "Content-Type": "image/png"})

        app.router.add_get("/imaging/img/{name}", img)


ADDON = Imaging
