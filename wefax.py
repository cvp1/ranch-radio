"""HF radiofax (WEFAX) receive: a radio's audio, as frequency from
picture.Discriminator, to grey weather charts, for the Imaging add-on. Pure:
feed it frequency samples, it returns events and holds the chart.

What a station sends (WMO / NOAA HF fax; the radio on USB, dial 1.9 kHz
below the published carrier): black 1500 Hz, white 2300 Hz, and

- a **start tone**: black/white alternating at 300 Hz (IOC 576) or 675 Hz
  (IOC 288) for about 5 s;
- **phasing**: about 30 s of black lines, each with a white pulse 5 % wide
  where the line starts;
- the **chart**, LPM lines a minute (120 almost everywhere), each line
  round(pi * IOC) pixels wide;
- a **stop tone**: alternating at 450 Hz for about 5 s.

Here the tones are found as one strong line in the frequency stream
(picture.goertzel_share). After the start tone, two stretches of phasing
lines are folded on the line period: where the pulse sits gives the line
start, and how far it moved between them gives the sound card's clock
error, so a chart is not slanted. Phasing lines are left off the chart.
The chart ends at the stop tone, a new start tone, MAX_LINES, or Stop.

Joined mid-chart (no start tone heard), the operator's Start begins one
here at nominal timing (`clock_ppm` from the host file, else 0); its left
edge lands wherever the line happens to start.
"""
import math

import numpy as np

import picture as P

WIN = int(0.5 * P.RATE)           # tone windows: 0.5 s ...
HOP = WIN // 2                    # ... every 0.25 s
TONE_SHARE = 0.35                 # this much of the variance in one line = a tone
TONE_SPREAD_HZ = 150              # and the signal must swing at least this much (std)
START_HOLD = 6                    # windows (1.5 s) of start tone to believe it
STOP_HOLD = 8                     # windows (2 s) of stop tone
FOLD_LINES = 10                   # phasing lines folded per estimate
FOLD_BINS = 2000
PROGRESS_EVERY = 25
LPMS = (60, 90, 120, 240)
IOCS = {300: 576, 675: 288}
MAX_PPM = 1000


def width(ioc):
    return int(round(math.pi * ioc))


