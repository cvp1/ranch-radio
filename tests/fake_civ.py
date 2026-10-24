"""A fake CI-V radio on a pseudo-terminal, for testing the real CivRadio code.

sim.py applies every set instantly inside the server, so it cannot show
queueing, write-loss or shutdown bugs. This fake sits on the other end of a
pty: the server opens the slave path as its serial port and talks CI-V to it,
exactly as it would to a G90 or IC-7300. Every frame the server writes is
recorded with a timestamp; that list is what the tests assert on.

Never connects to real hardware. Library use:

    fake = FakeCiv("g90")            # or "ic7300"; address from rigs.py
    fake.start()
    ... run server.py --port fake.path ...
    fake.ptt_frames()                # [(t, 0|1), ...] in wire order
    fake.stop()

CLI (manual poking): python3 tests/fake_civ.py g90
"""

import os
import select
import sys
import threading
import time
import tty
import queue

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import rigs  # noqa: E402

CTRL = 0xE0
OK, NG = 0xFB, 0xFA
MODE_CODE = {"LSB": 0x00, "USB": 0x01, "AM": 0x02, "CW": 0x03, "RTTY": 0x04,
             "FM": 0x05, "CW-R": 0x07, "RTTY-R": 0x08}
CODE_MODE = {v: k for k, v in MODE_CODE.items()}


