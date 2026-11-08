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

Listen windows (plan winlink-listen, listen_start/_end_listen): with a
host-file plan (listen_plan.ListenPlan, VHF/UHF packet only), the page can
open a bounded window in which Pat answers stations that call this one:
the radio tuned (TunedLane), Pat relaunched with --listen, the hub's
inbound window opened, all read back; ended in the order hub window,
session, Pat, radio. A record beside --addons-state lets the next start
undo a window a crash left, before any Pat runs.
"""

import asyncio
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import time
from collections import deque
from datetime import UTC, datetime
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlsplit

import aiohttp
from aiohttp import web

from listen_plan import (APRS_GUARD_HZ, APRS_HZ, MIN_HOURS, PACKET_BANDS, ListenError,  # noqa: F401
                         ListenPlan, packet_band_of)
from pat_config import pat_config_path, preflight  # noqa: F401 - re-exported for callers

log = logging.getLogger("winlink")

BOXES = ("in", "out", "sent", "archive")
# HF modem schemes the code knows (VARA HF through xara, plan winlink-vara);
# which of them a host has is its own list (PatEngine.hf_modems, published as
# `modems`). VARA FM ("varafm") and a bare "vara" stay refused.
HF_MODEMS = ("ardop", "varahf")
DEFAULT_HF_MODEMS = ("ardop",)   # a host that passes nothing: ARDOP only, as before
MODEM_NAMES = {"ardop": "ARDOP", "varahf": "VARA HF"}
RADIO_TRANSPORTS = HF_MODEMS + ("ax25", "ax25+agwpe", "ax25+linux", "ax25+serial-tnc")
# Where a packet (ax25*) URL may put us: the VHF/UHF data sub-bands of 47 CFR
# 97.305(c), never the APRS channel; the table lives in listen_plan.py, which
# the listen window's validator (and render.py) share. Only for packet URLs:
# the HF band list in server.py also drives auto-tune and codec levels, and an
# HF rig never tunes these. (Without the VHF list every packet gateway was
# refused as off-band, linux-station 2026-10-17, N7RCC-10 on 145.010.)
VHF_HZ = 100e6               # at and above this, a packet URL is checked against PACKET_BANDS
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
LIFE_WAIT_S = 15.0           # restart_pat: how long a new Pat has to run and send its first Status
PREFLIGHT_RETRY_S = 30.0     # Pat's config refused: look at it again this often (or on restart_pat)
STOP_WAIT_S = 5.0            # stop(): how long Pat has to exit on SIGTERM before SIGKILL
CONNECT_LINE_S = 10.0        # a "Got connect" log line names the caller of an edge this recent
GOT_CONNECT = re.compile(r"Got connect \(([^:()]+):([^()]+)\)")
# Listen windows (plan winlink-listen B2-B5).
LISTEN_FILE = "winlink-listen.json"   # the window's record, beside --addons-state
READBACK_S = 5.0             # a listen start's read-back: Pat's listener, the hub's registration and window
HUB_UNTIL_SLACK_S = 5.0      # the hub's window must end within this of ours
LISTEN_POLL_S = 1.0          # the window's watcher looks this often (until, the hub, the add-ons)
HUB_POLL_S = 5.0             # ...and asks the hub this often
HUB_MISSES = 3               # the hub not answering this many times in a row ends the window
LINK_WAIT_S = 600.0          # a hub link that outlives a window's end: the radio waits this long for it


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
        if u.scheme.startswith("ax25") and hz >= VHF_HZ:
            # VHF/UHF packet: only the data sub-bands, never the APRS channel.
            # (HF packet keeps the HF band check below, as before.)
            if abs(hz - APRS_HZ) <= APRS_GUARD_HZ:
                return (f"{float(freq[0]):g} kHz is the APRS channel (144.390 MHz); "
                        "Winlink packet never goes out there.")
            if not packet_band_of(hz) and band_of(hz) is None:
                return (f"{float(freq[0]):g} kHz is outside the packet data sub-bands "
                        "(2 m 144.1-148, 1.25 m 222-225, 70 cm 420-450 MHz; 47 CFR 97.305(c)); "
                        "not transmitting there.")
        elif band_of(hz) is None:
            return f"{float(freq[0]):g} kHz is outside the amateur bands; not transmitting there."
    return None


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


def msg_bytes(m):
    """A message's size from Pat's mailbox listing: its body plus its files'
    sizes, or None when the listing doesn't carry them."""
    body, files = m.get("Body"), m.get("Files")
    if not isinstance(body, str) and not files:
        return None
    try:
        return len((body or "").encode()) + sum(int(f.get("Size") or 0) for f in (files or []))
    except (TypeError, ValueError, AttributeError):
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


