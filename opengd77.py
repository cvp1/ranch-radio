"""OpenGD77 firmware over USB (TYT MD-UV380 / MD-UV390 and kin).

OpenGD77 has no CAT protocol. Over its USB serial port (STM32 CDC, VID:PID
1FC9:0094, /dev/ttyACM* on Linux, /dev/cu.usbmodem* on macOS) it speaks only
the binary protocol its CPS (programming software) uses. Reading gives:

  - who the radio is: firmware revision, build date, radio type (area 9)
  - the live VFO A and B: frequency, FM/DMR, tones, colour code, timeslot.
    A flash read that covers 0x7590..0x7590+112 in ONE request gets the
    in-RAM VFOs patched into the reply (usb_com.c cpsHandleReadCommand).
  - the screen: the 160x128 framebuffer (area 6), 2 bytes a pixel

There is no command to tune, change mode, key PTT or read the S-meter. What
the radio does have is its memory: it comes up on whatever its settings,
last-used-channel table and VFOs in flash say (opengd77_mem). So a change
from the page is save, patch, restart (Control):

  'C' 6 2        save the radio's RAM settings and VFOs to flash
  'X' 1/2/3      prepare a 4 KiB flash sector, patch bytes in it, write it
  'C' 6 1        restart without saving (so the patched flash is what loads)
  'C' 6 7 t[4]   set the clock (UTC seconds), no restart

_send refuses anything else before it reaches the port: any other 'C'
command (the screen takeover, LED, codec), any 'X' outside the three
records opengd77_mem.WRITABLE (so never another sector, calibration at
0x10000 above all), 'W', debug 'D', and hotspot frames (0xE0, which make
the radio a DMR transmitter). Nothing here keys the radio.

Each request goes out as one write shorter than a USB packet (64 bytes),
so the firmware takes it as complete (usbd_cdc_if.c CDC_Receive_FS only
reassembles when the first packet is exactly full size).

Protocol facts are from the OpenGD77 MD-UV380 firmware source (R20240908 and
R20260131 source drops: application/source/usb/usb_com.c, functions/codeplug.c,
include/functions/codeplug.h, include/hardware/HX8353E.h), read 2026-10-17;
the write path and settings from R20260131 usb_com.c, settings.c and
EEPROM.c, read 2026-10-20.
"""

import glob
import os
import re
import select
import struct
import subprocess
import threading
import time

import opengd77_mem as mem
from civ import open_serial

AREA_FLASH = 1
AREA_EEPROM = 2
FLASH_OFFSET = 128 * 1024         # FLASH_ADDRESS_OFFSET on the MD-UV380 (codeplug.h)
BANKS = 8                         # CODEPLUG_CHANNELS_BANKS_MAX, 128 channels each
AREA_DISPLAY = 6
AREA_RADIO_INFO = 9

VFO_ADDR = 0x7590                 # CODEPLUG_ADDR_VFO_A_CHANNEL
CHANNEL_SIZE = 56                 # CODEPLUG_CHANNEL_DATA_STRUCT_SIZE
# The VFO patch needs address <= 0x7590 and address+len-1 >= 0x7590+112.
VFO_READ_LEN = 2 * CHANNEL_SIZE + 8
ZONE_BANK = 0x8010                # EEPROM: zone in-use bitmap (32 bytes), then the zones
ZONE_COUNT = 68
ZONE_SIZE = 16 + 80 * 2           # name, then 80 channel numbers
MAX_READ = 2045                  # COM_REQUESTBUFFER_SIZE (2048) - 3
SCREEN_CHUNK = 1024

RADIO_TYPES = {0: "GD-77", 1: "GD-77S", 2: "DM-1801", 3: "RD-5R", 4: "DM-1801A", 5: "MD-9600",
               6: "MD-UV380/390", 7: "MD-380", 8: "DM-1701", 9: "MD-2017", 10: "DM-1701"}