class FaxDecoder:
    """feed(freq) -> events: {"event": "start"|"progress"|"done", ...}."""

    def __init__(self, lpm=120, ioc=576, clock_ppm=0.0, max_lines=1600):
        if lpm not in LPMS:
            raise ValueError(f"fax lpm must be one of {', '.join(map(str, LPMS))}, not {lpm!r}")
        if ioc not in IOCS.values():
            raise ValueError(f"fax ioc must be 576 or 288, not {ioc!r}")
        if not -MAX_PPM <= float(clock_ppm) <= MAX_PPM:
            raise ValueError(f"fax clock_ppm must be within ±{MAX_PPM}")
        if not 10 <= int(max_lines) <= 4000:
            raise ValueError("fax max_lines must be 10..4000")
        self.lpm, self.ioc, self.clock_ppm, self.max_lines = lpm, ioc, float(clock_ppm), int(max_lines)
        self.buf = P.Buffer()
        self.next_win = 0
        self.start_hits = self.stop_hits = self.quiet = 0
        self.tone_ioc = None
        self.tone_from = 0
        self.cur_ioc = ioc
        self.measured_ppm = None
        self._idle()

    # --- state

    def _idle(self):
        self.state = "idle"               # idle | starting | phasing | receiving
        self.rows = []
        self.phasing_at = None
        self.aligned = False

    @property
    def line_samples(self):
        return P.RATE * 60.0 / self.lpm

    def status(self):
        if self.state == "idle":
            return {"state": "listening"}
        if self.state in ("starting", "phasing"):
            return {"state": "phasing", "ioc": self.cur_ioc}
        return {"state": "receiving", "lines": len(self.rows), "ioc": self.cur_ioc,
                "aligned": self.aligned, "max": self.max_lines}

    def image(self):
        if not self.rows:
            return None
        return np.stack(self.rows)

    # --- input

    def feed(self, freq):
        self.buf.append(np.asarray(freq, dtype=np.float32))
        out = []
        for _ in range(10_000):           # bounded: each pass consumes a window or a line
            ev = self._step()
            if ev is False:
                break
            out.extend(ev)
        return out

    def start_now(self):
        """The operator's Start: a chart from here, at nominal timing."""
        out = self._finish("interrupted") if self.state == "receiving" else []
        self.cur_ioc = self.ioc
        self._begin(float(self.buf.end), self.line_samples * (1 + self.clock_ppm * 1e-6), aligned=False)
        return out + [self._start_event()]

    def abort(self):
        if self.state == "receiving":
            return self._finish("stopped")
        self._idle()
        return []

    # --- the tones

    def _tones(self, a):
        x, _ = self.buf.span(a, a + WIN)
        if x.size < WIN or float(np.std(x)) < TONE_SPREAD_HZ:
            return None
        share = {f: P.goertzel_share(x, f) for f in (300, 450, 675)}
        f = max(share, key=share.get)
        return f if share[f] >= TONE_SHARE else None

    def _step(self):
        line_ev = self._step_line() if self.state == "receiving" else False
        if line_ev is not False:
            return line_ev
        self.next_win = max(self.next_win, self.buf.base)
        if self.buf.end < self.next_win + WIN:
            if self.state == "idle":
                self.buf.drop_before(min(self.buf.end - 2 * WIN, self.next_win))
            return False
        a = self.next_win
        self.next_win += HOP
        if self.state == "phasing":
            return self._try_phase(a)
        tone = self._tones(a)
        if tone in (300, 675):
            if not self.start_hits:
                self.tone_from = a
            self.start_hits += 1
            self.stop_hits = 0
            self.tone_ioc = IOCS[tone]
        elif tone == 450:
            if not self.stop_hits:
                self.tone_from = a
            self.stop_hits += 1
            self.start_hits = 0
        else:
            self.start_hits = self.stop_hits = 0
        if self.state == "receiving":
            if self.stop_hits >= STOP_HOLD:
                self.stop_hits = 0
                self._trim(self.tone_from)
                return self._finish("complete")
            if self.start_hits >= START_HOLD:          # a new chart began without a stop
                self._trim(self.tone_from)
                out = self._finish("complete")
                self.state, self.cur_ioc = "starting", self.tone_ioc
                return out
            return []
        if self.state == "idle" and self.start_hits >= START_HOLD:
            self.state, self.cur_ioc = "starting", self.tone_ioc
            self.quiet = 0
            return []
        if self.state == "starting":
            self.quiet = self.quiet + 1 if tone not in (300, 675) else 0
            if self.quiet >= 2:                       # the start tone has ended: phasing from here
                self.state = "phasing"
                self.phasing_at = a + HOP
                self.buf.drop_before(self.phasing_at - WIN)
        return []

    # --- phasing

    def _fold(self, first, n):
        L = self.line_samples
        rows = [P.pixels(self.buf, first + k * L, L, FOLD_BINS).astype(np.float64) for k in range(n)]
        prof = np.median(np.stack(rows), axis=0)
        d = prof - np.median(prof)
        w = FOLD_BINS // 20                            # the pulse: 5 % of the line
        circ = np.concatenate([d, d[:w]])
        c = np.concatenate([[0.0], np.cumsum(circ)])
        run = (c[w:w + FOLD_BINS] - c[:FOLD_BINS]) / w  # mean over [i, i+w)
        i = int(np.argmax(run))
        contrast = float(run[i])
        # Sub-bin start of the pulse: the centroid of its bright part.
        idx = np.arange(i - w // 2, i + w + w // 2)
        wt = np.clip(d[idx % FOLD_BINS] - contrast / 2, 0, None)
        if wt.sum() <= 0:
            return None, contrast
        centre = float((idx * wt).sum() / wt.sum())
        return (centre - w / 2) / FOLD_BINS, contrast   # the pulse's leading edge, as a fraction of a line

    def _try_phase(self, _a):
        L = self.line_samples
        gap = 2                                        # lines between the two folds
        need = self.phasing_at + (2 * FOLD_LINES + gap + 1) * L
        if self.buf.end < need:
            return []
        p1, c1 = self._fold(self.phasing_at, FOLD_LINES)
        p2, c2 = self._fold(self.phasing_at + (FOLD_LINES + gap) * L, FOLD_LINES)
        if p1 is None or p2 is None or min(c1, c2) < 60:
            # No pulse to lock to (a station without phasing, or too weak):
            # run free from here, like a manual start.
            self._begin(float(self.phasing_at), L * (1 + self.clock_ppm * 1e-6), aligned=False)
            return [self._start_event()]
        moved = ((p2 - p1 + 0.5) % 1.0) - 0.5          # fraction of a line, over FOLD_LINES + gap lines
        per_line = moved / (FOLD_LINES + gap)
        per_line = max(-MAX_PPM * 1e-6, min(MAX_PPM * 1e-6, per_line))
        Lc = L * (1 + per_line)
        s0 = self.phasing_at + p1 * L
        self._begin(s0, Lc, aligned=True)
        self.measured_ppm = round(per_line * 1e6)
        return [self._start_event()]

    # --- the chart

    def _begin(self, s0, Lc, aligned):
        self.state = "receiving"
        self.s0, self.Lc, self.k = s0, Lc, 0
        self.aligned = aligned
        self.skipping = aligned                        # leave the phasing lines off
        self.rows = []
        self.rows_at = []                              # each row's first sample
        self.measured_ppm = None
        self.W = width(self.cur_ioc)
        self.start_hits = self.stop_hits = 0

    def _start_event(self):
        return {"event": "start", "mode": f"fax{self.lpm}", "label": f"Fax {self.lpm} LPM, IOC {self.cur_ioc}",
                "width": self.W, "aligned": self.aligned}

    def _is_phasing(self, row):
        w = max(1, len(row) // 20)
        return float(row[:w].mean()) > 170 and float(row[w + w // 2:].mean()) < 50

    def _step_line(self):
        a = self.s0 + self.k * self.Lc
        if self.buf.end < a + self.Lc + 1:
            return False
        row = P.pixels(self.buf, a, self.Lc, self.W)
        self.k += 1
        # Keep what the tone windows haven't read yet (2026-11-26: dropping
        # past them starved the stop-tone check and a chart never ended).
        self.buf.drop_before(min(a + self.Lc - WIN, self.next_win))
        if self.skipping and self._is_phasing(row) and self.k < 120:
            return []
        self.skipping = False
        self.rows.append(row)
        self.rows_at.append(a)
        if len(self.rows) >= self.max_lines:
            return self._finish("full")
        if len(self.rows) % PROGRESS_EVERY == 0:
            return [{"event": "progress", "mode": f"fax{self.lpm}", "lines": len(self.rows)}]
        return []

    def _trim(self, tone_at):
        """Leave off the lines drawn while a tone was being confirmed: the
        tone is heard STOP_HOLD windows before it is believed."""
        keep = sum(1 for at in self.rows_at if at + self.Lc / 2 <= tone_at)
        del self.rows[keep:], self.rows_at[keep:]

    def _finish(self, status):
        img = self.image()
        ev = {"event": "done", "mode": f"fax{self.lpm}", "label": f"Fax {self.lpm} LPM, IOC {self.cur_ioc}",
              "status": status, "lines": len(self.rows), "aligned": self.aligned,
              "clock_ppm": self.measured_ppm, "image": img}
        self._idle()
        return [ev] if img is not None else []
