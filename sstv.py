"""SSTV receive: a radio's audio (as frequency, from picture.Discriminator)
to colour pictures, for the Imaging add-on. Pure: feed it frequency samples,
it returns events and holds the picture; no I/O, no clocks.

How it works:

- **Hunt** for a VIS header: a 1900 Hz leader, then a 30 ms 1200 Hz start
  bit, seven data bits LSB first (1100 Hz = 1, 1300 Hz = 0), an even parity
  bit and a 1200 Hz stop bit. It is checked on 10 ms bins, then timed to the
  sample at the leader's fall to 1200 Hz.
- **Receive** the mode the VIS names, one line (or PD's line pair) at a
  time. Each line's sync pulse is looked for where the timing so far says it
  should be, and the line period is refit to the pulses found (least
  squares, outliers dropped): a sound card whose clock is a few hundred ppm
  off would otherwise slant the picture. Lines are drawn as they arrive;
  when the picture ends it is redrawn once with the final fit.
- **Ends** after the mode's full length (or the operator's Stop): it does
  not give up on missing sync pulses. Off the air (2026-11-26, 14.230, a
  Martin M1 whose sender's clock ran +1460 ppm against ours) the sync dip
  averaged 1450 Hz against a 1740 Hz picture: line by line only 3 syncs were
  found and the old rule (10 misses: "lost") dropped a picture that kept
  coming for all 114 s. None of three tests for "still coming" measured on
  that audio (a fold of the missed lines, a normalized dip, line rhythm)
  separated a picture from the band's other signals at 25 s, so, as slowrx
  and MMSSTV do, the whole length is received and timed at the end.
- **Timed at the end** from every line folded together: the clock error
  (+-CLOCK_MAX) and phase where the folded sync dip is deepest. A whole
  picture adds up where single lines don't (both halves of that Martin M1
  agreed within 1.5 ms and 24 ppm). With syncs found on most lines, their
  least-squares fit is used instead (it is exact on a clean signal).

Modes (timings from the published SSTV mode specifications; VIS codes are
the standard ones): Martin M1/M2, Scottie S1/S2/DX, Robot 36/72, PD 90,
PD 120 (the ISS's usual mode), PD 180.
"""
from dataclasses import dataclass, field

import numpy as np

import picture as P

MS = P.RATE / 1000.0
BIN = int(10 * MS)                 # VIS hunt: 10 ms bins
LEADER_BINS = 20                   # 200 ms of the 300 ms leader must read 1900 Hz
SYNC_HZ = 1350                     # below this is sync (1200), above is picture (1500..2300)
CLOCK_MAX = 0.003                  # +-3000 ppm: senders' PC sound cards run far off (+1460 measured)
FOLD_MIN_DEPTH = 150.0             # Hz, folded sync dip under the fold's median: whole real picture 273..294; noise, see tests
PROGRESS_EVERY = 8                 # lines between progress events
HUNT_KEEP_S = 2.0


@dataclass(frozen=True)
class Mode:
    name: str
    label: str
    vis: int
    width: int
    lines: int                     # rows in the picture
    period_ms: float               # one line (PD: one line pair)
    sync_ms: float                 # the sync pulse's start within a line
    sync_dur: float
    model: str                     # GBR, YC36 (Robot 36), YCC (Robot 72), PD
    segs: dict = field(default_factory=dict)   # channel -> (start ms in line, duration ms)
    first_ms: float = 0.0          # line 0's start after the VIS stop bit

    @property
    def periods(self):
        return self.lines // 2 if self.model == "PD" else self.lines

    @property
    def seconds(self):
        return self.periods * self.period_ms / 1000


def _martin(name, label, vis, c):
    g = 4.862 + 0.572
    return Mode(name, label, vis, 320, 256, 4 * 0.572 + 4.862 + 3 * c, 0.0, 4.862, "GBR",
                {"g": (g, c), "b": (g + c + 0.572, c), "r": (g + 2 * (c + 0.572), c)})


def _scottie(name, label, vis, c):
    sync = 1.5 + c + 1.5 + c
    return Mode(name, label, vis, 320, 256, sync + 9.0 + 1.5 + c, sync, 9.0, "GBR",
                {"g": (1.5, c), "b": (1.5 + c + 1.5, c), "r": (sync + 10.5, c)}, first_ms=9.0)


def _pd(name, label, vis, w, lines, c):
    y0 = 20.0 + 2.08
    return Mode(name, label, vis, w, lines, y0 + 4 * c, 0.0, 20.0, "PD",
                {"y0": (y0, c), "cr": (y0 + c, c), "cb": (y0 + 2 * c, c), "y1": (y0 + 3 * c, c)})


