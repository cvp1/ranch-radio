"""Winlink email through Pat, driven from the Radio Remote page.

Pat (github.com/la5nta/pat) does the protocol: secure login, B2F, compression,
the gateway list. It runs here as a child process, `pat http` bound to
127.0.0.1 (its API has no authentication), and its own web UI is never used.

Radio Remote owns everything around it:
  - the page's mailbox, compose and gateway list go through an allow-list of
    Pat's API (/winlink/api/...); config, connect and the rest are not exposed
  - a session is started from the page's control WebSocket (winlink_connect):
    the URL is checked, the Winlink rigctl lane is armed for that page, Pat's
    blocking /api/connect runs, and when it returns (or is stopped, capped,
    or the page goes away) the lane is disarmed and the radio unkeyed
  - Pat's WebSocket events (log, status, progress, new mail, prompts such as
    a busy channel) are relayed to the pages as {"t": "winlink", ...}
  - station ID (47 CFR 97.119(a), an ID at the end of each communication):
    ardopcf sends it at the end of an attempt, through Pat and the lane like
    any frame. A session that transmitted is disarmed only once that ID is
    out (or a bounded wait passes), never mid-ID; the page closing still
    disarms at once (operator gone). See station_id.py.

Pat keys the radio only through the lane (--winlink-rigctl-port); point Pat's
hamlib rig at it (config hamlib_rigs.<name>.address = localhost:<port>).
"""

import asyncio
import json
import logging
import shlex
import time
from collections import deque
from datetime import UTC, datetime
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

import aiohttp
from aiohttp import web

log = logging.getLogger("winlink")

BOXES = ("in", "out", "sent", "archive")
RADIO_TRANSPORTS = ("ardop", "ax25", "ax25+agwpe", "ax25+linux", "ax25+serial-tnc")
# VHF/UHF amateur bands (US) where packet gateways live (Pat's rmslist: 2 m
# and 70 cm around here). Only for packet (ax25*) URLs: the HF band list in
# server.py also drives auto-tune and codec levels, and an HF rig never tunes
# these. Without them every VHF packet gateway was refused as off-band
# (linux-station, 2026-10-17, N7RCC-10 on 145.010).
PACKET_BANDS = [("2 m", 144e6, 148e6), ("1.25 m", 222e6, 225e6), ("70 cm", 420e6, 450e6)]
TRANSPORTS = RADIO_TRANSPORTS + ("telnet",)
LOG_KEEP = 300               # log lines kept for a page that opens later
REPLAY_S = 1.0               # Pat replays its log on every WebSocket connect
RESTART_S = 2.0              # wait before starting Pat again after it exits
PING_S = 60.0                # Pat drops a WebSocket silent for 4m10s
ABORT_GRACE_S = 10.0         # after a dirty abort, wait this long for Pat to return
READY_WAIT_S = 5.0           # a session waits this long for the event link to Pat
# After Pat's /api/connect returns: how long a session that transmitted stays
# armed for ardopcf's end-of-session ID. ardopcf starts it within a few
# seconds of an abort (the frame in flight, then its 2 s repeat timer); an
# IDFrame + CW ID takes ~9 s.
ID_START_S = 8.0             # tracked: no ID started by then -> none is coming
ID_GRACE_S = 25.0            # never armed longer than this after Pat returns
TAIL_QUIET_S = 3.0           # not tracked: armed until the lane is this quiet
BOX_READ_S = 3.0             # reading a mailbox listing for the session's sent count


def url_hz(url):
    """The dial frequency (Hz) a connect URL's freq= (kHz) names, or None."""
    try:
        freq = parse_qs(urlsplit(url).query).get("freq")
        return int(round(float(freq[0]) * 1000)) if freq else None
    except ValueError:
        return None