# Radios whose screen is the 160x128 colour LCD at offset 0 (HX8353E.h:
# DISPLAY_Y_OFFSET is 0 except on the DM-1701). The others are left out.
COLOUR_SCREEN = {6, 7, 9}
SCREEN_W, SCREEN_H = 160, 128
SCREEN_BYTES = SCREEN_W * SCREEN_H * 2

CSS_NONE = 0xFFFF
CSS_DCS = 0x8000
CSS_DCS_INVERTED = 0x4000


USB_ID = (0x1FC9, 0x0094)         # USB_DEVICE/App/usbd_desc.c, every OpenGD77 radio


class OpenGD77Error(Exception):
    pass


def find_port(usb_id=USB_ID):
    """The serial device of the OpenGD77 radio plugged in, found by its USB
    ID, or None. Matching the ID first means no byte goes to another radio's
    port (the Meshtastic node is a /dev/cu.usbmodem too)."""
    vid, pid = usb_id
    if os.path.isdir("/sys/class/tty"):                       # Linux
        for tty in sorted(glob.glob("/sys/class/tty/ttyACM*")):
            dev = os.path.realpath(os.path.join(tty, "device"))
            for d in (os.path.dirname(dev), os.path.dirname(os.path.dirname(dev))):
                try:
                    with open(os.path.join(d, "idVendor")) as f:
                        v = int(f.read(), 16)
                    with open(os.path.join(d, "idProduct")) as f:
                        p = int(f.read(), 16)
                except (OSError, ValueError):
                    continue
                if (v, p) == (vid, pid):
                    return "/dev/" + os.path.basename(tty)
        return None
    try:                                                      # macOS (absolute: launchd's PATH has no /usr/sbin)
        # errors="replace": ioreg prints some properties raw (a USB network
        # adapter's IOMACAddress), and those bytes need not be UTF-8
        out = subprocess.run(["/usr/sbin/ioreg", "-c", "IOUSBHostDevice", "-l", "-w0"],
                             capture_output=True, text=True, errors="replace", timeout=5).stdout
    except (OSError, subprocess.SubprocessError) as e:      # not "no radio": say what broke
        raise OpenGD77Error(f"USB lookup failed (ioreg): {e}")
    return _mac_port(out, vid, pid)


def _mac_port(ioreg_text, vid, pid):
    """Walk ioreg's tree: each serial port (IOCalloutDevice) belongs to the
    nearest USB device above it; return the one whose ID matches."""
    stack = []                                   # [(column, props)] from root to the current node
    for line in ioreg_text.splitlines():
        col = line.find("+-o ")
        if col >= 0:
            while stack and stack[-1][0] >= col:
                stack.pop()
            stack.append((col, {}))
            continue
        m = re.search(r'"(idVendor|idProduct|IOCalloutDevice)" = (\d+|"[^"]*")', line)
        if not m or not stack:
            continue
        stack[-1][1][m.group(1)] = m.group(2).strip('"')
        if m.group(1) == "IOCalloutDevice":
            for _, props in reversed(stack[:-1]):
                if "idVendor" in props and "idProduct" in props:
                    if (int(props["idVendor"]), int(props["idProduct"])) == (vid, pid):
                        return m.group(2).strip('"')
                    break
    return None
    for block in re.split(r"(?m)^\+-o ", out)[1:]:      # one block per device, children inside
        v = re.search(r'"idVendor" = (\d+)', block)
        p = re.search(r'"idProduct" = (\d+)', block)
        if v and p and (int(v.group(1)), int(p.group(1))) == (vid, pid):
            m = re.search(r'"IOCalloutDevice" = "([^"]+)"', block)
            if m:
                return m.group(1)
    return None


def bcd(v):
    """int2bcd's output back to an int (0x14652000 -> 14652000)."""
    s = "%x" % v
    if not s.isdigit():
        raise ValueError(f"not BCD: {v:#x}")
    return int(s)


