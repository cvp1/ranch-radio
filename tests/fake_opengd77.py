"""A fake OpenGD77 MD-UV380/390 on a pseudo-terminal, for testing opengd77.py.

Answers the CPS protocol the way usb_com.c does on this radio:
  'R' area addr[4] len[2] -> 'R' len[2] data, or '-'. The VFOs are patched
      into a flash read only when one request covers 0x7590..0x7590+112.
  'X' 1 sector[3] / 2 addr[4] len[2] data / 3: prepare a 4 KiB sector of the
      emulated EEPROM (the SPI flash), patch it, write it -> 'X' n, or '-'
  'C' 6 2 save RAM settings and VFOs to flash, 'C' 6 1 restart without
      saving (gone from USB for restart_s, then RAM loads from flash),
      'C' 6 7 t[4] set the clock -> '-'
The radio keeps its settings, last-used channels and VFOs in RAM and
flash separately, as the real one does (saved only on C 6 2 or power off),
so a client that patches flash without saving first is caught.
Every byte the client sends is kept, so a test can prove what went out.

Never connects to real hardware.
"""

import json
import os
import select
import struct
import threading
import time
import tty

VFO_ADDR = 0x7590
LUCZ_ADDR, SETTINGS_ADDR, SETTINGS_LEN = 0x6000, 0x604B, 116
# mac-shack's MD-UV390 (R20260131), 2026-10-20: LUCZ, a pad byte, the settings
# (magic 0x4780, All Channels zone 4, master power level 6, squelch 10).
REAL_MEMORY = bytes.fromhex(
    "4c55435a" + "00" * 70 + "ff"
    "804700000000007f00000000400356670000000000000000000000000000000000000000000000003c1a78051ca367"
    "000000000000000000000004000410000000000000a2010602040504000012646400220001000500000000000a0301"
    "01000114050100750a0a0a0100050300000000ffffff")


def bcd(n):
    return int(str(n), 16)