MODES = {m.vis: m for m in (
    _martin("martin1", "Martin M1", 44, 146.432),
    _martin("martin2", "Martin M2", 40, 73.216),
    _scottie("scottie1", "Scottie S1", 60, 138.240),
    _scottie("scottie2", "Scottie S2", 56, 88.064),
    _scottie("scottiedx", "Scottie DX", 76, 345.600),
    Mode("robot36", "Robot 36", 8, 320, 240, 150.0, 0.0, 9.0, "YC36",
         {"y": (12.0, 88.0), "sep": (100.0, 4.5), "c": (106.0, 44.0)}),
    Mode("robot72", "Robot 72", 12, 320, 240, 300.0, 0.0, 9.0, "YCC",
         {"y": (12.0, 138.0), "cr": (156.0, 69.0), "cb": (231.0, 69.0)}),
    _pd("pd90", "PD 90", 99, 320, 256, 170.240),
    _pd("pd120", "PD 120", 95, 640, 496, 121.600),
    _pd("pd180", "PD 180", 96, 640, 496, 183.040),
)}
BY_NAME = {m.name: m for m in MODES.values()}


def vis_bits(code):
    """The 8 bits a VIS sends for `code`: 7 data LSB first, then even parity."""
    bits = [(code >> i) & 1 for i in range(7)]
    return bits + [sum(bits) % 2]


