"""Wavelog CAT: what WaveLogGate did, from the station (2026-11-18, mac-shack).

WaveLogGate (DJ7NT's Electron app) did three jobs: JTDX's QSOs to Wavelog
(logsync.py does that now), the radio's frequency and mode to Wavelog's live
QSO form, and a click on a DX-cluster or bandmap spot in Wavelog tuning the
radio. This module does the last two, for every radio the station fronts, so
WaveLogGate (and the flrig it needed) can go.

    push   on every change a radio's RadioWatch sees (station.rig_changed),
           and every `every_s` while nothing changes (Wavelog counts a radio
           live for 15 minutes after its last word: models/Cat.php):
           POST <url>/index.php/api/radio
                {key, radio, frequency (Hz), mode, cat_url}
           -> {"status": "success"}; 401 a bad key; 429 its rate limit
           (60 a minute per key by default, config.sample.php).
    qsy    Wavelog's page (assets/js/cat.js performRadioTuning) fetches
           GET <cat_url>/<hz>/<mode>   mode: lsb usb cw fm am rtty pkt dig
                                            pktlsb pktusb pktfm
           from the operator's browser. cat_url is the station's
           /wavelog/qsy/<radio id>, sent with every push.

Safety. A QSY retunes; it never keys. It is a GET, which the station's
cross-site guard lets through (webguard.cross_site_guard checks Origin only
on POSTs and WebSockets), so this handler checks Origin itself: it must be
one of the configured Wavelog origins, exactly. No Origin is refused too: an
<img> on any web page sends none. Refused while the radio transmits or its
Winlink lane is armed; the radio server refuses a retune while transmitting
on its own as well (server.retune_blocked). Not offered with a station
token: the token would have to sit in Wavelog's database and pages.

The block (host file "wavelog_cat", passed through station.json):

    {"url": "http://127.0.0.1:8086", "key_file": "~/.config/ranch-radio/wavelog-key",
     "radios": {"ic7300": "IC-7300"},
     "qsy": {"station_url": "https://192.168.1.21:7300",
             "origins": ["https://192.168.1.21:8087"], "mode": true},
     "every_s": 60}

url and key_file default to logsync.wavelog's (render.py fills them in).
radios: radio id -> the name Wavelog shows; absent means every radio, by its
name. qsy absent: push only. qsy.mode false: a click moves the frequency
only, as WaveLogGate's "set mode" switch did.

Standard library at import: render.py checks the block (check_block), and
render sits under the radio server. aiohttp is imported where it is used.
"""
import asyncio
import json
import logging
import os
import time
from urllib.parse import urlsplit

log = logging.getLogger("wavelog_cat")

API = "/index.php/api/radio"
QSY_PATH = "/wavelog/qsy"
EVERY_S = 60.0
MIN_GAP_S = 1.5              # between two pushes for one radio: under the 60/min rate limit
BACKOFF_S = (30.0, 900.0)
HTTP_TIMEOUT_S = 10.0
QSY_TIMEOUT_S = 4.0
# Wavelog's mode word -> (radio mode, data). cat.js sends one of these, lower case.
QSY_MODES = {"lsb": ("LSB", False), "usb": ("USB", False), "cw": ("CW", False), "fm": ("FM", False),
             "am": ("AM", False), "rtty": ("RTTY", False), "pkt": ("USB", True), "dig": ("USB", True),
             "pktusb": ("USB", True), "pktlsb": ("LSB", True), "pktfm": ("FM", True)}
# The radio's mode -> the word Wavelog's QSO form takes (Api.php radio() folds these too).
PUSH_MODES = {"CW-R": "CW", "RTTY-R": "RTTY"}


def _origin(text, field):
    u = urlsplit(str(text))
    if u.scheme not in ("http", "https") or not u.netloc or u.path not in ("", "/") or u.query or u.fragment:
        raise ValueError(f"wavelog_cat.{field} = {text!r}: scheme://host[:port], nothing after")
    return f"{u.scheme}://{u.netloc}".lower()


def check_block(b):
    """The block as the station reads it, or ValueError naming the field."""
    if not isinstance(b, dict):
        raise ValueError("wavelog_cat must be an object")
    unknown = set(b) - {"url", "key_file", "radios", "qsy", "every_s"}
    if unknown:
        raise ValueError(f"wavelog_cat: unknown field(s) {sorted(unknown)}")
    for f in ("url", "key_file"):
        if not isinstance(b.get(f), str) or not b[f]:
            raise ValueError(f"wavelog_cat.{f} is required (or a logsync.wavelog block to take it from)")
    u = urlsplit(b["url"])
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError(f"wavelog_cat.url = {b['url']!r}: http(s)://host[:port]")
    radios = b.get("radios")
    if radios is not None and not (isinstance(radios, dict) and radios
                                   and all(isinstance(k, str) and isinstance(v, str) and v.strip()
                                           for k, v in radios.items())):
        raise ValueError("wavelog_cat.radios: {radio id: name in Wavelog}, at least one")
    every = b.get("every_s", EVERY_S)
    if isinstance(every, bool) or not isinstance(every, (int, float)) or not 10 <= every <= 600:
        raise ValueError(f"wavelog_cat.every_s = {every!r}: 10 to 600 seconds")
    q = b.get("qsy")
    if q is not None:
        if not isinstance(q, dict):
            raise ValueError("wavelog_cat.qsy must be an object")
        _origin(q.get("station_url", ""), "qsy.station_url")
        origins = q.get("origins")
        if not isinstance(origins, list) or not origins:
            raise ValueError("wavelog_cat.qsy.origins: the Wavelog address(es) the browser opens, at least one")
        for o in origins:
            _origin(o, "qsy.origins")
        if not isinstance(q.get("mode", True), bool):
            raise ValueError("wavelog_cat.qsy.mode: true or false")
    return b


