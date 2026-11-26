"""The Imaging decoders (picture.py, sstv.py, wefax.py) against pictures
encoded independently (tests/imaging_synth.py), with noise and a
transmitter clock off by hundreds of ppm. Offline; numpy.

  SSTV
  - Martin M1, Scottie S1, Robot 36, PD 120: whole pictures, every row,
    close to what was sent; the clock error measured within 15 ppm
  - every other mode: its VIS header names it, and a transmission cut short
    still runs the mode's full length, the rows that came right
  - a weak picture from a sender whose clock runs +1460 ppm (as one did off
    the air, 2026-11-26): few syncs line by line, timed by the whole-picture
    fold, the clock error within 30 ppm, close to what was sent
  - the same picture however the audio is chunked (bit for bit)
  - negative controls: noise alone starts nothing; a VIS with bad parity
    starts nothing; a mode not allowed starts nothing; an unknown mode name
    is refused
  Fax
  - a 120 LPM IOC 576 chart: lined up from phasing, every row (the phasing
    and stop-tone lines left off), the clock error measured
  - IOC 288 (the 675 Hz start tone): 905-pixel lines
  - joined mid-chart: Start begins one, not lined up
  - noise alone starts nothing; max_lines ends a chart "full"; bad options
    refused, named
  PNG
  - grey and RGB: the signature, the size in IHDR, the pixels back out

    python3 tests/imaging_decode_test.py
"""
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import numpy as np  # noqa: E402

import imaging_synth as S  # noqa: E402
import picture as P  # noqa: E402
import sstv  # noqa: E402
import wefax  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


def run(dec, audio, chunk=1600):
    d, evs = P.Discriminator(), []
    for i in range(0, len(audio), chunk):
        evs += dec.feed(d.feed(audio[i:i + chunk]))
    return evs


def done(evs):
    return next((e for e in evs if e["event"] == "done"), None)


def err(a, b):
    h = min(len(a), len(b))
    return float(np.abs(a[:h].astype(int) - b[:h].astype(int)).mean())


def noise(seconds, sigma=0.15, seed=7):
    return (np.random.default_rng(seed).normal(0, sigma, int(seconds * S.RATE)) * 32000).astype(np.int16)


# --- SSTV: whole pictures

for mode, ppm in (("martin1", 300), ("scottie1", 0), ("robot36", -200), ("pd120", 150)):
    audio, sent = S.sstv(mode, ppm=ppm, noise=0.05)
    e = done(run(sstv.SstvDecoder(), audio))
    check(f"{mode} ({ppm:+} ppm): a whole picture", (e and e["status"], e and e["lines"]),
          ("complete", sstv.BY_NAME[mode].lines))
    if e:
        check(f"{mode}: close to what was sent (mean error < 12 of 255)", err(e["image"], sent) < 12, True)
        check(f"{mode}: the clock error measured within 15 ppm", abs(e["clock_ppm"] - ppm) <= 15, True)

# --- SSTV: every other mode named by its header, cut short

for mode in ("martin2", "scottie2", "scottiedx", "robot72", "pd90", "pd180"):
    audio, sent = S.sstv(mode, lines=24, noise=0.05)
    m = sstv.BY_NAME[mode]
    audio = np.concatenate([audio, noise(m.seconds - 24 * m.period_ms / 1000 * (1 if m.model != "PD" else 0.5) + 2, sigma=0.05)])
    evs = run(sstv.SstvDecoder(), audio)
    start = next((x for x in evs if x["event"] == "start"), None)
    check(f"{mode}: the VIS header names it", start and start["mode"], mode)
    e = done(evs)
    check(f"{mode}: cut short, it still runs the mode's full length", (e and e["status"], e and e["lines"]),
          ("complete", m.lines))
    if e:
        check(f"{mode}: the rows that came are right (mean error < 12)", err(e["image"][:22], sent[:22]) < 12, True)

# --- SSTV: weak, from a far-off clock (the 2026-11-26 off-air case)

audio, sent = S.sstv("martin1", ppm=1460, noise=0.2, seed=4, sync_hz=1440)
e = done(run(sstv.SstvDecoder(), audio))
check("weak Martin M1 at +1460 ppm: a whole picture", (e and e["status"], e and e["lines"]), ("complete", 256))
if e:
    check("  syncs found line by line are few (the case the fold is for)", e["syncs"] < 128, True)
    check("  timed by the whole-picture fold", e["timing"], "fold")
    check("  the clock error within 30 ppm", abs(e["clock_ppm"] - 1460) <= 30, True)
    check("  close to what was sent (mean error < 30 of 255 at this noise)", err(e["image"], sent) < 30, True)

