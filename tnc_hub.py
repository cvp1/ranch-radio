#!/usr/bin/env python3
"""TNC hub: share a radio's built-in KISS TNC (BTECH UV-PRO over Bluetooth)
between APRS apps and Pat Winlink, with a status page and transmit safety.

    python3 tnc_hub.py --tnc bt:38-d2-00-00-ee-18 --mycall N9ABC          # receive only
    python3 tnc_hub.py --tnc bt:38-d2-00-00-ee-18 --mycall N9ABC --tx     # allow transmit

What it serves (loopback only unless --bind says otherwise; neither protocol
has authentication, so anyone who can reach these ports can transmit):

    AGWPE  127.0.0.1:8000   Pat (ax25+agwpe), YAAC, Xastir. Connected mode
                            (Winlink) runs here: the hub is the AX.25 stack.
    KISS   127.0.0.1:8001   any KISS-over-TCP APRS client.
    CTRL   127.0.0.1:8002   the radio's command protocol (Gaia frames), for
                            the UV-PRO add-on (addons/uvpro_bridge.py).
    HTTP   0.0.0.0:7310     status page: link, heard stations, TX log.

One process owns the radio's serial port, as server.py does for the HF rigs;
apps share it through the hub instead of fighting over the port.

Transmit safety (every frame, from every client, goes through one gate):
- Off unless started with --tx (the hard switch). Any page on the LAN may
  switch it off or back on, like PTT on the radio pages; a POST must carry
  X-Ranch-Radio: 1 and come from this server's own pages (cross-site guard).
- The frame's source callsign must be one of --allow-calls (default: the base
  of --mycall), any SSID. Nothing goes out under someone else's call.
- Frames must decode as AX.25 and stay under 400 bytes; KISS parameter
  commands (TXDELAY, etc.) from clients are dropped, never sent to the radio.
- Rate caps by class (set by the hub from where a frame came from: app for
  KISS/AGWPE, msg and ack for messaging, digi, connected for a link's
  frames): --max-frames-per-min overall (a runaway guard, default 300, its
  last --ack-reserve kept for acks); UI frames (app + msg + ack)
  --max-ui-total-per-min 12 with --ack-reserve 4 for acks, apps
  --max-ui-per-min 6, messages 2, acks 6; UI airtime 15 s/min with 3 s for
  acks. Digipeated and connected-mode frames count only toward the overall
  cap. Every refusal names its cap.
- Every transmitted and refused frame is logged (packet-logs/tx.jsonl).
- The operator's Off is persisted (<log-dir>/tx-state.json, fsynced) before
  transmit clears and before the page is told; a hub restarted with --tx
  stays off until On. An unreadable or corrupt file starts it off.
- Off-channel holds (POST /api/hold from this machine; /api/hold/release
  from the add-on or a page): while any is set, no APRS message or ack goes.
  The UV-PRO add-on sets one before it retunes the radio for Winlink.

APRS messaging (aprs_msg.py, --aprs-messaging send|full): compose from the
page (POST /api/aprs/message), retries, acks; see that module.

iGate (optional, --igate-login): heard packets go to APRS-IS (igate.py),
marked qAO (a receive-only iGate); our own calls' frames are never gated.
Receive only: nothing from APRS-IS reaches the transmit gate or the radio.
With --igate-observe it also reads APRS-IS for our own calls (stored as
port 'is', shown "via internet"; never acked, matched or sent).
With --igate-beacon-min and --lat/--lon it beacons its own position to
APRS-IS (never RF) as a receive-only iGate.

Fill-in digipeater (optional, --digi CALL): WIDE1-1 requests that no wide
digipeater repeats within --digi-viscous seconds, and requests to CALL by
name (digi.py). Through the transmit gate like everything else, with its own
--max-digi-per-min.

Radio control: the UV-PRO's KISS channel (RFCOMM 1) also carries its command
protocol, Gaia frames (FF 01 ...), next to the KISS frames (linux-station,
2026-10-16: a GET_DEV_INFO there was answered while packets flowed). The hub
splits the two (RadioSplit) and serves the command side on --control-port,
so the add-on programs the radio while packet stays up. Only the commands in
CONTROL_ALLOWED pass: never HT_SEND_DATA (packet data that would skip the
transmit gate), programmable functions (some key PTT) or firmware.

Lending the radio: the UV-PRO answers its separate control channel (RFCOMM
4) only while nothing holds its KISS channel; the add-on can borrow the
radio for that instead of using the control port. POST /api/lend {"seconds": n, "to":
name} from this machine makes the hub let go of the radio for up to n
seconds; asking again extends it; POST /api/return takes it back at once.
The hub refuses while a packet link is up or just after it transmitted, and
takes the radio back by itself when the time runs out, so a borrower that
dies can't keep packet off.

Answering inbound connects: an inbound SABM (another station calling one of
the calls an AGWPE client registered, Pat's MyCall) is IGNORED, with no UA and
no DM (a DM would be a transmission), unless an inbound window is open for
that call. POST /api/inbound {"calls": ["N9ABC"], "seconds": n} from this
machine opens one (asking again extends it), POST /api/inbound/close ends it,
and it runs out by itself; the hub starts with none. Status shows
"inbound": {"calls", "until"} or null; ignored calls are counted
(inbound_ignored) and logged. The hub, the process that owns the transmitter,
is so the independent expiry for a listen window: a server that crashed or a
Pat that survived can't keep the station answering past "until". Before this,
Pat holding its registration after any packet session was enough for the hub
to answer a caller with a UA (and a DISC when Pat closed it), unattended.
"""

import argparse
import asyncio
import collections
import json
import logging
import os
import pathlib
import socket
import struct
import sys
import termios
import time

from aiohttp import web

import aprs
import aprs_msg
import aprs_probe
import aprs_store
import ax25
import digi
import igate
import webguard
from ax25 import U_DISC, U_DM, U_FRMR, U_SABM, U_UA, U_UI, Frame, FrameError, KissDecoder, kiss_wrap
from ax25_link import CONNECTED, CONNECTING, DISCONNECTED, Link, TxRefused

HERE = pathlib.Path(__file__).resolve().parent
log = logging.getLogger("tnc_hub")

AGW_HDR = struct.Struct("<B3xcxBx10s10sI4x")   # 36 bytes
LOG_CAP_BYTES = 5 * 1024 * 1024
RECENT_MAX = 300
HEARD_MAX = 500
AGW_MAX_DATA = 4096
RECONNECT_MIN_S, RECONNECT_MAX_S = 2.0, 30.0
LEND_MAX_S = 300              # one request; the borrower asks again to keep it
LEND_AFTER_TX_S = 5.0         # a frame handed to the radio may still be on the air
INBOUND_MAX_S = 12 * 3600     # one inbound window request (a listen window's cap, plan D2)
LINK_UTYPES = (U_SABM, U_UA, U_DISC, U_DM, U_FRMR)
HOLD_BY = __import__("re").compile(r"[a-z0-9][a-z0-9-]{0,39}")

# The radio's command protocol on the KISS channel: Gaia frames, FF 01 flags
# length, then 4 command bytes (group, command; bit 15 = reply), length bytes
# of body, and a checksum byte if flags bit 0 is set (benlink gaia_frame.py).
GAIA_START, GAIA_VERSION = 0xFF, 0x01
GAIA_GROUP_BASIC = 2
# What a control client may send (BASIC group, requests only). Names as in
# benlink's BasicCommand.
CONTROL_ALLOWED = {
    4: "GET_DEV_INFO", 5: "READ_STATUS", 6: "REGISTER_NOTIFICATION", 10: "READ_SETTINGS",
    11: "WRITE_SETTINGS", 13: "READ_RF_CH", 14: "WRITE_RF_CH", 20: "GET_HT_STATUS", 21: "SET_HT_ON_OFF",
    22: "GET_VOLUME", 23: "SET_VOLUME", 24: "RADIO_GET_STATUS", 25: "RADIO_SET_MODE", 26: "RADIO_SEEK_UP",
    27: "RADIO_SEEK_DOWN", 28: "RADIO_SET_FREQ", 33: "READ_BSS_SETTINGS", 34: "WRITE_BSS_SETTINGS",
    55: "GET_PF", 60: "SET_REGION", 71: "SET_APRS_PATH", 72: "GET_APRS_PATH", 73: "READ_REGION_NAME",
    76: "GET_POSITION"}


def gaia_size(head):
    """A whole Gaia frame's length, from its first 4 bytes."""
    return 4 + 4 + head[3] + (1 if head[2] & 0x01 else 0)


class RadioSplit:
    """The KISS channel's bytes -> (KISS bytes, [Gaia frames]).

    A Gaia frame starts only between KISS frames (before the first FEND, or
    after a FEND that closed one), where FF can't be a KISS type byte from
    this TNC (port 15 command 15 is "return", which a TNC never sends). Its
    end comes from its length, never from looking for FEND: a Gaia body can
    hold C0 bytes (a frequency or a name can)."""

    def __init__(self):
        self.boundary = True
        self.gaia = None

    def feed(self, data):
        kiss, frames = bytearray(), []
        for b in data:
            if self.gaia is not None:
                self.gaia.append(b)
                if len(self.gaia) == 2 and b != GAIA_VERSION:
                    kiss += self.gaia            # not Gaia after all
                    self.gaia = None
                    self.boundary = b == ax25.FEND
                elif len(self.gaia) >= 4 and len(self.gaia) == gaia_size(self.gaia):
                    frames.append(bytes(self.gaia))
                    self.gaia = None
                    self.boundary = True
                continue
            if self.boundary and b == GAIA_START:
                self.gaia = bytearray([b])
                continue
            kiss.append(b)
            self.boundary = b == ax25.FEND
        return bytes(kiss), frames