async def id_tail(station, started, closing=lambda: False, notify=None, scheme="ardop"):
    """Stay armed until the modem's end-of-session ID is out (bounded):
    ardopcf's (ARDOP) or xara's CW ID (VARA HF), by the session's scheme.

    Only for a session that transmitted, and only while still armed: the
    page closing disarms at once, an operator's Disarm too. Runs in the
    process that owns the radio (its lane, ledger and PTT), whoever runs Pat.
    Untracked (no log for this modem): armed until the lane is quiet for
    TAIL_QUIET_S. xara's blanks before its call are inside the keyed burst
    (crates/xara-phy/src/cwid.rs synth), so no quiet gap opens mid-ID; and a
    VARA lane exists only with xara's log, so VARA is always tracked."""
    import station_id
    st, led = station, station.id_ledger
    source = station_id.SOURCE_OF.get(scheme, "ardopcf")
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
            if led.tracked_for(scheme):
                if not led.in_progress(source):
                    if not led.owed():
                        break
                    if el >= ID_START_S:
                        log.warning("winlink: session transmitted but %s sent no "
                                    "station ID at its end; an ID is owed", source)
                        break
            elif time.time() - (led.last_ptt() or 0) >= TAIL_QUIET_S:
                break
        if not waited:
            waited = True
            log.info("winlink: holding the session armed for %s's station ID", source)
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
        """True once armed for this session; False if refused (the page was told why).
        The URL's scheme (ardop, varahf) is the only modem whose lane may key."""
        st = self.station
        scheme = urlsplit(url).scheme
        self.scheme = scheme if scheme in HF_MODEMS else "ardop"
        await st.winlink_arm(ws, True, url_freq_hz(url), scheme=self.scheme)
        if not st.winlink_armed:
            return False
        st.winlink_qsy_hz = url_hz(url)
        # Pat QSYs inside /api/connect and the modem keys at once: a band
        # change's auto-tune then ran under the modem's first key, which was
        # refused, and xara exits on a refused key (2026-11-07). The radio
        # goes to the session's frequency here and its tune ends first.
        wait = getattr(st, "winlink_qsy_and_settle", None)
        if wait is not None:
            why = await wait(url_hz(url))
            if why:
                st.notice(f"Not connecting: {why}", "warn", ws)
                await st.winlink_disarm(why)
                return False
        return True

    async def end(self, started, ok, reason, closing, notify):
        await id_tail(self.station, started, closing, notify, scheme=getattr(self, "scheme", "ardop"))
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

    def __init__(self, name, tuner, notice, listening=False):
        super().__init__(name, notice)
        self.tuner = tuner
        self.tuned = None               # the tuner's result while the radio is off its channel
        self.listening = listening      # a listen window's lane (B2), not a session's

    async def arm(self, ws, url):
        hz = url_hz(url)
        if not hz:
            return True                 # no frequency asked for: nothing to tune
        r = await self.tuner.qsy(hz)
        if not r.ok:
            log.warning("winlink: packet via %s: not tuned to %.4f MHz (%s); not connecting",
                        self.name, hz / 1e6, r.why)
            if self.listening:
                self.notice(f"Not listening: the {self.name} could not be tuned to {hz / 1e6:.3f} MHz "
                            f"({r.why}).", "warn", ws)
            else:
                self.notice(f"Not connecting: the {self.name} could not be tuned to {hz / 1e6:.3f} MHz "
                            f"({r.why}). Tune it by hand and connect without freq=, or fix the cause.", "warn", ws)
            return False
        self.tuned = r
        self.extra_grace_s = getattr(self.tuner, "restore_closing_s", 0.0)
        log.info("winlink: packet via %s: tuned to %.4f MHz (%s)", self.name, hz / 1e6, r.how)
        self.notice(f"{self.name} tuned to {hz / 1e6:.3f} MHz ({r.how}); back to {r.back} after the "
                    f"{'window' if self.listening else 'session'}.", "info", ws)
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
    def __init__(self, station, args, lane_for=None, hf_modems=None):
        """station: what pages are reached through (notice, _send,
        _broadcast, band_of). lane_for(url, via) -> (lane, None) or (None,
        why): which radio a session keys. Default: this process's radio.
        hf_modems: the HF modems this host has (published as `modems`);
        default ARDOP only, so no caller advertises VARA unless its host
        passes it."""
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
        self.hf_modems = tuple(m for m in (hf_modems or DEFAULT_HF_MODEMS) if m in HF_MODEMS)
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
        self.proc_argv = None           # the running child's argv (built from `want` at its spawn)
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
        # Listen windows (B2-B5). listen_plan: the host's winlink.listen (None:
        # not offered, hidden). listening: the open window. _listen_busy:
        # "starting"/"ending" while one is being opened or closed (it excludes
        # connects and outbox changes like a window does). The record on disk
        # (listen_path) lets the next start undo a window a crash left.
        try:
            self.listen_plan = ListenPlan.from_args(args)
        except ListenError as e:
            log.error("winlink: listening not offered: %s", e)
            self.listen_plan = None
        state = getattr(args, "addons_state", None)
        self.listen_path = (os.path.join(os.path.dirname(os.path.expanduser(state)), LISTEN_FILE)
                            if state else None)
        self.listening = None
        self._listen_busy = None
        self._listen_lane = None
        self._listen_task = None
        self._listen_end_task = None
        self._listen_restore = None
        self._listen_abort = False      # Stop listening (or stop()) while a window is still starting

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
            # A window a crash left: closed, and no listening Pat left, before
            # any Pat is launched (B5). Never resumed.
            await self._recover_listen()
            self._tasks = [asyncio.create_task(self._supervise()), asyncio.create_task(self._events()),
                           asyncio.create_task(self._inbound_watch())]
            self.running = True

    async def stop(self):
        self.running = False
        self._closing = True
        self._wake.set()                   # a supervisor napping (crashed Pat, refused config) returns
        self._listen_abort = True          # a window still starting undoes itself (it sees _closing too)
        if self._listen_busy == "starting":
            end = time.monotonic() + LIFE_WAIT_S + ABORT_GRACE_S
            while self._listen_busy == "starting" and time.monotonic() < end:
                await asyncio.sleep(0.05)
        if self._listen_end_task and not self._listen_end_task.done():
            await asyncio.wait({self._listen_end_task}, timeout=3 * ABORT_GRACE_S + 2 * STOP_WAIT_S)
        if self.listening or self._listen_busy or self._listen_lane:
            # B4 from stop(): the hub window, Pat and the radio, in that order.
            try:
                await asyncio.wait_for(self._end_listen("the server is stopping", stopping=True),
                                       3 * ABORT_GRACE_S + 2 * STOP_WAIT_S)
            except (TimeoutError, asyncio.CancelledError):
                log.warning("winlink: ending the listen window was cut short (stopping); the next start "
                            "finishes it from its record")
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
            self.proc_argv = argv
            if "--listen" in argv:
                self._note_listen_child(proc.pid, argv)
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
        await self._escalate(ib)

    async def _escalate(self, ib, gone=None):
        """A graceful disconnect did not end the exchange: SIGTERM Pat (its
        first one aborts the exchange and keeps running, F9), then SIGKILL;
        the hub drops a dead client's links (F12). The owner respawns Pat
        from `want`. gone(proc, seconds): the end test (default: _wait_gone)."""
        call = ib["call"]
        gone = gone or (lambda proc, seconds: self._wait_gone(ib, proc, seconds))
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
            if not await gone(proc, ABORT_GRACE_S):
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
                "inbound": self.inbound, "pat_busy": self.pat_busy, "listen": self.listen_state(),
                "mycall": self.mycall, "status": self.status, "log": list(self.lines)[-100:]}

    # --- sessions ---

    async def connect(self, ws, url, via=None, peer=False):
        st = self.station
        if self._admitting or (self.session and not self.session.done()):
            st.notice("A Winlink session is already running.", "warn", ws)
            return
        if self.listening or self._listen_busy:
            st.notice(self._listen_busy_text() + "; stop listening to connect.", "warn", ws)
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
        if not why and getattr(st, "tx_stopped", None) and urlsplit(url).scheme != "telnet":
            # STOP TX: no session that keys a radio until the operator resumes.
            why = "Not connecting: transmit is stopped (STOP TX); press Resume first."
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
        self._inbox_before = await self._sizes("in")   # for the session record's bytes received
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
        """{MID: P2P only?} for the outbox now, or None if it can't be read.
        Each message's size is kept for the session record (_queued_sizes)."""
        msgs = await self._listing("out")
        self._queued_sizes = {} if msgs is None else {m["MID"]: msg_bytes(m) for m in msgs}
        return None if msgs is None else {m["MID"]: bool(m.get("P2POnly")) for m in msgs}

    async def _sizes(self, box):
        """{MID: bytes or None} for a mailbox now, or None if it can't be read."""
        msgs = await self._listing(box)
        return None if msgs is None else {m["MID"]: msg_bytes(m) for m in msgs}

    async def _record(self, url, started, ok, reason, queued, sent_mids, received, peer):
        """The session's one structured line (plan winlink-vara A8): the trial's
        keep-or-drop data, appended to the server log. No password: the
        gateway is the URL's path, never its netloc."""
        u = urlsplit(url)
        q = parse_qs(u.query)
        end = time.time()
        sizes = getattr(self, "_queued_sizes", {}) or {}
        bytes_sent = None
        if sent_mids is not None:
            got = [sizes.get(m) for m in sent_mids]
            bytes_sent = None if any(b is None for b in got) else sum(got)
        bytes_received = None
        before = getattr(self, "_inbox_before", None)
        if before is not None:
            after = await self._sizes("in")
            if after is not None:
                new = [b for m, b in after.items() if m not in before]
                bytes_received = None if any(b is None for b in new) else sum(new)
        tail = getattr(self.station, "vara_tail", None)
        level = tail.max_level(started, end) if tail is not None and u.scheme == "varahf" else None
        if ok:
            outcome = "ok"
        elif reason and str(reason).startswith("stopped after the"):
            outcome = "capped"
        elif self._stop_asked:
            outcome = "stopped"
        else:
            outcome = "failed"
        rec = {"scheme": u.scheme, "bw": (q.get("bw") or [None])[0], "gateway": u.path.strip("/") or None,
               "freq_khz": (url_hz(url) or 0) / 1000 or None, "peer": bool(peer),
               "start": round(started, 3), "end": round(end, 3), "seconds": round(end - started, 1),
               "outcome": outcome, "reason": None if ok else (str(reason) if reason else None),
               "sent": None if sent_mids is None else len(sent_mids), "received": received,
               "bytes_sent": bytes_sent, "bytes_received": bytes_received, "max_level": level}
        log.info("winlink session: %s", json.dumps(rec, sort_keys=True))
        return rec

    async def _sent_count(self, queued):
        """(sent, left, unsent) for a session: of the messages in the outbox
        when it started (queued, {MID: P2P only?}), how many Pat moved to
        sent/ (measured, whatever the remote took: a peer takes what is
        addressed to the forwarders it advertised, F6), how many are still
        out, and of those how many a gateway should have taken (not P2P
        only). Pat's /api/connect reports only NumReceived, so this is read
        from the mailboxes. None where a listing could not be read (unknown,
        not 0)."""
        self._sent_mids = None
        if queued is None:
            return None, None, None
        if not queued:
            self._sent_mids = set()
            return 0, 0, 0
        if not isinstance(queued, dict):
            queued = dict.fromkeys(queued, False)
        sent, out = await self._mids("sent"), await self._mids("out")
        self._sent_mids = None if sent is None else set(queued) & sent
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
            try:
                await self._record(url, started, ok, reason, queued, getattr(self, "_sent_mids", None),
                                   received, peer)
            except asyncio.CancelledError:
                pass
            except Exception:              # the record is for the trial; never breaks a session
                log.exception("winlink: session record")
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

    # --- listen windows (plan winlink-listen B2-B5) ---
    #
    # A window answers stations that call this one, on the plan's VHF/UHF
    # packet frequency, for a bounded time: unattended, so automatic control
    # of a data emission (47 CFR 97.221(b): 6 m and shorter, here only in the
    # 97.305(c) data sub-bands). Three things are changed and each is undone:
    # the radio's channel (the UV-PRO tuner, which keeps its own record), Pat
    # (relaunched with --listen) and the hub's inbound window (A0, which runs
    # out by itself at `until`, whatever happens here). A page closing does
    # not end a window: the hub's expiry is the independent bound.

    def listen_state(self):
        """What a page needs: None when listening is not set up (hidden)."""
        if self.listen_plan is None:
            return None
        w = self.listening
        return {"plan": self.listen_plan.describe(), "busy": self._listen_busy,
                "window": None if w is None else {k: w[k] for k in ("method", "hz", "since", "until", "call", "name")}}

    def _listen_busy_text(self):
        plan = self.listen_plan
        on = f" on {plan.mhz_text()} MHz" if plan else ""
        if self._listen_busy == "starting":
            return f"A listen window is starting{on}"
        if self._listen_busy == "ending":
            return "A listen window is ending"
        return f"Listening for calls{on}"

    def _send_listen(self):
        self._send({"listen": self.listen_state()})

    # the record (B5)

    def _read_listen(self):
        if not self.listen_path:
            return None
        try:
            with open(self.listen_path) as f:
                rec = json.load(f)
            return rec if isinstance(rec, dict) else None
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            log.warning("winlink: listen record %s not readable (%s); treating it as a window to end",
                        self.listen_path, e)
            return {"stage": "unreadable"}

    def _save_listen(self, rec):
        os.makedirs(os.path.dirname(self.listen_path), exist_ok=True)
        tmp = self.listen_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.listen_path)

    def _set_listen_stage(self, rec, stage):
        """The record on disk at a new stage (keeping the pid the supervisor noted)."""
        rec["stage"] = stage
        self._save_listen({**rec, **{k: v for k, v in (self._read_listen() or {}).items()
                                     if k in ("pid", "argv")}})

    def _drop_listen(self):
        if self.listen_path and os.path.exists(self.listen_path):
            try:
                os.remove(self.listen_path)
            except OSError as e:
                log.warning("winlink: listen record %s not removed: %s", self.listen_path, e)

    def _note_listen_child(self, pid, argv):
        """The supervisor started a listening Pat: its pid goes in the record
        (a crash recovery ends it, and only it)."""
        if not self.listen_path:
            return                      # no state kept here (and no window can be opened)
        rec = self._read_listen()
        if rec is None:
            log.warning("winlink: a listening Pat (pid %d) started with no window record", pid)
            rec = {"stage": "orphan", "server_pid": os.getpid()}
        rec.update(pid=pid, argv=list(argv), pat_port=self.port)
        try:
            self._save_listen(rec)
        except OSError as e:
            log.warning("winlink: listen record not written: %s", e)

    # the hub

    async def _hub_status(self, hub):
        try:
            async with self.http.get(f"{hub}/api/status", timeout=aiohttp.ClientTimeout(total=5)) as r:
                d = await r.json(content_type=None)
            return d if isinstance(d, dict) else None
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError, AttributeError):
            return None

    async def _hub_post(self, hub, path, body):
        """(HTTP status, JSON body) or (None, why)."""
        try:
            async with self.http.post(f"{hub}{path}", json=body, headers={"X-Ranch-Radio": "1"},
                                      timeout=aiohttp.ClientTimeout(total=5)) as r:
                return r.status, await r.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError, AttributeError) as e:
            return None, str(e) or type(e).__name__

    async def _hub_close(self, hub):
        """Close the hub's inbound window (a few tries). True once it says so."""
        if not hub:
            return False
        for i in range(3):
            code, body = await self._hub_post(hub, "/api/inbound/close", {})
            if code == 200:
                log.info("winlink: the hub's inbound window is closed")
                return True
            log.warning("winlink: closing the hub's inbound window: %s %s", code, body)
            await asyncio.sleep(0.5 * (i + 1))
        log.warning("winlink: the hub's inbound window could not be closed; it runs out by itself at its end")
        return False

    async def _hub_links(self, hub, call):
        """The hub's links whose local end is `call`, or None (hub not answering)."""
        st = await self._hub_status(hub)
        if st is None or not isinstance(st.get("links"), list):
            return None
        return [l for l in st["links"] if str(l.get("local", "")).upper() == call.upper()]

    async def _links_clear(self, hub, call, seconds):
        end = time.monotonic() + seconds
        while True:
            links = await self._hub_links(hub, call)
            if links == []:
                return True
            if time.monotonic() >= end:
                return False
            await asyncio.sleep(0.2)

    # start (B2)

    async def listen_start(self, ws, hours=None):
        st, plan = self.station, self.listen_plan

        def refuse(why):
            log.info("winlink: not listening: %s", why)
            st.notice(f"Not listening: {why}.", "warn", ws)

        if plan is None:
            return refuse("listening is not set up on this station (the host file's winlink.listen)")
        if not self.running:
            return refuse("Winlink is switched off")
        try:
            hours = plan.max_hours if hours is None else float(hours)
        except (TypeError, ValueError):
            return refuse(f"{hours!r} is not a number of hours")
        if not MIN_HOURS <= hours <= plan.max_hours:
            return refuse(f"a window is {MIN_HOURS:g} to {plan.max_hours:g} hours")
        if getattr(st, "tx_stopped", None):
            return refuse("transmit is stopped (STOP TX); press Resume first")
        if self.listening or self._listen_busy:
            return refuse("a listen window is already open" if self.listening and not self._listen_busy
                          else self._listen_busy_text()[:1].lower() + self._listen_busy_text()[1:])
        if self._admitting or self._our_session():
            return refuse("a Winlink session is running")
        if self.inbound:
            return refuse(f"Pat is in a session with {self.inbound['call']}")
        if self.pat_busy:
            return refuse("Pat is busy (connected or dialing)" if self._status_live
                          else "Pat's state is not known (no status from it)")
        if self.blocked:
            return refuse(self.blocked)
        if not self.listen_path:
            return refuse("this server keeps no state (--addons-state), so a window could not be undone "
                          "after a crash")
        # Reserved from here (no await above): a connect or a second window waits.
        self._listen_busy = "starting"
        self._listen_abort = False
        self._send_listen()
        handed = False
        try:
            target_for = getattr(st, "winlink_listen_target", None)
            target, why = target_for() if target_for else (None, "no packet hub on this station")
            if why:
                return refuse(why[:1].lower() + why[1:].rstrip("."))
            name, hub, tuner = target["name"], target["hub"], target["tuner"]
            if tuner is None:
                return refuse(f"the UV-PRO add-on is off, so nothing can put the {name} on "
                              f"{plan.mhz_text()} MHz and back")
            hs = await self._hub_status(hub)
            if self._closing or self._listen_abort:
                return refuse("stopped before it started")
            if hs is None:
                return refuse(f"the {name} packet hub is not answering")
            if not (hs.get("tx") or {}).get("enabled"):
                return refuse(f"the {name} hub's transmit is off, so every call would be ignored; "
                              "turn it on first")
            call = (self.mycall or "").upper()
            if not call:
                return refuse("Pat has not said this station's call yet")
            why = self.preflight()
            if why:
                return refuse(why)
            now = time.time()
            rec = {"method": plan.method, "hz": plan.hz, "since": now, "until": now + hours * 3600,
                   "server_pid": os.getpid(),
                   "stage": "tuning", "hub": hub, "name": name, "call": call, "pat_port": self.port}
            try:
                self._save_listen(rec)    # before the radio is touched: a crash from here is undone
            except OSError as e:
                return refuse(f"the window's record could not be written ({e})")
            lane = TunedLane(name, tuner, st.notice, listening=True)
            try:
                tuned = await lane.arm(ws, f"{plan.method}:///{call}?freq={plan.hz / 1000:g}")
            except Exception as e:         # noqa: BLE001 - refused, nothing left changed
                log.exception("winlink: tuning %s for listening", name)
                tuned, lane.tuned = False, None
                st.notice(f"Not listening: tuning the {name} failed ({type(e).__name__}: {e}).", "warn", ws)
            if not tuned:
                self._drop_listen()       # the tuner put back whatever it had changed
                log.info("winlink: not listening: the %s was not tuned", name)
                return
            self._listen_lane = lane
            self.listening = {**rec, "stage": "starting"}
            handed = True
            await self._listen_open(ws, rec, hours)
        finally:
            if not handed:
                self._listen_busy = None
                self._send_listen()

    async def _listen_open(self, ws, rec, hours):
        """Tuned: Pat with --listen, the hub's window, then read it all back.
        Anything missing runs the end path (B4)."""
        st = self.station

        def still_wanted():
            if self._closing:
                raise PatRestartError("the server is stopping")
            if self._listen_abort:
                raise PatRestartError("stopped from the page")
        try:
            still_wanted()
            busy = (self._closing and "the server is stopping") or (
                self.inbound and f"Pat is in a session with {self.inbound['call']}") or (
                self._our_session() and "a Winlink session started")
            if busy:
                raise PatRestartError(busy)
            self._set_listen_stage(rec, "starting")
            self.want["listen"] = rec["method"]
            await self.restart_pat("a listen window")
            still_wanted()
            code, body = await self._hub_post(rec["hub"], "/api/inbound",
                                              {"calls": [rec["call"]], "seconds": max(1, rec["until"] - time.time())})
            if code != 200:
                raise PatRestartError(f"the hub did not open its inbound window ({code}: {body})")
            why = await self._listen_readback(rec)
            still_wanted()
            if why:
                raise PatRestartError(why)
        except (PatRestartError, OSError) as e:
            log.warning("winlink: listen window not opened: %s; undoing it", e)
            st.notice(f"Not listening: {e}. Everything is being put back.", "warn", ws)
            await self._end_listen(f"not opened: {e}")
            return
        self._set_listen_stage(rec, "listening")
        self.listening = dict(rec)
        self._listen_busy = None
        log.warning("winlink: listening for calls to %s on %.4f MHz (%s) until %s", rec["call"], rec["hz"] / 1e6,
                    rec["method"], time.strftime("%H:%M", time.localtime(rec["until"])))
        st.notice(f"Listening for calls to {rec['call']} on {rec['hz'] / 1e6:.3f} MHz for {hours:g} h. APRS and "
                  "the iGate are off-frequency meanwhile.", "info")
        self._send_listen()
        self._listen_task = asyncio.create_task(self._listen_watch())

    async def _listen_readback(self, rec):
        """Why the window isn't really open (None = it is): Pat's Status lists
        exactly the method, the hub has MyCall registered and its inbound
        window for MyCall ends within HUB_UNTIL_SLACK_S of ours."""
        end = time.monotonic() + READBACK_S
        why = "no read-back yet"
        while True:
            listeners = self.status.get("active_listeners") if self._status_live else None
            hs = await self._hub_status(rec["hub"])
            if listeners != [rec["method"]]:
                why = f"Pat is listening on {listeners!r}, not [{rec['method']!r}]"
            elif hs is None:
                why = "the hub is not answering"
            elif not any(rec["call"] in [str(c).upper() for c in a.get("calls", [])]
                         for a in (hs.get("clients") or {}).get("agw", [])):
                why = f"Pat has not registered {rec['call']} with the hub"
            elif not (hs.get("inbound") and rec["call"] in hs["inbound"].get("calls", [])
                      and abs(hs["inbound"].get("until", 0) - rec["until"]) <= HUB_UNTIL_SLACK_S):
                why = f"the hub's inbound window is {hs.get('inbound')!r}"
            else:
                return None
            if time.monotonic() >= end:
                return why
            await asyncio.sleep(0.2)

    async def _listen_watch(self):
        """End the window at `until`, or when what it stands on goes away."""
        misses, asked = 0, 0.0
        while True:
            await asyncio.sleep(LISTEN_POLL_S)
            w = self.listening
            if w is None or self._listen_busy:
                return
            reason = None
            if time.time() >= w["until"]:
                reason = "the window ran out"
            else:
                target_for = getattr(self.station, "winlink_listen_target", None)
                # The add-ons switched off end it at once; the hub itself is
                # asked here, and must fail HUB_MISSES times in a row (a radio
                # link that drops for a moment comes back by itself).
                target, why = target_for(need_up=False) if target_for else (None, "no packet hub")
                if why:
                    reason = why.rstrip(".")
                elif target["tuner"] is None:
                    reason = "the UV-PRO add-on was switched off"
                elif time.monotonic() - asked >= HUB_POLL_S:
                    asked = time.monotonic()
                    hs = await self._hub_status(w["hub"])
                    if hs is None or not (hs.get("radio") or {}).get("up"):
                        misses += 1
                        if misses >= HUB_MISSES:
                            reason = ("the packet hub stopped answering" if hs is None
                                      else "the packet hub's radio is down")
                    else:
                        misses = 0
                        if not (hs.get("inbound") and w["call"] in hs["inbound"].get("calls", [])):
                            reason = "the hub's inbound window is gone (the hub restarted?)"
            if reason:
                asyncio.ensure_future(self._end_listen(reason))
                return

    # end (B4)

    async def listen_stop(self, ws=None):
        if self._listen_busy == "starting":
            # Still being set up: it is undone at its next step (one end path).
            self._listen_abort = True
            self.station.notice("Stopping: the window is undone as soon as its current step is done.", "info", ws)
            return
        if not self.listening:
            if self._listen_busy != "ending":
                self.station.notice("Not listening.", "info", ws)
            return
        await self._end_listen("stopped from the page")

    async def _end_listen(self, reason, stopping=False):
        """One end at a time; a second caller waits for the first."""
        t = self._listen_end_task
        if t is None or t.done():
            t = self._listen_end_task = asyncio.ensure_future(self._do_end_listen(reason, stopping))
        await asyncio.shield(t)

    async def _do_end_listen(self, reason, stopping):
        st = self.station
        rec = {**(self._read_listen() or {}), **(self.listening or {})}
        hub, call = rec.get("hub"), (rec.get("call") or self.mycall or "").upper()
        lane, started = self._listen_lane, rec.get("since") or time.time()
        self._listen_busy = "ending"
        self._send_listen()
        log.warning("winlink: listen window ending: %s", reason)
        t = self._listen_task
        if t and not t.done() and t is not asyncio.current_task():
            t.cancel()
        # 1. Revoke first: nothing new may start answering.
        self.want["listen"] = None
        if self.listen_path and rec:
            try:
                self._save_listen({**rec, "stage": "ending"})
            except OSError as e:
                log.warning("winlink: listen record not written: %s", e)
        await self._hub_close(hub)
        # 2. An exchange still up: graceful, then the owner's signals (A4).
        if hub and call:
            links = await self._hub_links(hub, call)
            if self.inbound or links:
                who = self.inbound["call"] if self.inbound else links[0].get("remote", "?")
                log.info("winlink: %s is still connected; disconnecting", who)
                await self._disconnect(dirty=False)
                if not await self._links_clear(hub, call, ABORT_GRACE_S) and not stopping:
                    ib = self.inbound or {"call": who, "remote": f"AX.25:{who}"}
                    await self._escalate(ib, gone=lambda proc, s: self._links_clear(hub, call, s))
        # 3. Pat without the flag, read back.
        proc = self.proc
        if stopping:
            async with self._life:
                await self._end_child(self.proc)
        elif proc is not None and proc.returncode is None and "--listen" in (self.proc_argv or ["--listen"]):
            try:
                await self.restart_pat("the listen window ended")
            except PatRestartError as e:
                log.warning("winlink: Pat not restarted after the window (%s); it is not running", e)
        listeners = await self._listeners_cleared()
        if listeners:
            log.error("winlink: Pat still lists listeners %r after the window ended", listeners)
        # 4. The radio back, only once no link is left on it.
        clear = (not hub or not call
                 or await self._links_clear(hub, call, ABORT_GRACE_S if not stopping else STOP_WAIT_S))
        self.listening = None
        if lane is not None and clear:
            await self._listen_restore_radio(lane, started, reason, stopping)
        elif lane is not None:
            log.warning("winlink: a packet link on the %s outlived the window; the radio goes back once it "
                        "drops", lane.name)
            st.notice(f"A packet link on the {lane.name} is still up; the radio goes back once it drops.", "warn")
            if not stopping:
                self._listen_restore = asyncio.ensure_future(self._restore_when_clear(lane, hub, call, started, reason))
                self._listen_lane = None
                self._listen_busy = None
                self._send_listen()
                return
        self._listen_lane = None
        self._drop_listen()
        self._listen_busy = None
        self._send_listen()
        st.notice(f"Listening ended: {reason}.", "info")
        log.warning("winlink: listen window ended (%s)", reason)

    async def _listeners_cleared(self):
        """Pat's listeners after the window: [] (or Pat not running) is clear."""
        end = time.monotonic() + READBACK_S
        while True:
            p = self.proc
            if p is None or p.returncode is not None or self.blocked:
                return []
            got = self.status.get("active_listeners") if self._status_live else None
            if got == []:
                return []
            if time.monotonic() >= end:
                return got
            await asyncio.sleep(0.1)

    async def _listen_restore_radio(self, lane, started, reason, stopping):
        try:
            await lane.end(started, True, reason, (lambda: True) if stopping else (lambda: self._closing), self._send)
        except Exception:                  # noqa: BLE001 - the tuner keeps its own record and retries
            log.exception("winlink: putting the %s back after listening", lane.name)

    async def _restore_when_clear(self, lane, hub, call, started, reason):
        if await self._links_clear(hub, call, LINK_WAIT_S):
            await self._listen_restore_radio(lane, started, reason, False)
            self._drop_listen()
            return
        log.error("winlink: a packet link outlived the listen window by %gs; the %s was NOT put back",
                  LINK_WAIT_S, lane.name)
        self.station.notice(f"The {lane.name} was NOT put back after listening: a packet link stayed up. "
                            "Check the radio.", "warn")

    # crash recovery (B5)

    async def _recover_listen(self):
        """A record left by a server that stopped mid-window: close the hub's
        window, end a listening Pat that outlived it, put the radio back. Never
        resumed. Runs at start, before any Pat is launched."""
        rec = self._read_listen()
        if rec is None:
            return
        owner = rec.get("server_pid")
        if isinstance(owner, int) and owner != os.getpid() and self._is_server(owner):
            # Another server that is still running wrote it (a second server on
            # the same state, as every test server once was): its window, its
            # Pat and the hub's inbound window are its own, never ended here.
            log.warning("winlink: the listen record %s belongs to a running server (pid %d); left alone",
                        self.listen_path, owner)
            return
        log.warning("winlink: a listen window (stage %s) was left by a server that did not end it; ending it",
                    rec.get("stage"))
        await self._hub_close(rec.get("hub"))
        pid = rec.get("pid")
        if isinstance(pid, int) and pid > 0:
            await self._end_stale_pat(pid, rec)
        target_for = getattr(self.station, "winlink_listen_target", None)
        target, _ = target_for(need_hub=False) if target_for else (None, None)
        tuner = (target or {}).get("tuner")
        if tuner is None:
            log.warning("winlink: no UV-PRO add-on to put the radio back after the old window; its own record "
                        "does that when it starts")
            self._drop_listen()
            return
        self._listen_restore = asyncio.ensure_future(self._recover_radio(tuner, rec))

    async def _recover_radio(self, tuner, rec):
        done, why = await tuner.restore()
        if done:
            log.info("winlink: the radio is back after the old listen window")
            self.station.notice("Winlink: a listen window a restart interrupted is ended; the radio is back on "
                                "its channel.", "info")
        else:
            log.warning("winlink: the radio is not back after the old listen window yet (%s); the UV-PRO "
                        "add-on keeps trying", why)
        self._drop_listen()

    @classmethod
    def _is_server(cls, pid):
        """Is pid a running Ranch Radio server (server.py, or station.py, which runs Pat on Linux)?"""
        cmd = cls._command_of(pid) or ""
        return any(os.path.basename(w) in ("server.py", "station.py") for w in cmd.split())

    @staticmethod
    def _command_of(pid):
        try:
            r = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                               timeout=5)
            return r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    async def _end_stale_pat(self, pid, rec):
        """SIGTERM, then SIGKILL, the recorded pid, but only if it still runs
        a listening Pat on our port (a pid can be reused)."""
        cmd = self._command_of(pid)
        port = rec.get("pat_port") or self.port
        if not cmd:
            log.info("winlink: the old listening Pat (pid %d) is gone", pid)
            return
        if "--listen" not in cmd.split() or f"127.0.0.1:{port}" not in cmd or " http" not in f" {cmd}":
            log.warning("winlink: pid %d is not the old listening Pat (%r); left alone", pid, cmd[:200])
            return
        log.warning("winlink: the old listening Pat (pid %d) outlived its server; stopping it", pid)
        for sig, wait in ((signal.SIGTERM, STOP_WAIT_S), (signal.SIGKILL, STOP_WAIT_S)):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return
            end = time.monotonic() + wait
            while time.monotonic() < end:
                await asyncio.sleep(0.1)
                if self._command_of(pid) != cmd:
                    return
        log.error("winlink: the old listening Pat (pid %d) did not exit", pid)

    # --- HTTP: the allow-list of Pat's API ---

    def outbox_busy(self):
        """The outbox is Pat's: a session of ours arming (admission) or
        running, or Pat in any exchange (one we did not start too) or its
        state unknown."""
        return (self._admitting or self._our_session() or self.pat_busy or self.inbound is not None
                or self.listening is not None or bool(self._listen_busy))

    async def _outbox_change(self, call):
        async with self._outbox_lock:
            if self.outbox_busy():
                if (self.listening or self._listen_busy) and not self.inbound:
                    text = (self._listen_busy_text() + " (a station that calls may take the outbox); "
                            "it can't be changed until listening ends.")
                elif self.inbound and not self._our_session():
                    text = (f"Pat is in a session with {self.inbound['call']}; the outbox can't be "
                            "changed until it ends.")
                elif not (self._admitting or self._our_session()) and not self._status_live:
                    text = "Pat's state is not known (it isn't answering); the outbox can't be changed now."
                else:
                    text = OUTBOX_BUSY
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
