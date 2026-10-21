"""Radio model shared by every backend (CI-V, Hamlib, simulator).

State is a flat dict of raw radio values (levels stay in the radio's own
0-255 / meter units); the browser does the presentation maths.
"""

import logging

log = logging.getLogger("radio")

# Icom CI-V mode codes (Xiegu uses the same ones).
MODES = {0x00: "LSB", 0x01: "USB", 0x02: "AM", 0x03: "CW", 0x04: "RTTY",
         0x05: "FM", 0x07: "CW-R", 0x08: "RTTY-R"}
MODE_CODES = {v: k for k, v in MODES.items()}

DEFAULT_STATE = {
    "connected": False,
    "freq": 14_074_000,
    "freq_b": 7_074_000,
    "mode": "USB",
    "filter": 1,
    "data": False,
    "vfo": "A",
    "split": False,
    "tx": False,
    "smeter": 0,      # 0..241   (0 = S0, 120 = S9, 241 = S9+60)
    "po": 0,          # 0..213   (213 = 100 %)
    "swr": 0,         # 0..120   (0 = 1.0, 48 = 1.5, 80 = 2.0, 120 = 3.0)
    "alc": 0,         # 0..120
    "af": 80,         # 0..255
    "rf": 255,
    "sql": 0,
    "power": 128,
    "preamp": 0,      # 0 off, 1 P.AMP1, 2 P.AMP2
    "att": False,
    "nb": False,
    "nr": False,
    "agc": 2,         # 1 fast, 2 mid, 3 slow
    "tuner": 0,       # 0 off, 1 on, 2 tuning
    "scope_mode": "center",
    "scope_span": 25_000,   # half-span in Hz (center mode)
}

# What a client may set, and how to validate it.
SPANS = [2_500, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000]


def _int_range(lo, hi):
    def check(v):
        v = int(v)
        if not lo <= v <= hi:
            raise ValueError(f"out of range {lo}..{hi}")
        return v
    return check


def _flag(v):
    """Strict on/off: bool("0") is True, so strings are mapped, not cast."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("0", "1", "true", "false"):
        return v.strip().lower() in ("1", "true")
    raise ValueError(f"must be on/off, not {v!r}")


def _choice(options):
    def check(v):
        if v not in options:
            raise ValueError(f"must be one of {options}")
        return v
    return check


def settable(caps):
    """Validators for what a client may set on a radio with these capabilities."""
    feats, levels = set(caps["features"]), set(caps["levels"])
    v = {
        "freq": _int_range(*caps["freq"]),
        "mode": _choice(caps["modes"]),
        "tx": _flag,
        "preamp": _int_range(0, len(caps["preamp"]) - 1),
        "att": _flag,
    }
    if caps["filters"]:
        v["filter"] = _int_range(1, caps["filters"])
    if caps["data_modes"]:
        v["data"] = _flag
    if caps["agc"]:
        v["agc"] = _choice([code for code, _ in caps["agc"]])
    for k in ("af", "rf", "sql", "power"):
        if k in levels:
            v[k] = _int_range(0, 255)
    for k in ("nb", "nr", "split"):
        if k in feats:
            v[k] = _flag
    if "tuner" in feats:
        v["tuner"] = _int_range(0, 2)
    if "vfo_b" in feats:
        v["freq_b"] = _int_range(*caps["freq"])
        v["vfo"] = _choice(["A", "B"])
    if caps["scope"]:
        v["scope_mode"] = _choice(["center", "fixed"])
        v["scope_span"] = _choice(SPANS)
    return v


class Radio:
    """Base class: holds state and fans changes out to listeners."""

    def __init__(self, caps):
        self.caps = caps
        self.name = caps["name"]
        self._settable = settable(caps)
        self.state = dict(DEFAULT_STATE)
        for k in caps.get("unreadable", []):
            self.state[k] = None        # unknown until set from here
        lo, hi = caps["freq"]
        if not lo <= self.state["freq"] <= hi:   # e.g. a VHF/UHF-only rig
            self.state["freq"] = self.state["freq_b"] = lo + 174_000
        self._state_cbs = []
        self._scope_cbs = []

    def on_state(self, cb):
        self._state_cbs.append(cb)

    def on_scope(self, cb):
        self._scope_cbs.append(cb)

    def _update(self, **kv):
        diff = {k: v for k, v in kv.items() if self.state.get(k) != v}
        if not diff:
            return
        self.state.update(diff)
        for cb in self._state_cbs:
            cb(diff)

    def _emit_scope(self, start_hz, end_hz, data: bytes):
        for cb in self._scope_cbs:
            cb(start_hz, end_hz, data)

    def validate(self, key, value):
        """The value a set would apply, or ValueError. Safety checks must look
        at this, not at what the client sent ("2" is not 2)."""
        if key not in self._settable:
            raise ValueError(f"{self.name} has no setting {key!r}")
        return self._settable[key](value)

    async def set(self, key, value):
        await self._apply(key, self.validate(key, value))

    async def swap_vfo(self):
        raise NotImplementedError

    async def power(self, on: bool):
        raise NotImplementedError

    async def _apply(self, key, value):
        raise NotImplementedError

    async def start(self):
        raise NotImplementedError

    async def stop(self):
        raise NotImplementedError
