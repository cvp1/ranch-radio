"""Where and how long Winlink may listen: the host file's winlink.listen block.

    "winlink": {"listen": {"ax25+agwpe": {"mhz": 145.090, "max_hours": 4}}}

A listen window answers stations that call this one (Pat with --listen),
unattended for up to max_hours. That is automatic control of a data
emission, which 47 CFR 97.221(b) allows on 6 m and shorter; so only VHF/UHF
packet through the hub (ax25+agwpe) may listen, and only inside the packet
data sub-bands of 97.305(c), never on the APRS channel. HF listening is not
offered (plan winlink-listen, phase C).

One parser for every place the plan comes from: render.py checks the host
file with it (station.json keeps the block; flags() is the one-line form
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
        """The one-line form, METHOD=MHZ and H (the flags a radio's server once took)."""
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


# --------------------------------------------------------------- attended HF
#
# winlink.listen_hf: answer HF peer calls while the OPERATOR IS AT THE PAGE.
# That is local control (47 CFR 97.109(b)), not automatic control, so it is
# not limited to the 97.221(b) segments; each channel still says whether it
# sits inside one (the segments are where an unattended window could go
# later). Attended is enforced by the engine, not trusted from the page: the
# window ends when the page that opened it closes, when nobody answers
# "still there?" every presence_minutes, and at max_minutes.
#
#   "winlink": {"listen_hf": {"channels": [{"khz": 7102.5, "modem": "ardop"},
#                                          {"khz": 10145, "modem": "ardop"}],
#                             "max_minutes": 60, "presence_minutes": 15}}
#
# khz is the CENTRE frequency (as the P2P directory publishes it); the radio
# is put on the dial, 1.5 kHz below (Pat v1.0.0 app/freq.go:68-92).

HF_MODEMS = ("ardop", "varahf")
HF_DIAL_OFFSET_KHZ = -1.5
# The RTTY/data sub-bands at General-class privileges (97.305(c), 97.301(d)),
# kHz. The whole signal (centre +- HF_HALF_WIDTH_KHZ) must fit. 60 m is left
# out: data there is channelised and narrower than these modems.
HF_DATA_KHZ = (("160 m", 1800, 2000), ("80 m", 3525, 3600), ("40 m", 7025, 7125),
               ("30 m", 10100, 10150), ("20 m", 14025, 14150), ("17 m", 18068, 18110),
               ("15 m", 21025, 21200), ("12 m", 24890, 24930), ("10 m", 28000, 28300))
HF_HALF_WIDTH_KHZ = 1.5               # the widest these modems send (VARA HF 2750 Hz ~ +-1.4 kHz)
# 47 CFR 97.221(b): the HF segments where automatic control of data is allowed.
AUTO_SEGMENTS_KHZ = ((3585.0, 3600.0), (7100.0, 7105.0), (10140.0, 10150.0), (14095.0, 14099.5),
                     (14100.5, 14112.0), (18105.0, 18110.0), (21090.0, 21100.0), (24925.0, 24930.0),
                     (28120.0, 28189.0))
SEGMENT_HALF_WIDTH_KHZ = 0.25         # a 500 Hz signal, the width that fits these segments
MAX_CHANNELS = 6
HF_MAX_MINUTES = (5, 120)
HF_DEFAULT_MINUTES = 60
HF_PRESENCE_MINUTES = (5, 30)
HF_DEFAULT_PRESENCE = 15
HF_KEYS = {"channels", "max_minutes", "presence_minutes"}
CHANNEL_KEYS = {"khz", "modem"}


def hf_data_band_of(khz):
    """The data sub-band a whole HF signal centred on khz fits in, or None."""
    return next((n for n, lo, hi in HF_DATA_KHZ
                 if lo <= khz - HF_HALF_WIDTH_KHZ and khz + HF_HALF_WIDTH_KHZ <= hi), None)


def in_auto_segment(khz):
    """Does a 500 Hz signal centred on khz sit wholly inside a 97.221(b) segment?"""
    return any(lo <= khz - SEGMENT_HALF_WIDTH_KHZ and khz + SEGMENT_HALF_WIDTH_KHZ <= hi
               for lo, hi in AUTO_SEGMENTS_KHZ)


@dataclass(frozen=True)
class HfChannel:
    khz: float                         # centre
    modem: str

    @property
    def dial_khz(self):
        return round(self.khz + HF_DIAL_OFFSET_KHZ, 3)

    def describe(self):
        return {"khz": self.khz, "dial_khz": self.dial_khz, "modem": self.modem,
                "band": hf_data_band_of(self.khz), "segment": in_auto_segment(self.khz)}


@dataclass(frozen=True)
class HfListenPlan:
    channels: tuple
    max_minutes: float = HF_DEFAULT_MINUTES
    presence_minutes: float = HF_DEFAULT_PRESENCE

    def describe(self):
        return {"channels": [c.describe() for c in self.channels], "max_minutes": self.max_minutes,
                "presence_minutes": self.presence_minutes, "attended": True}

    @classmethod
    def from_block(cls, block, where="winlink.listen_hf"):
        if not isinstance(block, dict):
            raise ListenError(where, 'an object: {"channels": [{"khz": 7102.5, "modem": "ardop"}]}')
        extra = sorted(set(block) - HF_KEYS)
        if extra:
            raise ListenError(f"{where}.{extra[0]}", f"not a setting (use {', '.join(sorted(HF_KEYS))})")
        chans = block.get("channels")
        if not isinstance(chans, list) or not 1 <= len(chans) <= MAX_CHANNELS:
            raise ListenError(f"{where}.channels", f"a list of 1 to {MAX_CHANNELS} channels")
        out = []
        for i, c in enumerate(chans):
            f = f"{where}.channels[{i}]"
            if not isinstance(c, dict):
                raise ListenError(f, 'an object: {"khz": 7102.5, "modem": "ardop"}')
            extra = sorted(set(c) - CHANNEL_KEYS)
            if extra:
                raise ListenError(f"{f}.{extra[0]}", "not a channel setting (use khz, modem)")
            khz, modem = c.get("khz"), c.get("modem")
            if modem not in HF_MODEMS:
                raise ListenError(f"{f}.modem", f"{modem!r}: one of {', '.join(HF_MODEMS)}")
            if not _number(khz) or hf_data_band_of(float(khz)) is None:
                raise ListenError(f"{f}.khz", f"{khz!r}: a centre frequency in kHz whose whole signal "
                                  f"(+-{HF_HALF_WIDTH_KHZ:g} kHz) is inside an HF data sub-band at General "
                                  "privileges (e.g. 7102.5)")
            ch = HfChannel(float(khz), modem)
            if ch in out:
                raise ListenError(f, "listed twice")
            out.append(ch)
        mx = block.get("max_minutes", HF_DEFAULT_MINUTES)
        lo, hi = HF_MAX_MINUTES
        if not _number(mx) or not lo <= mx <= hi:
            raise ListenError(f"{where}.max_minutes", f"{mx!r}: minutes, {lo} to {hi}")
        pr = block.get("presence_minutes", HF_DEFAULT_PRESENCE)
        lo, hi = HF_PRESENCE_MINUTES
        if not _number(pr) or not lo <= pr <= hi or pr > mx:
            raise ListenError(f"{where}.presence_minutes", f"{pr!r}: minutes, {lo} to {hi}, and no more than max_minutes")
        return cls(tuple(out), float(mx), float(pr))

    @classmethod
    def from_args(cls, args):
        plan = getattr(args, "winlink_listen_hf", None)
        return plan if isinstance(plan, HfListenPlan) or plan is None else cls.from_block(plan)