# --- SSTV: chunking

audio, _ = S.sstv("martin2", lines=30, noise=0.1)
audio = np.concatenate([audio, noise(3)])
a = sstv.SstvDecoder()
run(a, audio, 1600)
rng = np.random.default_rng(3)
b, d, i = sstv.SstvDecoder(), P.Discriminator(), 0
while i < len(audio):
    n = int(rng.integers(1, 5000))
    b.feed(d.feed(audio[i:i + n]))
    i += n
ia, ib = a.image(), b.image()
check("the same picture however the audio is chunked", ia is not None and ib is not None and np.array_equal(ia, ib)
      or (ia is None and ib is None and a.state == b.state), True)

# --- SSTV: negative controls

check("noise alone starts nothing", run(sstv.SstvDecoder(), noise(20, sigma=0.3)), [])
audio, _ = S.sstv("robot36", lines=4, parity_ok=False)
check("a VIS with bad parity starts nothing", [x["event"] for x in run(sstv.SstvDecoder(), audio)], [])
audio, _ = S.sstv("martin1", lines=4)
check("a mode not allowed starts nothing", [x["event"] for x in run(sstv.SstvDecoder(["pd120"]), audio)], [])
try:
    sstv.SstvDecoder(["pd121"])
    check("an unknown mode name is refused", "accepted", "ValueError")
except ValueError as e:
    check("an unknown mode name is refused, named", "pd121" in str(e), True)


# --- Fax

