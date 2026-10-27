"""AX.25 frames and KISS framing, for a radio with a built-in KISS TNC.

Pure functions and small value types; no I/O. Covers what the TNC hub needs:

- KISS: escape/unescape and a streaming decoder for the radio's byte stream.
- AX.25 v2.0 addresses (callsign-SSID, the C/H bits and the end-of-address bit).
- Frame encode/decode for I, S (RR/RNR/REJ) and U (SABM, UA, DISC, DM, FRMR, UI)
  frames, modulo 8, as used by Winlink RMS gateways and APRS.
- TNC2 monitor text ("N9ABC-7>APRS,WIDE1-1*:payload") for logs and the page.
"""

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD

PID_NO_L3 = 0xF0

# U-frame control values with the P/F bit (0x10) cleared.
U_SABM, U_SABME, U_DISC, U_DM, U_UA, U_FRMR, U_UI, U_XID, U_TEST = (
    0x2F, 0x6F, 0x43, 0x0F, 0x63, 0x87, 0x03, 0xAF, 0xE3)
U_NAMES = {U_SABM: "SABM", U_SABME: "SABME", U_DISC: "DISC", U_DM: "DM", U_UA: "UA",
           U_FRMR: "FRMR", U_UI: "UI", U_XID: "XID", U_TEST: "TEST"}
S_RR, S_RNR, S_REJ, S_SREJ = 0, 1, 2, 3
S_NAMES = {S_RR: "RR", S_RNR: "RNR", S_REJ: "REJ", S_SREJ: "SREJ"}

MAX_FRAME_BYTES = 400   # an AX.25 frame longer than this is refused or dropped


class FrameError(ValueError):
    pass


# --------------------------------------------------------------------- KISS

def kiss_escape(data: bytes) -> bytes:
    return data.replace(bytes([FESC]), bytes([FESC, TFESC])).replace(
        bytes([FEND]), bytes([FESC, TFEND]))


def kiss_unescape(data: bytes) -> bytes:
    out = bytearray()
    it = iter(data)
    for b in it:
        if b == FESC:
            nxt = next(it, None)
            if nxt == TFEND:
                out.append(FEND)
            elif nxt == TFESC:
                out.append(FESC)
            elif nxt is not None:      # protocol violation: keep the byte, drop the FESC
                out.append(nxt)
        else:
            out.append(b)
    return bytes(out)


def kiss_wrap(ax25: bytes, port: int = 0, cmd: int = 0) -> bytes:
    """One KISS frame: FEND, type byte (port<<4 | cmd), escaped payload, FEND."""
    return bytes([FEND, ((port & 0x0F) << 4) | (cmd & 0x0F)]) + kiss_escape(ax25) + bytes([FEND])


class KissDecoder:
    """Feed it bytes as they arrive; it returns complete (port, cmd, payload) tuples.

    Bytes outside FEND...FEND are discarded, so stray non-KISS output from the
    radio can't turn into a frame. A frame that grows past `max_len` is dropped.
    """

    def __init__(self, max_len=MAX_FRAME_BYTES * 2 + 2):
        self.buf = bytearray()
        self.in_frame = False
        self.max_len = max_len
        self.dropped = 0

    def feed(self, data: bytes):
        frames = []
        for b in data:
            if b == FEND:
                if self.in_frame and self.buf:
                    raw = kiss_unescape(bytes(self.buf))
                    if raw:
                        frames.append((raw[0] >> 4, raw[0] & 0x0F, raw[1:]))
                self.buf.clear()
                self.in_frame = True
            elif self.in_frame:
                self.buf.append(b)
                if len(self.buf) > self.max_len:
                    self.buf.clear()
                    self.in_frame = False
                    self.dropped += 1
        return frames


# ---------------------------------------------------------------- addresses

def parse_call(text: str):
    """'N9ABC-7' -> ('N9ABC', 7). Raises FrameError on an invalid callsign."""
    text = text.strip().upper().rstrip("*")
    call, _, ssid = text.partition("-")
    if not (1 <= len(call) <= 6) or not call.isalnum():
        raise FrameError(f"bad callsign {text!r}")
    try:
        n = int(ssid) if ssid else 0
    except ValueError:
        raise FrameError(f"bad SSID in {text!r}") from None
    if not 0 <= n <= 15:
        raise FrameError(f"bad SSID in {text!r}")
    return call, n


def call_str(call, ssid):
    return f"{call}-{ssid}" if ssid else call


def base_call(text: str) -> str:
    return parse_call(text)[0]


class Address:
    __slots__ = ("bit7", "call", "ssid")

    def __init__(self, call, ssid=0, bit7=False):
        self.call, self.ssid, self.bit7 = call.upper(), ssid, bit7

    @classmethod
    def from_text(cls, text, bit7=False):
        call, ssid = parse_call(text)
        return cls(call, ssid, bit7)

    def encode(self, last=False) -> bytes:
        b = bytes((ord(c) << 1) for c in self.call.ljust(6))
        ssid = 0x60 | (self.ssid << 1) | (0x80 if self.bit7 else 0) | (1 if last else 0)
        return b + bytes([ssid])

    @classmethod
    def decode(cls, b: bytes):
        if len(b) != 7:
            raise FrameError("short address")
        chars = []
        for x in b[:6]:
            if x & 1:
                raise FrameError("address extension bit inside callsign")
            chars.append(chr(x >> 1))
        call = "".join(chars).rstrip()
        if not call or not call.isalnum():
            raise FrameError(f"bad callsign bytes {b[:6].hex()}")
        return cls(call, (b[6] >> 1) & 0x0F, bool(b[6] & 0x80))

    def __str__(self):
        return call_str(self.call, self.ssid)

    def same_station(self, other):
        return self.call == other.call and self.ssid == other.ssid