def bcd_le(n, nbytes):
    out = bytearray()
    for _ in range(nbytes):
        two = n % 100
        out.append((two // 10) << 4 | two % 10)
        n //= 100
    return bytes(out)


def from_bcd_le(b):
    return sum(((x >> 4) * 10 + (x & 0x0F)) * 100 ** i for i, x in enumerate(b))


class Frame:
    __slots__ = ("t", "cmd", "data")

    def __init__(self, t, cmd, data):
        self.t, self.cmd, self.data = t, cmd, bytes(data)

    def is_ptt(self):
        return self.cmd == 0x1C and len(self.data) == 2 and self.data[0] == 0x00

    def is_freq_set(self):
        return self.cmd == 0x05 and len(self.data) >= 5

    def __repr__(self):
        return f"<{self.t:.3f} {self.cmd:02x} {self.data.hex(' ')}>"


class FakeCiv:
    def __init__(self, rig="g90", ack_delay_s=0.0, tune_s=1.0, g90_1a06_echo=False):
        caps = rigs.get(rig)
        self.rig = rig
        self.addr = caps["addr"]
        # Replies take wire time, as on the real CI-V link (10 bits a byte):
        # an instant fake let the server poll ~25,000 frames/s and burn the
        # CPU a CI runner needs for everything else.
        self.byte_s = 10 / caps["baud"]
        # A slow radio: commands that change something (answered OK) are
        # acknowledged ack_delay_s late; reads are answered at once. Replies
        # stay in order, so a read queued behind a slow command waits for it.
        # (Delaying every reply made the fake answer ~4 frames/s against the
        # server's ~17: an unbounded backlog of replies describing the past.)
        self.ack_delay_s = ack_delay_s
        self._capture = None            # the worker's replies, held back to apply the ack delay
        self.tune_s = tune_s            # None = the tune never finishes
        self.ignore = set()             # cmd bytes or (cmd, sub) whose SETS are acked but not applied
        # Real G90 (fw 1.81, linux-station 2026-10-14): a 1A 06 set is acked, the display
        # stays in plain USB, and a 1A 06 read then reports the value set. (A
        # front-panel mode change does clear that read to 00, 13:31:38; not
        # modelled here.)
        self.g90_1a06_echo = g90_1a06_echo
        self._data_echo = None
        self.lock = threading.RLock()
        self._state = {"freq": 14_074_000, "mode": "USB", "filter": 1, "data": 1,
                      "split": 0, "tx": 0, "tuner": 1, "freq_b": 14_074_000,
                      "swr_raw": b"\x00\x00", "po_raw": b"\x00\x00", "alc_raw": b"\x00\x00",
                      "att": 0, "data_off_mod": 0x02}   # IC-7300 default: MIC/ACC
        self.frames = []                # every frame the server wrote, in order
        self._stale_tx = 0
        self._pending = 0               # frames received and not yet answered (or dropped)
        self.answered = []              # (monotonic time the reply was computed, Frame), in order
        self.tune_ended_at = None       # monotonic time the last tune finished
        self._stop = threading.Event()
        self._work = queue.Queue()
        self.master, self._slave = os.openpty()
        tty.setraw(self._slave)
        self.path = os.ttyname(self._slave)
        self._threads = []

    # -- control knobs --

    def inject(self, **kv):
        """A change made at the radio's front panel. Returns a mark taken
        after it: every reply computed after the mark carries the change."""
        with self.lock:
            self._state.update(kv)
        return time.monotonic()

    def stale_tx_reply_once(self):
        """The next 1C 00 poll reply says 'not transmitting', whatever the truth."""
        with self.lock:
            self._stale_tx += 1

    # -- queries --

    def ptt_frames(self, since=0.0):
        with self.lock:
            return [(f.t, f.data[1]) for f in self.frames if f.is_ptt() and f.t >= since]

    def sets_since(self, since=0.0):
        """PTT and frequency sets in wire order: ('ptt', 0|1) / ('freq', hz)."""
        out = []
        with self.lock:
            for f in self.frames:
                if f.t < since:
                    continue
                if f.is_ptt():
                    out.append(("ptt", f.data[1]))
                elif f.is_freq_set():
                    out.append(("freq", from_bcd_le(f.data[:5])))
        return out

    def mark(self):
        return time.monotonic()

    def status_replies_since(self, since, cmd=0x1C, data=b"\x00"):
        """How many polls of one kind (default PTT status, 1C 00; 1C 01 the
        tuner, 0F split, 26 00 mode) the fake answered after `since`. Each
        reply carried the state as of its answer, so n of them after a
        change means the server has been told about it n times."""
        with self.lock:
            return sum(1 for t, f in self.answered
                       if t >= since and f.cmd == cmd and f.data == data)

    def quiesce(self, quiet_s=0.3, timeout=10.0):
        """Wait until nothing is left to read or answer and nothing new has
        arrived for quiet_s: every frame the server wrote is recorded.
        Use before a 'nothing was written' check, after the server stopped."""
        end = time.monotonic() + timeout
        last = (-1, -1)
        since = time.monotonic()
        while time.monotonic() < end:
            with self.lock:
                now = (len(self.frames), self._pending)
            readable = bool(select.select([self.master], [], [], 0)[0]) if not self._stop.is_set() else False
            if now != last or readable or now[1]:
                last, since = now, time.monotonic()
            elif time.monotonic() - since >= quiet_s:
                return True
            time.sleep(0.02)
        return False

    @property
    def state(self):
        """The radio's state, as a test should see it: first waits until
        every frame the server has written is answered, so a check made
        right after a command sees it applied (as with an instant fake) and
        a 'nothing changed' check covers everything sent. The worker (and
        a stopped fake) read it directly."""
        # Not while this thread holds the lock: answering needs it.
        if (threading.current_thread() not in self._threads and not self._stop.is_set()
                and not self.lock._is_owned()):
            self.caught_up()
        return self._state

    def caught_up(self, timeout=10.0):
        """Wait until every frame the server has written so far is answered:
        no reply describing the past is still queued to land later. (The
        queue need not empty: a server that sends one frame per reply keeps
        a few in flight for good once a slow ack has let them build up.)"""
        with self.lock:
            n = len(self.frames)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if len(self.answered) >= n:
                    return True
            time.sleep(0.02)
        return False

    # -- lifecycle --

    def start(self):
        for target in (self._reader, self._worker):
            th = threading.Thread(target=target, daemon=True)
            th.start()
            self._threads.append(th)
        return self

    def stop(self):
        # Queued replies are dropped, not answered: with an ack delay a slow
        # machine can hold a backlog longer than any join timeout, and a reply
        # thread still running after the close below would write into the
        # next fake, which gets the same fd numbers (a stale "transmitting"
        # reply made civ_safety_test's F2 split check flaky on CI).
        self._stop.set()
        self._work.put(None)
        for th in self._threads:
            th.join(timeout=5)
        with self.lock:                 # _send writes only under the lock, after checking _stop
            for fd in (self.master, self._slave):
                try:
                    os.close(fd)
                except OSError:
                    pass

    # -- wire --

    def _reader(self):
        buf = bytearray()
        while not self._stop.is_set():
            r, _, _ = select.select([self.master], [], [], 0.1)
            if not r:
                continue
            if self._stop.is_set():
                break
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                time.sleep(0.05)
                continue
            now = time.monotonic()
            buf += chunk
            while True:
                s = buf.find(b"\xfe\xfe")
                if s < 0:
                    buf.clear()
                    break
                e = buf.find(b"\xfd", s)
                if e < 0:
                    del buf[:s]
                    break
                body = bytes(buf[s + 2:e]).lstrip(b"\xfe")
                del buf[:e + 1]
                if len(body) < 3 or body[0] != self.addr:
                    continue
                f = Frame(now, body[2], body[3:])
                with self.lock:
                    self.frames.append(f)
                    self._pending += 1
                self._work.put(f)

    def _send(self, cmd, data=b""):
        frame = b"\xfe\xfe" + bytes([CTRL, self.addr, cmd]) + data + b"\xfd"
        if self._capture is not None:   # in the worker: written after any ack delay
            self._capture.append((cmd, frame))
            return
        self._write_frame(frame)

    def _write_frame(self, frame):
        with self.lock:
            if self._stop.is_set():
                return                  # stopped: the fd may already belong to another pty
            try:
                os.write(self.master, frame)
            except OSError:
                pass

    def _worker(self):
        while True:
            f = self._work.get()
            if f is None or self._stop.is_set():
                return
            replies = self._capture = []
            computed = time.monotonic()     # the reply describes the state as of now
            try:
                self._answer(f)
            except Exception as e:   # a bad frame must not kill the fake
                print("fake_civ: error on", f, e, file=sys.stderr)
            finally:
                self._capture = None
                wait = self.byte_s * (len(f.data) + 6 + sum(len(fr) for _, fr in replies))
                if self.ack_delay_s and any(cmd == OK for cmd, _ in replies):
                    wait += self.ack_delay_s
                time.sleep(wait)
                for _, frame in replies:
                    self._write_frame(frame)
                with self.lock:
                    self._pending -= 1
                    self.answered.append((computed, f))

    def _ignored(self, cmd, sub=None):
        return cmd in self.ignore or (cmd, sub) in self.ignore

    def _answer(self, f):
        c, d = f.cmd, f.data
        with self.lock:
            s = self._state
            if c == 0x03:
                return self._send(0x03, bcd_le(s["freq"], 5))
            if c == 0x04:
                return self._send(0x04, bytes([MODE_CODE[s["mode"]], s["filter"]]))
            if c == 0x05:
                if not self._ignored(c):
                    s["freq"] = from_bcd_le(d[:5])
                return self._send(OK)
            if c == 0x06:
                if not self._ignored(c):
                    s["mode"], s["data"] = CODE_MODE.get(d[0], s["mode"]), 0
                    if len(d) > 1:
                        s["filter"] = d[1]
                return self._send(OK)
            if c == 0x1A and d[:3] == b"\x05\x00\x66" and self.rig == "ic7300":
                # IC-7300 DATA OFF MOD (00=MIC, 01=ACC, 02=MIC/ACC, 03=USB)
                if len(d) == 3:
                    return self._send(0x1A, d[:3] + bytes([s["data_off_mod"]]))
                s["data_off_mod"] = d[3]
                return self._send(OK)
            if c == 0x1A and d[:1] == b"\x06":
                if len(d) == 1:
                    data = s["data"]
                    if self.g90_1a06_echo and self._data_echo is not None:
                        data = self._data_echo
                    return self._send(0x1A, bytes([0x06, data, s["filter"] if data else 0]))
                if self.g90_1a06_echo:
                    self._data_echo = d[1]
                elif not self._ignored(c, 0x06):
                    s["data"] = d[1]
                return self._send(OK)
            if c == 0x26 and d[:1] == b"\x00":
                # Icom 26 00 <mode> <data> <filter>: mode + data mode in one frame.
                if len(d) == 1:
                    return self._send(0x26, bytes([0x00, MODE_CODE[s["mode"]], s["data"], s["filter"]]))
                if not self._ignored(c):
                    s["mode"] = CODE_MODE.get(d[1], s["mode"])
                    if len(d) > 2:
                        s["data"] = d[2]
                    if len(d) > 3:
                        s["filter"] = d[3]
                return self._send(OK)
            if c == 0x1C and d[:1] == b"\x00":
                if len(d) == 1:
                    tx = s["tx"]
                    if self._stale_tx:
                        self._stale_tx -= 1
                        tx = 0
                    return self._send(0x1C, bytes([0x00, tx]))
                if not self._ignored(c, 0x00):
                    s["tx"] = d[1]
                    if not d[1] and s["tuner"] == 2:
                        s["tuner"] = 1          # unkey aborts a tune (assumed; UNVERIFIED on a G90)
                return self._send(OK)
            if c == 0x1C and d[:1] == b"\x01":
                if len(d) == 1:
                    return self._send(0x1C, bytes([0x01, s["tuner"]]))
                if not self._ignored(c, 0x01):
                    s["tuner"] = d[1]
                    if d[1] == 2:
                        s["tx"] = 1
                        if self.tune_s is not None:
                            threading.Timer(self.tune_s, self._tune_done).start()
                return self._send(OK)
            if c == 0x0F:
                if not d:
                    return self._send(0x0F, bytes([s["split"]]))
                if not self._ignored(c):
                    s["split"] = d[0]
                return self._send(OK)
            if c == 0x15 and d:
                val = {0x11: s["po_raw"], 0x12: s["swr_raw"], 0x13: s["alc_raw"]}.get(d[0], b"\x00\x00")
                return self._send(0x15, bytes([d[0]]) + val)
            if c == 0x14 and d:
                if len(d) == 1:
                    return self._send(0x14, bytes([d[0], 0x01, 0x28]))
                return self._send(OK)
            if c == 0x16 and d:
                if len(d) == 1:
                    return self._send(0x16, bytes([d[0], 0x00]))
                return self._send(OK)
            if c == 0x11:
                if not d:
                    return self._send(0x11, bytes([s["att"]]))
                s["att"] = d[0]
                return self._send(OK)
            if c == 0x25 and d[:1] == b"\x01":
                if len(d) == 1:
                    return self._send(0x25, b"\x01" + bcd_le(s["freq_b"], 5))
                s["freq_b"] = from_bcd_le(d[1:6])
                return self._send(OK)
            if c == 0x27:
                if d[:2] == b"\x14\x00" and len(d) == 2:
                    return self._send(0x27, b"\x14\x00\x00")
                if d[:2] == b"\x15\x00" and len(d) == 2:
                    return self._send(0x27, b"\x15\x00" + bcd_le(25_000, 5))
                return self._send(OK)
            return self._send(OK)

    def end_tune(self):
        """End a tune now (for tune_s=None: the test, not a timer, decides
        when, so a slow check can't outlast it). Returns a mark after it."""
        self._tune_done()
        return time.monotonic()

    def _tune_done(self):
        with self.lock:
            if self._state["tuner"] == 2:
                self._state.update(tuner=1, tx=0)
                self.tune_ended_at = time.monotonic()


if __name__ == "__main__":
    fake = FakeCiv(sys.argv[1] if len(sys.argv) > 1 else "g90").start()
    print(fake.path, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n".join(map(repr, fake.frames[-40:])))
        fake.stop()
