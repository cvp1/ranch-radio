"""What the SSTV and weather-fax decoders share: the tone discriminator that
turns a radio's receive audio into an instantaneous frequency, a sample
buffer that forgets what it no longer needs, the pixel sampler, and a PNG
writer (stdlib zlib, so the add-on needs nothing but numpy).

Both pictures are frequency-coded on an audio tone: 1500 Hz is black, 2300 Hz
white, and SSTV's sync pulses sit at 1200 Hz (its VIS header also uses 1100,
1300 and 1900). The discriminator mixes the audio down around FC, low-passes
it to the band those tones use, and reads frequency off the phase step from
one sample to the next. Pure: no I/O, no clocks; times are sample indexes.

numpy is this module's one package (the Imaging add-on's manifest need), so
the core server never imports it.
"""
import struct
import zlib

import numpy as np

RATE = 16_000                # the radio server's audio (audio.RATE)
FC = 1700.0                  # mix-down centre: the middle of 1100..2300 Hz
CUTOFF = 900.0               # complex low-pass half-width: 800..2600 Hz pass
TAPS = 97
BLACK, WHITE = 1500.0, 2300.0
F_MIN, F_MAX = 900.0, 2700.0  # outside the pass band it is noise: clamp


def _lowpass(cutoff, fs, n):
    """A Hamming-windowed sinc, unit gain at DC."""
    t = np.arange(n) - (n - 1) / 2
    h = np.sinc(2 * cutoff / fs * t) * np.hamming(n)
    return (h / h.sum()).astype(np.float64)


class Discriminator:
    """Audio (int16 samples, any chunking) -> frequency in Hz, one value per
    input sample. Its state carries across chunks, so the output is the same
    however the stream is cut. The FIR delays everything by (TAPS-1)/2
    samples; both decoders time from this same stream, so it cancels."""

    def __init__(self, fs=RATE, fc=FC, cutoff=CUTOFF, taps=TAPS):
        self.fs, self.fc = fs, fc
        self.h = _lowpass(cutoff, fs, taps)
        self.n = 0                                  # samples taken so far (the LO's phase)
        self.tail = np.zeros(taps - 1, dtype=np.complex128)
        self.last = 1 + 0j

    def feed(self, pcm):
        x = np.asarray(pcm, dtype=np.float64)
        if x.size == 0:
            return np.zeros(0, dtype=np.float32)
        k = self.n + np.arange(x.size)
        self.n += x.size
        z = x * np.exp(-2j * np.pi * self.fc / self.fs * k)
        both = np.concatenate([self.tail, z])
        self.tail = both[-(len(self.h) - 1):]
        y = np.convolve(both, self.h, mode="valid")           # len == x.size
        prev = np.concatenate([[self.last], y[:-1]])
        self.last = y[-1]
        f = self.fc + np.angle(y * np.conj(prev)) * self.fs / (2 * np.pi)
        return np.clip(f, F_MIN, F_MAX).astype(np.float32)


class Buffer:
    """The frequency stream from sample `base` on: append at the end, read any
    span by absolute sample index, drop what is behind you. Bounded by its
    owner, who drops (each decoder keeps at most one picture's worth)."""

    def __init__(self):
        self.base = 0
        self.data = np.zeros(0, dtype=np.float32)

    @property
    def end(self):
        return self.base + self.data.size

    def append(self, f):
        self.data = np.concatenate([self.data, f]) if self.data.size else np.asarray(f, dtype=np.float32)

    def span(self, a, b):
        """Samples [a, b) clipped to what is held; returns (array, first index)."""
        a, b = max(int(a), self.base), min(int(b), self.end)
        if b <= a:
            return np.zeros(0, dtype=np.float32), a
        return self.data[a - self.base:b - self.base], a

    def drop_before(self, i):
        i = min(max(int(i), self.base), self.end)
        self.data = self.data[i - self.base:]
        self.base = i

    def clear_to_end(self):
        self.drop_before(self.end)


