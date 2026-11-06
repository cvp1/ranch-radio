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

One owner for Pat's life (PatEngine._life): start, stop, restart_pat and
the inbound-session cap all go through it. The argv is built from `want` at
spawn time, each child is stamped with a generation, and restart_pat returns
only once a child of the new generation runs and its first Status is in (or
raises PatRestartError). Before every spawn, Pat's config file is read for
four keys only (listen, schedule, ax25.beacon.every, agwpe.addr; the file
holds the Winlink password, nothing else is read or logged): a config that
would make Pat listen, beacon or run scheduled connects on its own keeps Pat
from being launched at all, with the reason on the page (preflight()).
"""

import asyncio
import json
import logging
import re
import shlex
import time
from collections import deque
from datetime import UTC, datetime
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlsplit

import aiohttp
from aiohttp import web

from pat_config import pat_config_path, preflight  # noqa: F401 - re-exported for callers

log = logging.getLogger("winlink")

BOXES = ("in", "out", "sent", "archive")
RADIO_TRANSPORTS = ("ardop", "ax25", "ax25+agwpe", "ax25+linux", "ax25+serial-tnc")
# Where a packet (ax25*) URL may put us: the VHF/UHF segments where US
# amateurs may send data, 47 CFR 97.305(c) (eCFR, current as of 2026-11-06;
# the same table at law.cornell.edu/cfr/text/47/97.305):
#   2 m     144.1-148.0 MHz   MCW, phone, image, RTTY, data, test
#   1.25 m  222-225 MHz       RTTY, data, test, MCW, phone, SS, image
#           (219-220 MHz is data too, but 97.303(l)(1) restricts it to fixed
#           point-to-point digital message forwarding: not offered)
#   70 cm   entire band       MCW, phone, image, RTTY, data, SS, test
#           (US band 420-450 MHz, 97.301(a))
# Below 144.1 MHz on 2 m is CW only: a packet URL at 144.050 is refused.
# Only for packet URLs: the HF band list in server.py also drives auto-tune
# and codec levels, and an HF rig never tunes these. (Without the VHF list
# every packet gateway was refused as off-band, linux-station 2026-10-17,
# N7RCC-10 on 145.010.) Plan winlink-listen's validator uses the same table.
PACKET_BANDS = [("2 m", 144.1e6, 148e6), ("1.25 m", 222e6, 225e6), ("70 cm", 420e6, 450e6)]
# The national APRS channel, never a Winlink packet frequency (+-10 kHz).
APRS_HZ, APRS_GUARD_HZ = 144.390e6, 10e3
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
OUTBOX_BUSY = "A Winlink session is running; the outbox can't be changed until it ends."
HF_MODEMS = ("ardop",)       # HF modem schemes a peer (P2P) session may name; the page reads `modems`
LIFE_WAIT_S = 15.0           # restart_pat: how long a new Pat has to run and send its first Status
PREFLIGHT_RETRY_S = 30.0     # Pat's config refused: look at it again this often (or on restart_pat)
STOP_WAIT_S = 5.0            # stop(): how long Pat has to exit on SIGTERM before SIGKILL
CONNECT_LINE_S = 10.0        # a "Got connect" log line names the caller of an edge this recent
GOT_CONNECT = re.compile(r"Got connect \(([^:()]+):([^()]+)\)")


def remote_call(remote_addr):
    """The caller in Pat's Status remote_addr ("AX.25:N0XYZ via DIGI" ->
    "N0XYZ"), else the address as Pat gave it (telnet's "tcp:ip:port")."""
    r = (remote_addr or "").strip()
    if r.upper().startswith("AX.25:"):
        return r[6:].split(" via ")[0].strip().upper() or r
    return r or "?"


class PatRestartError(RuntimeError):
    """restart_pat could not get a new Pat running (the reason is the message)."""




def mid_ok(mid, bad=web.HTTPNotFound):
    """The canonical (percent-decoded) MID, or raise bad: empty, a path
    separator, or a leading dot. Pat unescapes and cleans a path itself, so an
    encoded in/X%2F..%2F..%2Fout%2FOUT0 would reach out/OUT0 past every check
    on the box; forward quote(mid, safe="") of what this returns."""
    mid = unquote(mid or "")
    if not mid or "/" in mid or "\\" in mid or mid.startswith("."):
        raise bad()
    return mid


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
        if u.scheme.startswith("ax25"):
            if abs(hz - APRS_HZ) <= APRS_GUARD_HZ:
                return (f"{float(freq[0]):g} kHz is the APRS channel (144.390 MHz); "
                        "Winlink packet never goes out there.")
            if not packet_band_of(hz):
                return (f"{float(freq[0]):g} kHz is outside the packet data sub-bands "
                        "(2 m 144.1-148, 1.25 m 222-225, 70 cm 420-450 MHz; 47 CFR 97.305(c)); "
                        "not transmitting there.")
        elif band_of(hz) is None:
            return f"{float(freq[0]):g} kHz is outside the amateur bands; not transmitting there."
    return None


def packet_band_of(hz):
    """The packet data sub-band (PACKET_BANDS) name hz is in, or None."""
    return next((n for n, lo, hi in PACKET_BANDS if lo <= hz <= hi), None)


def check_peer(url, mycall):
    """Why this URL can't be a peer-to-peer (P2P) session (None = fine):
    it names exactly one valid callsign, not this station's own call (an
    SSID of ours is another station: N9ABC-5 is fine for N9ABC), and a radio
    transport says its frequency, so the radio is on a known channel."""
    import ax25
    u = urlsplit(url)
    path = u.path.strip("/")
    if not path or "/" in path:
        return "A peer session names one callsign (no digipeaters)."
    try:
        call = ax25.call_str(*ax25.parse_call(path))
    except ax25.FrameError as e:
        return f"Not a callsign: {e}."
    if path.upper().endswith("*") or path.upper() != call and path.upper() != call + "-0":
        return f"Not a callsign: {path!r}."
    if mycall and call == ax25.call_str(*ax25.parse_call(mycall)):
        return f"{call} is this station's own call; a peer is another station."
    if not mycall:
        return "Pat has not said this station's call yet; try again in a moment."
    if u.scheme in RADIO_TRANSPORTS and url_hz(url) is None:
        return "A peer session on the radio needs its frequency (freq=, kHz)."
    return None


def redact(url):
    """The URL with any password in it (telnet://CALL:PASS@...) masked, for logs and pages."""
    try:
        u = urlsplit(url)
    except ValueError:
        return url
    if u.password is None:
        return url
    return url.replace(f":{u.password}@", ":***@", 1)


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
    hub (whose own transmit gate applies). notice: the host's
    notice(msg, level, ws), for telling the page what to do about tuning."""

    armed = False
    extra_grace_s = 0.0             # PatEngine.stop: more time for end() (a lane that restores)

    def __init__(self, name, notice=None):
        self.name = name
        self.notice = notice

    def state(self):
        return {"armed": False, "codec_db": None, "id": None}

    async def arm(self, ws, url):
        hz = url_freq_hz(url)
        if hz and urlsplit(url).scheme.startswith("ax25") and self.notice:
            # Nothing here can retune the hub's radio: the operator does.
            self.notice(f"Packet via the {self.name}: Pat can't retune it, so it must already be on "
                        f"{hz / 1e6:.3f} MHz (its panel's VFO or channel, on the active side).", "info", ws)
        return True

    async def end(self, started, ok, reason, closing, notify):
        return


class TunedLane(NoLane):
    """Packet through the hub whose radio a tuner (the UV-PRO add-on,
    addons/uvpro.py: qsy(hz), restore(closing)) puts on the gateway's
    frequency before Pat dials and back afterwards. Keys nothing itself.

    If the tuner can't tune, the connect is refused: dialing on whatever
    channel the radio is on would put a Winlink call out on the wrong
    frequency (the APRS channel, as like as not)."""

    def __init__(self, name, tuner, notice):
        super().__init__(name, notice)
        self.tuner = tuner
        self.tuned = None               # the tuner's result while the radio is off its channel

    async def arm(self, ws, url):
        hz = url_hz(url)
        if not hz:
            return True                 # no frequency asked for: nothing to tune
        r = await self.tuner.qsy(hz)
        if not r.ok:
            log.warning("winlink: packet via %s: not tuned to %.4f MHz (%s); not connecting",
                        self.name, hz / 1e6, r.why)
            self.notice(f"Not connecting: the {self.name} could not be tuned to {hz / 1e6:.3f} MHz "
                        f"({r.why}). Tune it by hand and connect without freq=, or fix the cause.", "warn", ws)
            return False
        self.tuned = r
        self.extra_grace_s = getattr(self.tuner, "restore_closing_s", 0.0)
        log.info("winlink: packet via %s: tuned to %.4f MHz (%s)", self.name, hz / 1e6, r.how)
        self.notice(f"{self.name} tuned to {hz / 1e6:.3f} MHz ({r.how}); back to {r.back} after the session.",
                    "info", ws)
        return True

    async def end(self, started, ok, reason, closing, notify):
        r, self.tuned = self.tuned, None
        if r is None:
            return
        done, why = await self.tuner.restore(closing=closing)
        if done:
            log.info("winlink: %s back on %s", self.name, r.back)
            notify({"notify": {"title": f"{self.name} back on {r.back}"}})
        else:
            log.warning("winlink: %s NOT put back on %s: %s", self.name, r.back, why)
            self.notice(f"{self.name} NOT put back on {r.back}: {why}. The add-on keeps trying; "
                        "check the radio.", "warn", None)


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
        self.pat_cmd = shlex.split(args.winlink_pat)
        self.config_path = pat_config_path(args, self.pat_cmd)
        self.hf_modems = HF_MODEMS
        self.session_max = args.winlink_session_max
        # The lifecycle owner (A5). want: what Pat should be, read when a
        # child is spawned; gen counts restarts. proc_gen is the running
        # child's generation, status_gen the one whose first Status is in.
        self.want = {"listen": None, "gen": 0}
        self._life = asyncio.Lock()
        self._wake = asyncio.Event()
        self._expect_exit = None        # the child restart_pat asked to exit (not a crash)
        self._restart_req = 0           # restart_pat calls so far
        self._spawn_req = 0             # of those, how many the running child's argv covers
        self.blocked = None             # preflight's refusal: Pat is not launched
        self._blocked_gen = None
        self.proc = None
        self.proc_gen = None
        self.status_gen = None
        # Pat's occupancy (A4), whoever started its exchange. _status_live:
        # a Status has come on the current WebSocket (else Pat's state is
        # unknown, which counts as busy). inbound: an exchange we did not
        # start, {"call", "since", "remote"}.
        self._status_live = False
        self.inbound = None
        self._connect_line = (None, 0.0)   # (call, monotonic) from Pat's "Got connect" line
        self._capping = None
        self.http = None
        self.pat_ws = None
        self.live_at = None          # monotonic time Pat's replayed log is behind us
        self.mycall = None
        self.status = {}
        self.lines = deque(maxlen=LOG_KEEP)
        self.session = None          # running session task
        self.session_url = None
        self.session_peer = False
        self.session_owner = None       # the page that started the running session
        self._stop_asked = False
        self._closing = False
        self._admitting = False          # a connect is arming its lane (no session task yet)
        self._admit_stop = False         # Stop pressed meanwhile: don't start it
        # The outbox is Pat's from admission (its outbox snapshot) to the
        # session's end: a delete or move out of it holds this lock across its
        # check and its call to Pat, and connect() takes it once before _admit.
        self._outbox_lock = asyncio.Lock()
        self._tasks = []
        self.running = False         # between start() and stop(): the Winlink add-on's switch

    # --- lifecycle ---

    @property
    def cmd(self):
        """The argv the next Pat gets, from `want` (never a stale copy)."""
        a = list(self.pat_cmd)
        if self.want["listen"]:
            a += ["--listen", self.want["listen"]]       # a global flag: before the command
        return a + ["http", "--addr", f"127.0.0.1:{self.port}"]

    def preflight(self):
        return preflight(self.config_path, self.pat_cmd)

    async def start(self):
        # Startable again after stop() (the add-on's off/on): a new run.
        async with self._life:
            self._closing = self._stop_asked = False
            self._wake.clear()
            self._expect_exit = self.blocked = None
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
            self._tasks = [asyncio.create_task(self._supervise()), asyncio.create_task(self._events()),
                           asyncio.create_task(self._inbound_watch())]
            self.running = True

    async def stop(self):
        self.running = False
        self._closing = True
        self._wake.set()                   # a supervisor napping (crashed Pat, refused config) returns
        end = time.monotonic() + ABORT_GRACE_S
        while self._admitting and time.monotonic() < end:
            await asyncio.sleep(0.05)      # a connect arming its lane: it sees _closing and undoes it
        if self.session and not self.session.done():
            await self.abort("server stopping")
            # A lane that puts the radio back (TunedLane) gets a bounded extra
            # wait for that, so a restart doesn't strand the radio off its channel.
            grace = ABORT_GRACE_S + (getattr(self.lane, "extra_grace_s", 0.0) or 0.0)
            try:
                await asyncio.wait_for(asyncio.shield(self.session), grace)
            except (TimeoutError, asyncio.CancelledError):
                self.session.cancel()
        async with self._life:             # a restart_pat in flight sees _closing and gives up
            await self._end_child(self.proc)
            sup = self._tasks[0] if self._tasks else None
            if sup is not None:
                # The supervisor ends its own child: one it was spawning
                # when stop came is terminated there, never orphaned.
                try:
                    await asyncio.wait_for(asyncio.shield(sup), STOP_WAIT_S + 1)
                except (TimeoutError, asyncio.CancelledError):
                    pass
            for t in self._tasks:
                t.cancel()
            await self._end_child(self.proc)
        if self.http:
            await self.http.close()

    async def _end_child(self, proc):
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), STOP_WAIT_S)
        except TimeoutError:
            log.warning("winlink: Pat (pid %d) did not exit on SIGTERM; killing it", proc.pid)
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), STOP_WAIT_S)
            except TimeoutError:
                log.error("winlink: Pat (pid %d) did not exit on SIGKILL", proc.pid)

    async def _nap(self, seconds):
        """Sleep, or less if restart_pat or stop wakes the supervisor."""
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except TimeoutError:
            pass
        self._wake.clear()

    async def _supervise(self):
        while not self._closing:
            gen = self.want["gen"]
            why = self.preflight()
            if why:
                if why != self.blocked:
                    log.warning("winlink: not starting Pat: %s", why)
                    self.station.notice(f"Winlink is off: {why}.", "warn")
                self.blocked, self._blocked_gen = why, gen
                self._send({"blocked": why})
                await self._nap(PREFLIGHT_RETRY_S)
                continue
            if self.blocked:
                log.info("winlink: Pat's config is clear again; starting Pat")
                self.blocked = self._blocked_gen = None
                self._send({"blocked": None})
            argv, covers = self.cmd, self._restart_req
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            except OSError as e:
                log.error("cannot start Pat (%s): %s", " ".join(argv), e)
                await self._nap(30)
                continue
            self.proc, self.proc_gen, self._spawn_req = proc, gen, covers
            if self._closing:              # stopped while it was being started
                await self._end_child(proc)
                return
            log.info("Pat started (pid %d, generation %d) on 127.0.0.1:%d%s", proc.pid, gen, self.port,
                     f" listening on {self.want['listen']}" if "--listen" in argv else "")
            async for raw in proc.stdout:
                log.debug("pat: %s", raw.decode(errors="replace").rstrip())
            code = await proc.wait()
            if self._closing:
                return
            if self._expect_exit is proc:
                self._expect_exit = None
                continue                   # restart_pat(): asked for, start it again now
            log.warning("Pat exited (code %s); starting it again in %gs", code, RESTART_S)
            self.station.notice("Winlink engine (Pat) stopped; restarting it.", "warn")
            await self._nap(RESTART_S)

    async def _events(self):
        """Follow Pat's WebSocket and relay it to the pages."""
        while not self._closing:
            try:
                gen, proc = self.proc_gen, self.proc
                async with self.http.ws_connect(f"{self.base}/ws", heartbeat=None,
                                                timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
                    self.pat_ws = ws
                    self._ws_child = (gen, proc)
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
                        self._status_live = False   # unknown now: busy until a new Status says not
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
                got = GOT_CONNECT.search(line)
                if got:
                    call = got.group(2).strip().upper()
                    self._connect_line = (call, time.monotonic())
                    if self.inbound and self.inbound.get("from") == "remote_addr":
                        self.inbound.update(call=call, **{"from": "log"})
                        self._send({"inbound": self.inbound})
        elif "Status" in m:
            self.status = m["Status"]
            self._status_live = True
            gen, proc = getattr(self, "_ws_child", (None, None))
            if proc is not None and proc is self.proc and proc.returncode is None:
                self.status_gen = gen      # this child's Status is in (restart_pat waits for it)
            await self._track_inbound()
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

    # --- Pat's occupancy (A4) ---

    @property
    def pat_busy(self):
        """Pat is in an exchange or dialing, or we can't tell (no Status on
        the current WebSocket): only a newer Status saying otherwise clears it."""
        if self.pat_ws is None or not self._status_live:
            return True
        return bool(self.status.get("connected") or self.status.get("dialing"))

    def _our_session(self):
        return bool(self.session and not self.session.done())

    async def _track_inbound(self):
        up = bool(self.status.get("connected"))
        if up and self.inbound is None and not self._our_session():
            call, at = self._connect_line
            how = "log"
            if not call or time.monotonic() - at > CONNECT_LINE_S:
                call, how = remote_call(self.status.get("remote_addr")), "remote_addr"
            # As connect() does: a mailbox change already past its check
            # finishes first; any later one sees inbound and gets a 409.
            async with self._outbox_lock:
                self.inbound = {"call": call, "since": time.time(),
                                "remote": self.status.get("remote_addr") or "", "from": how}
            log.info("winlink: Pat is in a session with %s that was not started here (%s)",
                     call, self.inbound["remote"])
            self._send({"inbound": self.inbound})
        elif not up and self.inbound is not None:
            log.info("winlink: the session with %s ended", self.inbound["call"])
            self.inbound = None
            self._send({"inbound": None})

    def _busy_why(self):
        """Why a connect must not dial now (None = Pat is free)."""
        if self.inbound:
            return f"Pat is in a session with {self.inbound['call']}; not connecting."
        if self.pat_busy:
            return ("Pat is busy (connected or dialing)" if self._status_live
                    else "Pat's state is not known (no status from it)") + "; not connecting."
        return None

    async def _inbound_watch(self):
        """The session cap for exchanges we did not start."""
        while not self._closing:
            await asyncio.sleep(0.25)
            ib = self.inbound
            if (ib and self.session_max and not ib.get("capped")
                    and time.time() - ib["since"] > self.session_max):
                ib["capped"] = True
                self._capping = asyncio.create_task(self._cap_inbound(ib))

    async def _exchange_gone(self, ib, proc):
        """Is the capped exchange really over? On packet, only the hub's link
        leaving its list says so (Pat's dirty disconnect does nothing on
        AX.25, F9); elsewhere Pat's Status, or Pat being dead."""
        if ib.get("remote", "").upper().startswith("AX.25:"):
            probe = getattr(self.station, "winlink_hub_links", None)
            links = await probe() if probe else None
            if links is not None:
                return not any(str(l.get("remote", "")).upper() == ib["call"].upper() for l in links)
        return self.inbound is not ib or (proc is not None and proc.returncode is not None)

    async def _wait_gone(self, ib, proc, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self._closing:
            if await self._exchange_gone(ib, proc):
                return True
            await asyncio.sleep(0.2)
        return await self._exchange_gone(ib, proc)

    async def _cap_inbound(self, ib):
        call = ib["call"]
        log.warning("winlink: the session with %s ran past the %gs limit; disconnecting it", call, self.session_max)
        self.station.notice(f"Winlink: the session with {call} ran past {self.session_max:g}s; ending it.", "warn")
        await self._disconnect(dirty=False)
        if await self._wait_gone(ib, self.proc, ABORT_GRACE_S):
            log.info("winlink: the session with %s ended on a disconnect", call)
            return
        async with self._life:             # the owner signals Pat, and respawns it
            proc = self.proc
            if self._closing or proc is None or proc.returncode is not None:
                return
            log.warning("winlink: %s still connected %gs after a disconnect; stopping Pat (SIGTERM)",
                        call, ABORT_GRACE_S)
            self.want["gen"] += 1
            gen = self.want["gen"]
            self._expect_exit = proc
            proc.terminate()
            if not await self._wait_gone(ib, proc, ABORT_GRACE_S):
                log.warning("winlink: %s still connected after SIGTERM; killing Pat (SIGKILL)", call)
                proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), STOP_WAIT_S)
            except TimeoutError:
                # Pat survived SIGTERM but the exchange is gone: it runs on.
                self._expect_exit = None
                log.info("winlink: the session with %s ended; Pat kept running", call)
                return
            self._wake.set()
            end = time.monotonic() + LIFE_WAIT_S
            while time.monotonic() < end and not self._closing:
                if self.proc_gen == gen and self.status_gen == gen:
                    log.info("winlink: Pat started again after ending the session with %s", call)
                    return
                await asyncio.sleep(0.05)
            log.error("winlink: Pat did not come back after ending the session with %s", call)

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
        return {**self.lane_state(), "session": self.session_url, "peer": self.session_peer,
                "via": self.lane.name if self.lane else None,
                "modems": list(self.hf_modems), "blocked": self.blocked,
                "inbound": self.inbound, "pat_busy": self.pat_busy,
                "mycall": self.mycall, "status": self.status, "log": list(self.lines)[-100:]}

    # --- sessions ---

    async def connect(self, ws, url, via=None, peer=False):
        st = self.station
        if self._admitting or (self.session and not self.session.done()):
            st.notice("A Winlink session is already running.", "warn", ws)
            return
        # Reserved from here until the session task exists (or the connect is
        # refused): arming may take seconds (a tuner writing the radio), and a
        # second connect must not slip in meanwhile.
        self._admitting, self._admit_stop = True, False
        try:
            # A mailbox change already past its check finishes before the
            # outbox snapshot; any later one sees _admitting and gets a 409.
            async with self._outbox_lock:
                pass
            await self._admit(ws, url, via, peer)
        finally:
            self._admitting = False

    async def _admit(self, ws, url, via, peer=False):
        st = self.station
        why = check_url(url, st.band_of)
        if not why and peer:
            why = check_peer(url, self.mycall)
        lane = None
        if not why:
            lane, why = self.lane_for(url, via)
        if why or self.inbound:
            st.notice(why or self._busy_why(), "warn", ws)
            return
        if not await self._ready():
            st.notice("The Winlink engine (Pat) is not answering; not connecting.", "warn", ws)
            return
        # Pat may take an inbound call at any await below: re-checked after
        # each one, and the lane is undone if it was already armed (A4).
        if self._busy_why():
            st.notice(self._busy_why(), "warn", ws)
            return
        queued = await self._outbox()      # read before arming: what this session may send
        if self.session and not self.session.done():
            st.notice("A Winlink session is already running.", "warn", ws)
            return
        if self._busy_why():
            st.notice(self._busy_why(), "warn", ws)
            return
        try:
            armed = await lane.arm(ws, url)
        except Exception as e:             # never dial on a half-armed lane
            log.exception("winlink: arming %s", lane.name)
            st.notice(f"Not connecting: arming {lane.name} failed ({type(e).__name__}: {e}).", "warn", ws)
            await self._end_unstarted(lane, f"arming failed: {e}")
            return
        if not armed:
            return                         # refused; the reason was sent to the page
        if self._closing or self._admit_stop:
            st.notice("Stopped before connecting.", "info", ws)
            await self._end_unstarted(lane, "stopped before it started")
            return
        busy = self._busy_why()
        if busy:                           # nothing is dialed: the lane is undone
            log.info("winlink: connect undone after arming: %s", busy)
            st.notice(busy, "warn", ws)
            await self._end_unstarted(lane, busy)
            return
        self.lane = lane
        if not isinstance(lane, LocalLane):  # a local radio's arming told the pages itself
            self._send({**lane.state(), "armed": True, "via": lane.name})
        self.session_owner = ws
        self._stop_asked = False
        self.session_url = redact(url)
        self.session_peer = bool(peer)
        pat_url = url
        hz = url_freq_hz(url)
        if isinstance(lane, NoLane) and urlsplit(url).scheme.startswith("ax25") and hz:
            # Pat would QSY first and fail ("Unable to QSY: missing rig
            # reference ... ax25+agwpe", linux-station 2026-10-17): no rig control
            # reaches the hub's radio through Pat. The radio is tuned on its own
            # panel, or by the lane's tuner (TunedLane) in arm() above, which
            # saw the full URL; Pat gets it without freq=.
            pat_url = without_freq(url)
            log.info("winlink: packet via %s: freq dropped for Pat (no QSY); the radio must be on %.4f MHz",
                     lane.name, hz / 1e6)
        self.session = asyncio.create_task(self._session(url, queued, pat_url, peer))

    async def _end_unstarted(self, lane, reason):
        """A lane armed (or half-armed) for a session that never started: undo it."""
        try:
            await lane.end(time.time(), False, reason, lambda: self._closing, self._send)
        except Exception:                  # noqa: BLE001 - logged; the connect is refused either way
            log.exception("winlink: undoing %s", lane.name)

    async def restart_pat(self, why):
        """Start Pat again (its modem moved to another radio, a listen
        window began or ended) with the argv `want` gives now, whether the
        old Pat is alive or not. Returns once a child of the new generation
        runs and its first Status (and the live log) are in; raises
        PatRestartError otherwise. Concurrent calls share one respawn: a
        child whose argv was built after a call asked (the supervisor
        stamps each spawn with the calls it covers) satisfies that call. A
        no-op while Winlink is switched off (its next start reads `want`)."""
        if not self.running:
            return
        self._restart_req += 1
        mine = self._restart_req
        async with self._life:
            p = self.proc
            if (self._spawn_req >= mine and p is not None and p.returncode is None
                    and self.status_gen == self.proc_gen and self.pat_ws is not None):
                log.info("winlink: restart (%s) already done by the Pat started for an earlier one", why)
                return
            await self._respawn(why)

    async def _respawn(self, why):
        if self._closing:
            raise PatRestartError("Winlink is stopping")
        self.want["gen"] += 1
        gen = self.want["gen"]
        log.info("winlink: restarting Pat (%s), generation %d", why, gen)
        old = self.proc
        if old is not None and old.returncode is None:
            self._expect_exit = old
            await self._end_child(old)
        self._wake.set()                   # a supervisor napping after a crash starts it now
        end = time.monotonic() + LIFE_WAIT_S
        while time.monotonic() < end:
            if self._closing:
                raise PatRestartError("Winlink is stopping")
            if self.blocked and self._blocked_gen == gen:
                raise PatRestartError(self.blocked)
            p = self.proc
            if (self.proc_gen == gen and p is not None and p.returncode is None
                    and self.status_gen == gen and self.pat_ws is not None):
                if await self._ready():
                    return
            await asyncio.sleep(0.05)
        raise PatRestartError(f"Pat did not come back within {LIFE_WAIT_S:g}s")

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

    async def _listing(self, box):
        """One of Pat's mailbox listings, or None if it can't be read."""
        try:
            async with self.http.get(f"{self.base}/api/mailbox/{box}",
                                     timeout=aiohttp.ClientTimeout(total=BOX_READ_S)) as r:
                if r.status != 200:
                    log.warning("winlink: mailbox %s answered %s", box, r.status)
                    return None
                return [m for m in json.loads(await r.text()) if m.get("MID")]
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError, TypeError, KeyError, AttributeError) as e:
            log.warning("winlink: could not read mailbox %s: %s", box, e)
            return None

    async def _mids(self, box):
        """The MIDs in one of Pat's mailboxes, or None if it can't be read."""
        msgs = await self._listing(box)
        return None if msgs is None else {m["MID"] for m in msgs}

    async def _outbox(self):
        """{MID: P2P only?} for the outbox now, or None if it can't be read."""
        msgs = await self._listing("out")
        return None if msgs is None else {m["MID"]: bool(m.get("P2POnly")) for m in msgs}

    async def _sent_count(self, queued):
        """(sent, left, unsent) for a session: of the messages in the outbox
        when it started (queued, {MID: P2P only?}), how many Pat moved to
        sent/ (measured, whatever the remote took: a peer takes what is
        addressed to the forwarders it advertised, F6), how many are still
        out, and of those how many a gateway should have taken (not P2P
        only). Pat's /api/connect reports only NumReceived, so this is read
        from the mailboxes. None where a listing could not be read (unknown,
        not 0)."""
        if queued is None:
            return None, None, None
        if not queued:
            return 0, 0, 0
        if not isinstance(queued, dict):
            queued = dict.fromkeys(queued, False)
        sent, out = await self._mids("sent"), await self._mids("out")
        left = None if out is None else {m for m in queued if m in out}
        return (None if sent is None else len(set(queued) & sent),
                None if left is None else len(left),
                None if left is None else sum(1 for m in left if not queued[m]))

    async def _session(self, url, queued=None, pat_url=None, peer=False):
        ok, received, reason = False, 0, None
        sent = left = unsent = None
        started = time.time()
        shown = redact(url)
        log.info("winlink: %ssession to %s", "peer (P2P) " if peer else "", shown)
        self._send({"session": "started", "url": shown, "peer": bool(peer)})
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
                log.warning("winlink: ending the session on %s was cut short (stopping)%s", lane.name,
                            "; the radio may not be back on its channel" if isinstance(lane, TunedLane) else "")
            except Exception:              # never leave the session looking alive
                log.exception("winlink: ending the session on %s", lane.name)
            self.lane = self.session_owner = None
            if not isinstance(lane, LocalLane):
                self._send({"armed": False, "via": None})
            try:                            # after disarm: never holds the radio armed
                sent, left, unsent = await self._sent_count(queued)
            except asyncio.CancelledError:
                pass
            self.session_peer = False
            log.info("winlink: session ended ok=%s sent=%s received=%s left=%s%s", ok, sent, received, left,
                     f" ({reason})" if reason else "")
            # A gateway takes everything but P2P-only messages; a peer only
            # what is addressed to it (its forwarder list isn't visible
            # here), so "left in the outbox" is a warning for gateways only.
            if peer:
                unsent = None
            elif ok and unsent:
                log.warning("winlink: %d message(s) still in the outbox after a good session",
                            unsent)
            self._send({"session": "ended", "ok": ok, "sent": sent, "left": left, "unsent": unsent,
                        "peer": bool(peer), "received": received, "reason": reason,
                        "id": lane.state()["id"]})

    def _last_failure(self):
        for line in reversed(self.lines):
            if "Unable to establish" in line or "failed" in line.lower() or "cancel" in line.lower():
                return line.split(": ", 1)[-1] if line.startswith("20") else line
        return None

    async def stop_session(self, ws=None):
        if self._admitting and not (self.session and not self.session.done()):
            self._admit_stop = True        # still arming: it is undone instead of started
            return
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

    def outbox_busy(self):
        """The outbox is Pat's: a session of ours arming (admission) or
        running, or Pat in any exchange (one we did not start too) or its
        state unknown."""
        return self._admitting or self._our_session() or self.pat_busy or self.inbound is not None

    async def _outbox_change(self, call):
        async with self._outbox_lock:
            if self.outbox_busy():
                text = (f"Pat is in a session with {self.inbound['call']}; the outbox can't be changed "
                        "until it ends." if self.inbound and not self._our_session() else OUTBOX_BUSY)
                return web.Response(status=409, text=text)
            return await call()

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

        def msg_path(request):
            """/api/mailbox/<box>/<mid> for Pat, the MID canonical and re-encoded."""
            box_ok(request)
            mi = request.match_info
            return mi["box"], f"/api/mailbox/{mi['box']}/{quote(mid_ok(mi['mid']), safe='')}"

        async def message(request):
            _, path = msg_path(request)
            return await self._pass("GET", path, json_out=True)

        async def attachment(request):
            _, path = msg_path(request)
            return await self._pass("GET", f"{path}/{quote(request.match_info['file'], safe='')}")

        async def delete(request):
            box, path = msg_path(request)
            if box == "out":
                return await self._outbox_change(lambda: self._pass("DELETE", path))
            return await self._pass("DELETE", path)

        async def mark_read(request):
            _, path = msg_path(request)
            return await self._pass("POST", f"{path}/read", data=await request.read(),
                                    headers={"Content-Type": "application/json"})

        async def move(request):
            box_ok(request, ("in", "sent", "archive"))
            src = request.headers.get("X-Pat-SourcePath", "")
            parts = src.strip("/").split("/")
            if len(parts) != 4 or parts[:2] != ["api", "mailbox"] or parts[2] not in BOXES:
                raise web.HTTPBadRequest(text="bad X-Pat-SourcePath")
            src_box = parts[2]
            mid = mid_ok(parts[3], lambda: web.HTTPBadRequest(text="bad X-Pat-SourcePath"))
            source = f"/api/mailbox/{src_box}/{quote(mid, safe='')}"

            def call():
                return self._pass("POST", f"/api/mailbox/{request.match_info['box']}",
                                  headers={"X-Pat-SourcePath": source})
            if src_box == "out":
                return await self._outbox_change(call)
            return await call()

        async def compose(request):
            # POST to out is always compose (registered before the move route):
            # a move aimed at it must not turn silently into a new message.
            if "X-Pat-SourcePath" in request.headers:
                return web.Response(status=400, text="use the move route")
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