def check_url(url, band_of):
    """Why this connect URL is refused (None = fine)."""
    try:
        u = urlsplit(url)
    except ValueError:
        return "Not a connect URL."
    if u.scheme not in TRANSPORTS:
        return f"Unsupported transport {u.scheme or '(none)'!r}; use one of {', '.join(TRANSPORTS)}."
    if not u.path.strip("/") and u.scheme != "telnet":
        return "The URL names no gateway."
    freq = parse_qs(u.query).get("freq")
    if freq:
        if u.scheme not in RADIO_TRANSPORTS:
            return "A frequency only makes sense for a radio transport."
        try:
            hz = float(freq[0]) * 1000
        except ValueError:
            return "The frequency is not a number (kHz)."
        packet_band = u.scheme.startswith("ax25") and any(lo <= hz <= hi for _, lo, hi in PACKET_BANDS)
        if band_of(hz) is None and not packet_band:
            return f"{float(freq[0]):g} kHz is outside the amateur bands; not transmitting there."
    return None


def without_freq(url):
    """The connect URL with its freq= dropped (other parameters kept)."""
    # By string: urlunsplit turns Pat's empty-host "ax25:///CALL" into "ax25:/CALL".
    base, _, query = url.partition("?")
    q = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k != "freq"]
    return base + ("?" + urlencode(q) if q else "")


def url_freq_hz(url):
    """The connect URL's freq= (kHz) in Hz, or None."""
    try:
        freq = parse_qs(urlsplit(url).query).get("freq")
        return float(freq[0]) * 1000 if freq else None
    except ValueError:
        return None


async def id_tail(station, started, closing=lambda: False, notify=None):
    """Stay armed until ardopcf's end-of-session ID is out (bounded).

    Only for a session that transmitted, and only while still armed: the
    page closing disarms at once, an operator's Disarm too. Runs in the
    process that owns the radio (its lane, ledger and PTT), whoever runs Pat."""
    st, led = station, station.id_ledger
    if not led.last_tx or led.last_tx < started:
        return                          # never keyed: nothing to identify
    t0 = time.monotonic()
    waited = False
    while st.winlink_armed and not closing():
        el = time.monotonic() - t0
        if el >= ID_GRACE_S:
            log.warning("winlink: no end of station ID after %gs; disarming", ID_GRACE_S)
            break
        if not st._winlink_keying():
            if led.tracked:
                if not led.in_progress():
                    if not led.owed():
                        break
                    if el >= ID_START_S:
                        log.warning("winlink: session transmitted but ardopcf sent no "
                                    "station ID at its end; an ID is owed")
                        break
            elif time.time() - (led.last_ptt() or 0) >= TAIL_QUIET_S:
                break
        if not waited:
            waited = True
            log.info("winlink: holding the session armed for ardopcf's station ID")
            if notify:
                notify({"id": dict(led.snapshot(), waiting=True)})
        await asyncio.sleep(0.1)


class LocalLane:
    """The radio this process drives: its Winlink rigctl lane, codec level
    and station-ID ledger, in-process (Radio Remote with --winlink-pat)."""

    name = "local"

    def __init__(self, station):
        self.station = station

    @property
    def armed(self):
        return self.station.winlink_armed

    def state(self):
        st = self.station
        return {"armed": st.winlink_armed, "codec_db": st.codec and st.codec.current,
                "id": st.id_ledger.snapshot()}

    async def arm(self, ws, url):
        """True once armed for this session; False if refused (the page was told why)."""
        st = self.station
        await st.winlink_arm(ws, True, url_freq_hz(url))
        if not st.winlink_armed:
            return False
        st.winlink_qsy_hz = url_hz(url)
        return True

    async def end(self, started, ok, reason, closing, notify):
        await id_tail(self.station, started, closing, notify)
        await self.station.winlink_disarm(None if ok else reason)


class NoLane:
    """A session that keys no radio of ours: telnet, or packet through the
    UV-PRO hub (whose own transmit gate applies)."""

    armed = False

    def __init__(self, name):
        self.name = name

    def state(self):
        return {"armed": False, "codec_db": None, "id": None}

    async def arm(self, ws, url):
        return True

    async def end(self, started, ok, reason, closing, notify):
        return