class SstvDecoder:
    """feed(freq) -> events: {"event": "start"|"progress"|"done", ...}.
    `modes`: the mode names to accept (default all)."""

    def __init__(self, modes=None):
        self.allowed = set(modes) if modes else set(BY_NAME)
        unknown = self.allowed - set(BY_NAME)
        if unknown:
            raise ValueError(f"unknown SSTV mode(s): {', '.join(sorted(unknown))}")
        self.buf = P.Buffer()
        self._hunt_reset()

    # --- state

    def _hunt_reset(self):
        self.mode = None
        self.bins = []                       # (start sample, mean Hz)
        self.next_bin = self.buf.end
        self.img = None

    @property
    def state(self):
        return "receiving" if self.mode else "hunting"

    def status(self):
        if not self.mode:
            return {"state": "hunting"}
        return {"state": "receiving", "mode": self.mode.name, "label": self.mode.label,
                "lines": self.rows_done, "total": self.mode.lines}

    def image(self):
        """The picture so far (rows not yet received are black), HxWx3."""
        return None if self.img is None else self.img.copy()

    def abort(self):
        """The operator's Stop: the picture so far, as a "done" event (or None)."""
        if not self.mode:
            return None
        return self._finish("stopped")

    # --- input

    def feed(self, freq):
        self.buf.append(np.asarray(freq, dtype=np.float32))
        out = []
        guard = 0
        while guard < 10_000:                # bounded: each pass consumes a bin or a line
            guard += 1
            ev = self._step_image() if self.mode else self._step_hunt()
            if ev is False:
                break
            if ev:
                out.extend(ev)
        return out

    # --- VIS hunt

    def _step_hunt(self):
        if self.buf.end < self.next_bin + BIN:
            self.buf.drop_before(self.buf.end - int(HUNT_KEEP_S * P.RATE))
            return False
        m = P.mean(self.buf, self.next_bin, self.next_bin + BIN)
        self.bins.append((self.next_bin, m))
        self.next_bin += BIN
        if len(self.bins) > 80:
            del self.bins[:-80]
        return self._try_vis()

    def _try_vis(self):
        b = self.bins
        c = len(b) - 31                       # candidate: the first bin wholly in the start bit
        if c < LEADER_BINS + 1:
            return []
        if abs(b[c][1] - 1200) > 110:
            return []
        leader = [m for _, m in b[c - LEADER_BINS - 1:c - 1]]
        if sum(abs(m - 1900) < 90 for m in leader) < LEADER_BINS - 4:
            return []
        edge = self._edge(b[c][0])
        if edge is None:
            return []
        code = self._read_vis(edge)
        if code is None or code not in MODES or MODES[code].name not in self.allowed:
            return []
        return self._begin(MODES[code], edge + 300 * MS)

    def _edge(self, near):
        """The sample where the leader falls to the start bit's 1200 Hz."""
        a, b = int(near - 2 * BIN), int(near + BIN)
        x, first = self.buf.span(a, b)
        if x.size < 8:
            return None
        s = P.smooth(x, int(MS))
        down = np.nonzero((s[:-1] >= 1550) & (s[1:] < 1550))[0]
        if down.size == 0:
            return None
        i = down[np.argmin(np.abs(down + first - near))]
        return float(first + i + 1)

    def _read_vis(self, e):
        def at(lo_ms, hi_ms):
            return P.mean(self.buf, e + lo_ms * MS, e + hi_ms * MS)
        start, stop = at(5, 25), at(275, 295)
        if start is None or stop is None or abs(start - 1200) > 100 or abs(stop - 1200) > 100:
            return None
        bits = []
        for k in range(8):
            m = at(30 * (k + 1) + 5, 30 * (k + 2) - 5)
            if m is None or min(abs(m - 1100), abs(m - 1300)) > 110:
                return None
            bits.append(1 if m < 1200 else 0)
        if sum(bits[:7]) % 2 != bits[7]:
            return None
        return sum(bit << i for i, bit in enumerate(bits[:7]))

    # --- one picture

    def _begin(self, mode, t0):
        self.mode = mode
        self.t0 = t0
        self.P = mode.period_ms * MS
        self.a0 = t0 + (mode.first_ms + mode.sync_ms) * MS      # line 0's sync, as nominal
        self.syncs = []                      # (line, sample) of each sync pulse found
        self.n = 0                           # next line (period) to draw
        self.miss = 0
        self.rows_done = 0
        self.img = np.zeros((mode.lines, mode.width, 3), dtype=np.uint8)
        self.ycc36 = {}                      # Robot 36: row -> (y, chroma, "cr"|"cb")
        self.buf.drop_before(t0 - 50 * MS)
        return [{"event": "start", "mode": mode.name, "label": mode.label,
                 "width": mode.width, "lines": mode.lines, "seconds": round(mode.seconds)}]

    def _fit(self):
        """(a, b): line n's sync starts at a + n*b samples."""
        b0 = self.P
        s = self.syncs
        if not s:
            return self.a0, b0
        n = np.array([x for x, _ in s], dtype=np.float64)
        t = np.array([y for _, y in s], dtype=np.float64)
        if len(s) < 6:
            return float(np.median(t - n * b0)), b0
        keep = np.ones(len(s), dtype=bool)
        for _ in range(3):
            b, a = np.polyfit(n[keep], t[keep], 1)
            r = t - (a + b * n)
            new = np.abs(r) < max(2 * MS, 3 * float(np.std(r[keep])))
            if new.sum() < 4 or (new == keep).all():
                break
            keep = new
        b, a = np.polyfit(n[keep], t[keep], 1)
        lo, hi = b0 * (1 - CLOCK_MAX), b0 * (1 + CLOCK_MAX)
        if not lo <= b <= hi:
            b = min(max(b, lo), hi)
            a = float(np.median(t[keep] - n[keep] * b))
        return float(a), float(b)

    def _find_sync(self, expect):
        w = (4 if len(self.syncs) >= 6 else 14) * MS
        dur = self.mode.sync_dur * MS
        x, first = self.buf.span(expect - w, expect + w + dur)
        if x.size < dur:
            return None
        low = P.smooth(x, int(MS)) < SYNC_HZ
        edges = np.diff(np.concatenate([[0], low.astype(np.int8), [0]]))
        starts, ends = np.nonzero(edges == 1)[0], np.nonzero(edges == -1)[0]
        best = None
        for s0, e0 in zip(starts, ends):
            if e0 - s0 >= 0.6 * dur:
                t = first + s0
                if best is None or abs(t - expect) < abs(best - expect):
                    best = t
        return None if best is None else float(best)

    def _step_image(self):
        m = self.mode
        if self.n >= m.periods:
            return self._finish("complete")
        a, b = self._fit()
        expect = a + self.n * b
        if self.buf.end < expect - m.sync_ms * MS + b + 15 * MS:
            return False                                      # the line isn't all here yet
        t = self._find_sync(expect)
        if t is None:
            self.miss += 1
        else:
            self.miss = 0
            self.syncs.append((self.n, t))
            a, b = self._fit()
        self._draw(self.n, a, b)
        self.n += 1
        self.rows_done = min(m.lines, self.n * (2 if m.model == "PD" else 1))
        if self.n % PROGRESS_EVERY == 0:
            return [{"event": "progress", "mode": m.name, "lines": self.rows_done, "total": m.lines}]
        return []

    def _draw(self, n, a, b):
        m = self.mode
        ls = a + n * b - m.sync_ms * MS
        k = b / self.P                                        # the fit's stretch, applied within the line

        def ch(name, w=m.width):
            s, d = m.segs[name]
            return P.pixels(self.buf, ls + s * MS * k, d * MS * k, w)

        if m.model == "GBR":
            self.img[n] = np.stack([ch("r"), ch("g"), ch("b")], axis=-1)
        elif m.model == "YCC":
            self.img[n] = P.ycc_to_rgb(ch("y"), ch("cr"), ch("cb"))
        elif m.model == "PD":
            cr, cb = ch("cr"), ch("cb")
            self.img[2 * n] = P.ycc_to_rgb(ch("y0"), cr, cb)
            self.img[2 * n + 1] = P.ycc_to_rgb(ch("y1"), cr, cb)
        elif m.model == "YC36":
            s, d = m.segs["sep"]
            sep = P.mean(self.buf, ls + s * MS * k, ls + (s + d) * MS * k)
            # The separator says which colour this line carries: 1500 Hz R-Y
            # (even lines), 2300 Hz B-Y (odd); fall back on the line's parity.
            kind = ("cb" if sep > 1900 else "cr") if sep is not None else ("cr" if n % 2 == 0 else "cb")
            self.ycc36[n] = (ch("y"), ch("c"), kind)
            for r in (n - 1, n):
                self._draw36(r)

    def _draw36(self, r):
        if r not in self.ycc36:
            return
        y, c, kind = self.ycc36[r]
        other = None
        for q in (r + 1, r - 1) if kind == "cr" else (r - 1, r + 1):   # its pair partner first
            if q in self.ycc36 and self.ycc36[q][2] != kind:
                other = self.ycc36[q][1]
                break
        if other is None:
            other = np.full_like(c, 128)
        cr, cb = (c, other) if kind == "cr" else (other, c)
        self.img[r] = P.ycc_to_rgb(y, cr, cb)

    def _fold_fit(self, lines, start=0):
        """(a, b) from every line so far folded together, or None: the sync
        pulse's start in line n is a + n*b. Searches the clock error over
        +-CLOCK_MAX and the phase, scoring the folded sync dip (a box mean the
        sync's length) against the folded picture's median. One weak sync per
        line adds up over a whole picture where no single line shows it."""
        m = self.mode
        lines -= start
        if lines < 8:
            return None
        dur = max(1, int(m.sync_dur * MS))
        origin = self.a0 + (start - 0.5) * self.P            # fold from half a line before line `start`'s sync
        x, first = self.buf.span(origin, origin + (lines + 1) * self.P * (1 + CLOCK_MAX))
        if x.size < lines * self.P:
            return None
        c = np.concatenate([[0.0], np.cumsum(x, dtype=np.float64)])
        box = (c[dur:] - c[:-dur]) / dur
        o = origin - first

        def score(ppm, step=1):
            Lp = self.P * (1 + ppm * 1e-6)
            N = int(Lp)
            starts = [int(o + n * Lp) for n in range(lines) if 0 <= int(o + n * Lp) and int(o + n * Lp) + N <= box.size]
            if len(starts) < 8:
                return -1.0, 0
            prof = np.mean([box[i:i + N:step] for i in starts], axis=0)
            k = int(np.argmin(prof))
            return float(np.median(prof) - prof[k]), k * step
        coarse = max(range(-int(CLOCK_MAX * 1e6), int(CLOCK_MAX * 1e6) + 1, 40), key=lambda q: score(q, 4)[0])
        best = max(range(coarse - 40, coarse + 41, 4), key=lambda q: score(q)[0])
        depth, k = score(best)
        if depth < FOLD_MIN_DEPTH:
            return None
        b = self.P * (1 + best * 1e-6)
        a = first + o + k
        a -= start * b                                        # back to line 0's sync
        a += round((self.a0 - a) / b) * b
        self.fold_depth = depth
        return float(a), float(b)

    def _finish(self, status):
        m = self.mode
        if len(self.syncs) >= max(8, self.n // 2):
            a, b = self._fit()                                 # syncs on most lines: exact
            timing = "syncs"
        else:
            folded = self._fold_fit(self.n)
            a, b = folded if folded is not None else self._fit()
            timing = "fold" if folded is not None else ("syncs" if self.syncs else "nominal")
        for n in range(self.n):                                # redraw with the final timing
            self._draw(n, a, b)
        slant = (b / self.P - 1) * 1e6
        img = self.img if status == "complete" else self.img[:self.rows_done]
        ev = {"event": "done", "mode": m.name, "label": m.label, "status": status,
              "lines": self.rows_done, "total": m.lines, "image": img.copy(),
              "syncs": len(self.syncs), "clock_ppm": round(slant), "timing": timing}
        self.buf.clear_to_end()
        self._hunt_reset()
        return [ev]
