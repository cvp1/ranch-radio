"""QRP Labs QMX / QMX+ over its USB virtual COM port (Kenwood TS-480-style CAT).

Command set: QRP Labs "QMX CAT programming manual", firmware 1_04_003. Every
command is two letters, parameters, then ';'. Sets get no reply; reads echo
the command letters with the value; '?;' means rejected.

Why a native backend instead of Hamlib model 2057: that driver (beta in 4.7.2)
lists only CW/CW-R/PKTUSB/PKTLSB, so the Hamlib backend's discovery offers a
CW-only radio and FT8 never gets a mode. Data modes: PKTUSB is MD6 (FSK),
PKTLSB is MD9 (FSK reverse) -- the QMX's DiGi mode, which WSJT-X/JTDX FSK
modes need. Hamlib's QRP Labs emulation (kenwood.c is_emulation) has these
the other way round; a real QMX disagrees (fw 1_03_002 and 1_04_010,
2026-10-15): MD9 sets its demodulation sideband to LSB (Q1 reads 1), so FT8
above the dial was mirrored and undecodable while TX still worked.

Safety, beyond the server's own PTT rules:
- Never written, ever: MD8 (SWR tune mode, keys a carrier), PS0 (power off;
  nothing can switch it back on over CAT), any carriage return (switches the
  QMX's serial port into terminal mode). _write refuses them.
- While we're connected the radio's own CAT timeout is on (QB1, QC<n>): if
  this server dies or the USB link drops mid-transmission, the QMX returns to
  receive by itself. Our polls keep it fed in normal running. The radio's
  previous QB/QC values are put back at shutdown (they are session-only and
  never written to EEPROM anyway).
- Shutdown unkeys only a transmission we started (2026-10-13): a key
  at the radio is left alone.
"""

import asyncio
import collections
import logging
import os
import termios
import time

from civ import open_serial
from hamlib_rig import po_raw, swr_raw
from radio import Radio

log = logging.getLogger("qmx")

# MD digit <-> (mode, data). 8 is SWR tune mode: read as "tuning", never sent.
MD_READ = {"1": ("LSB", False), "2": ("USB", False), "3": ("CW", False),
           "5": ("AM", False), "6": ("USB", True), "7": ("CW-R", False),
           "9": ("LSB", True)}
MD_WRITE = {v: k for k, v in MD_READ.items()}
FORBIDDEN = ("MD8", "PS0")

CAT_TIMEOUT_S = 5          # QMX drops TX this long after the last CAT command
ORDERED_KEYS = {"tx"}      # never coalesced or reordered (see CivRadio._queue)
POLL_INTERVAL = 0.05       # the QMX answers in ~2 ms; unpaced, polls ran ~650/s


def is_ptt(cmd):
    return cmd in ("TQ1;", "TQ0;", "TX;", "RX;")


def smeter_raw(v):
    """SM reply -> Icom meter scale (0 = S0, 120 = S9, 241 = S9+60).

    The manual says only "the S-meter value in dB"; the operating manual puts
    S0 at -127 dBm, 6 dB per S-unit. Read a negative value as dBm and a
    positive one as dB above S0. UNVERIFIED on hardware: run with -v and the
    raw values are logged on change, so the first live session can settle it.
    """
    over_s9 = v + 73 if v < 0 else v - 54
    if over_s9 <= 0:
        return int(max(0, (over_s9 + 54) / 54 * 120))
    return int(min(241, 120 + over_s9 / 60 * 121))


