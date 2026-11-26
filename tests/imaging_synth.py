"""Test signals for the Imaging decoders: a known picture encoded as SSTV
or radiofax audio at 16 kHz, as a transmitter would send it, with optional
noise and a transmitter clock that runs `ppm` slow or fast (the slant a
decoder must correct). Not a test itself; imaging_*_test.py import it.

The encoder is written from the mode specifications independently of the
decoder's tables (it shares only the VIS bit order), so a timing mistake on
one side shows up as a bad picture instead of cancelling out.
"""
import numpy as np

RATE = 16_000


def test_card(h, w):
    """Three smooth gradients, every channel distinct: red runs left to
    right, green top to bottom, blue along the other diagonal. Smooth on
    purpose: hard edges blur in any analog channel, and the colour-difference
    modes amplify that in blue (x1.78), so a test card of blocks measures the
    channel's bandwidth, not the decoder's timing (2026-11-26)."""
    y, x = np.mgrid[0:h, 0:w]
    r = (x * 255 // max(1, w - 1))
    g = (y * 255 // max(1, h - 1))
    b = 40 + ((w - 1 - x) / max(1, w - 1) + y / max(1, h - 1)) * 87
    return np.stack([r, g, b], axis=-1).astype(np.uint8)


class Tx:
    """Builds a frequency track (Hz per sample) from (freq, ms) pieces, with
    the transmitter's clock error applied, then makes phase-continuous audio."""

    def __init__(self, ppm=0.0):
        self.k = 1 + ppm * 1e-6
        self.t = 0.0                    # ms, transmitter time
        self.parts = []                 # (start sample, end sample, freq or array)

    def tone(self, f, ms):
        a, self.t = self.t, self.t + ms
        self.parts.append((a, self.t, float(f)))

    def scan(self, values, ms):
        """One channel of one line: pixel values 0..255 across `ms`."""
        f = 1500.0 + np.asarray(values, dtype=np.float64) * (800.0 / 255.0)
        a, self.t = self.t, self.t + ms
        self.parts.append((a, self.t, f))

    def audio(self, lead_ms=400, tail_ms=400, noise=0.0, seed=1):
        total = int((lead_ms + self.t * self.k + tail_ms) * RATE / 1000) + 1
        freq = np.zeros(total)
        on = np.zeros(total, dtype=bool)
        to_s = lambda ms: (lead_ms + ms * self.k) * RATE / 1000      # noqa: E731
        for a, b, f in self.parts:
            i, j = int(round(to_s(a))), int(round(to_s(b)))
            if j <= i:
                continue
            if isinstance(f, float):
                freq[i:j] = f
            else:
                idx = np.minimum((np.arange(j - i) * len(f) / (j - i)).astype(int), len(f) - 1)
                freq[i:j] = f[idx]
            on[i:j] = True
        phase = np.cumsum(2 * np.pi * freq / RATE)
        x = np.where(on, np.sin(phase), 0.0) * 0.5
        if noise:
            x = x + np.random.default_rng(seed).normal(0, noise, total)
        return (np.clip(x, -1, 1) * 32000).astype(np.int16)


def vis(tx, code, parity_ok=True):
    tx.tone(1900, 300)
    tx.tone(1200, 10)
    tx.tone(1900, 300)
    tx.tone(1200, 30)
    bits = [(code >> i) & 1 for i in range(7)]
    par = sum(bits) % 2
    for bit in bits + [par if parity_ok else 1 - par]:
        tx.tone(1100 if bit else 1300, 30)
    tx.tone(1200, 30)


def _ycc(img):
    rgb = img.astype(np.float64)
    y = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    cr = np.clip(128 + (rgb[..., 0] - y) / 1.40, 0, 255)
    cb = np.clip(128 + (rgb[..., 2] - y) / 1.78, 0, 255)
    return y, cr, cb


SPEC = {
    # name: (vis, width, lines, family, colour ms)
    "martin1": (44, 320, 256, "martin", 146.432), "martin2": (40, 320, 256, "martin", 73.216),
    "scottie1": (60, 320, 256, "scottie", 138.240), "scottie2": (56, 320, 256, "scottie", 88.064),
    "scottiedx": (76, 320, 256, "scottie", 345.600),
    "robot36": (8, 320, 240, "robot36", None), "robot72": (12, 320, 240, "robot72", None),
    "pd90": (99, 320, 256, "pd", 170.240), "pd120": (95, 640, 496, "pd", 121.600),
    "pd180": (96, 640, 496, "pd", 183.040),
}


def sstv(mode, img=None, ppm=0.0, noise=0.0, lines=None, parity_ok=True, seed=1, sync_hz=1200.0):
    """(audio int16, the picture sent). `lines`: send only that many rows (a
    transmission cut short). `sync_hz`: Martin's line sync tone; off the air
    (2026-11-26) a station's folded to ~1450 Hz, not 1200, while its VIS
    header read clean."""
    code, w, h, fam, c = SPEC[mode]
    img = test_card(h, w) if img is None else img
    tx = Tx(ppm)
    vis(tx, code, parity_ok)
    rows = h if lines is None else lines
    if fam == "martin":
        for r in range(rows):
            tx.tone(sync_hz, 4.862)
            tx.tone(1500, 0.572)
            for ch in (1, 2, 0):                           # G, B, R
                tx.scan(img[r, :, ch], c)
                tx.tone(1500, 0.572)
    elif fam == "scottie":
        tx.tone(1200, 9.0)
        for r in range(rows):
            tx.tone(1500, 1.5)
            tx.scan(img[r, :, 1], c)
            tx.tone(1500, 1.5)
            tx.scan(img[r, :, 2], c)
            tx.tone(1200, 9.0)
            tx.tone(1500, 1.5)
            tx.scan(img[r, :, 0], c)
    elif fam == "robot72":
        y, cr, cb = _ycc(img)
        for r in range(rows):
            tx.tone(1200, 9.0)
            tx.tone(1500, 3.0)
            tx.scan(y[r], 138.0)
            tx.tone(1500, 4.5)
            tx.tone(1900, 1.5)
            tx.scan(cr[r], 69.0)
            tx.tone(2300, 4.5)
            tx.tone(1900, 1.5)
            tx.scan(cb[r], 69.0)
    elif fam == "robot36":
        y, cr, cb = _ycc(img)
        for r in range(rows):
            even = r % 2 == 0
            pair = slice(r, r + 2) if even else slice(r - 1, r + 1)
            tx.tone(1200, 9.0)
            tx.tone(1500, 3.0)
            tx.scan(y[r], 88.0)
            tx.tone(1500 if even else 2300, 4.5)
            tx.tone(1900, 1.5)
            tx.scan((cr if even else cb)[pair].mean(axis=0), 44.0)
    elif fam == "pd":
        y, cr, cb = _ycc(img)
        for p in range(rows // 2):
            r = 2 * p
            tx.tone(1200, 20.0)
            tx.tone(1500, 2.08)
            tx.scan(y[r], c)
            tx.scan(cr[r:r + 2].mean(axis=0), c)
            tx.scan(cb[r:r + 2].mean(axis=0), c)
            tx.scan(y[r + 1], c)
    return tx.audio(noise=noise, seed=seed), img


def fax(img, lpm=120, ioc=576, ppm=0.0, noise=0.0, start=True, phasing_s=30, stop=True, seed=1,
        pulse_level=255):
    """Radiofax audio of a grey picture (HxW, any W: it is resampled to the
    line): start tone (5 s), phasing (a pulse 5 % of the line, on black),
    the picture, the stop tone (5 s at 450 Hz), as WMO/NOAA HF fax sends it.
    `pulse_level`: the phasing pulse's level, 0..255; NMC's measured ~70 off
    the air (2026-11-26), not white."""
    line_ms = 60_000.0 / lpm
    tx = Tx(ppm)
    if start:
        half = 1000.0 / ({576: 300, 288: 675}[ioc]) / 2
        for _ in range(int(5000 / (2 * half))):
            tx.tone(2300, half)
            tx.tone(1500, half)
    for _ in range(int(phasing_s * 1000 / line_ms)):
        tx.tone(1500 + pulse_level * 800 / 255, line_ms * 0.05)
        tx.tone(1500, line_ms * 0.95)
    for row in img:
        tx.scan(row, line_ms)
    if stop:
        half = 1000.0 / 450 / 2
        for _ in range(int(5000 / (2 * half))):
            tx.tone(2300, half)
            tx.tone(1500, half)
    return tx.audio(noise=noise, seed=seed)