class GaiaReader:
    """A control client's bytes -> whole Gaia frames. Anything that isn't one
    raises ValueError (the client is dropped)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= 4:
            if self.buf[0] != GAIA_START or self.buf[1] != GAIA_VERSION:
                raise ValueError(f"not a Gaia frame: {bytes(self.buf[:4]).hex()}")
            n = gaia_size(self.buf)
            if len(self.buf) < n:
                break
            out.append(bytes(self.buf[:n]))
            del self.buf[:n]
        return out


def gaia_command(frame):
    """(group, command, is_reply) of a Gaia frame."""
    group = int.from_bytes(frame[4:6], "big")
    cmd = int.from_bytes(frame[6:8], "big")
    return group, cmd & 0x7FFF, bool(cmd & 0x8000)


def now():
    return time.time()


# ------------------------------------------------------------------ gate

# Frame classes (APRS messaging plan, 2026-11-07, section 3.3). The hub sets
# the class from where a frame came from, never from anything the caller says:
#   app        a KISS or AGWPE client's UI frame (and any U frame not a link's)
#   ack, msg   the messaging engine's own acks and messages (aprs_msg.py)
#   digi       the fill-in digipeater (another station's frame repeated)
#   connected  I and S frames, and SABM/UA/DISC/DM/FRMR of a link in hub.links
UI_CLASSES = ("app", "msg", "ack")
APRS_BAUD = 1200              # the APRS channel's rate, whatever --baud says for T1
HDLC_FLAG_BITS = 16           # one opening and one closing flag
FCS_BYTES = 2
STUFFING = 1.2                # worst-case bit stuffing (one extra bit per five ones)


def ui_airtime_s(nbytes, txdelay_ms=300, baud=APRS_BAUD):
    """Seconds one UI frame of NBYTES (address..info, no FCS) keeps the channel:
    (flags + (bytes + FCS) x 8 x stuffing) / baud + TX delay. Conservative by
    design: every bit stuffed at the worst rate, the radio's whole key-up delay."""
    return (HDLC_FLAG_BITS + (nbytes + FCS_BYTES) * 8 * STUFFING) / baud + txdelay_ms / 1000.0


class TxGate:
    """Every frame's last check before the radio. Caps per minute (a sliding
    60 s window): the overall runaway guard with ACK_RESERVE frames kept for
    acks; UI counts for app + msg + ack with acks' own reserve; UI airtime with
    an ack reserve. Digipeated frames keep their own cap and count only toward
    the overall one; connected-mode frames count only toward the overall one,
    so a Winlink transfer keeps its full rate."""

    def __init__(self, enabled, allow_calls, max_per_min, ui_per_min, digi_per_min=6, ui_total_per_min=12,
                 ack_reserve=4, msg_per_min=2, ack_per_min=6, airtime_s_per_min=15.0,
                 ack_airtime_reserve_s=3.0, overall_ack_reserve=4, txdelay_ms=300):
        self.enabled = enabled
        self.allowed_by_start = enabled
        self.allow = {c.upper() for c in allow_calls}
        self.max_per_min, self.ui_per_min, self.digi_per_min = max_per_min, ui_per_min, digi_per_min
        self.ui_total, self.ack_reserve = ui_total_per_min, ack_reserve
        self.msg_per_min, self.ack_per_min = msg_per_min, ack_per_min
        self.airtime_s, self.ack_airtime_reserve_s = float(airtime_s_per_min), float(ack_airtime_reserve_s)
        self.overall_ack_reserve = overall_ack_reserve
        self.txdelay_ms = txdelay_ms
        self.holds = []                      # off-channel holds (Hub.state): no ack or msg while any
        self.sent = collections.deque()      # (t, cls, airtime_s)

    def _prune(self, t):
        while self.sent and t - self.sent[0][0] > 60:
            self.sent.popleft()

    def usage(self):
        """What the last minute used, per class, and the UI airtime."""
        self._prune(now())
        n = collections.Counter(c for _, c, _ in self.sent)
        return {"total": len(self.sent), **{c: n[c] for c in ("app", "msg", "ack", "digi", "connected")},
                "ui_airtime_s": round(sum(a for _, c, a in self.sent if c in UI_CLASSES), 2)}

    def caps(self):
        return {"total_per_min": self.max_per_min, "total_ack_reserve": self.overall_ack_reserve,
                "ui_total_per_min": self.ui_total, "ack_reserve": self.ack_reserve,
                "app_per_min": self.ui_per_min, "msg_per_min": self.msg_per_min, "ack_per_min": self.ack_per_min,
                "digi_per_min": self.digi_per_min, "airtime_s_per_min": self.airtime_s,
                "ack_airtime_reserve_s": self.ack_airtime_reserve_s, "txdelay_ms": self.txdelay_ms}

    def check(self, f: Frame, raw: bytes, cls="app"):
        """Why this frame may not go, or None. Every refusal names the cap."""
        if not self.enabled:
            return "transmit is off"
        if cls != "digi" and f.src.call not in self.allow:
            return f"source {f.src} is not an allowed callsign ({', '.join(sorted(self.allow))})"
        if len(raw) > ax25.MAX_FRAME_BYTES:
            return "frame too long"
        if cls in ("ack", "msg") and self.holds:
            h = self.holds[0]
            return f"off-channel hold by {h['by']} ({h.get('why') or 'off the APRS channel'})"
        t = now()
        self._prune(t)
        total = len(self.sent)
        if cls == "ack":
            if total >= self.max_per_min:
                return f"rate limit ({self.max_per_min} frames/min)"
        elif total >= self.max_per_min - self.overall_ack_reserve:
            return (f"rate limit ({self.max_per_min} frames/min, the last {self.overall_ack_reserve} "
                    f"kept for acks)")
        n = collections.Counter(c for _, c, _ in self.sent)
        if cls == "digi":
            if n["digi"] >= self.digi_per_min:
                return f"digipeater rate limit ({self.digi_per_min}/min)"
            return None
        if cls == "connected":
            return None
        ui = n["app"] + n["msg"] + n["ack"]
        if cls == "ack":
            if n["ack"] >= self.ack_per_min:
                return f"ack rate limit ({self.ack_per_min}/min)"
            if ui >= self.ui_total:
                return f"UI total rate limit ({self.ui_total}/min)"
        else:
            if ui >= self.ui_total - self.ack_reserve:
                return (f"UI total rate limit ({self.ui_total}/min, the last {self.ack_reserve} "
                        f"kept for acks)")
            if cls == "app" and n["app"] >= self.ui_per_min:
                return f"UI rate limit ({self.ui_per_min}/min)"
            if cls == "msg" and n["msg"] >= self.msg_per_min:
                return f"message rate limit ({self.msg_per_min}/min)"
        used = sum(a for _, c, a in self.sent if c in UI_CLASSES)
        air = ui_airtime_s(len(raw), self.txdelay_ms)
        budget = self.airtime_s if cls == "ack" else self.airtime_s - self.ack_airtime_reserve_s
        if used + air > budget + 1e-9:
            return (f"UI airtime limit ({self.airtime_s:g} s/min" +
                    ("" if cls == "ack" else f", the last {self.ack_airtime_reserve_s:g} s kept for acks") +
                    f"; {used:.1f} s used, this frame {air:.1f} s)")
        return None

    def record(self, f: Frame, raw: bytes, cls="app"):
        air = ui_airtime_s(len(raw), self.txdelay_ms) if cls in UI_CLASSES else 0.0
        self.sent.append((now(), cls, air))


# ------------------------------------------------------- transmit state

# The operator's stop and the off-channel holds, on disk (plan 2026-11-07,
# sections 3.1 and 3.2): <log-dir>/tx-state.json, written atomically (temp
# file, fsync, rename, fsync of the directory). Off is written BEFORE the live
# gate clears and before the page is told, so a crash can never restart a
# transmitting hub after an Off was acknowledged. Holds survive a restart too.
STATE_FILE = "tx-state.json"
TEST_CRASH_ENV = "RANCH_RADIO_TEST_CRASH_AT"   # tests only: os._exit at a named point


def test_point(name):
    """A named crash point for the stop and messaging tests (unset: nothing)."""
    if os.environ.get(TEST_CRASH_ENV) == name:
        log.warning("test crash point %s", name)
        logging.shutdown()
        os._exit(75)


class TxState:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.inhibit = False
        self.holds = []
        self.t = None
        self.by = None
        self.notice = None          # why transmit came up off, for the page

    def load(self):
        """'missing', 'ok' or 'bad' (unreadable or corrupt: the caller starts off)."""
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return "missing"
        except OSError as e:
            self.notice = f"transmit state {self.path.name} unreadable ({e.strerror or e}): transmit starts off"
            return "bad"
        try:
            d = json.loads(raw)
            inhibit = d["tx_inhibit"]
            holds = d.get("holds", [])
            if not isinstance(inhibit, bool) or not isinstance(holds, list) \
                    or not all(isinstance(h, dict) and isinstance(h.get("by"), str) for h in holds):
                raise ValueError("bad fields")
        except (ValueError, KeyError, TypeError) as e:
            self.notice = f"transmit state {self.path.name} is corrupt ({e}): transmit starts off"
            return "bad"
        self.inhibit, self.holds, self.t, self.by = inhibit, holds, d.get("t"), d.get("by")
        return "ok"

    def save(self, inhibit=None, holds=None, by=None):
        """Write the whole state; raises OSError (nothing in memory changes then)."""
        new_inhibit = self.inhibit if inhibit is None else inhibit
        new_holds = self.holds if holds is None else holds
        t = now()
        data = json.dumps({"tx_inhibit": new_inhibit, "t": t, "by": by or self.by, "holds": new_holds})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, data.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)
        dfd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        self.inhibit, self.holds, self.t, self.by = new_inhibit, list(new_holds), t, by or self.by


# ------------------------------------------------------------- jsonl log

class JsonLog:
    def __init__(self, path: pathlib.Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, rec):
        try:
            if self.path.exists() and self.path.stat().st_size > LOG_CAP_BYTES:
                self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))
            with self.path.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError as e:
            log.warning("log write failed (%s): %s", self.path, e)


# ----------------------------------------------------------- radio port

# ------------------------------------------------- keep the radio's link

# The UV-PRO's KISS service won't take a new connection after its link drops,
# however the drop happened, until the radio is power-cycled (tested 2026-10-19:
# a 60 s wait, a fresh ACL and a clean stop all refused; RFCOMM 4 still
# answered). So the hub never lets the link drop on a restart: once connected,
# it parks a copy of the Bluetooth socket in systemd's file-descriptor store
# (FileDescriptorStoreMax=1, FileDescriptorStorePreserve=yes in the unit), and
# the next start adopts it instead of dialling. The copy is withdrawn when the
# link really dies, so a dead one is never handed on.

STORE_NAME = "radio"