def channel(name, rx_hz, tx_hz, dmr=False, rx_tone=0xFFFF, tx_tone=0xFFFF, cc=1, ts2=False, wide=False):
    b = bytearray(b"\xff" * 56)
    b[:16] = name.encode().ljust(16, b"\xff")
    struct.pack_into("<II", b, 16, bcd(rx_hz // 10), bcd(tx_hz // 10))
    b[24] = 1 if dmr else 0
    struct.pack_into("<HH", b, 32, rx_tone, tx_tone)
    b[44] = cc
    b[49] = 0x40 if ts2 else 0
    b[51] = 0x02 if wide else 0
    return bytes(b)


def render_screen(texts, fg=0x0000, bg=0xFFFF):
    """A 160x128 framebuffer (big-endian 16-bit pixels) with each (x, y, font,
    text) drawn the way the firmware does (HX8353E_display.c displayPrintCore):
    column-major glyphs from opengd77_font, foreground pixels only."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import opengd77_font
    px = [bg] * (160 * 128)
    for x0, y0, font, text in texts:
        w, h, glyphs = opengd77_font.FONTS[font]
        for i, ch in enumerate(text):
            if ch == " ":
                continue
            g = glyphs[ord(ch)]
            for x in range(w):
                for y in range(h):
                    if (g[x + (y // 8) * w] >> (y % 8)) & 1:
                        px[(y0 + y) * 160 + x0 + i * w + x] = fg
    return b"".join(struct.pack(">H", p) for p in px)


def channel_screen(name, zone_line, bar="DMR TS1 3W C1", contact="Local"):
    """What the MD-UV390 shows in channel mode (mac-shack, 2026-10-17): the top
    bar, the contact, the channel name (8x16) and "<zone> Ch:<n>"."""
    return render_screen([(2, 2, "font_6x8", bar), (2, 24, "font_8x16", contact),
                          (80 - 4 * len(name), 64, "font_8x16", name), (2, 114, "font_6x8", zone_line)])


class FakeOpenGD77:
    def __init__(self, radio_type=6, revision="abc1234"):
        self.lock = threading.Lock()
        self.radio_type = radio_type
        self.revision = revision
        self.vfo = [channel("VFO A", 146_520_000, 146_520_000, rx_tone=0x1000, tx_tone=0x1000, wide=True),
                    channel("VFO B", 439_400_000, 430_400_000, dmr=True, cc=3, ts2=True)]
        self.slots = list(range(1, 4))       # channel slots in use (1-based)
        self.names = {1: "2m Call", 2: "Rpt 147.000", 3: "BM 3104"}   # slot -> name
        self.zones = [{"name": "Home", "slots": [1, 2, 3]}]          # by slot number
        self.power = {}                   # slot -> power level byte (default 9)
        self.records = {}                 # slot -> 56-byte channel record (default: 146.520 FM, its name)
        self.layout_file = None           # a JSON {slots, names, zones} a fake dmrconf write leaves
        self._layout_at = None
        self.screen = channel_screen("2m Call", "Home Ch:1", bar="FM 3W")
        self.flash = bytearray(b"\xff" * 0x10000)      # the emulated EEPROM (bottom of the SPI flash)
        self.flash[LUCZ_ADDR:LUCZ_ADDR + len(REAL_MEMORY)] = REAL_MEMORY
        struct.pack_into("<h", self.flash, SETTINGS_ADDR + 58, 0)        # zone 0 ("Home")
        self.flash[SETTINGS_ADDR + 81] = 34                              # channel mode
        self.flash[VFO_ADDR:VFO_ADDR + 112] = self.vfo[0] + self.vfo[1]
        self.ram = self._load()          # what the radio runs on: {"lucz", "settings"}; self.vfo is RAM too
        self.sector = None               # the sector prepared by X 1
        self.sector_buf = None
        self.flash_writes = []           # sector addresses written (X 3)
        self.saves = 0                   # C 6 2
        self.restarts = 0                # C 6 1
        self.clock = None                # C 6 7
        self.restart_s = 0.6             # off USB this long after a restart
        self.auto_screen = False         # redraw the screen from the state after a restart
        self.on_restart = None           # callback(fake) after a restart
        self.refuse_x = False            # answer every X with '-'
        self.clobber = None              # an address in the prepared sector the firmware itself overwrites
        self.raw = bytearray()
        self.requests = []
        self.times = []                   # when each request arrived (time.time())
        self.mute = False                 # stop answering (an unplugged / hung radio)
        self._stop = threading.Event()
        self.master, self._slave = os.openpty()
        tty.setraw(self._slave)
        self.path = os.ttyname(self._slave)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def stop(self):
        self._stop.set()

    def _load(self):
        """What the radio loads from flash at boot."""
        f = self.flash
        self.vfo = [bytes(f[VFO_ADDR:VFO_ADDR + 56]), bytes(f[VFO_ADDR + 56:VFO_ADDR + 112])]
        return {"lucz": bytes(f[LUCZ_ADDR:LUCZ_ADDR + 74]), "settings": bytes(f[SETTINGS_ADDR:SETTINGS_ADDR + SETTINGS_LEN])}

    def keypad(self, settings=None, lucz=None):
        """A change at the radio's keypad: RAM only, as on the radio. settings
        / lucz are {offset: byte}."""
        with self.lock:
            for key, patch in (("settings", settings), ("lucz", lucz)):
                if patch:
                    b = bytearray(self.ram[key])
                    for o, v in patch.items():
                        b[o] = v
                    self.ram[key] = bytes(b)

    def flash_settings(self):
        return bytes(self.flash[SETTINGS_ADDR:SETTINGS_ADDR + SETTINGS_LEN])

    def state_screen(self):
        """The screen the radio would show for its RAM state (auto_screen)."""
        st = self.ram["settings"]
        if st[81] == 33:
            v = self.vfo[st[89]]
            rx = int("%x" % struct.unpack_from("<I", v, 16)[0]) * 10
            return render_screen([(2, 2, "font_6x8", "FM 3W"), (2, 64, "font_8x16", "%.5f" % (rx / 1e6))])
        zone = struct.unpack_from("<h", st, 58)[0]
        lu = self.ram["lucz"]
        if 0 <= zone < len(self.zones):
            z = self.zones[zone]
            i = lu[4 + zone] if lu[:4] == b"LUCZ" else 0
            slot = z["slots"][min(i, len(z["slots"]) - 1)]
            return channel_screen(self.names.get(slot, "?"), f"{z['name']} Ch:{i + 1}")
        n = (struct.unpack_from("<h", lu, 4 + 68)[0] + 1) if lu[:4] == b"LUCZ" else 1
        return channel_screen(self.names.get(n, "?"), f"All Channels Ch:{n}")

    def _restart(self):
        time.sleep(0.5)                          # NVIC_SystemReset 500 ms after the reply
        with self.lock:
            self.mute = True
        time.sleep(self.restart_s)
        with self.lock:
            self.ram = self._load()
            self.restarts += 1
            if self.auto_screen:
                self.screen = self.state_screen()
            self.mute = False
        if self.on_restart:
            self.on_restart(self)

    def _command(self, req):
        cmd, sub = req[1], req[2] if len(req) > 2 else None
        if (cmd, sub) == (6, 2):
            self.flash[SETTINGS_ADDR:SETTINGS_ADDR + SETTINGS_LEN] = self.ram["settings"]
            self.flash[VFO_ADDR:VFO_ADDR + 112] = self.vfo[0] + self.vfo[1]
            self.saves += 1                      # (LUCZ is saved only at power off: not here)
        elif (cmd, sub) == (6, 1):
            threading.Thread(target=self._restart, daemon=True).start()
        elif (cmd, sub) == (6, 7):
            self.clock = struct.unpack("<I", req[3:7])[0]
        return b"-"

    def _write(self, req):
        if self.refuse_x:
            return b"-"
        sub = req[1]
        if sub == 1:
            if self.sector is not None:          # firmware: only when no sector is pending
                self.sector = None
                return b"-"
            self.sector = int.from_bytes(req[2:5], "big") * 4096
            self.sector_buf = bytearray(self.flash[self.sector:self.sector + 4096])
        elif sub == 2:
            if self.sector is None:
                return b"-"
            addr, n = struct.unpack(">IH", req[2:8])
            for i, v in enumerate(req[8:8 + n]):
                if self.sector <= addr + i < self.sector + 4096:
                    self.sector_buf[addr + i - self.sector] = v
        elif sub == 3:
            if self.sector is None:
                return b"-"
            if self.clobber is not None and self.sector <= self.clobber < self.sector + 4096:
                self.sector_buf[self.clobber - self.sector] ^= 0xFF
            self.flash[self.sector:self.sector + 4096] = self.sector_buf
            self.flash_writes.append(self.sector)
            self.sector = None
        else:
            return b"-"
        return b"X" + bytes([sub])

    def _info(self):
        return struct.pack("<II16s16sIH", 3, self.radio_type, self.revision.encode(), b"20260131120000", 0xEF4018, 0)

    def _follow_writes(self):
        """Take up what a (fake) dmrconf write left: the radio's memory changed."""
        try:
            at = os.path.getmtime(self.layout_file)
        except (TypeError, OSError):
            return
        if at != self._layout_at:
            with open(self.layout_file) as f:
                d = json.load(f)
            self.slots, self.zones = d["slots"], d["zones"]
            self.names = {int(k): v for k, v in d["names"].items()}
            self._layout_at = at

    def _eeprom(self, addr, length):
        """Channel bank 0 (bitmap at 0x3780, channels after it) and the zone
        bank (bitmap at 0x8010, zones after it), as the radio lays them out."""
        mem = bytearray(b"\xff" * 0x10000)
        bm = bytearray(16)
        for n in self.slots:
            if n <= 128:
                bm[(n - 1) // 8] |= 1 << ((n - 1) % 8)
                rec = bytearray(self.records.get(n) or channel(self.names.get(n, f"Ch {n}"), 146_520_000, 146_520_000))
                rec[25] = self.power.get(n, 9)                 # power level qdmr can write back
                rec[38:42] = b"\x00\x00\x16\x00"            # no DMR ID override
                mem[0x3790 + (n - 1) * 56:0x3790 + n * 56] = rec
        mem[0x3780:0x3790] = bm
        zb = bytearray(32)
        for i, z in enumerate(self.zones):
            zb[i // 8] |= 1 << (i % 8)
            rec = z["name"].encode().ljust(16, b"\xff") + struct.pack("<80H", *(z["slots"] + [0] * (80 - len(z["slots"]))))
            mem[0x8030 + i * 176:0x8030 + (i + 1) * 176] = rec
        mem[0x8010:0x8030] = zb
        return bytes(mem[addr:addr + length])

    def _answer(self, req):
        area, addr, length = struct.unpack(">BIH", req[1:8])
        self._follow_writes()
        if area == 9:
            data = self._info()
        elif area == 6:
            data = self.screen[addr:addr + length]
        elif area == 2 and (0x3780 <= addr < 0x3790 + 128 * 56 or 0x8010 <= addr < 0x8030 + 68 * 176):
            data = self._eeprom(addr, length)
        elif area == 2 and addr + length <= len(self.flash):
            data = bytes(self.flash[addr:addr + length])
        elif area == 1 and addr >= 128 * 1024 + 0x7B1B0:   # flash bank headers: empty banks
            data = b"\x00" * length
        elif area == 1:
            data = bytearray(b"\x00" * length)       # the stored (stale) codeplug VFOs
            if addr <= VFO_ADDR and addr + length - 1 >= VFO_ADDR + 112:
                off = VFO_ADDR - addr
                data[off:off + 112] = self.vfo[0] + self.vfo[1]
            data = bytes(data)
        else:
            return b"-"
        return b"R" + struct.pack(">H", len(data)) + data

    def _run(self):
        buf = bytearray()
        while not self._stop.is_set():
            r, _, _ = select.select([self.master], [], [], 0.1)
            if not r:
                continue
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                return
            with self.lock:
                self.raw += chunk
            buf += chunk
            while buf:
                n = self._length(buf)
                if n is None:                       # not a request the radio knows: '-'
                    with self.lock:
                        self.requests.append(bytes(buf))
                    buf.clear()
                    os.write(self.master, b"-")
                    break
                if len(buf) < n:
                    break
                req, buf = bytes(buf[:n]), buf[n:]
                with self.lock:
                    self.requests.append(req)
                    self.times.append(time.time())
                    mute = self.mute
                if mute:
                    continue
                with self.lock:
                    if req[:1] == b"R":
                        out = self._answer(req)
                    elif req[:1] == b"X":
                        out = self._write(req)
                    else:
                        out = self._command(req)
                os.write(self.master, out)

    @staticmethod
    def _length(buf):
        """How long the request at the head of buf is (None: unknown)."""
        k = buf[0:1]
        if k == b"R":
            return 8
        if k == b"X" and len(buf) >= 2:
            if buf[1] == 2:
                return 8 + struct.unpack(">H", bytes(buf[6:8]))[0] if len(buf) >= 8 else 10 ** 6
            return {1: 5, 3: 2}.get(buf[1])
        if k == b"C" and len(buf) >= 3 and buf[1] == 6:
            return 7 if buf[2] == 7 else 3
        if k == b"X" or (k == b"C" and len(buf) < 3):
            return 10 ** 6 if len(buf) < 3 else None
        return None
