"""A fake radio with a built-in KISS TNC on a pseudo-terminal (stands in for
the UV-PRO's Bluetooth serial port).

tnc_hub.py opens `fake.path` as its --tnc. Every KISS frame the hub writes is
"transmitted": recorded with a timestamp and handed to `on_air(raw)`, where a
simulated station can answer through `inject(raw)`. `loss` drops a share of
frames in each direction to exercise AX.25 retries: each direction has its
own seeded generator, so a direction's loss pattern depends only on how many
frames it has carried (not on how two threads' draws interleave), and never
drops more than `max_run` frames in a row, so an unlucky draw cannot outlast
the link's retries.

Like the UV-PRO, the same port also carries the radio's command protocol
(Gaia frames): those the hub sends are kept in `control` and handed to
`on_control(frame)`, whose return value (bytes, or None) is written back;
`inject_bytes(b)` writes anything as is.

Never connects to real hardware.
"""

import os
import random
import select
import threading
import time
import tty

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ax25 import KissDecoder, kiss_wrap  # noqa: E402


class FakeKissRadio:
    def __init__(self, on_air=None, loss=0.0, seed=1, on_control=None, max_run=3):
        self.on_air = on_air or (lambda raw: None)
        self.on_control = on_control or (lambda frame: None)
        self.control = []          # Gaia frames the hub sent
        self.loss = loss
        self.max_run = max_run
        # One generator (and drop run) per direction: "up" = hub to air,
        # "down" = air to hub. A shared one, drawn from by the reader thread
        # and the simulated station's thread in whatever order the scheduler
        # picked, made the seed reproduce nothing.
        self._rng = {"up": random.Random(seed), "down": random.Random(seed + 1000)}
        self._run_len = {"up": 0, "down": 0}
        self.dropped = {"up": 0, "down": 0}
        self.lock = threading.Lock()
        self.tx = []               # (t, raw) frames the hub transmitted
        self.cmds = []             # non-data KISS commands the hub sent
        self.raw_in = bytearray()
        self.master, self._slave = os.openpty()
        tty.setraw(self._slave)
        os.set_blocking(self.master, False)
        self.path = os.ttyname(self._slave)
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._t.start()
        return self

    def stop(self):
        """Stop reading and close the pty. The fds are closed only once the
        reader thread has exited, and under the lock writers take: a write
        after stop() is dropped, never sent into a closed fd that the next
        test's pty (or a socket) may already have reused."""
        self._stop.set()
        if self._t.is_alive() and self._t is not threading.current_thread():
            self._t.join(10)
            if self._t.is_alive():
                raise RuntimeError("fake radio reader did not stop")
        with self.lock:
            fds, self.master, self._slave = (self.master, self._slave), None, None
        for fd in fds:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _lose(self, way):
        """Whether to drop the next frame going `way` (see the module doc)."""
        if not self.loss:
            return False
        with self.lock:
            drop = self._rng[way].random() < self.loss and self._run_len[way] < self.max_run
            self._run_len[way] = self._run_len[way] + 1 if drop else 0
            self.dropped[way] += drop
        return drop

    def _write(self, data: bytes, timeout=10.0):
        """Write to the hub, under the lock stop() closes the fd under. The
        master is non-blocking, so a hub that isn't reading can't wedge a
        writer while it holds the lock: retry outside it instead."""
        view = memoryview(data)
        deadline = time.monotonic() + timeout
        while view:
            with self.lock:
                if self._stop.is_set() or self.master is None:
                    return              # stopped: the fd may belong to someone else now
                try:
                    view = view[os.write(self.master, view):]
                    continue
                except BlockingIOError:
                    pass
            if time.monotonic() > deadline:
                raise RuntimeError("fake radio: the hub stopped reading the pty")
            time.sleep(0.005)

    def inject(self, raw: bytes, lossy=True):
        """A frame heard over the air, delivered to the hub."""
        if lossy and self._lose("down"):
            return
        self._write(kiss_wrap(raw))

    def inject_bytes(self, data: bytes):
        self._write(data)

    def transmitted(self):
        with self.lock:
            return list(self.tx)

    def _run(self):
        from tnc_hub import RadioSplit
        dec, split = KissDecoder(), RadioSplit()
        while not self._stop.is_set():
            fd = self.master
            r, _, _ = select.select([fd], [], [], 0.1)
            if not r:
                continue
            try:
                data = os.read(fd, 4096)
            except BlockingIOError:
                continue
            except OSError:
                return
            with self.lock:
                self.raw_in += data
            data, gaia = split.feed(data)
            for frame in gaia:
                with self.lock:
                    self.control.append(frame)
                answer = self.on_control(frame)
                if answer:
                    self._write(answer)
            for port, cmd, payload in dec.feed(data):
                if cmd != 0:
                    with self.lock:
                        self.cmds.append((port, cmd, payload))
                    continue
                with self.lock:
                    self.tx.append((time.monotonic(), payload))
                if self._lose("up"):
                    continue
                self.on_air(payload)