class PatEngine:
    def __init__(self, station, args, lane_for=None):
        """station: what pages are reached through (notice, _send,
        _broadcast, band_of). lane_for(url, via) -> (lane, None) or (None,
        why): which radio a session keys. Default: this process's radio."""
        self.station = station
        self.args = args
        local = LocalLane(station) if hasattr(station, "winlink_arm") else None
        self.lane_for = lane_for or (lambda url, via: (local, None))
        self.idle_lane = local          # whose state the page shows between sessions
        self.lane = None                # the running session's lane
        self.port = args.winlink_pat_port
        self.base = f"http://127.0.0.1:{self.port}"
        self.cmd = shlex.split(args.winlink_pat) + ["http", "--addr", f"127.0.0.1:{self.port}"]
        self.session_max = args.winlink_session_max
        self.proc = None
        self.http = None
        self.pat_ws = None
        self.live_at = None          # monotonic time Pat's replayed log is behind us
        self.mycall = None
        self.status = {}
        self.lines = deque(maxlen=LOG_KEEP)
        self.session = None          # running session task
        self.session_url = None
        self.session_owner = None       # the page that started the running session
        self._stop_asked = False
        self._closing = False
        self._restarting = False
        self._tasks = []
        self.running = False         # between start() and stop(): the Winlink add-on's switch

    # --- lifecycle ---

    async def start(self):
        # Startable again after stop() (the add-on's off/on): a new run.
        self._closing = self._restarting = self._stop_asked = False
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self._tasks = [asyncio.create_task(self._supervise()), asyncio.create_task(self._events())]
        self.running = True

    async def stop(self):
        self.running = False
        self._closing = True
        if self.session and not self.session.done():
            await self.abort("server stopping")
            try:
                await asyncio.wait_for(asyncio.shield(self.session), ABORT_GRACE_S)
            except (TimeoutError, asyncio.CancelledError):
                self.session.cancel()
        for t in self._tasks:
            t.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                self.proc.kill()
        if self.http:
            await self.http.close()

    async def _supervise(self):
        while not self._closing:
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *self.cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            except OSError as e:
                log.error("cannot start Pat (%s): %s", " ".join(self.cmd), e)
                await asyncio.sleep(30)
                continue
            log.info("Pat started (pid %d) on 127.0.0.1:%d", self.proc.pid, self.port)
            async for raw in self.proc.stdout:
                log.debug("pat: %s", raw.decode(errors="replace").rstrip())
            code = await self.proc.wait()
            if self._closing:
                return
            if self._restarting:
                self._restarting = False
                continue                   # restart_pat(): asked for, start it again now
            log.warning("Pat exited (code %s); starting it again in %gs", code, RESTART_S)
            self.station.notice("Winlink engine (Pat) stopped; restarting it.", "warn")
            await asyncio.sleep(RESTART_S)

    async def _events(self):
        """Follow Pat's WebSocket and relay it to the pages."""
        while not self._closing:
            try:
                async with self.http.ws_connect(f"{self.base}/ws", heartbeat=None,
                                                timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
                    self.pat_ws = ws
                    self.lines.clear()
                    joined = time.monotonic()
                    self.live_at = joined + REPLAY_S
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._on_pat(json.loads(msg.data), time.monotonic() - joined < REPLAY_S)
                    finally:
                        pinger.cancel()
                        self.pat_ws = self.live_at = None
            except (aiohttp.ClientError, OSError, ValueError):
                pass
            await asyncio.sleep(0.2)

    async def _ping(self, ws):
        while True:
            await asyncio.sleep(PING_S)
            await ws.send_str(json.dumps({"Pong": True}))

    async def _on_pat(self, m, replay):
        if "Ping" in m and self.pat_ws:
            await self.pat_ws.send_str(json.dumps({"Pong": True}))
        elif "MyCall" in m:
            self.mycall = m["MyCall"]
        elif "LogLine" in m:
            line = m["LogLine"].rstrip()
            self.lines.append(line)
            if not replay:
                self._send({"log": line})
        elif "Status" in m:
            self.status = m["Status"]
            self._send({"status": self.status})
        elif "Progress" in m:
            self._send({"progress": m["Progress"]})
        elif "Notification" in m:
            self._send({"notify": m["Notification"]})
        elif "UpdateMailbox" in m:
            self._send({"mailbox": True})
        elif "Prompt" in m:
            self._send({"prompt": m["Prompt"]})
        elif "PromptAbort" in m:
            self._send({"prompt_abort": m["PromptAbort"]})

    def _send(self, d, ws=None):
        payload = json.dumps({"t": "winlink", **d})
        if ws is not None:
            asyncio.ensure_future(self.station._send(ws, payload))
        else:
            self.station._broadcast(payload)

    def lane_state(self):
        lane = self.lane or self.idle_lane
        return lane.state() if lane else {"armed": False, "codec_db": None, "id": None}

    def snapshot(self):
        return {**self.lane_state(), "session": self.session_url,
                "via": self.lane.name if self.lane else None,
                "mycall": self.mycall, "status": self.status, "log": list(self.lines)[-100:]}

    # --- sessions ---

    async def connect(self, ws, url, via=None):
        st = self.station
        if self.session and not self.session.done():
            st.notice("A Winlink session is already running.", "warn", ws)
            return
        why = check_url(url, st.band_of)
        lane = None
        if not why:
            lane, why = self.lane_for(url, via)
        if why:
            st.notice(why, "warn", ws)
            return
        if not await self._ready():
            st.notice("The Winlink engine (Pat) is not answering; not connecting.", "warn", ws)
            return
        queued = await self._mids("out")   # read before arming: what this session may send
        if self.session and not self.session.done():
            st.notice("A Winlink session is already running.", "warn", ws)
            return
        if not await lane.arm(ws, url):
            return                         # refused; the reason was sent to the page
        self.lane = lane
        if not isinstance(lane, LocalLane):  # a local radio's arming told the pages itself
            self._send({**lane.state(), "armed": True, "via": lane.name})
        self.session_owner = ws
        self._stop_asked = False
        self.session_url = url
        pat_url = url
        hz = url_freq_hz(url)
        if isinstance(lane, NoLane) and urlsplit(url).scheme.startswith("ax25") and hz:
            # Pat would QSY first and fail ("Unable to QSY: missing rig
            # reference ... ax25+agwpe", linux-station 2026-10-17): no rig control
            # reaches the UV-PRO through Pat. The radio is tuned on its own
            # panel; Pat gets the URL without freq=.
            pat_url = without_freq(url)
            log.info("winlink: packet via %s: freq dropped for Pat (no QSY); the radio must be on %.4f MHz",
                     lane.name, hz / 1e6)
            st.notice(f"Packet via the {lane.name}: Pat can't retune it, so it must already be on "
                      f"{hz / 1e6:.3f} MHz (its panel's VFO, on the active side).", "info", ws)
        self.session = asyncio.create_task(self._session(url, queued, pat_url))

    async def restart_pat(self, why):
        """Start Pat again (e.g. its modem moved to another radio) and wait
        until its events are live. Not while a session runs."""
        if not (self.proc and self.proc.returncode is None):
            return
        log.info("winlink: restarting Pat (%s)", why)
        old, self._restarting = self.proc, True
        old.terminate()
        try:
            await asyncio.wait_for(old.wait(), 5)
        except TimeoutError:
            log.warning("winlink: Pat did not exit within 5 s; killing it")
            old.kill()
        end = time.monotonic() + 15
        while time.monotonic() < end and (self.proc is old or self.pat_ws is not None):
            await asyncio.sleep(0.05)
        await self._ready()

    async def _ready(self):
        """Wait until Pat's events reach the pages live (not its log replay),
        so a page sees every line and prompt of the session."""
        end = time.monotonic() + READY_WAIT_S
        while time.monotonic() < end:
            if self.pat_ws is not None and self.live_at and time.monotonic() >= self.live_at:
                return True
            await asyncio.sleep(0.05)
        return False

    async def _call(self, url):
        async with self.http.get(f"{self.base}/api/connect", params={"url": url},
                                 timeout=aiohttp.ClientTimeout(total=None)) as r:
            return r.status, await r.text()

    async def _mids(self, box):
        """The MIDs in one of Pat's mailboxes, or None if it can't be read."""
        try:
            async with self.http.get(f"{self.base}/api/mailbox/{box}",
                                     timeout=aiohttp.ClientTimeout(total=BOX_READ_S)) as r:
                if r.status != 200:
                    log.warning("winlink: mailbox %s answered %s", box, r.status)
                    return None
                return {m["MID"] for m in json.loads(await r.text()) if m.get("MID")}
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError, TypeError, KeyError, AttributeError) as e:
            log.warning("winlink: could not read mailbox %s: %s", box, e)
            return None

    async def _sent_count(self, queued):
        """(sent, unsent) for a session: of the messages in the outbox when it
        started, how many Pat has moved to sent/ and how many are still out.
        Pat's /api/connect reports only NumReceived, so this is read from the
        mailboxes. None where a listing could not be read (unknown, not 0)."""
        if queued is None:
            return None, None
        if not queued:
            return 0, 0
        sent, out = await self._mids("sent"), await self._mids("out")
        return (None if sent is None else len(queued & sent),
                None if out is None else len(queued & out))

    async def _session(self, url, queued=None, pat_url=None):
        ok, received, reason = False, 0, None
        sent = unsent = None
        started = time.time()
        log.info("winlink: session to %s", url)
        self._send({"session": "started", "url": url})
        # Pat's call runs as its own task, so that after a cap or an abort we
        # can still see when Pat returns (with ardopcf's ID sent by then).
        call = asyncio.ensure_future(self._call(pat_url or url))
        try:
            done, _ = await asyncio.wait({call}, timeout=self.session_max)
            if call in done:
                status, body = call.result()
                tail = body[body.find("{"):] if "{" in body else "{}"
                received = json.loads(tail).get("NumReceived", 0)
                ok = status == 200
                if not ok:
                    reason = self._last_failure() or "session failed"
            else:
                reason = f"stopped after the {self.session_max:g}s session limit"
                log.warning("winlink: %s", reason)
                await self._disconnect(dirty=True)
                await asyncio.wait({call}, timeout=ABORT_GRACE_S)
        except asyncio.CancelledError:
            reason = reason or "cancelled"
            await self._disconnect(dirty=True)
        except (aiohttp.ClientError, OSError, ValueError) as e:
            reason = f"lost the Winlink engine: {e}"
            log.warning("winlink: %s", reason)
        finally:
            if not call.done():
                call.cancel()
            self.session_url = None
            lane = self.lane
            try:
                await lane.end(started, ok, reason, lambda: self._closing, self._send)
            except asyncio.CancelledError:
                pass
            except Exception:              # never leave the session looking alive
                log.exception("winlink: ending the session on %s", lane.name)
            self.lane = self.session_owner = None
            if not isinstance(lane, LocalLane):
                self._send({"armed": False, "via": None})
            try:                            # after disarm: never holds the radio armed
                sent, unsent = await self._sent_count(queued)
            except asyncio.CancelledError:
                pass
            log.info("winlink: session ended ok=%s sent=%s received=%s%s", ok, sent, received,
                     f" ({reason})" if reason else "")
            if ok and unsent:
                log.warning("winlink: %d message(s) still in the outbox after a good session",
                            unsent)
            self._send({"session": "ended", "ok": ok, "sent": sent, "unsent": unsent,
                        "received": received, "reason": reason, "id": lane.state()["id"]})

    def _last_failure(self):
        for line in reversed(self.lines):
            if "Unable to establish" in line or "failed" in line.lower() or "cancel" in line.lower():
                return line.split(": ", 1)[-1] if line.startswith("20") else line
        return None

    async def stop_session(self, ws=None):
        if not (self.session and not self.session.done()):
            return
        dirty = self._stop_asked           # a second Stop aborts the modem hard
        self._stop_asked = True
        await self._disconnect(dirty=dirty)

    async def abort(self, why):
        """Operator gone / server stopping: end the session now."""
        if self.session and not self.session.done():
            log.warning("winlink: aborting session (%s)", why)
            await self._disconnect(dirty=True)

    async def _disconnect(self, dirty):
        try:
            async with self.http.get(f"{self.base}/api/disconnect",
                                     params={"dirty": "true" if dirty else "false"},
                                     timeout=aiohttp.ClientTimeout(total=5)) as r:
                await r.read()
        except (TimeoutError, aiohttp.ClientError, OSError) as e:
            log.warning("winlink: disconnect request failed: %s", e)

    async def answer_prompt(self, pid, value):
        if self.pat_ws is not None:
            await self.pat_ws.send_str(json.dumps(
                {"prompt_response": {"id": str(pid), "value": str(value), "error": None}}))

    # --- HTTP: the allow-list of Pat's API ---

    def routes(self, app, authorised):
        def guard(handler):
            async def h(request):
                if not authorised(request):
                    return web.Response(status=401, text="token required")
                if not self.running:      # the Winlink add-on is switched off: Pat isn't running
                    return web.json_response({"error": "Winlink is switched off"}, status=503)
                return await handler(request)
            return h

        def box_ok(request, allowed=BOXES):
            if request.match_info["box"] not in allowed:
                raise web.HTTPNotFound()

        async def mailbox(request):
            box_ok(request)
            return await self._pass("GET", f"/api/mailbox/{request.match_info['box']}", json_out=True)

        async def message(request):
            box_ok(request)
            mi = request.match_info
            return await self._pass("GET", f"/api/mailbox/{mi['box']}/{mi['mid']}", json_out=True)

        async def attachment(request):
            box_ok(request)
            mi = request.match_info
            return await self._pass("GET", f"/api/mailbox/{mi['box']}/{mi['mid']}/{mi['file']}")

        async def delete(request):
            box_ok(request)
            mi = request.match_info
            return await self._pass("DELETE", f"/api/mailbox/{mi['box']}/{mi['mid']}")

        async def mark_read(request):
            box_ok(request)
            mi = request.match_info
            return await self._pass("POST", f"/api/mailbox/{mi['box']}/{mi['mid']}/read",
                                    data=await request.read(),
                                    headers={"Content-Type": "application/json"})

        async def move(request):
            box_ok(request, ("in", "sent", "archive"))
            src = request.headers.get("X-Pat-SourcePath", "")
            parts = src.strip("/").split("/")
            if len(parts) != 4 or parts[:2] != ["api", "mailbox"] or parts[2] not in BOXES:
                raise web.HTTPBadRequest(text="bad X-Pat-SourcePath")
            return await self._pass("POST", f"/api/mailbox/{request.match_info['box']}",
                                    headers={"X-Pat-SourcePath": "/" + "/".join(parts)})

        async def compose(request):
            form = await request.post()
            out = aiohttp.FormData()
            for k, v in form.items():
                if isinstance(v, web.FileField):
                    out.add_field(k, v.file.read(), filename=v.filename,
                                  content_type=v.content_type)
                else:
                    out.add_field(k, v)
            if not form.get("date"):
                # Pat refuses a message without a date (400).
                out.add_field("date", datetime.now(UTC).isoformat(timespec="seconds"))
            return await self._pass("POST", "/api/mailbox/out", data=out)

        async def rmslist(request):
            q = {k: request.query[k] for k in ("mode", "band") if k in request.query}
            return await self._pass("GET", "/api/rmslist", params=q, json_out=True)

        async def status(request):
            return web.json_response(self.snapshot())

        r = app.router
        r.add_get("/winlink/api/mailbox/{box}", guard(mailbox))
        r.add_post("/winlink/api/mailbox/out", guard(compose))
        r.add_post("/winlink/api/mailbox/{box}", guard(move))
        r.add_get("/winlink/api/mailbox/{box}/{mid}", guard(message))
        r.add_delete("/winlink/api/mailbox/{box}/{mid}", guard(delete))
        r.add_post("/winlink/api/mailbox/{box}/{mid}/read", guard(mark_read))
        r.add_get("/winlink/api/mailbox/{box}/{mid}/{file}", guard(attachment))
        r.add_get("/winlink/api/rmslist", guard(rmslist))
        r.add_get("/winlink/api/status", guard(status))

    async def _pass(self, method, path, json_out=False, **kw):
        try:
            async with self.http.request(method, self.base + path, **kw) as r:
                body = await r.read()
                headers = {}
                if not json_out:
                    for h in ("Content-Type", "Content-Disposition", "Content-Security-Policy"):
                        if h in r.headers:
                            headers[h] = r.headers[h]
                elif r.status < 400:
                    headers["Content-Type"] = "application/json"
                return web.Response(status=r.status, body=body, headers=headers)
        except (TimeoutError, aiohttp.ClientError, OSError) as e:
            return web.Response(status=503, text=f"Winlink engine not available: {e}")