def sd_notify(state, fds=()):
    """A message to systemd's notify socket (with fds attached); False without one."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return False
    if path.startswith("@"):
        path = "\0" + path[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(path)               # send_fds ignores an address on an unconnected socket
            if fds:
                socket.send_fds(s, [state.encode()], list(fds))
            else:
                s.send(state.encode())
        return True
    except OSError as e:
        log.warning("systemd notify failed: %s", e)
        return False


def stored_socket(name=STORE_NAME):
    """The socket systemd handed this start under `name` (LISTEN_FDS), or None.
    Every other handed fd is closed."""
    try:
        if int(os.environ.get("LISTEN_PID", "0")) != os.getpid():
            return None
        n = int(os.environ.get("LISTEN_FDS", "0"))
    except ValueError:
        return None
    names = os.environ.get("LISTEN_FDNAMES", "").split(":")
    for k in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        os.environ.pop(k, None)
    found = None
    for i in range(n):
        fd = 3 + i
        if found is None and i < len(names) and names[i] == name:
            try:
                found = socket.socket(fileno=fd)
                continue
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass
    return found


class RadioPort:
    """The radio's KISS byte stream: a serial device (Bluetooth SPP, USB, pty)
    or tcp:host:port. Reconnects with backoff whenever the link drops."""

    def __init__(self, spec, on_frame, on_state, on_control=None):
        self.spec, self.on_frame, self.on_state = spec, on_frame, on_state
        self.on_control = on_control or (lambda frame: None)
        self.split = RadioSplit()
        self.fd = None
        self.writer = None
        self.bt = None
        self.up = False
        self.decoder = KissDecoder()
        self.last_rx = None
        self.opened_at = None
        self.error = None
        self.held = False         # let go of the radio (lent) until release()
        self.stored = False       # a copy of the link is parked with systemd
        self.adopt = stored_socket() if spec.startswith("bt:") else None
        self._task = None
        self._closed = asyncio.Event()

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        """The hub is shutting down: close our end but leave the parked copy,
        so the link to the radio stays up for the next start."""
        if self._task:
            self._task.cancel()
        self._close(keep_stored=True)

    async def hold(self):
        """Let go of the radio, and don't reopen it until release()."""
        self.held = True
        if self._task and not self._task.done():
            self._task.cancel()          # also interrupts a connect in progress
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._close()
        self.error = None
        self._set_up(False)

    def release(self):
        """Open the radio again now (held or not: reconnect without waiting)."""
        self.held = False
        if self.up:
            return
        if self._task and not self._task.done():
            self._task.cancel()          # a backoff wait or a connect: start over now
        self.start()

    async def _run(self):
        delay = RECONNECT_MIN_S
        while True:
            try:
                await self._open()
                delay = RECONNECT_MIN_S
                self._closed.clear()
                await self._closed.wait()
            except asyncio.CancelledError:
                raise
            except Exception as e:     # noqa: BLE001 - any failure means "retry later"
                self.error = str(e)
                if self.up:
                    self._close()
                log.debug("radio open failed: %s", e)
            self._set_up(False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_S)

    async def _open(self):
        self.decoder = KissDecoder()
        self.split = RadioSplit()
        if self.spec.startswith("bt:"):
            # bt:<address>[:<channel>] - Bluetooth RFCOMM to the radio.
            parts = self.spec[3:].replace("-", ":").split(":")
            channel = parts[6] if len(parts) > 6 else "auto"
            loop = asyncio.get_running_loop()
            if sys.platform.startswith("linux"):
                # BlueZ: a plain RFCOMM socket. "auto" = channel 1, the
                # UV-PRO's "SPP Dev" service by SDP (read on mac-shack 2026-10-14;
                # BlueZ 5.87 ships no sdptool to look it up here).
                addr = ":".join(parts[:6]).upper()
                ch = 1 if channel == "auto" else int(channel)
                sock, self.adopt = self.adopt, None
                if sock is not None:
                    try:
                        peer = sock.getpeername()
                    except OSError as e:
                        peer = (f"not connected: {e}",)
                    if str(peer[0]).upper() != addr:
                        log.warning("parked radio link not usable (%s); dialling", peer[0])
                        sd_notify(f"FDSTOREREMOVE=1\nFDNAME={STORE_NAME}")
                        sock.close()
                        sock = None
                    else:
                        log.info("radio link adopted from the last run (no reconnect)")
                        self.stored = True        # systemd still holds it: don't park it twice
                if sock is None:
                    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                                         socket.BTPROTO_RFCOMM)
                    sock.setblocking(False)
                    try:
                        await asyncio.wait_for(loop.sock_connect(sock, (addr, ch)), 20)
                    except BaseException:
                        sock.close()
                        raise
                sock.setblocking(False)
                if not self.stored and sd_notify(f"FDSTORE=1\nFDNAME={STORE_NAME}", [sock.fileno()]):
                    self.stored = True
                reader, self.writer = await asyncio.open_connection(sock=sock)
                self._set_up(True)
                asyncio.create_task(self._tcp_reader(reader))
                return
            # macOS: IOBluetooth. The /dev/cu.* device didn't bring the link
            # up on mac-shack.
            import bt_rfcomm
            addr = "-".join(parts[:6])
            self.bt = bt_rfcomm.RfcommLink(
                addr, channel,
                on_data=lambda b: loop.call_soon_threadsafe(self._feed, b),
                on_close=lambda why: loop.call_soon_threadsafe(self._bt_closed, why))
            await loop.run_in_executor(None, self.bt.open)
            self._set_up(True)
            return
        if self.spec.startswith("tcp:"):
            host, _, port = self.spec[4:].rpartition(":")
            reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(host, int(port)), 10)
            self._set_up(True)
            asyncio.create_task(self._tcp_reader(reader))
            return
        fd = os.open(self.spec, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            a = termios.tcgetattr(fd)
            a[0] = a[1] = 0
            a[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
            a[2] |= termios.CLOCAL | termios.CREAD
            a[6][termios.VMIN], a[6][termios.VTIME] = 0, 0
            termios.tcsetattr(fd, termios.TCSANOW, a)
        except termios.error:
            pass    # Bluetooth SPP ports accept but ignore most of this
        self.fd = fd
        asyncio.get_running_loop().add_reader(fd, self._on_readable)
        self._set_up(True)

    async def _tcp_reader(self, reader):
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                self._feed(data)
        except (OSError, asyncio.IncompleteReadError) as e:
            self.error = str(e)
        self._close()

    def _on_readable(self):
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            return
        except OSError as e:
            self.error = str(e)
            self._close()
            return
        if not data:
            self.error = "port closed (EOF)"
            self._close()
            return
        self._feed(data)

    def _bt_closed(self, why):
        if self.bt is not None:
            self.error = why
            self._close()

    def _feed(self, data):
        self.last_rx = now()
        kiss, gaia = self.split.feed(data)
        for frame in gaia:
            self.on_control(frame)
        for port, cmd, payload in self.decoder.feed(kiss):
            if cmd == 0:
                self.on_frame(payload)

    def write(self, ax25_bytes: bytes):
        self._write(kiss_wrap(ax25_bytes))

    def write_control(self, frame: bytes):
        """A Gaia frame to the radio (checked by the caller: Hub.control)."""
        self._write(frame)

    def _write(self, data: bytes):
        if self.held:
            raise TxRefused("the radio is lent out")
        if not self.up:
            raise TxRefused("radio not connected")
        try:
            if self.bt is not None:
                self.bt.write(data)
            elif self.writer is not None:
                self.writer.write(data)
            else:
                view = memoryview(data)
                deadline = time.monotonic() + 2
                while view:
                    try:
                        n = os.write(self.fd, view)
                        view = view[n:]
                    except BlockingIOError:
                        if time.monotonic() > deadline:
                            raise TxRefused("radio port not accepting data")
                        time.sleep(0.01)
        except OSError as e:
            self.error = str(e)
            self._close()
            raise TxRefused(f"radio write failed: {e}") from e

    def _close(self, keep_stored=False):
        if self.stored and not keep_stored:
            # The link itself ended: don't hand a dead socket to the next start.
            sd_notify(f"FDSTOREREMOVE=1\nFDNAME={STORE_NAME}")
            self.stored = False
        if self.fd is not None:
            try:
                asyncio.get_running_loop().remove_reader(self.fd)
            except (RuntimeError, ValueError):
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        if self.writer is not None:
            self.writer.close()
            self.writer = None
        if self.bt is not None:
            self.bt.close()
            self.bt = None
        self._closed.set()

    def _set_up(self, up):
        if up != self.up:
            self.up = up
            self.opened_at = now() if up else None
            if up:
                self.error = None
            log.info("radio %s (%s)", "connected" if up else "disconnected", self.spec)
            self.on_state(up)


# ------------------------------------------------------------------- hub

class Hub:
    def __init__(self, args):
        self.args = args
        allow = args.allow_calls or [ax25.base_call(args.mycall)]
        # Off until the transmit state is read (below): a hub stopped by the
        # operator stays stopped across a restart, --tx or not.
        self.gate = TxGate(False, allow, args.max_frames_per_min, args.max_ui_per_min, args.max_digi_per_min,
                           ui_total_per_min=args.max_ui_total_per_min, ack_reserve=args.ack_reserve,
                           msg_per_min=args.max_msg_per_min, ack_per_min=args.max_ack_per_min,
                           airtime_s_per_min=args.max_airtime_s_per_min,
                           ack_airtime_reserve_s=args.ack_airtime_reserve_s,
                           overall_ack_reserve=min(args.ack_reserve, max(0, args.max_frames_per_min - 1)),
                           txdelay_ms=args.aprs_txdelay_ms)
        self.gate.allowed_by_start = args.tx
        self.state = TxState(pathlib.Path(args.log_dir) / STATE_FILE)
        loaded = self.state.load()
        if loaded == "missing":
            self.gate.enabled = args.tx
        elif loaded == "ok":
            self.gate.enabled = args.tx and not self.state.inhibit
            if self.state.inhibit:
                self.state.notice = "stopped by the operator" + (f" ({self.state.by})" if self.state.by else "")
        else:
            log.warning("%s", self.state.notice)
        self.gate.holds = self.state.holds
        self.last_ui_tx = None          # the last app/msg/ack frame sent (the engine paces after it)
        self.aprs_msg = None            # aprs_msg.Engine with --aprs-messaging send|full
        self.probe = None
        self.radio = RadioPort(args.tnc, self.on_radio_frame, self.on_radio_state, self.on_radio_control)
        self.control_clients = set()
        self.links = {}                 # (local, remote) -> (Link, AgwClient)
        self.agw_clients = set()
        self.kiss_clients = set()
        self.heard = {}                 # station -> dict
        self.recent = collections.deque(maxlen=RECENT_MAX)
        self.messages = collections.deque(maxlen=100)
        self.counts = collections.Counter()
        self.started = now()
        self.lent = None                # {"to", "since", "until"} while the radio is lent
        self._lend_end = None
        self.inbound = None             # {"calls": [...], "until"} while inbound connects are answered
        self._inbound_end = None
        self.last_tx = None
        logdir = pathlib.Path(args.log_dir)
        self.rxlog = JsonLog(logdir / "rx.jsonl")
        self.txlog = JsonLog(logdir / "tx.jsonl")
        self.my_bases = {c.upper() for c in allow}
        self.aprs = None                # AprsStore (APRS plan M2), None with --no-aprs
        self.aprs_summary = {}          # refreshed from the store every few seconds
        self.igate = None               # igate.IGate with --igate-login: heard packets to APRS-IS
        self.digi = digi.Digi(args.digi, args.digi_viscous) if args.digi else None
        self._digi_timer = None
        self.loop_lag = collections.deque(maxlen=3000)   # event-loop lag samples, ms

    # -- radio side --

    def on_radio_state(self, up):
        if not up:
            for link, _ in list(self.links.values()):
                link.abort("radio disconnected")
            for c in list(self.control_clients):
                c.close("radio disconnected")

    def on_radio_control(self, frame: bytes):
        self.counts["control_rx"] += 1
        for c in list(self.control_clients):
            c.send(frame)

    def control(self, frame: bytes, origin: str):
        """A control client's Gaia frame: to the radio if its command is one
        a client may send. Raises TxRefused."""
        group, cmd, reply = gaia_command(frame)
        if group != GAIA_GROUP_BASIC or reply or cmd not in CONTROL_ALLOWED:
            self.counts["control_refused"] += 1
            log.warning("control from %s refused: group %d command %d%s", origin, group, cmd,
                        " (reply)" if reply else "")
            raise TxRefused(f"command {cmd} is not one a control client may send")
        self.radio.write_control(frame)
        self.counts["control_tx"] += 1
        log.debug("control %s from %s", CONTROL_ALLOWED[cmd], origin)

    def on_radio_frame(self, raw: bytes):
        self.counts["rx_frames"] += 1
        try:
            f = Frame.decode(raw)
        except FrameError as e:
            self.counts["rx_bad"] += 1
            log.debug("undecodable frame (%s): %s", e, raw.hex())
            return
        text = f.tnc2()
        rec = {"t": now(), "dir": "rx", "text": text}
        self.recent.append(rec)
        self.rxlog.write(rec)
        if self.aprs:
            self.aprs.ingest(rec["t"], "rf", text)
        self._note_heard(f, text)
        if self.igate:
            self.igate.offer(f)
        if self.aprs_msg:
            try:
                self.aprs_msg.on_rf(rec["t"], f)
            except Exception:     # noqa: BLE001 - the receive path never stops for messaging
                log.exception("messaging: heard frame not handled")
        log.info("RX %s", text)
        if self.digi:
            self.digi.heard(f, now())
            self._digi_flush()
        for c in list(self.kiss_clients):
            c.send_frame(raw)
        for c in list(self.agw_clients):
            c.monitor(f, raw)
        self._route_connected(f)

    def _note_heard(self, f, text):
        key = str(f.src)
        h = self.heard.get(key)
        if h is None:
            if len(self.heard) >= HEARD_MAX:
                oldest = min(self.heard, key=lambda k: self.heard[k]["last"])
                del self.heard[oldest]
            h = self.heard[key] = {"station": key, "first": now(), "count": 0}
        h["last"] = now()
        h["count"] += 1
        h["text"] = text
        h["direct"] = not any(d.bit7 for d in f.digis)
        if f.kind == "U" and f.utype == U_UI and f.info[:1] == b":" and len(f.info) > 10:
            addressee = f.info[1:10].decode("latin-1").strip().upper()
            try:
                if ax25.base_call(addressee) in self.my_bases:
                    self.messages.append({"t": now(), "from": key, "to": addressee,
                                          "text": f.info[11:].decode("latin-1")})
            except FrameError:
                pass

    def _route_connected(self, f: Frame):
        key = (str(f.dest), str(f.src))
        entry = self.links.get(key)
        if entry:
            entry[0].handle(f)
            return
        if f.kind == "U" and f.utype == U_SABM:
            for c in self.agw_clients:
                if str(f.dest) in c.registered:
                    if not self.gate.enabled:
                        log.info("inbound connect from %s to %s ignored: transmit is off",
                                 f.src, f.dest)
                        return
                    if not self.inbound_open(str(f.dest)):
                        # No UA and no DM: answering at all is a transmission.
                        self.counts["inbound_ignored"] += 1
                        log.info("inbound connect from %s to %s ignored: no inbound window open",
                                 f.src, f.dest)
                        return
                    link = self._new_link(str(f.dest), str(f.src), c,
                                          [str(d) for d in reversed(f.digis)])
                    link.accept(f)
                    return

    def on_is_line(self, t, text):
        """A packet line from the APRS-IS observer (igate.py, observe=). Stored
        as port 'is' and handed to messaging as evidence; it can never reach
        transmit(): nothing here calls it."""
        parts = aprs_store.parse_line(text)
        d = aprs.decode(*parts) if parts else None
        if d is not None and d.get("error") == "embedded line break":
            self.counts["is_line_break"] += 1        # a bare CR or LF inside: never stored, never two packets
            return
        if self.aprs:
            self.aprs.ingest(t, "is", text)
        if d is None:
            return
        if self.aprs_msg:
            self.aprs_msg.on_is(t, d)

    # -- fill-in digipeater (digi.py) --

    def _digi_flush(self):
        """Send what the digipeater's holds released; wake again for the next."""
        if self._digi_timer:
            self._digi_timer.cancel()
            self._digi_timer = None
        for f in self.digi.due(now()):
            try:
                self.transmit(f, "digi", digi=True)
            except TxRefused:
                self.counts["digi_refused"] += 1
        nxt = self.digi.next_due()
        if nxt is not None:
            loop = asyncio.get_running_loop()
            self._digi_timer = loop.call_later(max(0.0, nxt - now()) + 0.01, self._digi_flush)

    # -- transmit --

    def frame_class(self, f: Frame, origin: str, digi=False):
        """The gate class, from the origin the hub itself gave the frame (a
        client can't choose it: KISS and AGWPE origins are kiss:/agw:)."""
        if digi:
            return "digi"
        if origin == "aprs:ack":
            return "ack"
        if origin == "aprs:msg":
            return "msg"
        if f.kind in ("I", "S"):
            return "connected"
        if f.kind == "U" and f.utype in LINK_UTYPES and (str(f.src), str(f.dest)) in self.links:
            return "connected"
        return "app"

    def transmit(self, f: Frame, origin: str, digi=False):
        """Every frame leaves through here. Raises TxRefused."""
        try:
            raw = f.encode()
        except FrameError as e:
            raise TxRefused(str(e)) from e
        cls = self.frame_class(f, origin, digi)
        why = self.lent_why() or self.gate.check(f, raw, cls)
        text = f.tnc2()
        if why:
            self.counts["tx_refused"] += 1
            self.txlog.write({"t": now(), "dir": "refused", "origin": origin, "cls": cls, "why": why,
                              "text": text})
            log.warning("TX refused (%s) from %s: %s", why, origin, text)
            raise TxRefused(why)
        self.radio.write(raw)
        self.last_tx = now()
        if cls in UI_CLASSES:
            self.last_ui_tx = self.last_tx
        self.gate.record(f, raw, cls)
        self.counts["tx_frames"] += 1
        rec = {"t": now(), "dir": "tx", "origin": origin, "cls": cls, "text": text}
        self.recent.append(rec)
        self.txlog.write(rec)
        log.info("TX %s (%s)", text, origin)

    def transmit_raw(self, raw: bytes, origin: str):
        try:
            f = Frame.decode(raw)
        except FrameError as e:
            self.counts["tx_refused"] += 1
            self.txlog.write({"t": now(), "dir": "refused", "origin": origin,
                              "why": f"undecodable: {e}", "text": raw[:64].hex()})
            raise TxRefused(f"undecodable frame: {e}") from e
        self.transmit(f, origin)

    # -- lending the radio (the UV-PRO add-on's control channel) --

    def lent_why(self):
        if self.lent is None:
            return None
        return (f"the radio is lent to {self.lent['to']} until "
                f"{time.strftime('%H:%M:%S', time.localtime(self.lent['until']))}")

    async def lend(self, seconds, to):
        """Let go of the radio for SECONDS (asking again extends it). Returns
        why not, or None. The check and the letting go happen with nothing in
        between, so no client can start a link in the gap."""
        if self.lent is None:
            busy = [l for l, _ in self.links.values() if l.state != DISCONNECTED]
            if busy:
                return f"a packet link is up ({busy[0].key[1]})"
            if self.last_tx is not None and now() - self.last_tx < LEND_AFTER_TX_S:
                return "the hub transmitted a moment ago; try again in a few seconds"
            log.warning("lending the radio to %s for %gs", to, seconds)
            self.lent = {"to": to, "since": now(), "until": now() + seconds}
            await self.radio.hold()
        else:
            self.lent.update(to=to, until=now() + seconds)
        if self._lend_end:
            self._lend_end.cancel()
        self._lend_end = asyncio.create_task(self._lend_runs_out(seconds))
        return None

    async def _lend_runs_out(self, seconds):
        await asyncio.sleep(seconds)
        self._lend_end = None
        log.warning("radio lend to %s ran out: taking the radio back", (self.lent or {}).get("to"))
        self.take_back()

    def take_back(self):
        """End a lend (if any) and reopen the radio at once."""
        if self._lend_end and self._lend_end is not asyncio.current_task():
            self._lend_end.cancel()
        self._lend_end = None
        if self.lent is not None:
            log.warning("radio back from %s", self.lent["to"])
        self.lent = None
        self.radio.release()

    # -- answering inbound connects (a listen window) --

    def inbound_open(self, call):
        w = self.inbound
        return bool(w and now() < w["until"] and call.upper() in w["calls"])

    def open_inbound(self, calls, seconds):
        """Answer inbound connects to CALLS for SECONDS (asking again
        replaces the calls and extends it)."""
        calls = sorted({ax25.call_str(*ax25.parse_call(c)) for c in calls})
        self.inbound = {"calls": calls, "since": (self.inbound or {}).get("since", now()),
                        "until": now() + seconds}
        log.warning("inbound window open for %s for %gs", ", ".join(calls), seconds)
        if self._inbound_end:
            self._inbound_end.cancel()
        self._inbound_end = asyncio.create_task(self._inbound_runs_out(seconds))

    async def _inbound_runs_out(self, seconds):
        await asyncio.sleep(seconds)
        self._inbound_end = None
        log.warning("inbound window for %s ran out", ", ".join((self.inbound or {}).get("calls", [])))
        self.inbound = None

    def close_inbound(self):
        if self._inbound_end and self._inbound_end is not asyncio.current_task():
            self._inbound_end.cancel()
        self._inbound_end = None
        if self.inbound is not None:
            log.warning("inbound window for %s closed", ", ".join(self.inbound["calls"]))
        self.inbound = None

    # -- the operator's stop and off-channel holds (tx-state.json) --

    def switch_tx(self, want, who):
        """Off: persist the inhibit first, then clear the gate (Off always
        works; a failed write is returned so the page can say so). On: only
        with --tx; persist, then enable. Returns (status, body)."""
        if want and not self.gate.allowed_by_start:
            return 403, {"error": "started without --tx"}
        if not want:
            try:
                test_point("tx-off-before-fsync")
                self.state.save(inhibit=True, by=who)
                test_point("tx-off-after-fsync")
                err = None
            except OSError as e:
                err = f"stopped, not persisted ({e.strerror or e}): a restart may transmit again"
            self.gate.enabled = False
            self.state.notice = "stopped by the operator" if err is None else err
            log.warning("transmit switched OFF from the page (%s)%s", who, f"; {err}" if err else "")
            for link, _ in list(self.links.values()):
                link.abort("transmit switched off")
            if err:
                return 500, {"enabled": False, "error": err}
            return 200, {"enabled": False}
        try:
            self.state.save(inhibit=False, by=who)
        except OSError as e:
            return 500, {"enabled": self.gate.enabled,
                         "error": f"not switched on: the transmit state could not be saved ({e.strerror or e})"}
        self.gate.enabled = True
        self.state.notice = None
        log.warning("transmit switched ON from the page (%s)", who)
        return 200, {"enabled": True}

    def set_hold(self, by, why, back):
        """An off-channel hold (the UV-PRO add-on, before it retunes). Kept in
        memory even when the write fails (it only stops APRS); raises OSError
        then, so the caller refuses to retune."""
        hold = {"by": by, "why": why, "back": back, "t": now()}
        holds = [h for h in self.state.holds if h["by"] != by] + [hold]
        try:
            self.state.save(holds=holds)
        finally:
            self.state.holds = holds
            self.gate.holds = holds
            log.warning("off-channel hold set by %s: %s (back: %s)", by, why or "-", back or "-")

    def release_hold(self, by, who):
        """Release BY's hold. On a failed write the hold stays (the safe side)."""
        holds = [h for h in self.state.holds if h["by"] != by]
        if len(holds) == len(self.state.holds):
            return False
        self.state.save(holds=holds)
        self.gate.holds = self.state.holds
        log.warning("off-channel hold by %s released (%s)", by, who)
        self.txlog.write({"t": now(), "dir": "hold-released", "by": by, "who": who})
        return True

    # -- links --

    def _new_link(self, local, remote, client, digis=()):
        a = self.args
        link = Link(local, remote, lambda fr: self.transmit(fr, f"agw:{client.name}"),
                    client, digis=digis, k=a.maxframe, paclen=a.paclen, n2=a.retries,
                    frack=a.frack, baud=a.baud)
        self.links[link.key] = (link, client)
        return link

    def drop_link(self, link):
        self.links.pop(link.key, None)

    def find_link(self, a, b):
        return (self.links.get((a, b)) or self.links.get((b, a)) or (None, None))[0]

    # -- status --

    def status(self):
        r = self.radio
        return {
            "radio": {"spec": self.args.tnc, "up": r.up, "since": r.opened_at,
                      "last_rx": r.last_rx, "error": r.error, "lent": self.lent},
            "inbound": (dict(self.inbound) if self.inbound and now() < self.inbound["until"] else None),
            "tx": {"enabled": self.gate.enabled, "allowed_by_start": self.gate.allowed_by_start,
                   "inhibit": self.state.inhibit, "notice": self.state.notice, "holds": list(self.state.holds),
                   "allow_calls": sorted(self.gate.allow),
                   "max_per_min": self.gate.max_per_min, "ui_per_min": self.gate.ui_per_min,
                   "caps": self.gate.caps(), "used": self.gate.usage()},
            "mycall": self.args.mycall,
            "title": getattr(self.args, "title", "UV-PRO Packet"),
            "counts": dict(self.counts),
            "clients": {"agw": [c.describe() for c in self.agw_clients],
                        "kiss": [c.name for c in self.kiss_clients],
                        "control": [c.name for c in self.control_clients]},
            "links": [{"local": l.key[0], "remote": l.key[1], "state": l.state,
                       "outstanding": l.outstanding(), **l.stats}
                      for l, _ in self.links.values()],
            "heard": sorted(self.heard.values(), key=lambda h: -h["last"])[:100],
            "recent": list(self.recent)[-100:][::-1],
            "messages": list(self.messages)[::-1],
            "started": self.started,
            "ports": {"agw": self.args.agw_port, "kiss": self.args.kiss_port,
                      "control": self.args.control_port, "bind": self.args.bind},
            "aprs": self.aprs_status(),
            "igate": self.igate.status() if self.igate else {"enabled": False},
            "digi": self.digi.status() if self.digi else {"enabled": False},
        }

    def aprs_status(self):
        lag = sorted(self.loop_lag)
        loop = {"p99_ms": round(lag[int(len(lag) * 0.99) - 1 if len(lag) > 1 else 0], 1) if lag else None,
                "max_ms": round(lag[-1], 1) if lag else None, "samples": len(lag)}
        me = ({"lat": self.args.lat, "lon": self.args.lon}
              if self.args.lat is not None and self.args.lon is not None else None)
        msg = (self.aprs_msg.status() if self.aprs_msg else
               {"mode": self.args.aprs_messaging, "effective": "off",
                "why": "no APRS store (--no-aprs)" if self.args.no_aprs and self.args.aprs_messaging != "off" else None})
        if not self.aprs:
            return {"enabled": False, "me": me, "loop": loop, "messaging": msg, "probe": None}
        return {"enabled": True, "me": me, "loop": loop, "queue": self.aprs.qsize(),
                "dropped": self.aprs.dropped, "errors": self.aprs.errors, **self.aprs_summary, "messaging": msg,
                "probe": self.probe.status() if self.probe else None}

    async def open_aprs(self):
        """Open the store, backfill it once from the logs and reload the heard
        list and our messages from it. Runs before the radio opens; nothing
        here reaches transmit()."""
        a = self.args
        path = a.aprs_db or str(pathlib.Path(a.log_dir) / "aprs.db")
        loop = asyncio.get_running_loop()
        self.aprs = await loop.run_in_executor(None, lambda: aprs_store.AprsStore(path).start())
        done = await self.aprs.call("backfill", a.log_dir)
        if "skipped" not in done:
            log.info("APRS store: backfilled %d lines from %s (%d malformed)",
                     done["lines"], a.log_dir, done["malformed"])
        for h in await self.aprs.call("heard", HEARD_MAX):
            self.heard[h["station"]] = h
        self.messages.extend(await self.aprs.call("messages_to", self.my_bases, self.messages.maxlen))
        self.aprs_summary = await self.aprs.call("summary")

    async def aprs_refresh(self):
        while True:
            await asyncio.sleep(5)
            try:
                self.aprs_summary = await self.aprs.call("summary")
            except Exception:     # noqa: BLE001
                log.exception("APRS summary failed")

    async def lag_ticker(self, every=0.02):
        loop = asyncio.get_running_loop()
        while True:
            t0 = loop.time()
            await asyncio.sleep(every)
            self.loop_lag.append((loop.time() - t0 - every) * 1000)


# ------------------------------------------------------------ AGWPE side

def _call(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("latin-1").strip().upper()


def _c10(s: str) -> bytes:
    return s.encode("latin-1")[:10].ljust(10, b"\0")


class AgwClient:
    """One AGWPE TCP client (Pat, YAAC, ...)."""

    def __init__(self, hub, reader, writer):
        self.hub, self.reader, self.writer = hub, reader, writer
        peer = writer.get_extra_info("peername")
        self.name = f"{peer[0]}:{peer[1]}" if peer else "agw"
        self.registered = set()
        self.mon = False
        self.raw = False

    def describe(self):
        return {"name": self.name, "calls": sorted(self.registered), "monitor": self.mon,
                "raw": self.raw}

    def send(self, kind, frm="", to="", data=b"", pid=0, port=0):
        if self.writer.is_closing():
            return
        self.writer.write(AGW_HDR.pack(port, kind.encode(), pid, _c10(frm), _c10(to), len(data))
                          + data)

    # -- monitoring --

    def monitor(self, f: Frame, raw: bytes):
        if self.raw:
            self.send("K", str(f.src), str(f.dest), b"\0" + raw)
        if self.mon and f.kind == "U" and f.utype == U_UI:
            ts = time.strftime("%H:%M:%S")
            text = (f" 1:Fm {f.src} To {f.dest} <UI pid={f.pid or 0xF0:02X} Len={len(f.info)} >"
                    f"[{ts}]\r").encode() + f.info + b"\r"
            self.send("U", str(f.src), str(f.dest), text, pid=f.pid or 0xF0)

    # -- link events (Link calls these) --

    def on_connected(self, link, inbound):
        local, remote = link.key
        word = "To" if inbound else "With"
        self.send("C", remote, local, f"*** CONNECTED {word} Station {remote}\r".encode())

    def on_data(self, link, data):
        local, remote = link.key
        self.send("D", remote, local, data, pid=0xF0)

    def on_disconnected(self, link, reason):
        local, remote = link.key
        self.hub.drop_link(link)
        if "retry" in reason or "no answer" in reason:
            msg = f"*** DISCONNECTED RETRYOUT With {remote}\r"
        else:
            msg = f"*** DISCONNECTED From Station {remote} ({reason})\r"
        self.send("d", remote, local, msg.encode())

    # -- requests --

    async def run(self):
        hub = self.hub
        log.info("AGWPE client %s connected", self.name)
        try:
            while True:
                hdr = await self.reader.readexactly(AGW_HDR.size)
                port, kind, pid, frm, to, n = AGW_HDR.unpack(hdr)
                if n > AGW_MAX_DATA:
                    log.warning("AGWPE %s: frame of %d bytes, dropping client", self.name, n)
                    return
                data = await self.reader.readexactly(n) if n else b""
                self.handle(kind.decode("latin-1"), _call(frm), _call(to), pid, data)
                await self.writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            log.info("AGWPE client %s gone", self.name)
            hub.agw_clients.discard(self)
            for link, owner in list(hub.links.values()):
                if owner is self:
                    link.disconnect("application closed")
            self.writer.close()

    def handle(self, k, frm, to, pid, data):
        hub = self.hub
        if k == "R":
            self.send("R", data=struct.pack("<II", 2005, 127))
        elif k == "G":
            self.send("G", data=b"1;Port1 UV-PRO KISS TNC via tnc_hub;\0")
        elif k == "g":
            caps = struct.pack("<BBBBBBBBI", 0, 0xFF, 30, 3, 63, 10, hub.args.maxframe,
                               sum(1 for _, o in hub.links.values() if o is self), 0)
            self.send("g", data=caps)
        elif k == "X":
            ok = 1
            try:
                ax25.parse_call(frm)
            except FrameError:
                ok = 0
            if ok and any(frm in c.registered for c in hub.agw_clients if c is not self):
                ok = 0
            if ok:
                self.registered.add(frm)
            self.send("X", frm, data=bytes([ok]))
        elif k == "x":
            self.registered.discard(frm)
        elif k == "m":
            self.mon = not self.mon
        elif k == "k":
            self.raw = not self.raw
        elif k == "y":
            n = sum(l.outstanding() for l, _ in hub.links.values())
            self.send("y", data=struct.pack("<I", n))
        elif k == "Y":
            link = hub.find_link(frm, to)
            self.send("Y", frm, to, struct.pack("<I", link.outstanding() if link else 0))
        elif k == "M":
            self._ui(frm, to, pid, data, [])
        elif k == "V":
            ndig = data[0] if data else 0
            digis = [_call(data[1 + 10 * i: 11 + 10 * i]) for i in range(ndig)]
            self._ui(frm, to, pid, data[1 + 10 * ndig:], digis)
        elif k == "K":
            try:
                hub.transmit_raw(data[1:], f"agw:{self.name}")
            except TxRefused as e:
                log.info("AGWPE raw frame refused: %s", e)
        elif k in ("C", "v", "c"):
            digis = []
            if k == "v" and data:
                digis = [_call(data[1 + 10 * i: 11 + 10 * i]) for i in range(data[0])]
            self._connect(frm, to, digis)
        elif k == "D":
            link = hub.find_link(frm, to)
            if link is None or link.state not in (CONNECTED, CONNECTING):
                log.info("AGWPE data for %s>%s with no link", frm, to)
                return
            try:
                link.send(data)
            except (BufferError, ConnectionError) as e:
                log.warning("send on %s failed: %s", link.key, e)
        elif k == "d":
            link = hub.find_link(frm, to)
            if link is None or link.state == DISCONNECTED:
                remote = to
                self.send("d", remote, frm, f"*** DISCONNECTED From Station {remote}\r".encode())
            else:
                link.disconnect("application disconnect")
        else:
            log.debug("AGWPE %s: ignoring '%s' frame", self.name, k)

    def _ui(self, frm, to, pid, info, digis):
        try:
            f = Frame.ui(frm, to, info, digis=digis, pid=pid or 0xF0)
            self.hub.transmit(f, f"agw:{self.name}")
        except (TxRefused, FrameError) as e:
            log.info("AGWPE UI frame refused: %s", e)

    def _connect(self, frm, to, digis):
        hub = self.hub
        why = None
        if not hub.gate.enabled:
            why = "transmit is off on the hub"
        elif hub.lent_why():
            why = hub.lent_why()
        elif not hub.radio.up:
            why = "radio not connected"
        else:
            try:
                if ax25.base_call(frm) not in hub.gate.allow:
                    why = f"{frm} is not an allowed callsign"
                ax25.parse_call(to)
            except FrameError as e:
                why = str(e)
        if why is None and hub.find_link(frm, to) is not None:
            why = "already connected to that station"
        if why:
            log.info("connect %s>%s refused: %s", frm, to, why)
            self.send("d", to, frm, f"*** DISCONNECTED From Station {to} ({why})\r".encode())
            return
        link = hub._new_link(frm, to, self, digis)
        try:
            link.connect()
        except TxRefused as e:
            link.abort(f"transmit refused: {e}")


# ------------------------------------------------------------- KISS side

class KissClient:
    def __init__(self, hub, reader, writer):
        self.hub, self.reader, self.writer = hub, reader, writer
        peer = writer.get_extra_info("peername")
        self.name = f"{peer[0]}:{peer[1]}" if peer else "kiss"
        self.decoder = KissDecoder()
        self.dropped_cmds = 0

    def send_frame(self, raw):
        if not self.writer.is_closing():
            self.writer.write(kiss_wrap(raw))

    async def run(self):
        log.info("KISS client %s connected", self.name)
        try:
            while True:
                data = await self.reader.read(4096)
                if not data:
                    break
                for port, cmd, payload in self.decoder.feed(data):
                    if cmd != 0:
                        self.dropped_cmds += 1
                        log.info("KISS %s: dropped parameter command %d (never sent to the radio)",
                                 self.name, cmd)
                        continue
                    try:
                        self.hub.transmit_raw(payload, f"kiss:{self.name}")
                    except TxRefused as e:
                        log.info("KISS frame from %s refused: %s", self.name, e)
                await self.writer.drain()
        except ConnectionError:
            pass
        finally:
            log.info("KISS client %s gone", self.name)
            self.hub.kiss_clients.discard(self)
            self.writer.close()


# ---------------------------------------------------------- control side

class ControlClient:
    """One client of the radio's command protocol (the UV-PRO add-on's
    bridge): Gaia frames both ways. Every frame from the radio goes to it;
    its frames go through Hub.control."""

    def __init__(self, hub, reader, writer):
        self.hub, self.reader, self.writer = hub, reader, writer
        peer = writer.get_extra_info("peername")
        self.name = f"{peer[0]}:{peer[1]}" if peer else "control"
        self.gaia = GaiaReader()

    def send(self, frame):
        if not self.writer.is_closing():
            self.writer.write(frame)

    def close(self, why):
        if not self.writer.is_closing():
            log.info("control client %s closed: %s", self.name, why)
            self.writer.close()

    async def run(self):
        log.info("control client %s connected", self.name)
        try:
            if not self.hub.radio.up or self.hub.radio.held:
                self.close("the radio is not connected" if not self.hub.radio.held else "the radio is lent out")
                return
            while True:
                data = await self.reader.read(4096)
                if not data:
                    break
                try:
                    frames = self.gaia.feed(data)
                except ValueError as e:
                    self.close(str(e))
                    break
                for frame in frames:
                    try:
                        self.hub.control(frame, f"control:{self.name}")
                    except TxRefused as e:
                        log.info("control frame from %s refused: %s", self.name, e)
                await self.writer.drain()
        except ConnectionError:
            pass
        finally:
            log.info("control client %s gone", self.name)
            self.hub.control_clients.discard(self)
            self.writer.close()


# -------------------------------------------------------------- web page

# Cross-site guard (2026-10-19, APRS plan M0). The LAN is trusted and any page
# on it may switch transmit; what must not is a web page from elsewhere, open in
# a browser on the LAN, POSTing here. Every POST carries X-Ranch-Radio: 1 (a
# custom header makes a cross-site browser send a CORS preflight, which this
# server never answers), and a browser's Origin, when present, must be this
# server. The station's proxy checks the browser's Origin itself, then
# forwards with the header and without the Origin.
CSRF_HEADER = "X-Ranch-Radio"


same_origin = webguard.same_origin


def cross_site_guard(allow_hosts=()):
    """Every request names a Host we know (DNS rebinding, review 2026-10-19);
    every POST carries the header and, from a browser, this server's Origin."""
    @web.middleware
    async def guard(req, handler):
        if not webguard.host_allowed(req.host, allow_hosts):
            return web.json_response({"error": "unknown host name (add it with --allow-host)"}, status=403)
        if req.method not in ("GET", "HEAD", "OPTIONS"):
            if req.headers.get(CSRF_HEADER) != "1":
                return web.json_response({"error": f"missing {CSRF_HEADER} header"}, status=403)
            if not same_origin(req):
                return web.json_response({"error": "cross-site request refused"}, status=403)
        return await handler(req)
    return guard


def make_web(hub):
    app = web.Application(middlewares=[cross_site_guard(getattr(hub.args, "allow_host", []))])

    async def index(_):
        return web.FileResponse(HERE / "static" / "packet.html", headers={"Cache-Control": "no-cache"})

    async def status(_):
        return web.json_response(hub.status())

    async def set_tx(req):
        try:
            body = await req.json()
            want = bool(body.get("enabled"))
        except (ValueError, AttributeError):
            return web.json_response({"error": "expected JSON {\"enabled\": bool}"}, status=400)
        # On from any page when started with --tx, like PTT on the radio pages
        # (APRS plan D7, 2026-10-19); --tx is the hard switch. Anyone may stop.
        code, out = hub.switch_tx(want, str(req.remote))
        return web.json_response(out, status=code)

    def local(req):
        return req.remote in ("127.0.0.1", "::1")

    async def hold(req):
        if not local(req):
            return web.json_response({"error": "holds are only set from this machine"}, status=403)
        try:
            body = await req.json()
            by, why, back = body.get("by"), str(body.get("why") or "")[:120], str(body.get("back") or "")[:60]
        except (ValueError, AttributeError):
            return web.json_response({"error": "expected JSON {\"by\", \"why\", \"back\"}"}, status=400)
        if not isinstance(by, str) or not HOLD_BY.fullmatch(by):
            return web.json_response({"error": "by: a lower-case name"}, status=400)
        try:
            hub.set_hold(by, why, back)
        except OSError as e:
            return web.json_response({"error": f"hold set, not persisted ({e.strerror or e})",
                                      "holds": hub.state.holds}, status=500)
        return web.json_response({"holds": hub.state.holds})

    async def hold_release(req):
        # The add-on (this machine) after a verified restore, or the operator
        # from the packet page (any page, as transmit Off is).
        try:
            body = await req.json()
            by = body.get("by")
        except (ValueError, AttributeError):
            return web.json_response({"error": "expected JSON {\"by\"}"}, status=400)
        if not isinstance(by, str) or not HOLD_BY.fullmatch(by):
            return web.json_response({"error": "by: a lower-case name"}, status=400)
        who = ("operator " if body.get("operator") else "") + str(req.remote)
        try:
            if not hub.release_hold(by, who):
                return web.json_response({"error": f"no hold by {by}", "holds": hub.state.holds}, status=404)
        except OSError as e:
            return web.json_response({"error": f"not released: the transmit state could not be saved "
                                               f"({e.strerror or e})", "holds": hub.state.holds}, status=500)
        return web.json_response({"holds": hub.state.holds})

    async def lend(req):
        if not local(req):
            return web.json_response({"error": "lending the radio is only allowed from this machine"},
                                     status=403)
        try:
            body = await req.json()
            seconds = body.get("seconds")
            to = str(body.get("to") or "control")[:40]
        except (ValueError, AttributeError):
            return web.json_response({"error": "expected JSON {\"seconds\": n, \"to\": name}"}, status=400)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) \
                or not 1 <= seconds <= LEND_MAX_S:
            return web.json_response({"error": f"seconds: 1 to {LEND_MAX_S}"}, status=400)
        why = await hub.lend(float(seconds), to)
        if why:
            log.info("radio not lent to %s: %s", to, why)
            return web.json_response({"error": why}, status=409)
        return web.json_response({"lent": hub.lent})

    async def give_back(req):
        if not local(req):
            return web.json_response({"error": "only allowed from this machine"}, status=403)
        hub.take_back()
        return web.json_response({"lent": None})

    async def inbound(req):
        if not local(req):
            return web.json_response({"error": "inbound windows are only opened from this machine"},
                                     status=403)
        try:
            body = await req.json()
            calls, seconds = body.get("calls"), body.get("seconds")
        except (ValueError, AttributeError):
            return web.json_response({"error": "expected JSON {\"calls\": [CALL], \"seconds\": n}"},
                                     status=400)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) \
                or not 1 <= seconds <= INBOUND_MAX_S:
            return web.json_response({"error": f"seconds: 1 to {INBOUND_MAX_S}"}, status=400)
        if not isinstance(calls, list) or not 1 <= len(calls) <= 4 \
                or not all(isinstance(c, str) for c in calls):
            return web.json_response({"error": "calls: a list of 1 to 4 callsigns"}, status=400)
        try:
            for c in calls:
                ax25.parse_call(c)
        except FrameError as e:
            return web.json_response({"error": str(e)}, status=400)
        hub.open_inbound(calls, float(seconds))
        return web.json_response({"inbound": hub.status()["inbound"]})

    async def inbound_close(req):
        if not local(req):
            return web.json_response({"error": "only allowed from this machine"}, status=403)
        hub.close_inbound()
        return web.json_response({"inbound": None})

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/inbound", inbound)
    app.router.add_post("/api/inbound/close", inbound_close)
    app.router.add_post("/api/tx", set_tx)
    app.router.add_post("/api/hold", hold)
    app.router.add_post("/api/hold/release", hold_release)
    app.router.add_post("/api/lend", lend)
    app.router.add_post("/api/return", give_back)

    # APRS (plan M2): read-only views of the store.
    async def from_store(name, *args):
        if not hub.aprs:
            raise web.HTTPNotFound(text="APRS is off (--no-aprs)")
        try:
            return await hub.aprs.call(name, *args)
        except aprs_store.queue.Full:
            raise web.HTTPServiceUnavailable(text="APRS store busy")

    def rev_arg(req):
        try:
            return max(0, int(req.query.get("rev", "0")))
        except ValueError:
            raise web.HTTPBadRequest(text="rev: a number")

    async def aprs_stations(req):
        return web.json_response(await from_store("stations", rev_arg(req)))

    async def aprs_objects(req):
        return web.json_response(await from_store("objects", rev_arg(req)))

    async def aprs_station(req):
        s = await from_store("station", req.match_info["call"][:12])
        if s is None:
            raise web.HTTPNotFound(text="not heard")
        return web.json_response(s)

    async def aprs_messages(req):
        return web.json_response({"messages": await from_store("messages", req.query.get("with", "")[:12] or None)})

    # APRS messaging (plan 2026-11-07, 3.5): a page sends like radio PTT.
    async def aprs_message(req):
        if not hub.aprs_msg:
            return web.json_response({"error": "messaging is off"}, status=409)
        try:
            body = await req.json()
            to, text = body.get("to"), body.get("text")
        except (ValueError, AttributeError):
            return web.json_response({"error": "expected JSON {\"to\", \"text\"}"}, status=400)
        try:
            row = await hub.aprs_msg.compose(to, text)
        except aprs_msg.MsgError as e:
            return web.json_response({"error": str(e)}, status=409 if str(e).startswith(("not now", "messaging is off"))
                                     else 400)
        log.warning("APRS message %s to %s from the page (%s)", row["msgno"], row["dst"], req.remote)
        return web.json_response({"id": row["id"], "state": row["state"], "msgno": row["msgno"]})

    async def aprs_thread(req):
        call = req.query.get("with", "")[:12]
        owned = hub.aprs_msg.owned if hub.aprs_msg else [hub.args.mycall]
        if not call:
            return web.json_response({"with": None, "threads": await from_store("correspondents", owned)})
        rows = await from_store("thread", call, owned)
        active = {r["id"]: r for r in (hub.aprs_msg.out.values() if hub.aprs_msg else ())}
        out = []
        for r in rows:
            if r["dir"] == "out":
                live = active.get(r["id"])
                r = {**r, **(hub.aprs_msg.describe(live) if live else {}), "via": "rf"}
            else:
                r = {**r, "via": "internet" if r["port"] == "is" else "rf"}
            out.append(r)
        return web.json_response({"with": aprs.norm_call(call), "messages": out})

    # The round-trip probe (aprs_probe.py; plan 2026-11-07, section 4).
    async def aprs_probe_start(req):
        if not hub.probe:
            return web.json_response({"error": "messaging is off"}, status=409)
        try:
            body = await req.json()
            armed = body.get("armed") is True
        except (ValueError, AttributeError):
            return web.json_response({"error": "expected JSON {\"armed\": true}"}, status=400)
        try:
            p = await hub.probe.begin(armed)
        except aprs_probe.ProbeError as e:
            return web.json_response({"error": str(e)}, status=409)
        log.warning("APRS probe %d from the page (%s)", p["id"], req.remote)
        return web.json_response(hub.probe.describe(p))

    async def aprs_probes(_):
        rows = await from_store("probes", 20)
        return web.json_response({"probes": [hub.probe.describe(p) if hub.probe else p for p in rows],
                                  "probe": hub.probe.status() if hub.probe else None})

    app.router.add_post("/api/aprs/probe", aprs_probe_start)
    app.router.add_get("/api/aprs/probes", aprs_probes)
    app.router.add_post("/api/aprs/message", aprs_message)
    app.router.add_get("/api/aprs/thread", aprs_thread)
    app.router.add_get("/api/aprs/stations", aprs_stations)
    app.router.add_get("/api/aprs/objects", aprs_objects)
    app.router.add_get("/api/aprs/station/{call}", aprs_station)
    app.router.add_get("/api/aprs/messages", aprs_messages)
    app.router.add_static("/static", HERE / "static")

    # Revalidate the page and its scripts on every load, so a browser never
    # keeps an old page after the hub is updated (as the station does).
    async def no_cache(req, resp):
        if req.path == "/" or req.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
    app.on_response_prepare.append(no_cache)
    return app


# ------------------------------------------------------------------ main

async def amain(args):
    hub = Hub(args)
    tasks = [asyncio.create_task(hub.lag_ticker())]

    async def on_agw(r, w):
        c = AgwClient(hub, r, w)
        hub.agw_clients.add(c)
        await c.run()

    async def on_kiss(r, w):
        c = KissClient(hub, r, w)
        hub.kiss_clients.add(c)
        await c.run()

    async def on_control(r, w):
        c = ControlClient(hub, r, w)
        hub.control_clients.add(c)
        await c.run()

    # The page first (so the backfill can be watched), then the APRS store and
    # its backfill, then the radio and the client ports.
    runner = None
    if args.http_port:
        runner = web.AppRunner(make_web(hub), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, args.http_bind, args.http_port).start()
    if not args.no_aprs:
        await hub.open_aprs()
        tasks.append(asyncio.create_task(hub.aprs_refresh()))
        if args.aprs_messaging != "off":
            hub.aprs_msg = aprs_msg.Engine(hub, args.aprs_messaging, args.aprs_owned_calls, args.aprs_firmware_acks,
                                           args.aprs_path, cooldown_s=args.aprs_cooldown)
            await hub.aprs_msg.start()
            if hub.aprs_msg.why:
                log.warning("messaging: %s", hub.aprs_msg.why)
            log.info("messaging: %s (configured %s) as %s", hub.aprs_msg.effective, args.aprs_messaging,
                     ", ".join(hub.aprs_msg.owned))
            hub.probe = await aprs_probe.Probe(hub, hub.aprs_msg, args.aprs_probe_target, args.aprs_probe_text).start()
    if args.igate_login:
        beacon = (igate.beacon_info(args.lat, args.lon, args.igate_comment)
                  if args.igate_beacon_min else None)
        own = {ax25.base_call(c) for c in args.aprs_owned_calls}
        observe = None
        if args.igate_observe:
            calls = "/".join(sorted(f"{c}*" for c in own))
            observe = f"b/{calls} g/{calls}"
        hub.igate = igate.IGate(args.igate_login, args.igate_passcode, args.igate_server,
                                beacon=beacon, beacon_s=args.igate_beacon_min * 60, own_bases=own,
                                observe=observe, on_line=hub.on_is_line).start()
    hub.radio.start()
    servers = []
    if args.agw_port:
        servers.append(await asyncio.start_server(on_agw, args.bind, args.agw_port))
    if args.kiss_port:
        servers.append(await asyncio.start_server(on_kiss, args.bind, args.kiss_port))
    if args.control_port:
        # loopback only, whatever --bind says: it can rewrite the radio
        servers.append(await asyncio.start_server(on_control, "127.0.0.1", args.control_port))
    log.info("tnc_hub up: radio %s, AGWPE %s:%s, KISS %s:%s, page http://%s:%s/, transmit %s "
             "(allowed: %s)", args.tnc, args.bind, args.agw_port, args.bind, args.kiss_port,
             args.http_bind, args.http_port, "ON" if hub.gate.enabled else "OFF",
             ", ".join(sorted(hub.gate.allow)))
    try:
        await asyncio.Event().wait()
    finally:
        for link, _ in list(hub.links.values()):
            link.abort("hub shutting down")
        for s in servers:
            s.close()
        if runner:
            await runner.cleanup()
        await hub.radio.stop()
        if hub.igate:
            await hub.igate.stop()
        if hub.probe:
            await hub.probe.stop()
        if hub.aprs_msg:
            await hub.aprs_msg.stop()
        for t in tasks:
            t.cancel()
        if hub.aprs:
            await asyncio.get_running_loop().run_in_executor(None, hub.aprs.stop)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tnc", default="bt:38-d2-00-00-ee-18",
                   help="radio KISS port: bt:<address>[:<channel>] (Bluetooth via IOBluetooth, "
                        "macOS), a serial device, or tcp:host:port (default %(default)s)")
    p.add_argument("--mycall", required=True, help="your callsign, e.g. N9ABC")
    p.add_argument("--title", default="UV-PRO Packet", help="the page's heading (the host file's packet title)")
    p.add_argument("--allow-calls", type=lambda s: [c.strip().upper() for c in s.split(",") if c.strip()],
                   help="base callsigns allowed as a transmit source (default: --mycall's)")
    p.add_argument("--tx", action="store_true", help="allow transmitting (default: receive only)")
    p.add_argument("--bind", default="127.0.0.1", help="address for the AGWPE and KISS ports")
    p.add_argument("--agw-port", type=int, default=8000, help="0 turns it off")
    p.add_argument("--kiss-port", type=int, default=8001, help="0 turns it off")
    p.add_argument("--control-port", type=int, default=8002,
                   help="the radio's command protocol, loopback only (0 turns it off)")
    p.add_argument("--http-bind", default="0.0.0.0")
    p.add_argument("--allow-host", action="append", default=[],
                   help="a dotted host name browsers use for the page (IPs, single-label, .local, "
                        ".home.arpa and .ts.net names are always allowed)")
    p.add_argument("--http-port", type=int, default=7310, help="0 turns it off")
    p.add_argument("--maxframe", type=int, default=4, help="AX.25 window k (1-7)")
    p.add_argument("--paclen", type=int, default=128, help="data bytes per I frame")
    p.add_argument("--retries", type=int, default=10, help="AX.25 N2")
    p.add_argument("--frack", type=float, default=3.0, help="base AX.25 T1, seconds")
    p.add_argument("--baud", type=int, default=1200,
                   help="on-air packet rate; T1 grows with the air time of frames in flight")
    # A runaway guard, not a traffic limit: 1200 baud carries about two frames
    # a second (~120/min). 60/min killed a lossy Winlink transfer in testing
    # (2026-10-14); a refused frame ends the link.
    p.add_argument("--max-frames-per-min", type=int, default=300)
    p.add_argument("--max-ui-per-min", type=int, default=6, help="UI frames from apps (KISS/AGWPE) a minute")
    # The gate's classes (APRS messaging plan 2026-11-07, 3.3).
    p.add_argument("--max-ui-total-per-min", type=int, default=12, help="UI frames a minute: apps, messages, acks")
    p.add_argument("--ack-reserve", type=int, default=4,
                   help="of those (and of --max-frames-per-min), how many only acks may use")
    p.add_argument("--max-msg-per-min", type=int, default=2)
    p.add_argument("--max-ack-per-min", type=int, default=6)
    p.add_argument("--max-airtime-s-per-min", type=float, default=15.0, help="UI airtime a minute, seconds")
    p.add_argument("--ack-airtime-reserve-s", type=float, default=3.0)
    p.add_argument("--aprs-txdelay-ms", type=int, default=300, help="the radio's key-up delay, for airtime")
    p.add_argument("--log-dir", default=str(HERE / "packet-logs"))
    p.add_argument("--aprs-db", help="APRS store (default: <log-dir>/aprs.db)")
    p.add_argument("--no-aprs", action="store_true", help="no APRS store, API or backfill")
    p.add_argument("--lat", type=float, help="this station's latitude, for the map and distances")
    p.add_argument("--lon", type=float, help="this station's longitude")
    p.add_argument("--igate-login", help="APRS-IS login (callsign-SSID); turns the receive-only iGate on")
    p.add_argument("--igate-passcode-file", help="a file holding the APRS-IS passcode")
    p.add_argument("--igate-server", default=igate.DEFAULT_SERVER, help="host or host:port (default %(default)s)")
    p.add_argument("--igate-beacon-min", type=int, default=0,
                   help="minutes between the iGate's position beacons to APRS-IS (needs --lat/--lon; 0: none)")
    p.add_argument("--igate-comment", default=igate.BEACON_COMMENT, help="the iGate beacon's comment")
    p.add_argument("--igate-observe", action="store_true",
                   help="also read APRS-IS for our own calls (filter b/<call>* g/<call>*), stored as port 'is'; "
                        "receive only")
    # APRS messaging (aprs_msg.py; plan 2026-11-07, 3.4).
    p.add_argument("--aprs-messaging", choices=aprs_msg.MODES, default="off",
                   help="off; send (compose, retries, matching acks; never acks); full (acks too)")
    p.add_argument("--aprs-owned-calls", type=lambda s: [c.strip().upper() for c in s.split(",") if c.strip()],
                   help="exact call-SSIDs messaging owns (default: --mycall)")
    p.add_argument("--aprs-firmware-acks", choices=("true", "false", "null"), default="null",
                   help="whether the radio's firmware acks messages by itself (full needs false)")
    p.add_argument("--aprs-path", default="WIDE1-1,WIDE2-1", help="the messages' and acks' digipeater path")
    p.add_argument("--aprs-cooldown", type=float, default=aprs_msg.COOLDOWN_S,
                   help="seconds after start before any message or ack goes")
    p.add_argument("--aprs-probe-target", help="the round-trip probe's target station (a call)")
    p.add_argument("--aprs-probe-text", default="", help="the probe message's text (a 3-character token is added)")
    p.add_argument("--digi", metavar="CALL", help="be a fill-in digipeater as CALL (callsign-SSID)")
    p.add_argument("--digi-viscous", type=float, default=5.0,
                   help="seconds a WIDE1-1 request waits for a wide digipeater to repeat it first")
    p.add_argument("--max-digi-per-min", type=int, default=6, help="frames the digipeater may send a minute")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    ax25.parse_call(a.mycall)
    if not 1 <= a.maxframe <= 7:
        p.error("--maxframe must be 1-7")
    if not 16 <= a.paclen <= 256:
        p.error("--paclen must be 16-256")
    for k in ("max_ui_total_per_min", "max_msg_per_min", "max_ack_per_min", "max_frames_per_min"):
        if getattr(a, k) < 1:
            p.error(f"--{k.replace('_', '-')} must be at least 1")
    if not 0 <= a.ack_reserve < a.max_ui_total_per_min:
        p.error("--ack-reserve must be 0 or more and below --max-ui-total-per-min")
    if not 0 <= a.ack_airtime_reserve_s < a.max_airtime_s_per_min:
        p.error("--ack-airtime-reserve-s must be 0 or more and below --max-airtime-s-per-min")
    if not 0 <= a.aprs_txdelay_ms <= 2000:
        p.error("--aprs-txdelay-ms must be 0-2000")
    a.aprs_firmware_acks = {"true": True, "false": False, "null": None}[a.aprs_firmware_acks]
    a.aprs_owned_calls = a.aprs_owned_calls or [a.mycall.upper()]
    allow = a.allow_calls or [ax25.base_call(a.mycall)]
    for c in a.aprs_owned_calls:
        ax25.parse_call(c)
        if ax25.base_call(c) not in allow:
            p.error(f"--aprs-owned-calls {c}: not admitted by --allow-calls ({', '.join(allow)})")
    a.aprs_path = [x.strip().upper() for x in a.aprs_path.split(",") if x.strip()]
    if len(a.aprs_path) > 2:
        p.error("--aprs-path: at most 2 hops (WIDE1-1,WIDE2-1)")
    for x in a.aprs_path:
        ax25.parse_call(x)
    if a.aprs_probe_target:
        ax25.parse_call(a.aprs_probe_target)
    if len(a.aprs_probe_text) > 63 or any(not (" " <= ch <= "~") or ch in "|~{}" for ch in a.aprs_probe_text):
        p.error("--aprs-probe-text: up to 63 printable ASCII characters, no | ~ { }")
    if not 0 <= a.aprs_cooldown <= 3600:
        p.error("--aprs-cooldown must be 0-3600 seconds")
    a.igate_passcode = None
    if a.igate_login:
        ax25.parse_call(a.igate_login)
        if not a.igate_passcode_file:
            p.error("--igate-login needs --igate-passcode-file")
        try:
            a.igate_passcode = igate.read_passcode(a.igate_passcode_file)
        except ValueError as e:
            p.error(str(e))
    if a.igate_beacon_min:
        if not a.igate_login:
            p.error("--igate-beacon-min needs --igate-login")
        if a.lat is None or a.lon is None:
            p.error("--igate-beacon-min needs --lat and --lon (the position it beacons)")
        if not 10 <= a.igate_beacon_min <= 120:
            p.error("--igate-beacon-min must be 10-120 (APRS-IS etiquette: no more often than every 10 min)")
    if a.digi:
        ax25.parse_call(a.digi)
        if not 0 <= a.digi_viscous <= 30:
            p.error("--digi-viscous must be 0-30 seconds")
        if not 1 <= a.max_digi_per_min <= 30:
            p.error("--max-digi-per-min must be 1-30")
    return a


def main():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