def mean(buf, a, b):
    """Mean frequency over samples [a, b) (absolute), or None if none held."""
    x, _ = buf.span(a, b)
    return float(x.mean()) if x.size else None


def pixels(buf, start, dur, n):
    """n pixel values (0..255) across [start, start+dur) samples (floats
    allowed: line timing is fractional). Each pixel is the box mean of the
    frequency over its own width, which is the right filter for a scan line
    and cheap with a cumulative sum."""
    a, b = int(np.floor(start)) - 1, int(np.ceil(start + dur)) + 2
    x, first = buf.span(a, b)
    if x.size < 2:
        return np.zeros(n, dtype=np.uint8)
    # Clamp to the picture's own range first, so a discriminator "click"
    # (one sample kHz away, under deep noise) can't drag a whole pixel to
    # black or white. Measured 2026-11-26 on synthetic Gaussian noise it
    # changes little (the error there is ordinary FM noise, not clicks);
    # it is kept for the impulsive noise real bands have.
    x = np.clip(x, BLACK - 40, WHITE + 40)
    c = np.concatenate([[0.0], np.cumsum(x, dtype=np.float64)])
    edges = start + dur * np.arange(n + 1) / n - first
    edges = np.clip(edges, 0, x.size)
    ce = np.interp(edges, np.arange(c.size), c)
    w = np.diff(edges)
    with np.errstate(invalid="ignore", divide="ignore"):
        f = np.where(w > 1e-9, np.diff(ce) / np.where(w > 1e-9, w, 1), BLACK)
    return level(f)


def level(f):
    return np.clip((np.asarray(f) - BLACK) * (255.0 / (WHITE - BLACK)), 0, 255).astype(np.uint8)


def smooth(x, n):
    """Moving average over n samples, same length (edges averaged short)."""
    if n <= 1 or x.size == 0:
        return x
    c = np.concatenate([[0.0], np.cumsum(x, dtype=np.float64)])
    i = np.arange(x.size)
    lo = np.clip(i - n // 2, 0, x.size)
    hi = np.clip(i - n // 2 + n, 0, x.size)
    return ((c[hi] - c[lo]) / np.maximum(hi - lo, 1)).astype(np.float32)


def goertzel_share(x, freq, fs=RATE):
    """How much of x's variance sits at `freq` (0..1): the fax start and stop
    tones are the picture signal itself switching black/white at 300, 450 or
    675 Hz, so they show as one strong line in the frequency stream."""
    x = np.asarray(x, dtype=np.float64)
    if x.size < 16:
        return 0.0
    x = x - x.mean()
    total = float(np.dot(x, x))
    if total <= 0:
        return 0.0
    k = np.arange(x.size)
    s = np.dot(x, np.exp(-2j * np.pi * freq / fs * k))
    return float(min(1.0, 2 * abs(s) ** 2 / x.size / total))


def ycc_to_rgb(y, cr, cb):
    """SSTV's Y, R-Y, B-Y (each 0..255 as sent) to RGB, the mapping the Robot
    and PD modes are commonly decoded with (slowrx, MMSSTV agree on it)."""
    y, cr, cb = (np.asarray(v, dtype=np.float64) for v in (y, cr, cb))
    r = y + 1.40 * (cr - 128)
    g = y - 0.71 * (cr - 128) - 0.33 * (cb - 128)
    b = y + 1.78 * (cb - 128)
    return np.clip(np.stack([r, g, b], axis=-1), 0, 255).astype(np.uint8)


def png(img):
    """A PNG of an HxW (grey) or HxWx3 (RGB) uint8 array."""
    a = np.ascontiguousarray(img, dtype=np.uint8)
    if a.ndim == 2:
        h, w = a.shape
        ctype, rows = 0, a
    elif a.ndim == 3 and a.shape[2] == 3:
        h, w = a.shape[:2]
        ctype, rows = 2, a.reshape(h, w * 3)
    else:
        raise ValueError(f"not a grey or RGB image: shape {a.shape}")
    raw = np.concatenate([np.zeros((h, 1), dtype=np.uint8), rows], axis=1).tobytes()

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
