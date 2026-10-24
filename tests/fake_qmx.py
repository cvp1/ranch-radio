"""A fake QRP Labs QMX on a pseudo-terminal, for testing the real QmxRadio code.

Speaks the QMX's Kenwood-style CAT (QMX CAT manual, firmware 1_04_003) on the
master side of a pty; server.py opens the slave path as its serial port. Every
command the server writes is recorded with a timestamp, and every raw byte is
kept so tests can prove nothing forbidden (CR, MD8, PS0) ever went out.

Models the radio's CAT timeout (QB/QC): with QB1, a transmission ends by
itself when no CAT command has arrived for QC seconds.

Never connects to real hardware.

    fake = FakeQmx().start()
    ... server.py --rig qmx --port fake.path ...
    fake.ptt()          # [(t, 0|1), ...] in wire order
    fake.stop()
"""

import os
import select
import threading
import time
import tty


class FakeQmx:
    def __init__(self):
        self.lock = threading.RLock()
        self.state = {"fa": 14_074_000, "fb": 14_074_000, "md": "6", "tx": 0, "sp": 0,
                      "fr": 0, "qb": "0", "qc": "010", "sm": -80, "pc": 45, "sw": 150,
                      "ss": "2"}  # SSB source: 0 USB, 1 two-tone test, 2 external mic (default)
        self.cmds = []            # (t, command without ';')
        self.raw = bytearray()
        self.last_cmd_t = time.monotonic()
        self.timeout_unkeys = 0
        self._stop = threading.Event()
        self.master, self._slave = os.openpty()
        tty.setraw(self._slave)
        self.path = os.ttyname(self._slave)
        self._threads = []

    # -- control --

    def inject(self, **kv):
        """A change made at the radio's own front panel. Returns a mark taken
        after it: every reply computed after the mark carries the change."""
        with self.lock:
            self.state.update(kv)
        return time.monotonic()

    # -- queries --

    def sent(self, since=0.0):
        with self.lock:
            return [c for t, c in self.cmds if t >= since]

    def ptt(self, since=0.0):
        m = {"TQ1": 1, "TX": 1, "TQ0": 0, "RX": 0}
        with self.lock:
            return [(t, m[c]) for t, c in self.cmds if c in m and t >= since]

    def mark(self):
        return time.monotonic()

    def count(self, cmd=None, since=0.0):
        """How many commands (all, or one kind: "IF", "SW") arrived after
        `since`. Each is answered as it is recorded, so a read counted here
        was answered with the state as of then."""
        with self.lock:
            return sum(1 for t, c in self.cmds if t >= since and (cmd is None or c == cmd))

    def quiesce(self, quiet_s=0.3, timeout=10.0):
        """Wait until nothing is left in the pty to read and no command has
        arrived for quiet_s: everything the server wrote is recorded. Use
        after the server exits (its last writes may still be in the pty) and
        before a 'nothing was written' check."""
        end = time.monotonic() + timeout
        last, since = -1, time.monotonic()
        while time.monotonic() < end:
            with self.lock:
                n = len(self.cmds) + len(self.raw)
            readable = (not self._stop.is_set()
                        and bool(select.select([self.master], [], [], 0)[0]))
            if n != last or readable:
                last, since = n, time.monotonic()
            elif time.monotonic() - since >= quiet_s:
                return True
            time.sleep(0.02)
        return False

    # -- radio --

    def _if(self):
        s = self.state
        freq = s["fb"] if (s["fr"] == 1 or (s["sp"] and s["tx"])) else s["fa"]
        # 11 freq, 5 spaces, RIT +0000, RIT, XIT, bank, 2 memory, then TX ...
        return (f"IF{freq:011d}     +0000" "0" "0" "0" "00" f"{s['tx']}{s['md']}"
                f"{1 if s['fr'] == 1 else 0}0{s['sp']}000 ;")

    def _reply(self, c):
        s = self.state
        cmd, v = c[:2], c[2:]
        if cmd in ("FA", "FB"):
            k = cmd.lower()
            if v:
                s[k] = int(v)
                return ""
            return f"{cmd}{s[k]:011d};"
        if c == "IF":
            return self._if()
        if cmd == "MD":
            if v:
                if v in "1235679":
                    s["md"] = v
                    return ""
                return "?;"
            return f"MD{s['md']};"
        if c in ("TX", "TQ1"):
            s["tx"] = 1
            return ""
        if c in ("RX", "TQ0"):
            s["tx"] = 0
            return ""
        if c == "TQ":
            return f"TQ{s['tx']};"
        if cmd == "SP":
            if v:
                s["sp"] = int(v)
                return ""
            return f"SP{s['sp']};"
        if cmd == "FR":
            if v:
                s["fr"], s["sp"] = (int(v), 0) if v in "01" else (0, 1)
                return ""
            return f"FR{1 if s['fr'] == 1 else 0};"
        if c == "SM":
            return f"SM{s['sm']};"
        if c == "PC":
            return f"PC{s['pc']};" if s["tx"] else "PC;"
        if c == "SW":
            return f"SW{s['sw']};" if s["tx"] else "SW;"
        if cmd == "SS":
            if v:
                if v in "012":
                    s["ss"] = v
                    return ""
                return "?;"
            return f"SS{s['ss']};"
        if cmd in ("QB", "QC"):
            k = cmd.lower()
            if v:
                s[k] = v
                return ""
            return f"{cmd}{s[k]};"
        if c == "VN":
            return "VN1_04_003QMX;"
        if c == "ID":
            return "ID020;"
        return "?;"

    def _serve(self):
        buf = bytearray()
        while not self._stop.is_set():
            r, _, _ = select.select([self.master], [], [], 0.05)
            if r:
                try:
                    chunk = os.read(self.master, 4096)
                except OSError:
                    return
                with self.lock:
                    self.raw += chunk
                buf += chunk
                out = ""
                while (end := buf.find(b";")) >= 0:
                    c = buf[:end].decode("ascii", "replace")
                    del buf[:end + 1]
                    if not c:
                        continue
                    with self.lock:
                        now = time.monotonic()
                        self.cmds.append((now, c))
                        self.last_cmd_t = now
                        out += self._reply(c)
                if out:
                    with self.lock:
                        if self._stop.is_set():
                            return      # stopped: the fd may already belong to another pty
                        try:
                            os.write(self.master, out.encode())
                        except OSError:
                            return
            with self.lock:   # the radio's CAT timeout
                s = self.state
                if (s["tx"] and s["qb"] == "1"
                        and time.monotonic() - self.last_cmd_t > int(s["qc"])):
                    s["tx"] = 0
                    self.timeout_unkeys += 1

    def start(self):
        t = threading.Thread(target=self._serve, daemon=True)
        t.start()
        self._threads.append(t)
        return self

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(10)
        if any(t.is_alive() for t in self._threads):
            # Never close fds a live thread still uses: the next fake's openpty
            # would reuse the numbers and this thread would read its frames.
            return
        for fd in (self.master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass


if __name__ == "__main__":
    f = FakeQmx().start()
    print(f"fake QMX on {f.path}  (Ctrl-C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        f.stop()
