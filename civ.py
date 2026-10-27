"""CI-V driver for Icom radios and CI-V compatibles (Xiegu G90/X6100).

What gets polled and which commands are used come from the rig profile in
rigs.py.

No third-party serial library: the port is opened with termios and read via
the asyncio event loop. Everything the radio sends — replies to our reads and
unsolicited "transceive" broadcasts when someone turns the front-panel knob —
goes through one parser that just updates state, so reads never need to be
paired with their replies.
"""

import asyncio
import collections
import fcntl
import logging
import os
import struct
import termios
import time

from radio import MODE_CODES, MODES, Radio

log = logging.getLogger("civ")

PREAMBLE = b"\xfe\xfe"
EOM = 0xFD
OK, NG = 0xFB, 0xFA


# --- BCD helpers -----------------------------------------------------------

def to_bcd_le(n, nbytes):
    out = bytearray()
    for _ in range(nbytes):
        two = n % 100
        out.append((two // 10) << 4 | two % 10)
        n //= 100
    return bytes(out)


def from_bcd_le(b):
    n = 0
    for i, x in enumerate(b):
        n += ((x >> 4) * 10 + (x & 0x0F)) * 100 ** i
    return n


def to_bcd_be(n, nbytes):
    return to_bcd_le(n, nbytes)[::-1]


def from_bcd_be(b):
    return from_bcd_le(bytes(reversed(b)))


# SWR meter scale the page draws (Icom/Xiegu CI-V: 0000=1.0, 0048=1.5, 0080=2.0,
# 0120=3.0; static/app.js swrVal ends at 255=9.9).
SWR_METER_POINTS = [(0, 1.0), (48, 1.5), (80, 2.0), (120, 3.0), (255, 9.9)]


def fixed88_swr(b):
    """G90 (fw 1.81) 15 12 reply: SWR as binary 8.8 fixed point, not BCD
    (01 4c = 332/256 = 1.30); 00 00 at a match (its meter shows ~1.0)."""
    return max(1.0, ((b[0] << 8) | b[1]) / 256)


def swr_meter(ratio):
    """An SWR ratio as a 0-255 meter value on SWR_METER_POINTS."""
    for (m0, r0), (m1, r1) in zip(SWR_METER_POINTS, SWR_METER_POINTS[1:]):
        if ratio <= r1:
            return round(m0 + (ratio - r0) / (r1 - r0) * (m1 - m0))
    return 255


# --- Serial port -----------------------------------------------------------

def open_serial(path, baud):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        speed = getattr(termios, f"B{baud}")
        cc = attrs[6]
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        # raw 8N1, no HUPCL, no flow control
        termios.tcsetattr(fd, termios.TCSANOW, [
            0, 0, termios.CS8 | termios.CREAD | termios.CLOCAL, 0, speed, speed, cc])
        # Drop DTR/RTS: if the radio's USB SEND / USB Keying is mapped to
        # either line, leaving them asserted would key the transmitter.
        # Some adapters (and virtual ports) have no modem lines at all.
        try:
            fcntl.ioctl(fd, termios.TIOCMBIC,
                        struct.pack("I", termios.TIOCM_DTR | termios.TIOCM_RTS))
        except OSError as e:
            log.info("%s: no modem-control lines (%s)", path, e)
        termios.tcflush(fd, termios.TCIOFLUSH)
    except Exception:
        os.close(fd)
        raise
    return fd


# --- Polling plan ----------------------------------------------------------

LEVELS = {0x01: "af", 0x02: "rf", 0x03: "sql", 0x0A: "power"}
METERS = {0x02: "smeter", 0x11: "po", 0x12: "swr", 0x13: "alc"}
LEVEL_SUBS = {v: k for k, v in LEVELS.items()}
METER_SUBS = {v: k for k, v in METERS.items()}


def poll_plan(caps):
    """(slow polls, RX meter polls, TX meter polls) as (cmd, sub-bytes) lists."""
    feats = set(caps["features"])
    tx_status = (0x1C, b"\x00")
    slow = [(0x03, b""), (0x04, b"")]
    if caps.get("mode_cmd_26"):
        slow.append((0x26, b"\x00"))       # mode + data + filter in one read (F13)
    elif caps["data_modes"]:
        slow.append((0x1A, b"\x06"))
    slow += [(0x14, bytes([LEVEL_SUBS[k]])) for k in caps["levels"] if k not in caps.get("unreadable", [])]
    slow += [(0x16, b"\x02"), (0x11, b"")]
    if caps["agc"]:
        slow.append((0x16, b"\x12"))
    if "nb" in feats:
        slow.append((0x16, b"\x22"))
    if "nr" in feats:
        slow.append((0x16, b"\x40"))
    if "tuner" in feats:
        slow.append((0x1C, b"\x01"))
    if "split" in feats:
        slow.append((0x0F, b""))
    if "vfo_b" in feats:
        slow.append((0x25, b"\x01"))
    if caps["scope"]:
        slow += [(0x27, b"\x14\x00"), (0x27, b"\x15\x00")]
    # Transmit status matters most: read it every few slots.
    plan = []
    for i, item in enumerate(slow):
        if i % 3 == 0:
            plan.append(tx_status)
        plan.append(item)
    tx_meters = [(0x15, bytes([METER_SUBS[m]])) for m in ("po", "swr", "alc") if m in caps["meters"]]
    return plan, [(0x15, b"\x02")], tx_meters or [tx_status]


TUNE_OURS_S = 30.0   # a tune we started counts as ours this long at most

# Frames that must keep strict order: they start or stop a transmission.
ORDERED_KEYS = {"tx", "tuner"}


def is_ptt_frame(frame):
    """FE FE to from 1C 00 <0|1> FD"""
    return len(frame) == 8 and frame[4] == 0x1C and frame[5] == 0x00


def calibrate(raw, table):
    """Radio-specific raw S-meter -> Icom scale (0 = S0, 120 = S9, 241 = S9+60)."""
    if not table:
        return raw
    for (r0, d0), (r1, d1) in zip(table, table[1:]):
        if raw <= r1:
            db = d0 + (raw - r0) * (d1 - d0) / (r1 - r0)
            break
    else:
        db = table[-1][1]
    return int(max(0, (db + 54) / 54 * 120) if db <= 0 else min(241, 120 + db / 60 * 121))


class CivRadio(Radio):
    def __init__(self, caps, port, baud=None, addr=None, ctrl=0xE0):
        super().__init__(caps)
        self.port, self.ctrl = port, ctrl
        self.baud = baud or caps["baud"]
        self.addr = caps["addr"] if addr is None else addr
        self._slow_polls, self._rx_meters, self._tx_meters = poll_plan(caps)
        self.fd = None
        self._rx = bytearray()
        self._cmds = collections.OrderedDict()   # dedupe key -> frame
        # Transmit frames (PTT, tuner) are never coalesced or reordered: they go
        # through a FIFO pumped before the dict, together with everything
        # requested before them and everything requested while they wait.
        self._ordered = collections.deque()      # (key, frame), strict order
        self._ptt_sent_on = False                # we keyed it and it hasn't unkeyed since
        self._ptt_sent_at = 0.0
        self._rx_replies = 0                     # consecutive "not transmitting" replies
        self._tune_sent_at = None                # monotonic time we started an ATU tune
        self._wake = asyncio.Event()
        self._reply = asyncio.Event()
        self._tasks = []
        self._last_rx = 0.0
        self._slow_i = 0
        self._meter_i = 0
        if caps.get("data_off_mod"):
            # Only rigs with a known DATA OFF MOD command get this method; the
            # server checks for it (QMX has its own, over SS).
            self.ssb_from_usb = self._ssb_from_usb
            self._mod_src = None                 # last 1A 05 <data_off_mod> reply
            self._mod_src_was = None             # the source to put back
        self._meter_raw = {}            # last raw reply per TX meter (change-only log)
        self._tick = 0
        self._scope_buf = bytearray()
        self._scope_edges = None

    # -- framing --

    def _frame(self, cmd, data=b""):
        return PREAMBLE + bytes([self.addr, self.ctrl, cmd]) + data + bytes([EOM])

    def _queue(self, key, cmd, data=b""):
        """Queue a user command; a newer command with the same key replaces it,
        except around transmit frames, which keep strict order (see _ordered)."""
        frame = self._frame(cmd, data)
        if key in ORDERED_KEYS:
            # Whatever was asked for before this frame goes out before it
            # (e.g. the split TX frequency before the key command).
            self._ordered.extend(self._cmds.items())
            self._cmds.clear()
            self._ordered.append((key, frame))
        elif self._ordered:
            # Asked for after a pending transmit frame: stays after it
            # (e.g. the RX frequency after an unkey).
            self._ordered.append((key, frame))
        else:
            self._cmds.pop(key, None)
            self._cmds[key] = frame
        self._wake.set()

    def _pending(self):
        return [f for _, f in self._ordered] + list(self._cmds.values())

    def _write(self, frame):
        os.write(self.fd, frame)
        if is_ptt_frame(frame):
            self._ptt_sent_on, self._ptt_sent_at = bool(frame[6]), time.monotonic()
        elif len(frame) == 8 and frame[4:7] == b"\x1c\x01\x02":
            self._tune_sent_at = time.monotonic()

    def _ours_on_air(self):
        """Is the radio transmitting because of a frame WE sent (CAT key or
        tune)? 2026-10-13: only programmatic inputs may trigger a
        cut-off, never a manual (front-panel / mic) transmission."""
        tuning = (self._tune_sent_at is not None and self.state.get("tuner") == 2
                  and time.monotonic() - self._tune_sent_at < TUNE_OURS_S)
        return self._ptt_sent_on or tuning

    # -- lifecycle --

    async def start(self):
        self._tasks = [asyncio.create_task(self._io_loop(), name="civ-io"),
                       asyncio.create_task(self._watchdog(), name="civ-watchdog")]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        if self.fd is not None:
            pending = self._pending()
            restore = self._restore_owed()
            self._ordered.clear()
            self._cmds.clear()
            # Unkey if we keyed it (or started a tune) and it hasn't unkeyed
            # since, or a PTT frame is still queued. Never trust the optimistic
            # state: an unkey queued a moment ago set it False without writing.
            # A transmission started at the radio itself is left alone.
            ptt_pending = any(is_ptt_frame(f) for f in pending)
            try:
                if self._ours_on_air() or ptt_pending:
                    self._write(self._frame(0x1C, b"\x00\x00"))
                    log.info("stop: unkey written")
                if self.caps["scope"]:
                    os.write(self.fd, self._frame(0x27, b"\x11\x00"))
                # A queued RX frequency (emulated split restore) still goes out,
                # after the unkey; any queued key or tune is dropped.
                freqs = [f for f in pending if f[4] == 0x05]
                if freqs:
                    await asyncio.sleep(0.1)
                    os.write(self.fd, freqs[-1])
                if restore:
                    # A setting changed for a transmission (DATA OFF MOD) goes
                    # back too, after the unkey: dropping it left the IC-7300's
                    # front mic on USB (review 2026-10-18, Sol, 3/3 SIGTERMs).
                    await asyncio.sleep(0.05)
                    os.write(self.fd, restore)
                    log.info("stop: DATA OFF MOD restore written")
            except OSError as e:
                log.warning("stop: write failed: %s", e)
            try:   # let the bytes leave before the port closes
                termios.tcdrain(self.fd)
            except (OSError, termios.error):
                pass   # not a tty (tests) or already gone
        self._update(tx=False)
        self._close()

    def _close(self):
        if self.fd is not None:
            try:
                asyncio.get_running_loop().remove_reader(self.fd)
            except Exception:
                pass
            os.close(self.fd)
            self.fd = None

    async def _io_loop(self):
        """Open the port (retrying forever), then pump commands and polls."""
        loop = asyncio.get_running_loop()
        while True:
            try:
                self.fd = open_serial(self.port, self.baud)
            except OSError as e:
                log.warning("cannot open %s: %s (retrying)", self.port, e)
                self._update(connected=False)
                await asyncio.sleep(3)
                continue
            log.info("opened %s at %d baud", self.port, self.baud)
            loop.add_reader(self.fd, self._on_readable)
            if self.caps["scope"]:
                # Scope on, and stream waveform data over CI-V.
                self._queue("scope_on", 0x27, b"\x10\x01")
                self._queue("scope_out", 0x27, b"\x11\x01")
            try:
                await self._pump()
            except OSError as e:
                log.warning("serial error: %s", e)
            self._close()
            self._update(connected=False)
            await asyncio.sleep(2)

    async def _pump(self):
        while True:
            if self.fd is None:
                raise OSError("port closed")
            if self._ordered:
                _, frame = self._ordered.popleft()
            elif self._cmds:
                _, frame = self._cmds.popitem(last=False)
            else:
                frame = self._next_poll()
            self._reply.clear()
            self._write(frame)
            # Half-duplex courtesy: wait for the radio to answer (or give up).
            try:
                await asyncio.wait_for(self._reply.wait(), 0.08)
            except TimeoutError:
                pass
            if not self.state["connected"]:
                # Radio silent (off?): poll gently instead of hammering it.
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), 0.5)
                except TimeoutError:
                    pass

    def _next_poll(self):
        self._tick += 1
        if self._tick % 2:
            meters = self._tx_meters if self.state["tx"] else self._rx_meters
            cmd, sub = meters[self._meter_i % len(meters)]
            self._meter_i += 1
        else:
            cmd, sub = self._slow_polls[self._slow_i % len(self._slow_polls)]
            self._slow_i += 1
        return self._frame(cmd, sub)

    async def _watchdog(self):
        while True:
            await asyncio.sleep(1)
            if self.state["connected"] and time.monotonic() - self._last_rx > 3:
                log.warning("radio stopped answering")
                self._update(connected=False)

    # -- receive --

    def _on_readable(self):
        try:
            chunk = os.read(self.fd, 4096)
        except BlockingIOError:
            return
        except OSError as e:
            log.warning("read failed: %s", e)
            self._close()
            return
        if not chunk:
            return
        self._rx += chunk
        while True:
            start = self._rx.find(PREAMBLE)
            if start < 0:
                self._rx.clear()
                return
            end = self._rx.find(bytes([EOM]), start)
            if end < 0:
                if start:
                    del self._rx[:start]
                return
            frame = bytes(self._rx[start + 2:end])
            del self._rx[:end + 1]
            # Collisions can leave stray FE bytes at the front.
            frame = frame.lstrip(b"\xfe")
            if len(frame) >= 3:
                try:
                    self._handle(frame)
                except Exception:
                    log.exception("bad frame %s", frame.hex(" "))

    def _handle(self, f):
        to, frm, cmd, body = f[0], f[1], f[2], f[3:]
        if frm != self.addr or to not in (self.ctrl, 0x00):
            return  # our own echo, or traffic for someone else
        self._last_rx = time.monotonic()
        if not self.state["connected"]:
            self._update(connected=True)
        if cmd == 0x27 and body[:1] == b"\x00":
            self._handle_scope(body)
            return
        self._reply.set()

        if cmd == NG:
            log.debug("radio rejected a command")
        elif cmd in (0x00, 0x03) and len(body) >= 5:
            self._update(freq=from_bcd_le(body[:5]))
        elif cmd in (0x01, 0x04) and body:
            upd = {"mode": MODES.get(body[0], self.state["mode"])}
            if len(body) > 1:
                upd["filter"] = body[1]
            self._update(**upd)
        elif cmd == 0x14 and len(body) >= 3 and body[0] in LEVELS:
            self._update(**{LEVELS[body[0]]: from_bcd_be(body[1:3])})
        elif cmd == 0x15 and len(body) >= 3 and body[0] in METERS:
            val = from_bcd_be(body[1:3])
            if body[0] == 0x12 and self.caps.get("swr_fixed88"):
                val = swr_meter(fixed88_swr(body[1:3]))
            if (body[0] != 0x02 and self.caps.get("log_meter_raw")
                    and bytes(body[1:3]) != self._meter_raw.get(body[0])):
                # Log every change of a raw TX meter reply next to how it decodes.
                self._meter_raw[body[0]] = bytes(body[1:3])
                log.info("%s raw %s -> decoded %d", METERS[body[0]], body[1:3].hex(" "), val)
            if body[0] == 0x02:
                val = calibrate(val, self.caps["smeter_cal"])
            self._update(**{METERS[body[0]]: val})
        elif cmd == 0x16 and len(body) >= 2:
            sub, v = body[0], body[1]
            if sub == 0x02:
                self._update(preamp=v)
            elif sub == 0x12:
                self._update(agc=v)
            elif sub == 0x22:
                self._update(nb=bool(v))
            elif sub == 0x40:
                self._update(nr=bool(v))
        elif cmd == 0x11 and body:
            self._update(att=body[0] != 0)
        elif cmd == 0x1C and len(body) >= 2:
            if body[0] == 0x00:
                tx = bool(body[1])
                self._rx_replies = 0 if tx else self._rx_replies + 1
                if (not tx and self._rx_replies >= 2
                        and time.monotonic() - self._ptt_sent_at > 1.0):
                    # The radio is receiving: our key (if any) is over. The 1 s
                    # skips a stale reply that crossed the key command; two in a
                    # row skips a single stale poll reply later on (F8).
                    self._ptt_sent_on = False
                # TX meters aren't polled on receive; don't leave stale readings up.
                self._update(tx=tx, **({} if tx else {"po": 0, "swr": 0, "alc": 0}))
            elif body[0] == 0x01:
                self._update(tuner=body[1])
        elif cmd == 0x26 and len(body) >= 4 and body[0] == 0x00:
            if bytes(body[1:4]) != getattr(self, "_mode26_raw", None):
                self._mode26_raw = bytes(body[1:4])
                log.info("mode raw 26 %s -> %s data=%s", body[1:4].hex(" "),
                         MODES.get(body[1], "?"), bool(body[2]))
            self._update(mode=MODES.get(body[1], self.state["mode"]), data=bool(body[2]),
                         filter=body[3])
        elif cmd == 0x1A and len(body) >= 2 and body[0] == 0x06:
            if bytes(body[1:]) != getattr(self, "_data_raw", None):
                # Diagnosis aid: the G90 was in plain USB while this read as
                # data on (2026-10-13). Log each change of the raw reply.
                self._data_raw = bytes(body[1:])
                log.info("data raw %s -> %s", body[1:].hex(" "), bool(body[1]))
            self._update(data=bool(body[1]))
        elif (cmd == 0x1A and len(body) >= 4 and body[0] == 0x05
              and list(body[1:3]) == (self.caps.get("data_off_mod") or [])):
            self._mod_src = body[3]
        elif cmd == 0x0F and body:
            self._update(split=body[0] == 0x01)
        elif cmd == 0x25 and len(body) >= 6 and body[0] == 0x01:
            self._update(freq_b=from_bcd_le(body[1:6]))
        elif cmd == 0x27 and len(body) >= 3 and body[0] == 0x14:
            self._update(scope_mode="fixed" if body[2] else "center")
        elif cmd == 0x27 and len(body) >= 7 and body[0] == 0x15:
            self._update(scope_span=from_bcd_le(body[2:7]))

    def _handle_scope(self, body):
        # body: 00 main seq max ...; seq 1 carries edges, 2..max carry data.
        if len(body) < 4:
            return
        seq, seq_max = from_bcd_le(body[2:3]), from_bcd_le(body[3:4])
        if seq == 1:
            if len(body) < 16:
                return
            center_mode = body[4] == 0x00
            a, b = from_bcd_le(body[5:10]), from_bcd_le(body[10:15])
            self._scope_edges = (a - b, a + b) if center_mode else (a, b)
            self._scope_buf.clear()
            if len(body) > 100:  # whole line in one frame (LAN-style)
                self._scope_buf += body[16:]
                self._emit_scope(*self._scope_edges, bytes(self._scope_buf))
        elif self._scope_edges:
            self._scope_buf += body[4:]
            if seq == seq_max:
                self._emit_scope(*self._scope_edges, bytes(self._scope_buf))
                self._scope_edges = None

    # -- commands --

    def _restore_owed(self):
        """The DATA OFF MOD frame still owed at stop(): the saved source if it
        was switched and no restore is queued yet, else the queued restore."""
        if not self.caps.get("data_off_mod"):
            return None
        sub = bytes(self.caps["data_off_mod"])
        if self._mod_src_was is not None:
            was, self._mod_src_was = self._mod_src_was, None
            return self._frame(0x1A, b"\x05" + sub + bytes([was]))
        queued = [f for k, f in list(self._ordered) + list(self._cmds.items()) if k == "mod_src"]
        return queued[-1] if queued else None

    async def _ssb_from_usb(self, on):
        """Transmit audio from the USB codec in plain SSB (DATA OFF), for the
        page's voice, and the previous source back afterwards. The IC-7300's
        DATA OFF MOD is MIC/ACC by default: page voice in plain USB then sent
        nothing, near 0 W (mac-shack, 2026-10-18). Queued writes keep order with
        the key frame: set before keying, put back after the unkey."""
        sub, usb = bytes(self.caps["data_off_mod"]), self.caps.get("data_off_mod_usb", 0x03)
        if on:
            self._mod_src = None
            self._queue("mod_src_read", 0x1A, b"\x05" + sub)
            end = time.monotonic() + 2.0
            while self._mod_src is None and time.monotonic() < end:
                await asyncio.sleep(0.05)
            if self._mod_src is None:
                raise ValueError(f"the {self.caps['name']} did not report its DATA OFF MOD source")
            if self._mod_src != usb:
                self._mod_src_was = self._mod_src
                log.info("DATA OFF MOD: USB for the page's voice (was %02x)", self._mod_src)
                self._queue("mod_src", 0x1A, b"\x05" + sub + bytes([usb]))
        elif self._mod_src_was is not None:
            was, self._mod_src_was = self._mod_src_was, None
            log.info("DATA OFF MOD: back to %02x", was)
            self._queue("mod_src", 0x1A, b"\x05" + sub + bytes([was]))

    async def _apply(self, key, v):
        s = self.state
        if key == "freq":
            self._queue("freq", 0x05, to_bcd_le(v, 5))
        elif key == "freq_b":
            self._queue("freq_b", 0x25, b"\x01" + to_bcd_le(v, 5))
        elif key in ("mode", "filter", "data") and self.caps.get("mode_cmd_26"):
            # F13: the G90 acks 1A 06 but never applies it (fw 1.81, linux-station
            # 2026-10-14); 26 00 sets mode, data and filter in one frame.
            mode = v if key == "mode" else s["mode"]
            filt = v if key == "filter" else s["filter"]
            # A mode or filter change keeps DATA on where the new mode allows it.
            data = bool(v) if key == "data" else bool(s["data"]) and mode in self.caps["data_modes"]
            self._queue("mode", 0x26, bytes([0x00, MODE_CODES[mode], int(data), filt]))
            if key == "mode":
                self._update(data=data)
        elif key == "mode":
            self._queue("mode", 0x06, bytes([MODE_CODES[v], s["filter"]]))
            # Some rigs (G90) drop data mode on any 06 mode set, and their
            # line audio dies with it. Keep DATA on if the new mode allows it.
            if (self.caps.get("data_sticky") and s["data"]
                    and v in self.caps["data_modes"]):
                self._queue("data", 0x1A, bytes([0x06, 1, s["filter"]]))
        elif key == "filter":
            self._queue("mode", 0x06, bytes([MODE_CODES[s["mode"]], v]))
        elif key == "data":
            self._queue("data", 0x1A, bytes([0x06, int(v), s["filter"] if v else 0]))
        elif key in LEVEL_SUBS:
            self._queue(key, 0x14, bytes([LEVEL_SUBS[key]]) + to_bcd_be(v, 2))
        elif key == "preamp":
            self._queue(key, 0x16, bytes([0x02, v]))
        elif key == "agc":
            self._queue(key, 0x16, bytes([0x12, v]))
        elif key == "nb":
            self._queue(key, 0x16, bytes([0x22, int(v)]))
        elif key == "nr":
            self._queue(key, 0x16, bytes([0x40, int(v)]))
        elif key == "att":
            self._queue(key, 0x11, bytes([self.caps["att_on"] if v else 0]))
        elif key == "tx":
            self._queue(key, 0x1C, bytes([0x00, int(v)]))
        elif key == "tuner":
            self._queue(key, 0x1C, bytes([0x01, v]))
        elif key == "split":
            self._queue(key, 0x0F, bytes([int(v)]))
        elif key == "vfo":
            self._queue(key, 0x07, b"\x00" if v == "A" else b"\x01")
        elif key == "scope_mode":
            self._queue(key, 0x27, bytes([0x14, 0x00, int(v == "fixed")]))
        elif key == "scope_span":
            self._queue(key, 0x27, b"\x15\x00" + to_bcd_le(v, 5))
        # Optimistic update; the poll loop corrects us if the radio disagrees.
        self._update(**{key: v})

    async def swap_vfo(self):
        if "vfo_b" not in self.caps["features"]:
            raise ValueError(f"{self.name} has no VFO swap")
        self._queue("swap", 0x07, b"\xb0")
        self._update(freq=self.state["freq_b"], freq_b=self.state["freq"])

    async def power(self, on: bool):
        if "power_on" not in self.caps["features"]:
            raise ValueError(f"{self.name} can't be powered on or off remotely")
        if self.fd is None:
            raise RuntimeError("serial port not open")
        if on:
            # The radio needs a run of preamble bytes to wake its CPU first.
            os.write(self.fd, b"\xfe" * 150 + self._frame(0x18, b"\x01"))
        else:
            self._queue("power", 0x18, b"\x00")
