"""Where and how long Winlink may listen: the host file's winlink.listen block.

    "winlink": {"listen": {"ax25+agwpe": {"mhz": 145.090, "max_hours": 4}}}

A listen window answers stations that call this one (Pat with --listen),
unattended for up to max_hours. That is automatic control of a data
emission, which 47 CFR 97.221(b) allows on 6 m and shorter; so only VHF/UHF
packet through the hub (ax25+agwpe) may listen, and only inside the packet
data sub-bands of 97.305(c), never on the APRS channel. HF listening is not
offered (plan winlink-listen, phase C).

One parser for every place the plan comes from: render.py checks the host
file with it (and renders --winlink-listen METHOD=MHZ --winlink-listen-max-h
H for a Mac's server.py), station.py reads station.json's copy of the block,
and winlink.PatEngine reads the flags. Stdlib only (render imports it).
"""

from dataclasses import dataclass

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
PACKET_BANDS = [("2 m", 144.1e6, 148e6), ("1.25 m", 222e6, 225e6), ("70 cm", 420e6, 450e6)]
# The national APRS channel, never a Winlink packet frequency (+-10 kHz).
APRS_HZ, APRS_GUARD_HZ = 144.390e6, 10e3

LISTEN_METHODS = ("ax25+agwpe",)     # VHF/UHF packet through the hub; nothing else listens
MIN_HOURS, MAX_HOURS = 0.25, 12.0    # one window (plan D2); the hub caps a window at 12 h too
DEFAULT_HOURS = 4.0
KEYS = {"mhz", "max_hours"}


def packet_band_of(hz):
    """The packet data sub-band (PACKET_BANDS) name hz is in, or None."""
    return next((n for n, lo, hi in PACKET_BANDS if lo <= hz <= hi), None)


class ListenError(ValueError):
    """A listen plan that can't be used; the message names the field."""

    def __init__(self, field, why):
        self.field = field
        super().__init__(f"{field}: {why}")


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


@dataclass(frozen=True)
class ListenPlan:
    method: str
    hz: int
    max_hours: float = DEFAULT_HOURS

    @property
    def mhz(self):
        return self.hz / 1e6

    def mhz_text(self):
        return f"{self.hz / 1e6:.4f}".rstrip("0").rstrip(".")

    def flags(self):
        """server.py's flags for this plan."""
        return ["--winlink-listen", f"{self.method}={self.mhz_text()}",
                "--winlink-listen-max-h", f"{self.max_hours:g}"]

    def describe(self):
        return {"method": self.method, "hz": self.hz, "mhz": self.mhz, "max_hours": self.max_hours}

    @classmethod
    def make(cls, method, mhz, max_hours=DEFAULT_HOURS, where="winlink.listen"):
        f = f"{where}.{method}"
        if method not in LISTEN_METHODS:
            raise ListenError(f, f"only {', '.join(LISTEN_METHODS)} may listen (VHF/UHF packet through the "
                                 "hub, 47 CFR 97.221(b)); HF listening is not offered")
        if not _number(mhz):
            raise ListenError(f"{f}.mhz", f"{mhz!r}: the listen frequency in MHz, e.g. 145.090")
        hz = int(round(mhz * 1e6))
        if abs(hz - APRS_HZ) <= APRS_GUARD_HZ:
            raise ListenError(f"{f}.mhz", f"{mhz:g} MHz is the APRS channel (144.390); never a listen frequency")
        if not packet_band_of(hz):
            raise ListenError(f"{f}.mhz", f"{mhz:g} MHz is outside the VHF/UHF packet data sub-bands "
                                          "(2 m 144.1-148, 1.25 m 222-225, 70 cm 420-450 MHz; 47 CFR 97.305(c))")
        if not _number(max_hours) or not MIN_HOURS <= max_hours <= MAX_HOURS:
            raise ListenError(f"{f}.max_hours", f"{max_hours!r}: hours, {MIN_HOURS:g} to {MAX_HOURS:g}")
        return cls(method, hz, float(max_hours))

    @classmethod
    def from_block(cls, block, where="winlink.listen"):
        """The host file's / station.json's winlink.listen block."""
        if not isinstance(block, dict) or len(block) != 1:
            raise ListenError(where, 'one method, e.g. {"ax25+agwpe": {"mhz": 145.090, "max_hours": 4}}')
        (method, opts), = block.items()
        if not isinstance(opts, dict):
            raise ListenError(f"{where}.{method}", 'an object: {"mhz": 145.090, "max_hours": 4}')
        extra = sorted(set(opts) - KEYS)
        if extra:
            raise ListenError(f"{where}.{method}.{extra[0]}", f"not a listen setting (use {', '.join(sorted(KEYS))})")
        return cls.make(method, opts.get("mhz"), opts.get("max_hours", DEFAULT_HOURS), where)

    @classmethod
    def from_flags(cls, spec, max_hours=None):
        """server.py's --winlink-listen METHOD=MHZ and --winlink-listen-max-h H."""
        method, sep, mhz = str(spec).partition("=")
        try:
            mhz = float(mhz) if sep else None
        except ValueError:
            mhz = mhz
        return cls.make(method.strip(), mhz, DEFAULT_HOURS if max_hours is None else max_hours,
                        where="--winlink-listen")

    @classmethod
    def from_args(cls, args):
        """The engine's plan from its args (None: listening is not set up here)."""
        spec = getattr(args, "winlink_listen", None)
        if not spec:
            return None
        if isinstance(spec, ListenPlan):
            return spec
        return cls.from_flags(spec, getattr(args, "winlink_listen_max_h", None))