def chart(h, w):
    y, x = np.mgrid[0:h, 0:w]
    img = ((x // 60 + y // 30) % 2) * 200 + 30
    return np.where((y % 50) < 3, 255, img).astype(np.uint8)


W = wefax.width(576)
sent = chart(160, W)
e = done(run(wefax.FaxDecoder(clock_ppm=250), S.fax(sent, ppm=250, noise=0.05), chunk=3200))
check("fax 120/576 (a +250 ppm clock, clock_ppm 250): a whole chart, lined up",
      (e and e["status"], e and e["lines"], e and e["aligned"]), ("complete", 160, True))
if e:
    check("fax: 1810-pixel lines", e["image"].shape[1], 1810)
    # Lined up = the same small offset on every row (no slant), within 3 px
    # of 1810, and right once that offset is allowed. A raw pixel diff would
    # weigh a 2 px offset against this card's hard edges (measured
    # 2026-11-26: offset 2 px under this noise, error 8.3 at that offset).
    out = e["image"].astype(int)

    def shift(r):
        return min(range(-10, 11), key=lambda s: np.abs(np.roll(out[r], s) - sent[r].astype(int)).mean())
    top, bottom = shift(10), shift(140)       # not on a rule row (y % 50 < 3: every offset fits)
    check("fax: no slant (top and bottom rows offset alike)", abs(top - bottom) <= 1, True)
    check("fax: lined up within 3 px", abs(top) <= 3, True)
    check("fax: close to what was sent at that offset (mean error < 10)",
          float(np.abs(np.roll(out, top, axis=1) - sent.astype(int)).mean()) < 10, True)
    check("fax: reports the clock correction it applied", e["clock_ppm"], 250)

# Phasing sets the line start only, never the line period: off the air a fold
# of faint phasing read -158 ppm where the chart was square (2026-11-26).
e = done(run(wefax.FaxDecoder(), S.fax(sent, noise=0.15, pulse_level=70), chunk=3200))
check("fax: a dim phasing pulse (NMC's ~70/255) under noise still lines a chart up",
      (e and e["status"], e and e["aligned"], e and e["lines"]), ("complete", True, 160))
if e:
    out = e["image"].astype(int)
    tops = [min(range(-10, 11), key=lambda s, r=r: np.abs(np.roll(out[r], s) - sent[r].astype(int)).mean()) for r in (10, 140)]
    check("  and draws it square, at no clock correction (no slant from phasing)", abs(tops[0] - tops[1]) <= 1, True)


def tone_track(seconds, share_target_sigma, f=300, seed=5):
    """A start tone as received off the air: the 300 Hz alternation buried in
    frequency noise, so only ~0.15 of the window's variance is the tone (real
    NMC start tones measured 0.12..0.37; synthetic square waves 0.95)."""
    n = int(seconds * S.RATE)
    t = np.arange(n) / S.RATE
    sq = 1900 + 400 * np.sign(np.sin(2 * np.pi * f * t))
    return (sq + np.random.default_rng(seed).normal(0, share_target_sigma, n)).astype(np.float32)


trk = tone_track(1.0, 840)
check("  (the weak tone track's share is real-like, 0.1..0.25)", 0.1 <= P.goertzel_share(trk[:8000], 300) <= 0.25, True)
dec = wefax.FaxDecoder()
dec.feed(np.full(S.RATE, 1500, dtype=np.float32))
dec.feed(tone_track(4.5, 840))
check("fax: a weak, real-shaped start tone (share ~0.15, 4.5 s) is heard", dec.state in ("starting", "phasing"), True)
dec = wefax.FaxDecoder()
rng = np.random.default_rng(9)
dec.feed((1900 + rng.normal(0, 840, 30 * S.RATE)).astype(np.float32))
check("  (control: the same noise without the tone starts nothing)", dec.state, "idle")

sent288 = chart(60, wefax.width(288))
e = done(run(wefax.FaxDecoder(ioc=288), S.fax(sent288, ioc=288, noise=0.05), chunk=3200))
check("fax IOC 288 (675 Hz start tone): 905-pixel lines, every row",
      (e and e["image"].shape[1], e and e["lines"]), (905, 60))

dec = wefax.FaxDecoder()
audio = S.fax(chart(40, W), start=False, phasing_s=0, stop=False, noise=0.05)
d = P.Discriminator()
evs = dec.feed(d.feed(audio[:1600]))
evs += dec.start_now()
for i in range(1600, len(audio), 3200):
    evs += dec.feed(d.feed(audio[i:i + 3200]))
evs += dec.abort()
e = done(evs)
check("fax joined mid-chart: Start begins one, not lined up, Stop saves it",
      (e and e["status"], e and e["aligned"], e and 38 <= e["lines"] <= 41), ("stopped", False, True))

check("fax: noise alone starts nothing", run(wefax.FaxDecoder(), noise(30, sigma=0.3), chunk=3200), [])
e = done(run(wefax.FaxDecoder(max_lines=50), S.fax(chart(120, W)), chunk=3200))
check("fax: max_lines ends a chart full", (e and e["status"], e and e["lines"]), ("full", 50))
for kw, word in (({"lpm": 100}, "lpm"), ({"ioc": 500}, "ioc"), ({"clock_ppm": 5000}, "clock_ppm"),
                 ({"max_lines": 5}, "max_lines")):
    try:
        wefax.FaxDecoder(**kw)
        check(f"fax refuses {kw}", "accepted", "ValueError")
    except ValueError as x:
        check(f"fax refuses {kw}, naming {word}", word in str(x), True)

# --- PNG


def unpng(b):
    assert b[:8] == b"\x89PNG\r\n\x1a\n"
    w, h, depth, ctype = struct.unpack(">IIBB", b[16:26])
    i, idat = 8, b""
    while i < len(b):
        n = struct.unpack(">I", b[i:i + 4])[0]
        kind = b[i + 4:i + 8]
        if kind == b"IDAT":
            idat += b[i + 8:i + 8 + n]
        i += 12 + n
    ch = 3 if ctype == 2 else 1
    raw = np.frombuffer(zlib.decompress(idat), dtype=np.uint8).reshape(h, 1 + w * ch)
    return w, h, ctype, raw[:, 1:].reshape(h, w, ch).squeeze()


g = (np.arange(12 * 7) % 256).astype(np.uint8).reshape(7, 12)
w, h, ctype, back = unpng(P.png(g))
check("PNG grey: size, type, pixels", (w, h, ctype, np.array_equal(back, g)), (12, 7, 0, True))
rgb = np.random.default_rng(1).integers(0, 256, (5, 9, 3)).astype(np.uint8)
w, h, ctype, back = unpng(P.png(rgb))
check("PNG RGB: size, type, pixels", (w, h, ctype, np.array_equal(back, rgb)), (9, 5, 2, True))

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
