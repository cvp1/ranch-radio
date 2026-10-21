"""A fake radio with a built-in KISS TNC on a pseudo-terminal (stands in for
the UV-PRO's Bluetooth serial port).

tnc_hub.py opens `fake.path` as its --tnc. Every KISS frame the hub writes is
"transmitted": recorded with a timestamp and handed to `on_air(raw)`, where a
simulated station can answer through `inject(raw)`. `loss` drops a share of
frames in each direction to exercise AX.25 retries.

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
    def __init__(self, on_air=None, loss=0.0, seed=1, on_control=None):
        self.on_air = on_air or (lambda raw: None)
        self.on_control = on_control or (lambda frame: None)
        self.control = []          # Gaia frames the hub sent
        self.loss = loss
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.tx = []               # (t, raw) frames the hub transmitted
        self.cmds = []             # non-data KISS commands the hub sent
        self.raw_in = bytearray()
        self.master, self._slave = os.openpty()
        tty.setraw(self._slave)
        self.path = os.ttyname(self._slave)
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._t.start()
        return self

    def stop(self):
        self._stop.set()
        self._t.join(2)
        for fd in (self.master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def inject(self, raw: bytes, lossy=True):
        """A frame heard over the air, delivered to the hub."""
        if lossy and self.loss and self.rng.random() < self.loss:
            return
        os.write(self.master, kiss_wrap(raw))

    def inject_bytes(self, data: bytes):
        os.write(self.master, data)

    def transmitted(self):
        with self.lock:
            return list(self.tx)

    def _run(self):
        from tnc_hub import RadioSplit
        dec, split = KissDecoder(), RadioSplit()
        while not self._stop.is_set():
            r, _, _ = select.select([self.master], [], [], 0.1)
            if not r:
                continue
            try:
                data = os.read(self.master, 4096)
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
                    os.write(self.master, answer)
            for port, cmd, payload in dec.feed(data):
                if cmd != 0:
                    with self.lock:
                        self.cmds.append((port, cmd, payload))
                    continue
                with self.lock:
                    self.tx.append((time.monotonic(), payload))
                if self.loss and self.rng.random() < self.loss:
                    continue
                self.on_air(payload)