class QmxRadio(Radio):
    def __init__(self, caps, port, baud=None):
        super().__init__(caps)
        self.port = port
        self.baud = baud or caps["baud"]
        self.fd = None
        self._rx = bytearray()
        self._cmds = collections.OrderedDict()   # key -> command, coalesced
        self._ordered = collections.deque()      # (key, command), strict order
        self._ptt_sent_on = False
        self._ptt_sent_at = 0.0
        self._fa = self._fb = None
        self._saved_timeout = {}                 # "QB"/"QC" -> radio's own value
        self._reply = asyncio.Event()
        self._tasks = []
        self._last_rx = 0.0
        self._tick = 0
        self._slow = ["IF;", "FA;", "FB;"]
        self._rx_meters = ["SM;"]
        self._tx_meters = ["PC;", "SW;"]
        self._sm_raw = None
        self._asking = None                      # the read whose reply we await
        self._ssb_src = None                     # SS: SSB audio source, as last read
        self._ssb_src_was = None                 # ...before ssb_from_usb(True)
        self._wake = asyncio.Event()             # a command is queued: stop waiting
        self._next_poll_at = 0.0

    # -- wire --

    def _write(self, cmd):
        if "\r" in cmd or "\n" in cmd or any(cmd.startswith(f) for f in FORBIDDEN):
            raise ValueError(f"refusing to send {cmd!r} to the QMX")
        os.write(self.fd, cmd.encode())
        if cmd in ("TQ1;", "TX;"):
            self._ptt_sent_on, self._ptt_sent_at = True, time.monotonic()
        elif cmd in ("TQ0;", "RX;"):
            self._ptt_sent_on, self._ptt_sent_at = False, time.monotonic()

    def _queue(self, key, cmd):
        if key in ORDERED_KEYS:
            # Whatever was asked for first goes out first (split TX freq, then key).
            self._ordered.extend(self._cmds.items())
            self._cmds.clear()
            self._ordered.append((key, cmd))
        elif self._ordered:
            self._ordered.append((key, cmd))   # stays behind a pending key/unkey
        else:
            self._cmds.pop(key, None)
            self._cmds[key] = cmd
        self._wake.set()

    def _pending(self):
        return [c for _, c in self._ordered] + list(self._cmds.values())

    # -- lifecycle --

    async def start(self):
        self._tasks = [asyncio.create_task(self._io_loop(), name="qmx-io"),
                       asyncio.create_task(self._watchdog(), name="qmx-watchdog")]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        if self.fd is not None:
            pending = self._pending()
            # The SSB source switched for a transmission goes back too (as the
            # IC-7300's DATA OFF MOD does): the saved one, else a queued restore.
            src = None
            if getattr(self, "_ssb_src_was", None) in ("0", "2"):
                src, self._ssb_src_was = f"SS{self._ssb_src_was};", None
            else:
                queued = [c for k, c in list(self._ordered) + list(self._cmds.items()) if k == "ssb_src"]
                src = queued[-1] if queued and queued[-1] != "SS0;" else None
            self._ordered.clear()
            self._cmds.clear()
            try:
                if self._ptt_sent_on or any(is_ptt(c) for c in pending):
                    self._write("TQ0;")
                    log.info("stop: unkey written")
                freqs = [c for c in pending if c[:2] in ("FA", "FB")]
                if freqs:   # emulated-split restore still goes out, after the unkey
                    await asyncio.sleep(0.05)
                    self._write(freqs[-1])
                if src:
                    await asyncio.sleep(0.05)
                    self._write(src)
                    log.info("stop: SSB source restore written (%s)", src)
                for k in ("QC", "QB"):   # radio's own CAT timeout back as it was
                    if k in self._saved_timeout:
                        self._write(f"{k}{self._saved_timeout[k]};")
            except OSError as e:
                log.warning("stop: write failed: %s", e)
            try:
                termios.tcdrain(self.fd)
            except (OSError, termios.error):
                pass
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
        loop = asyncio.get_running_loop()
        while True:
            try:
                self.fd = open_serial(self.port, self.baud)
            except OSError as e:
                log.warning("cannot open %s: %s (retrying)", self.port, e)
                self._update(connected=False)
                await asyncio.sleep(3)
                continue
            log.info("opened %s", self.port)
            loop.add_reader(self.fd, self._on_readable)
            try:
                await self._hello()
                await self._pump()
            except OSError as e:
                log.warning("serial error: %s", e)
            self._close()
            self._update(connected=False)
            await asyncio.sleep(2)

    async def _ask(self, cmd, timeout=0.3):
        self._reply.clear()
        self._asking = cmd
        self._write(cmd)
        try:
            await asyncio.wait_for(self._reply.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def _hello(self):
        # A ';' alone flushes any half-typed command; then identify and arm
        # the radio's own TX watchdog, remembering its previous setting.
        self._write(";")
        await self._ask("VN;")
        await self._ask("QB;")
        await self._ask("QC;")
        self._write(f"QC{CAT_TIMEOUT_S};")
        self._write("QB1;")

    async def _pump(self):
        while True:
            if self.fd is None:
                raise OSError("port closed")
            if self._ordered:
                _, cmd = self._ordered.popleft()
            elif self._cmds:
                _, cmd = self._cmds.popitem(last=False)
            else:
                wait = self._next_poll_at - time.monotonic()
                if wait > 0:             # pace polls; a queued command cuts in
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), wait)
                    except asyncio.TimeoutError:
                        pass
                    continue
                self._next_poll_at = time.monotonic() + POLL_INTERVAL
                cmd = self._next_poll()
            if cmd.endswith(";") and len(cmd) == 3:      # a read: wait for its reply
                await self._ask(cmd, 0.1 if self.state["connected"] else 0.5)
            else:                                        # a set: no reply comes
                self._write(cmd)
                await asyncio.sleep(0.01)

    def _next_poll(self):
        self._tick += 1
        if self._tick % 2:
            meters = self._tx_meters if self.state["tx"] else self._rx_meters
            return meters[(self._tick // 2) % len(meters)]
        return self._slow[(self._tick // 2) % len(self._slow)]

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
        self._rx += chunk
        while (end := self._rx.find(b";")) >= 0:
            reply = self._rx[:end].decode("ascii", "replace").strip()
            del self._rx[:end + 1]
            if reply:
                try:
                    self._handle(reply)
                except (ValueError, IndexError):
                    log.debug("could not parse %r", reply)
        if len(self._rx) > 512:      # no ';' in a long run: not CAT, drop it
            self._rx.clear()

    def _handle(self, r):
        self._last_rx = time.monotonic()
        self._reply.set()
        if not self.state["connected"]:
            self._update(connected=True)
        cmd, v = r[:2], r[2:]
        if cmd == "FA" and self._asking == "FB;":
            # The CAT manual's own FB example shows an FA-prefixed reply; take
            # the reply to an FB read as VFO B whatever its letters say.
            cmd = "FB"
        self._asking = None
        if r == "?":
            log.debug("radio rejected a command")
        elif cmd == "FA" and v:
            self._fa = int(v)
            self._freqs()
        elif cmd == "FB" and v:
            self._fb = int(v)
            self._freqs()
        elif cmd == "IF" and len(v) >= 31:
            self._handle_if(v)
        elif cmd == "MD" and v:
            self._handle_md(v[0])
        elif cmd == "TQ" and v:
            self._handle_tx(v[0] == "1")
        elif cmd == "SP" and v:
            self._update(split=v[0] == "1")
        elif cmd == "SS" and v:
            self._ssb_src = v[0]
        elif cmd == "SM" and v:
            raw = int(v)
            if raw != self._sm_raw:
                self._sm_raw = raw
                log.debug("sm raw %d -> %d", raw, smeter_raw(raw))
            self._update(smeter=smeter_raw(raw))
        elif cmd == "PC":
            frac = int(v) / 10 / self.caps["max_watts"] if v else 0.0
            self._update(po=po_raw(min(frac, 1.0)))
        elif cmd == "SW":
            self._update(swr=swr_raw(int(v) / 100) if v else 0)
        elif cmd in ("QB", "QC") and v and cmd not in self._saved_timeout:
            self._saved_timeout[cmd] = v
        elif cmd == "VN":
            log.info("QMX firmware %s", v)

    def _handle_if(self, v):
        # 11 freq, 5 spaces, 5 RIT, RIT, XIT, bank, 2 memory, TX, mode, RX VFO,
        # scan, split, tone, ... (TS-480 layout; QMX sends 37 or 38 bytes).
        # The frequency is left to FA/FB: during split TX it is VFO B's.
        self._handle_tx(v[26] == "1")
        self._handle_md(v[27])
        self._update(vfo="B" if v[28] == "1" else "A", split=v[30] == "1")
        self._freqs()

    def _handle_md(self, c):
        if c == "8":
            self._update(tuner=2)        # SWR tune mode, started at the radio
            return
        self._update(tuner=0)
        if c in MD_READ:
            mode, data = MD_READ[c]
            self._update(mode=mode, data=data)

    def _handle_tx(self, tx):
        if not tx and time.monotonic() - self._ptt_sent_at > 1.0:
            self._ptt_sent_on = False    # receiving: whatever we keyed is over
        self._update(tx=tx, **({} if tx else {"po": 0, "swr": 0, "alc": 0}))

    def _freqs(self):
        a, b = self._fa, self._fb
        if self.state["vfo"] == "B":
            a, b = b, a
        upd = {}
        if a is not None:
            upd["freq"] = a
        if b is not None:
            upd["freq_b"] = b
        self._update(**upd)

    # -- commands --

    def _vfo_cmd(self, which):
        """'FA' or 'FB' for the selected ('main') or other ('sub') VFO."""
        on_b = self.state["vfo"] == "B"
        return "FB" if (which == "main") == on_b else "FA"

    async def _apply(self, key, v):
        s = self.state
        if key in ("freq", "freq_b"):
            vfo = self._vfo_cmd("main" if key == "freq" else "sub")
            self._queue(key, f"{vfo}{v:011d};")
            # Keep the cache in step, or the next IF poll would put the old
            # frequency back on screen (and the hold would see a "drift").
            if vfo == "FA":
                self._fa = v
            else:
                self._fb = v
        elif key in ("mode", "data"):
            mode = v if key == "mode" else s["mode"]
            data = (v if key == "data" else s["data"]) and mode in self.caps["data_modes"]
            code = MD_WRITE.get((mode, bool(data)))
            if code is None:
                raise ValueError(f"QMX has no mode {mode}{' data' if data else ''}")
            self._queue("mode", f"MD{code};")
            self._update(mode=mode, data=bool(data))
            return
        elif key == "tx":
            self._queue("tx", "TQ1;" if v else "TQ0;")
        elif key == "split":
            self._queue("split", f"SP{int(v)};")
        elif key == "vfo":
            self._queue("vfo", "FR0;" if v == "A" else "FR1;")
        elif key in ("preamp", "att"):
            if v:
                raise ValueError(f"{self.name} has no {key} control")
            return
        else:
            raise ValueError(f"{self.name} has no setting {key!r}")
        self._update(**{key: v})

    async def ssb_from_usb(self, on):
        """SSB transmit audio from the USB sound card (SS0) for a data session
        in plain USB (Winlink/ARDOP), and the previous source back afterwards.
        The QMX's SSB source is the external mic by default (SS2): in USB mode
        it then sent nothing from the PC, 0 W (linux-station, 2026-10-16). SS1 (the
        two-tone test generator) is never set here."""
        if on:
            self._ssb_src = None
            self._queue("ssb_read", "SS;")      # through the pump, between polls
            end = time.monotonic() + 2.0
            while self._ssb_src is None and time.monotonic() < end:
                await asyncio.sleep(0.05)
            if self._ssb_src is None:
                raise ValueError("the QMX did not report its SSB audio source (SS)")
            if self._ssb_src != "0":
                self._ssb_src_was = self._ssb_src
                log.info("SSB audio source: USB for the session (was %s)", self._ssb_src)
                self._queue("ssb_src", "SS0;")
        elif self._ssb_src_was is not None:
            was, self._ssb_src_was = self._ssb_src_was, None
            if was in ("0", "2"):
                log.info("SSB audio source: back to %s", was)
                self._queue("ssb_src", f"SS{was};")

    async def swap_vfo(self):
        a, b = self.state["freq"], self.state["freq_b"]
        await self.set("freq", b)
        await self.set("freq_b", a)

    async def power(self, on: bool):
        # PS0 would switch it off with no way back over CAT.
        raise ValueError(f"{self.name} can't be powered on or off remotely")