def tone(v):
    """A codeplug CSS value: "" | "100.0" | "D023N" / "D023I" (the radio's own
    display: uiUtilities.c, dcsPrintf)."""
    if v in (CSS_NONE, 0):
        return ""
    if v & (CSS_DCS | CSS_DCS_INVERTED):
        return "D%03X%s" % (v & ~(CSS_DCS | CSS_DCS_INVERTED) & 0xFFFF, "I" if v & CSS_DCS_INVERTED else "N")
    t = bcd(v)
    return "%d.%d" % (t // 10, t % 10)


def channel(b):
    """A 56-byte CodeplugChannel_t in codeplug form (codeplug.h)."""
    if len(b) != CHANNEL_SIZE:
        raise ValueError(f"channel is {len(b)} bytes, not {CHANNEL_SIZE}")
    name = b[:16].split(b"\xff")[0].split(b"\x00")[0].decode("ascii", "replace")
    rx, tx = struct.unpack_from("<II", b, 16)
    rx_tone, tx_tone = struct.unpack_from("<HH", b, 32)
    rx_hz, tx_hz = bcd(rx) * 10, bcd(tx) * 10
    dmr = b[24] == 1
    flag2, flag4 = b[49], b[51]
    out = {"name": name, "rx_hz": rx_hz, "tx_hz": tx_hz, "mode": "DMR" if dmr else "FM",
           "rx_only": bool(flag4 & 0x04)}
    if dmr:
        out.update(colour_code=b[44], timeslot=2 if flag2 & 0x40 else 1)
    else:
        out.update(rx_tone=tone(rx_tone), tx_tone=tone(tx_tone),
                   bandwidth="25 kHz" if flag4 & 0x02 else "12.5 kHz")
    return out


def channel_name(b):
    return b[:16].split(b"\xff")[0].split(b"\x00")[0].decode("ascii", "replace").strip()


def slot_addr(n):
    """(area, address) of channel slot n's 56 bytes: bank 0 (1-128) in
    EEPROM after its 16-byte bitmap at 0x3780, banks 1-7 in SPI flash."""
    bank, i = divmod(n - 1, 128)
    if not 0 <= bank < BANKS:
        raise ValueError(f"channel slot {n}")
    if bank == 0:
        return AREA_EEPROM, 0x3780 + 16 + i * CHANNEL_SIZE
    return AREA_FLASH, FLASH_OFFSET + 0x7B1B0 + (bank - 1) * (16 + 128 * CHANNEL_SIZE) + 16 + i * CHANNEL_SIZE


def radio_info(b):
    """The radio-info struct (usb_com.c, structVersion 3), little-endian."""
    if len(b) < 46:
        raise ValueError(f"radio info is {len(b)} bytes")
    version, rtype, rev, built, flash_id, features = struct.unpack_from("<II16s16sIH", b, 0)
    s = lambda x: x.split(b"\x00")[0].decode("ascii", "replace")
    return {"struct": version, "type": rtype, "model": RADIO_TYPES.get(rtype, f"type {rtype}"),
            "revision": s(rev), "built": s(built), "flash_id": f"{flash_id:#x}", "features": features,
            "screen": rtype in COLOUR_SCREEN}


WRITE_CHUNK = 48                  # X 2 data bytes per request: 8 + 48 < one 64-byte USB packet
WRITABLE_SECTORS = {a - a % mem.SECTOR for a, n in mem.WRITABLE} | {(a + n - 1) - (a + n - 1) % mem.SECTOR
                                                                    for a, n in mem.WRITABLE}
COMMANDS = {(6, 1): 3, (6, 2): 3, (6, 7): 7}   # 'C' (command, sub): request length


def refusal(req):
    """Why req may not go to the radio, or None. Reads go; writes only into
    opengd77_mem.WRITABLE; commands only save, restart-without-save and clock."""
    if not req:
        return "empty request"
    kind = req[:1]
    if kind == b"R":
        return None if len(req) == 8 else "malformed read"
    if kind == b"C":
        if len(req) >= 3 and COMMANDS.get((req[1], req[2])) == len(req):
            return None
        return f"command {req[1:3].hex()} is not one the page sends"
    if kind == b"X":
        sub = req[1] if len(req) > 1 else None
        if sub == 1 and len(req) == 5:
            sector = int.from_bytes(req[2:5], "big") * mem.SECTOR
            return None if sector in WRITABLE_SECTORS else f"flash sector {sector:#x} is not writable from here"
        if sub == 2 and len(req) >= 8:
            addr, n = struct.unpack(">IH", req[2:8])
            if len(req) != 8 + n or n > WRITE_CHUNK:
                return "malformed write"
            return None if mem.writable(addr, n) else f"write at {addr:#x}+{n} is outside the settings records"
        if sub == 3 and len(req) == 2:
            return None
        return "malformed write"
    return f"{kind!r} requests are never sent"


class OpenGD77:
    """One radio on one serial port. Blocking; the add-on runs it in a thread.
    The lock makes each request/reply a unit."""

    def __init__(self, port, timeout=1.5):
        self.want = port
        self.port = port
        self.timeout = timeout
        self.fd = None
        self.lock = threading.Lock()

    def open(self):
        """Open the port; port "auto" is found by USB ID each time (a
        re-plugged radio may come back under another name)."""
        path = find_port() if self.want == "auto" else self.want
        if not path:
            raise OpenGD77Error("no OpenGD77 radio on USB (is it switched on?)")
        self.port = path
        self.fd = open_serial(path, 115200)        # USB CDC: the speed is ignored
        self._drain()                              # leftovers of someone else's last reply

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def _send(self, req):
        why = refusal(req)
        if why:
            raise OpenGD77Error(f"refused: {why}")
        if os.write(self.fd, req) != len(req):
            raise OpenGD77Error("short write to the radio")

    def _recv(self, n, head=False):
        buf = bytearray()
        end = time.monotonic() + self.timeout
        while len(buf) < n:
            left = end - time.monotonic()
            if left <= 0:
                raise OpenGD77Error(f"timed out: {len(buf)} of {n} bytes")
            r, _, _ = select.select([self.fd], [], [], left)
            if r:
                chunk = os.read(self.fd, n - len(buf))
                if not chunk:
                    raise OpenGD77Error("port closed")
                buf += chunk
                if head and buf[:1] == b"-":      # only a reply's first byte can be the refusal;
                    raise OpenGD77Error("the radio refused the read")   # data may hold '-' anywhere
        return bytes(buf)

    def read(self, area, addr, length):
        if not 0 < length <= MAX_READ:
            raise ValueError(f"read length {length}")
        with self.lock:
            if self.fd is None:
                raise OpenGD77Error("not open")
            try:
                self._send(struct.pack(">cBIH", b"R", area, addr, length))
                head = self._recv(3, head=True)
                if head[:1] != b"R":
                    raise OpenGD77Error(f"unexpected reply {head!r}")
                got = (head[1] << 8) | head[2]
                if got != length and area != AREA_RADIO_INFO:
                    raise OpenGD77Error(f"asked {length} bytes, the radio sends {got}")
                return self._recv(got)
            except OpenGD77Error:
                self._drain()        # a late reply must not be read as the next one's
                raise

    def _drain(self, quiet_s=0.3):
        """Throw away whatever arrives until the line has been quiet a moment."""
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            r, _, _ = select.select([self.fd], [], [], quiet_s)
            if not r:
                return
            try:
                if not os.read(self.fd, 4096):
                    return
            except OSError:
                return

    def info(self):
        return radio_info(self.read(AREA_RADIO_INFO, 0, 64))

    def vfos(self):
        b = self.read(AREA_FLASH, VFO_ADDR, VFO_READ_LEN)
        return [channel(b[i * CHANNEL_SIZE:(i + 1) * CHANNEL_SIZE]) for i in (0, 1)]

    def channel_slots(self):
        """The channel slots in use (1-based), from the radio's own in-use
        bitmaps: bank 0 (channels 1-128) in EEPROM at 0x3780, banks 1-7 in
        SPI flash at 128 KiB + 0x7B1B0, one bank every 16 + 128*56 bytes
        (codeplug.c codeplugAllChannelsReadHeaderBank; FLASH_ADDRESS_OFFSET
        is 128 KiB on the MD-UV380). Independent of any codeplug tool."""
        used = []
        for bank in range(BANKS):
            if bank == 0:
                bm = self.read(AREA_EEPROM, 0x3780, 16)
            else:
                bm = self.read(AREA_FLASH, FLASH_OFFSET + 0x7B1B0 + (bank - 1) * (16 + 128 * CHANNEL_SIZE), 16)
            if bank and bm == b"\xff" * 16:          # erased flash: bank never written
                continue
            used += [bank * 128 + i + 1 for i in range(128) if bm[i // 8] & (1 << (i % 8))]
        return used

    def slot_record(self, n):
        """What the radio holds in channel slot n (1-based): name, power
        level (byte 25: 0 = the radio's master power, 1-10 the levels) and
        the DMR ID override (byte 38 bit 7, ID big-endian in bytes 39-41;
        None when off). qdmr layout: opengd77base_codeplug.hh ChannelElement."""
        area, addr = slot_addr(n)
        b = self.read(area, addr, CHANNEL_SIZE)
        try:
            detail = channel(b)
        except ValueError:                       # a record qdmr or the radio left half-written
            detail = None
        return {"name": channel_name(b), "power": b[25], "detail": detail,
                "dmr_id": int.from_bytes(b[39:42], "big") if b[38] & 0x80 else None}

    def zones(self):
        """The radio's zones, in order: [{"name", "slots": [channel slot, ...]}].
        EEPROM 0x8010: a 32-byte in-use bitmap, then 68 zones of a 16-byte
        name and 80 little-endian channel numbers, 1-based, 0 ends the list
        (codeplug.c codeplugZoneGetDataForNumber; qdmr openuv380_codeplug.hh
        zoneBank). These are the radio's own slot numbers, whatever a
        codeplug tool renumbers them to."""
        bm = self.read(AREA_EEPROM, ZONE_BANK, 32)
        out = []
        for z in range(ZONE_COUNT):
            if not bm[z // 8] & (1 << (z % 8)):
                continue
            b = self.read(AREA_EEPROM, ZONE_BANK + 32 + z * ZONE_SIZE, ZONE_SIZE)
            slots = []
            for (c,) in struct.iter_unpack("<H", b[16:]):
                if c in (0, 0xFFFF):
                    break
                slots.append(c)
            out.append({"name": channel_name(b[:16]), "slots": slots})
        return out

    def layout(self):
        """What the radio's own memory says: the channel slots in use, the
        name, power level and DMR ID override in each, and the zones by slot
        number."""
        slots = self.channel_slots()
        recs = {n: self.slot_record(n) for n in slots}
        return {"slots": slots, "names": {n: r["name"] for n, r in recs.items()},
                "power": {n: r["power"] for n, r in recs.items()},
                "channels": {n: r["detail"] for n, r in recs.items()},
                "dmr_id": {n: r["dmr_id"] for n, r in recs.items()}, "zones": self.zones()}

    # ---------------------------------------------------------- control (opengd77_mem)

    def _reply(self, want):
        """The reply to a command or write: want, or '-' (refused)."""
        with_head = self._recv(1)
        if with_head == b"-" and want != b"-":
            raise OpenGD77Error("the radio refused it")
        rest = self._recv(len(want) - 1) if len(want) > 1 else b""
        if with_head + rest != want:
            raise OpenGD77Error(f"unexpected reply {(with_head + rest)!r}")

    def _request(self, req, want):
        with self.lock:
            if self.fd is None:
                raise OpenGD77Error("not open")
            try:
                self._send(req)
                self._reply(want)
            except OpenGD77Error:
                self._drain()
                raise

    def command(self, cmd, sub, payload=b""):
        """'C' cmd sub [payload]; the radio answers every command with '-'."""
        self._request(bytes([ord("C"), cmd, sub]) + payload, b"-")

    def save(self):
        """Save the radio's RAM settings and VFOs to flash (C 6 2), so what is
        read from flash next is what the radio is on now."""
        self.command(6, 2)

    def restart(self):
        """Restart the radio without saving (C 6 1): it comes up on its flash.
        It leaves USB about half a second later."""
        self.command(6, 1)

    def set_clock(self, t):
        """Set the radio's clock to t (UTC seconds since 1970; C 6 7)."""
        self.command(6, 7, struct.pack("<I", int(t)))

    def memory(self):
        """The records the radio starts on: {"lucz", "settings", "vfo"} raw
        bytes from flash (VFOs as stored, not the live ones)."""
        return {"lucz": self.read(AREA_EEPROM, mem.LUCZ_ADDR, mem.LUCZ_LEN),
                "settings": self.read(AREA_EEPROM, mem.SETTINGS_ADDR, mem.SETTINGS_LEN),
                "vfo": self.read(AREA_EEPROM, mem.VFO_ADDR, mem.VFO_LEN)}

    def sectors(self):
        """{sector address: 4096 bytes} of every sector the page may write, for a backup."""
        return {a: b"".join(self.read(AREA_EEPROM, a + o, 1024) for o in range(0, mem.SECTOR, 1024))
                for a in sorted(WRITABLE_SECTORS)}

    def write_flash(self, spans, before=None):
        """Write [(address, bytes)] into flash, a sector at a time (prepare,
        patch, write: the firmware reads the sector first, so the rest of it
        is kept), then read every span back. With before ({sector address:
        4096 bytes}, from sectors() just now), each written sector is read
        back whole and must equal before with the spans in it: the firmware
        builds the sector in one buffer it shares with its own flash writes,
        so this catches anything else in the sector having moved. Raises if
        any byte differs."""
        by_sector = {}
        for addr, data in spans:
            if not mem.writable(addr, len(data)):
                raise OpenGD77Error(f"refused: write at {addr:#x}+{len(data)} is outside the settings records")
            if addr // mem.SECTOR != (addr + len(data) - 1) // mem.SECTOR:
                raise OpenGD77Error(f"write at {addr:#x} crosses a sector")
            by_sector.setdefault(addr - addr % mem.SECTOR, []).append((addr, data))
        for sector, parts in sorted(by_sector.items()):
            prep = b"X\x01" + (sector // mem.SECTOR).to_bytes(3, "big")
            try:
                self._request(prep, b"X\x01")
            except OpenGD77Error:                    # a sector left prepared by an earlier, broken run:
                self._request(prep, b"X\x01")       # the refusal reset it, so once more
            for addr, data in parts:
                for o in range(0, len(data), WRITE_CHUNK):
                    chunk = data[o:o + WRITE_CHUNK]
                    self._request(b"X\x02" + struct.pack(">IH", addr + o, len(chunk)) + chunk, b"X\x02")
            self._request(b"X\x03", b"X\x03")
        for addr, data in spans:
            got = self.read(AREA_EEPROM, addr, len(data))
            if got != data:
                raise OpenGD77Error(f"flash at {addr:#x} reads back different from what was written")
        for sector in sorted(by_sector) if before else ():
            want = bytearray(before[sector])
            for addr, data in by_sector[sector]:
                want[addr - sector:addr - sector + len(data)] = data
            got = b"".join(self.read(AREA_EEPROM, sector + o, 1024) for o in range(0, mem.SECTOR, 1024))
            if got != bytes(want):
                bad = next(i for i in range(mem.SECTOR) if got[i] != want[i])
                raise OpenGD77Error(f"flash sector {sector:#x} changed outside the write (first at {sector + bad:#x})")

    def screen(self):
        """The raw framebuffer: 160x128, each pixel a big-endian 16-bit colour
        (the firmware stores PLATFORM_COLOUR_FORMAT_SWAP_BYTES values), RGB565
        or BGR565 by panel."""
        out = bytearray()
        for addr in range(0, SCREEN_BYTES, SCREEN_CHUNK):
            out += self.read(AREA_DISPLAY, addr, min(SCREEN_CHUNK, SCREEN_BYTES - addr))
        return bytes(out)