# ------------------------------------------------------------------- frames

class Frame:
    """A decoded AX.25 frame.

    kind: "I", "S" or "U". For S frames `stype` is S_RR etc.; for U frames
    `utype` is U_SABM etc. `command` is the v2 command/response sense taken
    from the C bits (dest C=1, src C=0 means command).
    """

    __slots__ = (
        "command",
        "dest",
        "digis",
        "info",
        "kind",
        "nr",
        "ns",
        "pf",
        "pid",
        "src",
        "stype",
        "utype",
    )

    def __init__(self, dest, src, digis=(), kind="U", ns=0, nr=0, pf=False, stype=None,
                 utype=U_UI, pid=None, info=b"", command=True):
        self.dest, self.src, self.digis = dest, src, list(digis)
        self.kind, self.ns, self.nr, self.pf = kind, ns, nr, pf
        self.stype, self.utype, self.pid, self.info, self.command = stype, utype, pid, info, command

    # -- constructors --

    @classmethod
    def ui(cls, src, dest, info: bytes, digis=(), pid=PID_NO_L3):
        return cls(Address.from_text(dest), Address.from_text(src),
                   [Address.from_text(d) for d in digis], "U", utype=U_UI, pid=pid,
                   info=info, command=True)

    def control(self):
        pf = 0x10 if self.pf else 0
        if self.kind == "I":
            return ((self.nr & 7) << 5) | pf | ((self.ns & 7) << 1)
        if self.kind == "S":
            return ((self.nr & 7) << 5) | pf | (self.stype << 2) | 0x01
        return self.utype | pf

    def encode(self) -> bytes:
        dest = Address(self.dest.call, self.dest.ssid, bit7=self.command)
        src = Address(self.src.call, self.src.ssid, bit7=not self.command)
        parts = [dest.encode(), src.encode(last=not self.digis)]
        for i, d in enumerate(self.digis):
            parts.append(d.encode(last=i == len(self.digis) - 1))
        parts.append(bytes([self.control()]))
        if self.kind == "I" or (self.kind == "U" and self.utype == U_UI):
            parts.append(bytes([self.pid if self.pid is not None else PID_NO_L3]))
            parts.append(self.info)
        elif self.kind == "U" and self.utype in (U_FRMR, U_XID, U_TEST):
            parts.append(self.info)
        out = b"".join(parts)
        if len(out) > MAX_FRAME_BYTES:
            raise FrameError(f"frame too long ({len(out)} bytes)")
        return out

    @classmethod
    def decode(cls, b: bytes):
        if len(b) < 15:
            raise FrameError("frame too short")
        if len(b) > MAX_FRAME_BYTES:
            raise FrameError("frame too long")
        addrs = []
        i = 0
        while True:
            if i + 7 > len(b):
                raise FrameError("address field runs off the end")
            addrs.append(Address.decode(b[i:i + 7]))
            last = b[i + 6] & 1
            i += 7
            if last:
                break
            if len(addrs) > 10:
                raise FrameError("too many addresses")
        if len(addrs) < 2:
            raise FrameError("no source address")
        if i >= len(b):
            raise FrameError("no control field")
        dest, src, digis = addrs[0], addrs[1], addrs[2:]
        # v2: dest C=1/src C=0 is a command; the reverse a response. v1 (both
        # equal) is treated as a command.
        command = not (src.bit7 and not dest.bit7)
        ctl = b[i]
        i += 1
        pf = bool(ctl & 0x10)
        f = cls(dest, src, digis, pf=pf, command=command)
        if ctl & 1 == 0:
            f.kind, f.ns, f.nr = "I", (ctl >> 1) & 7, (ctl >> 5) & 7
            if i >= len(b):
                raise FrameError("I frame without PID")
            f.pid, f.info = b[i], b[i + 1:]
        elif ctl & 3 == 1:
            f.kind, f.stype, f.nr = "S", (ctl >> 2) & 3, (ctl >> 5) & 7
        else:
            f.kind, f.utype = "U", ctl & ~0x10 & 0xFF
            if f.utype == U_UI:
                if i >= len(b):
                    raise FrameError("UI frame without PID")
                f.pid, f.info = b[i], b[i + 1:]
            else:
                f.info = b[i:]
        return f

    # -- presentation --

    def name(self):
        if self.kind == "I":
            return f"I{self.ns}{self.nr}"
        if self.kind == "S":
            return f"{S_NAMES[self.stype]}{self.nr}"
        return U_NAMES.get(self.utype, f"U{self.utype:02X}")

    def tnc2(self) -> str:
        """Monitor text. Digis that have repeated the frame are starred (last one only)."""
        path = [str(self.dest)]
        last_h = max((n for n, d in enumerate(self.digis) if d.bit7), default=-1)
        for n, d in enumerate(self.digis):
            path.append(str(d) + ("*" if n == last_h else ""))
        head = f"{self.src}>{','.join(path)}"
        if self.kind == "U" and self.utype == U_UI:
            return f"{head}:{self.info.decode('latin-1')}"
        pf = "P" if self.command and self.pf else ("F" if self.pf else "")
        tail = f" {self.info[:40].decode('latin-1')!r}" if self.kind == "I" else ""
        return f"{head} <{self.name()}{pf} {'C' if self.command else 'R'}>{tail}"