class WavelogCat:
    """Push and QSY for one station. `station` gives rigs_view(), watches,
    radios, token, notice(); it calls kick() on every rig change."""

    def __init__(self, block, station):
        check_block(block)
        self.station = station
        self.url = block["url"].rstrip("/")
        self.key_file = os.path.expanduser(block["key_file"])
        self.names = dict(block.get("radios") or {r.id: r.name for r in station.radios})
        unknown = set(self.names) - {r.id for r in station.radios}
        if unknown:
            raise ValueError(f"wavelog_cat.radios names no radio here: {sorted(unknown)}")
        self.every_s = float(block.get("every_s", EVERY_S))
        q = block.get("qsy")
        self.qsy = None
        if q is not None:
            if getattr(station, "token", None):
                raise ValueError("wavelog_cat.qsy: not with a station token (it would sit in Wavelog's pages)")
            self.qsy = {"station_url": _origin(q["station_url"], "qsy.station_url"),
                        "origins": {_origin(o, "qsy.origins") for o in q["origins"]},
                        "mode": q.get("mode", True)}
        self.sent = {}               # rid -> (payload without key, time)
        self.last_error = None       # a class word, never a URL or the key
        self.held_until = 0.0
        self._fails = 0
        self._wake = asyncio.Event()
        self._task = None

    # ------------------------------------------------------------- push

    def kick(self):
        self._wake.set()

    async def start(self):
        if not os.path.exists(self.key_file):
            raise ValueError(f"wavelog_cat: key file {self.key_file} not present")
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def status(self):
        """For pages: an allowlist."""
        return {"loaded": True, "qsy": self.qsy is not None, "error": self.last_error,
                "radios": {rid: {"freq": p.get("frequency"), "mode": p.get("mode"), "at": round(t)}
                           for rid, (p, t) in self.sent.items()}}

    def payload(self, view):
        """The /api/radio body for one rigs_view() entry, without the key; None
        when there is nothing true to say (absent, not connected, no reading)."""
        if not (view.get("present") and view.get("connected") and view.get("freq") and view.get("mode")):
            return None
        p = {"radio": self.names[view["id"]], "frequency": int(view["freq"]),
             "mode": PUSH_MODES.get(view["mode"], view["mode"])}
        if self.qsy:
            p["cat_url"] = f"{self.qsy['station_url']}{QSY_PATH}/{view['id']}"
        return p

    def due(self, now):
        """[(rid, payload)] to send now: changed and past the gap, or quiet past every_s."""
        out = []
        for v in self.station.rigs_view():
            if v["id"] not in self.names:
                continue
            p = self.payload(v)
            if p is None:
                continue
            last = self.sent.get(v["id"])
            if last is None or (p != last[0] and now - last[1] >= MIN_GAP_S) or now - last[1] >= self.every_s:
                out.append((v["id"], p))
        return out

    async def _run(self):
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)) as http:
            while True:
                now = time.monotonic()
                if now >= self.held_until:
                    for rid, p in self.due(now):
                        if not await self._post(http, rid, p):
                            break
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=min(MIN_GAP_S, self.every_s))
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()

    def _key(self):
        with open(self.key_file) as f:
            return f.read().strip()

    async def _post(self, http, rid, p):
        import aiohttp
        try:
            async with http.post(self.url + API, json={"key": self._key(), **p}) as r:
                status = r.status
                retry = r.headers.get("Retry-After")
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
            return self._failed("network", type(e).__name__)
        if status == 200:
            self.sent[rid] = (p, time.monotonic())
            if self.last_error:
                log.info("wavelog_cat: pushing again")
            self.last_error, self._fails = None, 0
            return True
        if status == 429:
            wait = float(retry) if (retry or "").isdigit() else BACKOFF_S[0]
            return self._failed("rate limited", f"HTTP 429, retry in {wait:.0f} s", wait)
        if status in (401, 403):
            return self._failed("auth", f"HTTP {status}: Wavelog refused the API key", BACKOFF_S[1])
        return self._failed("http", f"HTTP {status}")

    def _failed(self, cls, detail, wait=None):
        """Back off; tell the operator once per new kind of failure (edge, not every try)."""
        self._fails += 1
        if wait is None:
            wait = min(BACKOFF_S[0] * 2 ** (self._fails - 1), BACKOFF_S[1])
        self.held_until = time.monotonic() + wait
        if cls != self.last_error:
            log.warning("wavelog_cat: %s (%s); trying again in %.0f s", cls, detail, wait)
            if cls == "auth":
                self.station.notice("Wavelog refused the CAT API key; the radio's frequency isn't reaching it.",
                                    "warn")
        self.last_error = cls
        return False

    # ------------------------------------------------------------- qsy

    async def handle_qsy(self, request):
        from aiohttp import web
        origin = (request.headers.get("Origin") or "").lower()
        if self.qsy is None:
            return web.json_response({"error": "QSY from Wavelog is off here"}, status=404)
        if origin not in self.qsy["origins"]:
            log.warning("wavelog qsy refused: Origin %r is not a configured Wavelog", origin or None)
            return web.json_response({"error": "not from a configured Wavelog"}, status=403)
        cors = {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
        rid, mode = request.match_info["rid"], request.match_info["mode"].lower()
        try:
            hz = int(request.match_info["hz"])
        except ValueError:
            return web.json_response({"error": "frequency: whole hertz"}, status=400, headers=cors)
        if rid not in self.names or mode not in QSY_MODES:
            return web.json_response({"error": "no such radio or mode"}, status=404, headers=cors)
        radio = next(r for r in self.station.radios if r.id == rid)
        w = self.station.watches.get(rid)
        if not (radio.present and w is not None and w.rig.get("connected")):
            return web.json_response({"error": f"{radio.name} is not connected"}, status=503, headers=cors)
        if w.rig.get("tx") or w.state.get("armed"):
            log.warning("wavelog qsy refused: %s is transmitting or its Winlink lane is armed", rid)
            return web.json_response({"error": f"{radio.name} is busy (on the air or in a Winlink session)"},
                                     status=409, headers=cors)
        sets = [("freq", hz)]
        if self.qsy["mode"]:
            m, data = QSY_MODES[mode]
            sets += [("mode", m), ("data", data)]
        ok, why = await self._tune(radio, sets)
        log.info("wavelog qsy %s -> %d %s: %s", rid, hz, mode, "ok" if ok else why)
        if not ok:
            return web.json_response({"error": why}, status=502, headers=cors)
        return web.Response(text="OK", headers=cors)

    async def _tune(self, radio, sets):
        """Each set over the radio's own WebSocket (its checks apply), then wait
        for its state to show the frequency. (ok, why)."""
        import aiohttp
        from aiohttp import WSMsgType
        tok = self.station.token
        # The station's own credential: under auth readonly-lan a click from the
        # operator's Wavelog (Origin-pinned above) still tunes (review plan PR 4).
        auth = getattr(self.station, "auth", None)
        auth_headers = auth.bearer_headers() if auth is not None else {}
        url = f"http://127.0.0.1:{radio.web}/ws" + (f"?token={tok}" if tok else "")
        want = sets[0][1]
        try:
            async with aiohttp.ClientSession() as s, s.ws_connect(url, heartbeat=5, headers=auth_headers) as ws:
                hello = {}
                while hello.get("t") != "hello":         # a scope frame can come first
                    msg = await ws.receive(timeout=QSY_TIMEOUT_S)
                    if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                        return False, "the radio closed the connection"
                    if msg.type == WSMsgType.TEXT:
                        hello = json.loads(msg.data)
                freq = (hello.get("state") or {}).get("freq")
                for k, v in sets:
                    await ws.send_str(json.dumps({"t": "set", "k": k, "v": v}))
                # Already there: no state change will come, so only wait out a refusal.
                end = time.monotonic() + (0.5 if freq == want else QSY_TIMEOUT_S)
                seen = []
                while (left := end - time.monotonic()) > 0:
                    try:
                        msg = await ws.receive(timeout=left)
                    except asyncio.TimeoutError:
                        break
                    if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING, WSMsgType.ERROR):
                        break
                    if msg.type != WSMsgType.TEXT:
                        continue             # the scope's binary frames (2026-11-18: ended the wait in 1 ms)
                    m = json.loads(msg.data)
                    seen.append(m.get("t") if m.get("t") != "state" else f"state{sorted(m.get('d') or {})}")
                    if m.get("t") == "state":
                        freq = (m.get("d") or {}).get("freq", freq)
                        if freq == want and not m.get("confirmed"):
                            return True, ""
                    elif m.get("t") == "notice" and m.get("level") in ("warn", "error"):
                        return False, str(m.get("msg") or "the radio refused")[:200]
                if freq == want:
                    return True, ""
                log.warning("wavelog qsy: no confirmation; heard %s, last freq %r", seen[-8:], freq)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError) as e:
            return False, f"radio: {type(e).__name__}"
        return False, "the radio did not confirm the frequency"
