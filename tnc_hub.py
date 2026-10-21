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
- Rate caps: --max-frames-per-min overall (a runaway guard, default 300,
  above what 1200 baud can carry), --max-ui-per-min for UI/APRS (default 6).
- Every transmitted and refused frame is logged (packet-logs/tx.jsonl).

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

import aprs_store
import webguard
import ax25
from ax25 import Frame, FrameError, KissDecoder, U_SABM, U_UI, kiss_wrap
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

class TxGate:
    def __init__(self, enabled, allow_calls, max_per_min, ui_per_min):
        self.enabled = enabled
        self.allowed_by_start = enabled
        self.allow = {c.upper() for c in allow_calls}
        self.max_per_min, self.ui_per_min = max_per_min, ui_per_min
        self.sent = collections.deque()      # (t, is_ui)

    def check(self, f: Frame, raw: bytes):
        if not self.enabled:
            return "transmit is off"
        if f.src.call not in self.allow:
            return f"source {f.src} is not an allowed callsign ({', '.join(sorted(self.allow))})"
        if len(raw) > ax25.MAX_FRAME_BYTES:
            return "frame too long"
        t = now()
        while self.sent and t - self.sent[0][0] > 60:
            self.sent.popleft()
        if len(self.sent) >= self.max_per_min:
            return f"rate limit ({self.max_per_min} frames/min)"
        is_ui = f.kind == "U" and f.utype == U_UI
        if is_ui and sum(1 for _, u in self.sent if u) >= self.ui_per_min:
            return f"UI rate limit ({self.ui_per_min}/min)"
        return None

    def record(self, f: Frame):
        self.sent.append((now(), f.kind == "U" and f.utype == U_UI))


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
        self.gate = TxGate(args.tx, allow, args.max_frames_per_min, args.max_ui_per_min)
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
        self.last_tx = None
        logdir = pathlib.Path(args.log_dir)
        self.rxlog = JsonLog(logdir / "rx.jsonl")
        self.txlog = JsonLog(logdir / "tx.jsonl")
        self.my_bases = {c.upper() for c in allow}
        self.aprs = None                # AprsStore (APRS plan M2), None with --no-aprs
        self.aprs_summary = {}          # refreshed from the store every few seconds
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
        log.info("RX %s", text)
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
                    link = self._new_link(str(f.dest), str(f.src), c,
                                          [str(d) for d in reversed(f.digis)])
                    link.accept(f)
                    return

    # -- transmit --

    def transmit(self, f: Frame, origin: str):
        """Every frame leaves through here. Raises TxRefused."""
        try:
            raw = f.encode()
        except FrameError as e:
            raise TxRefused(str(e)) from e
        why = self.lent_why() or self.gate.check(f, raw)
        text = f.tnc2()
        if why:
            self.counts["tx_refused"] += 1
            self.txlog.write({"t": now(), "dir": "refused", "origin": origin, "why": why,
                              "text": text})
            log.warning("TX refused (%s) from %s: %s", why, origin, text)
            raise TxRefused(why)
        self.radio.write(raw)
        self.last_tx = now()
        self.gate.record(f)
        self.counts["tx_frames"] += 1
        rec = {"t": now(), "dir": "tx", "origin": origin, "text": text}
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
            "tx": {"enabled": self.gate.enabled, "allowed_by_start": self.gate.allowed_by_start,
                   "allow_calls": sorted(self.gate.allow),
                   "max_per_min": self.gate.max_per_min, "ui_per_min": self.gate.ui_per_min},
            "mycall": self.args.mycall,
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
        }

    def aprs_status(self):
        lag = sorted(self.loop_lag)
        loop = {"p99_ms": round(lag[int(len(lag) * 0.99) - 1 if len(lag) > 1 else 0], 1) if lag else None,
                "max_ms": round(lag[-1], 1) if lag else None, "samples": len(lag)}
        me = ({"lat": self.args.lat, "lon": self.args.lon}
              if self.args.lat is not None and self.args.lon is not None else None)
        if not self.aprs:
            return {"enabled": False, "me": me, "loop": loop}
        return {"enabled": True, "me": me, "loop": loop, "queue": self.aprs.qsize(),
                "dropped": self.aprs.dropped, "errors": self.aprs.errors, **self.aprs_summary}

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
        body = await req.json()
        want = bool(body.get("enabled"))
        # On from any page when started with --tx, like PTT on the radio pages
        # (APRS plan D7, 2026-10-19); --tx is the hard switch. Anyone may stop.
        if want and not hub.gate.allowed_by_start:
            return web.json_response({"error": "started without --tx"}, status=403)
        hub.gate.enabled = want
        log.warning("transmit switched %s from the page (%s)", "ON" if want else "OFF", req.remote)
        if not want:
            for link, _ in list(hub.links.values()):
                link.abort("transmit switched off")
        return web.json_response({"enabled": hub.gate.enabled})

    def local(req):
        return req.remote in ("127.0.0.1", "::1")

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

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/tx", set_tx)
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
    p.add_argument("--max-ui-per-min", type=int, default=6)
    p.add_argument("--log-dir", default=str(HERE / "packet-logs"))
    p.add_argument("--aprs-db", help="APRS store (default: <log-dir>/aprs.db)")
    p.add_argument("--no-aprs", action="store_true", help="no APRS store, API or backfill")
    p.add_argument("--lat", type=float, help="this station's latitude, for the map and distances")
    p.add_argument("--lon", type=float, help="this station's longitude")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    ax25.parse_call(a.mycall)
    if not 1 <= a.maxframe <= 7:
        p.error("--maxframe must be 1-7")
    if not 16 <= a.paclen <= 256:
        p.error("--paclen must be 16-256")
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
