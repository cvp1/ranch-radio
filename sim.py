"""A simulated radio (any rig profile) for developing and demoing without hardware.

Generates a believable band: CW, SSB and data signals seeded from frequency
(so they stay put as you tune), fading over time, plus noise.
"""

import asyncio
import math
import random
import time

from radio import Radio

BINS = 475
BUCKET = 3_000

# FT8 / FT4 / digital watering holes get a busy cluster of data signals.
DIGITAL = [1_840_000, 3_573_000, 7_074_000, 10_136_000, 14_074_000,
           18_100_000, 21_074_000, 24_915_000, 28_074_000, 50_313_000]

PASSBAND = {"LSB": (-2700, -300), "USB": (300, 2700), "CW": (-250, 250),
            "CW-R": (-250, 250), "RTTY": (-250, 250), "RTTY-R": (-250, 250),
            "AM": (-4500, 4500), "FM": (-7500, 7500)}


def _signals_in(lo, hi):
    out = []
    for b in range(int(lo // BUCKET) - 1, int(hi // BUCKET) + 2):
        rng = random.Random(b * 7919 + 13)
        if rng.random() < 0.33:
            kind = rng.choice(["cw", "cw", "ssb", "ssb", "data"])
            out.append({
                "f": b * BUCKET + rng.randint(0, BUCKET - 1),
                "kind": kind,
                "width": {"cw": 80, "ssb": 2400, "data": 500}[kind],
                "level": rng.uniform(25, 105),
                "rate": rng.uniform(0.05, 0.4),
                "phase": rng.uniform(0, 6.28),
                "seed": rng.randint(0, 1 << 30),
            })
    for d in DIGITAL:
        if lo - 4000 < d < hi + 4000:
            rng = random.Random(d)
            for _ in range(14):
                out.append({"f": d + rng.randint(100, 3000), "kind": "data",
                            "width": 50, "level": rng.uniform(25, 75),
                            "rate": rng.uniform(0.1, 0.3),
                            "phase": rng.uniform(0, 6.28), "seed": rng.randint(0, 1 << 30)})
    return out


def _strength(sig, t):
    fade = 0.65 + 0.35 * math.sin(t * sig["rate"] + sig["phase"])
    if sig["kind"] == "cw":
        # Pseudo-random keying pattern, ~ 20 wpm.
        on = random.Random(sig["seed"] + int(t * 9)).random() < 0.6
        return sig["level"] * fade if on else 0.0
    if sig["kind"] == "ssb":
        syl = random.Random(sig["seed"] + int(t * 6)).random()
        return sig["level"] * fade * (0.3 + 0.7 * syl) if syl > 0.15 else 0.0
    if sig["kind"] == "data":
        # FT8-style 15 s cycles: transmit for ~13 s of every other period.
        period = int((t + sig["seed"] % 15) // 15)
        return sig["level"] * fade if (period + sig["seed"]) % 2 == 0 and (t % 15) < 13 else 0.0
    return 0.0


class SimRadio(Radio):
    def __init__(self, caps):
        super().__init__(caps)
        self.name = f"{caps['name']} (simulator)"
        self._task = None
        self._tune_until = 0.0
        self.audio_hint = {"noise": 0.1, "tone": 0.0, "voice": 0.0}

    async def start(self):
        self._update(connected=True)
        self._task = asyncio.create_task(self._run(), name="sim")

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def _apply(self, key, value):
        if key == "tuner" and value == 2:
            self._tune_until = time.monotonic() + 2.5
        self._update(**{key: value})

    async def swap_vfo(self):
        self._update(freq=self.state["freq_b"], freq_b=self.state["freq"])

    async def power(self, on: bool):
        self._update(connected=on)

    def _edges(self):
        s = self.state
        if s["scope_mode"] == "center":
            return s["freq"] - s["scope_span"], s["freq"] + s["scope_span"]
        # Fixed mode: a band-edge window that contains the VFO.
        width = 2 * s["scope_span"]
        lo = (s["freq"] // width) * width
        return lo, lo + width

    async def _run(self):
        rnd = random.random
        while True:
            await asyncio.sleep(1 / 20)
            s = self.state
            if not s["connected"]:
                continue
            t = time.monotonic()
            lo, hi = self._edges()
            per_bin = (hi - lo) / BINS
            sigs = [(sig, _strength(sig, t)) for sig in _signals_in(lo, hi)]

            gain = (8 if s["preamp"] == 1 else 14 if s["preamp"] == 2 else 0) - (18 if s["att"] else 0)
            floor = 22 + gain * 0.6
            line = [floor + rnd() * 9 + rnd() * 5 for _ in range(BINS)]
            for sig, lvl in sigs:
                if lvl <= 0:
                    continue
                lvl += gain
                half = max(sig["width"] / 2, per_bin * 0.7)
                i0 = int((sig["f"] - half - lo) / per_bin)
                i1 = int((sig["f"] + half - lo) / per_bin) + 1
                for i in range(max(i0, 0), min(i1 + 1, BINS)):
                    x = (lo + (i + 0.5) * per_bin - sig["f"]) / half
                    shape = max(0.0, 1 - x * x) if sig["kind"] == "ssb" else math.exp(-x * x * 2)
                    jitter = 0.75 + 0.25 * rnd() if sig["kind"] == "ssb" else 1.0
                    line[i] = max(line[i], floor + (lvl - floor) * shape * jitter)

            if s["tx"]:
                # Our own signal dominates the scope while transmitting.
                c = int((s["freq"] - lo) / per_bin)
                for i in range(max(c - 12, 0), min(c + 12, BINS)):
                    line[i] = max(line[i], 140 - abs(i - c) * 3)
            if self.caps["scope"]:
                self._emit_scope(lo, hi, bytes(min(160, max(0, int(v))) for v in line))

            # What lands in the receive passband drives the S-meter and audio.
            plo, phi = PASSBAND.get(s["mode"], (-1500, 1500))
            peak, tone, voice = 0.0, 0.0, 0.0
            for sig, lvl in sigs:
                off = sig["f"] - s["freq"]
                if plo - sig["width"] / 2 <= off <= phi + sig["width"] / 2 and lvl > 0:
                    peak = max(peak, lvl + gain)
                    if sig["kind"] in ("cw", "data"):
                        tone = max(tone, lvl / 110)
                    else:
                        voice = max(voice, lvl / 110)
            rf = s["rf"] / 255
            smeter = max(0.0, (max(peak, floor) - 18) * 1.9) * (0.4 + 0.6 * rf)
            if s["tx"]:
                pwr = (s["power"] or 0) / 255
                self._update(smeter=0, po=int(213 * pwr * (0.85 + 0.15 * rnd())),
                             swr=int(14 + rnd() * 6) if s["tuner"] else int(38 + rnd() * 8),
                             alc=int(20 + rnd() * 40))
            else:
                self._update(smeter=int(min(241, smeter + rnd() * 4)), po=0, swr=0, alc=0)
            self.audio_hint = {"noise": 0.06 + 0.1 * rf, "tone": tone if not s["tx"] else 0,
                               "voice": voice if not s["tx"] else 0}

            if self._tune_until:
                if t >= self._tune_until:
                    self._tune_until = 0.0
                    self._update(tuner=1)
