"""QMX support at the wire: the real QmxRadio code against tests/fake_qmx.py.

Each scenario starts the fake QMX on a pseudo-terminal and server.py --rig qmx
on it (TX allowed; nothing here ever opens real hardware), drives the server
over rigctl the way JTDX does, and asserts on the CAT commands the fake got.

    .venv/bin/python tests/qmx_test.py          (ONLY=<name part> to pick one)
"""
import asyncio
import os
import signal
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from fake_qmx import FakeQmx  # noqa: E402
from testkit import free_port, wait_for  # noqa: E402

TX_TIMEOUT = 8
CAT_TIMEOUT_S = 5   # qmx.CAT_TIMEOUT_S
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} got {got!r}" + ("" if ok else f" want {want!r}"))
    return ok


def check_true(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'ok  ' if cond else 'FAIL'}  {name:58} {detail}")
    return cond


def expect(label, get, want, timeout=8.0):
    """check() that waits (up to timeout) for get() to equal want: for a
    state the server reaches on its own time. Never a sleep-then-look."""
    wait_for(lambda: get() == want, timeout)
    return check(label, get(), want)


class Client:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.f = self.s.makefile("r")

    def cmd(self, line, nlines=1):
        self.s.sendall((line + "\n").encode())
        return [self.f.readline().strip() for _ in range(nlines)]

    def close(self):
        self.f.close()
        self.s.close()


class Rig:
    """fake QMX + server.py on it, for one scenario. lane: also wait for the
    Winlink rigctl lane's listener (pass its --winlink-rigctl-port in extra)."""

    def __init__(self, *extra, lane=False):
        self.fails_at_start = fails
        self.fake = FakeQmx().start()
        self.rport, self.web = free_port(), free_port()
        self.out = None
        self.proc = subprocess.Popen(
            [sys.executable, "server.py", "--rig", "qmx", "--port", self.fake.path, "--no-audio",
             "--listen", str(self.web), "--host", "127.0.0.1", "--rigctl-host", "127.0.0.1",
             "--rigctl-port", str(self.rport),
             "--tx-timeout", str(TX_TIMEOUT), "-v", *extra],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.lines = []                    # the server's log, read as it is written
        self._log_reader = threading.Thread(target=self._read_log, daemon=True)
        self._log_reader.start()
        # Ready = listening, the hello done (QB1 written: the radio's CAT
        # timeout armed), and the poll loop has handled an IF, FA and FB
        # reply since (the server knows mode, TX and both VFOs):
        # waited for, not slept on (Python + aiohttp start slowly on a
        # 2-CPU runner).
        up = (self.wait_log("rigctl listener on", timeout=30)
              and (not lane or self.wait_log("rigctl winlink listener on", timeout=30))
              and wait_for(lambda: "QB1" in self.fake.sent(), 30)
              and all(self.told(m, cmd, n=1, timeout=30)
                      for m in [self.fake.mark()] for cmd in ("IF", "FA", "FB")))
        if not up:
            check_true("server started and polled the radio", False, "".join(self.lines)[-800:])

    def _read_log(self):
        for line in self.proc.stdout:
            self.lines.append(line)

    def log_mark(self):
        return len(self.lines)

    def wait_log(self, text, since=0, timeout=10.0):
        """Wait for a server log line containing text, after line `since`."""
        return wait_for(lambda: any(text in l for l in self.lines[since:]), timeout)

    def told(self, since, cmd, n=2, timeout=15.0):
        """Wait until the fake has answered n reads of one kind (IF, SW, ...)
        after `since`, a mark taken right after a change, and the server has
        handled them. Answered isn't handled: the reply may still be in the
        pty. The server's pump writes its next command once a reply is
        handled (or after 100 ms), so three more commands from it after the
        n-th answer mean the reply was taken in."""
        if not wait_for(lambda: self.fake.count(cmd, since) >= n, timeout):
            return False
        k = self.fake.count()
        return wait_for(lambda: self.fake.count() >= k + 3, timeout)

    def client(self):
        return Client(self.rport)

    def end(self, sig=signal.SIGTERM, timeout=10):
        self.proc.send_signal(sig)
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self._log_reader.join(timeout=5)
        self.out = "".join(self.lines)
        # The server's last writes (unkey, CAT timeout restore) may still be
        # in the pty: let the fake read them all before anything looks.
        self.fake.quiesce()
        return self.out

    def close(self):
        if self.out is None:
            self.end()
        self.fake.stop()
        out = self.out
        if "Traceback" in out:
            check_true("no Traceback in server log", False, out[-800:])
        else:
            check_true("no Traceback in server log", True)
        raw = bytes(self.fake.raw)
        check("no CR/LF ever written (terminal-mode trigger)", b"\r" in raw or b"\n" in raw, False)
        bad = [c for c in self.fake.sent() if c.startswith(("MD8", "PS0", "SS1"))]
        check("never MD8 (SWR tune), PS0 (power off) or SS1 (two-tone test)", bad, [])
        if fails > self.fails_at_start:    # the context a failing check needs, in CI's log too
            lines = [l.rstrip() for l in out.splitlines() if "aiohttp.access" not in l]
            print("      server log (last 40 lines):\n" + "\n".join("      | " + l for l in lines[-40:]))
            print(f"      fake: last 30 commands: {self.fake.sent()[-30:]}")


def test_connect_and_hello():
    print("\n# connect: firmware read, radio's CAT timeout armed, state read")
    r = Rig()
    try:
        sent = r.fake.sent()
        check("VN read", "VN" in sent, True)
        check("QB/QC read before being changed",
              sent.index("QB") < sent.index("QB1") and sent.index("QC") < sent.index(f"QC{CAT_TIMEOUT_S}"),
              True)
        check("radio CAT timeout on", (r.fake.state["qb"], r.fake.state["qc"]), ("1", str(CAT_TIMEOUT_S)))
        c = r.client()
        check("rigctl f", c.cmd("f"), ["14074000"])
        check("rigctl m (MD6 = DiGi USB)", c.cmd("m", 2)[0], "PKTUSB")
        c.close()
    finally:
        r.close()
    check("radio's own CAT timeout restored", (r.fake.state["qb"], r.fake.state["qc"]), ("0", "010"))


def test_freq_and_modes():
    print("\n# frequency and mode sets, as JTDX sends them")
    r = Rig()
    try:
        c = r.client()
        m = r.fake.mark()
        check("F 7074000", c.cmd("F 7074000"), ["RPRT 0"])
        wait_for(lambda: "FA00007074000" in r.fake.sent(m) and r.fake.state["fa"] == 7_074_000)
        check("FA written, 11 digits", "FA00007074000" in r.fake.sent(m), True)
        check("radio on 7074000", r.fake.state["fa"], 7_074_000)
        check("rigctl f follows", c.cmd("f"), ["7074000"])
        for hl, md in (("USB", "2"), ("LSB", "1"), ("CW", "3"), ("PKTLSB", "9"), ("PKTUSB", "6")):
            m = r.fake.mark()
            c.cmd(f"M {hl} 0")
            expect(f"M {hl} -> MD{md}", lambda: (f"MD{md}" in r.fake.sent(m), r.fake.state["md"]), (True, md))
        check("rigctl m reads back PKTUSB", c.cmd("m", 2)[0], "PKTUSB")
        check("AM refused (not claimed)", c.cmd("M AM 0")[0].startswith("RPRT -"), True)
        c.close()
    finally:
        r.close()


def test_front_panel_change_seen():
    print("\n# a change made at the radio shows up (no app connected, so no hold)")
    r = Rig()
    try:
        m = r.fake.inject(fa=10_136_000, md="3")
        # The server has read FA and IF (the mode) since, before any app connects.
        check("server read the radio since the change", r.told(m, "FA") and r.told(m, "IF"), True)
        c = r.client()
        check("freq from the radio", c.cmd("f"), ["10136000"])
        check("mode from the radio", c.cmd("m", 2)[0], "CW")
        c.close()
    finally:
        r.close()


def test_poll_rate_capped():
    print("\n# polls are paced (the real QMX answers in ~2 ms); commands are not")
    r = Rig()
    try:
        m = r.fake.mark()
        time.sleep(2.0)                  # a measuring window, not a wait for readiness
        polls = [c for c in r.fake.sent(m) if len(c) == 2]
        rate = len(polls) / 2.0
        check_true("idle poll rate at most 25/s", rate <= 25, f"{rate:.0f}/s")
        check_true("still polling (S-meter and IF seen)", "SM" in polls and "IF" in polls,
                   f"{sorted(set(polls))}")
        c = r.client()
        t = time.monotonic()
        check("T 1", c.cmd("T 1"), ["RPRT 0"])
        deadline = time.monotonic() + 1
        while not r.fake.ptt(t) and time.monotonic() < deadline:
            time.sleep(0.005)
        ptt = r.fake.ptt(t)
        check_true("key reaches the wire within 100 ms", bool(ptt) and ptt[0][0] - t < 0.1,
                   f"{(ptt[0][0] - t) * 1000:.0f} ms" if ptt else "never")
        check("T 0", c.cmd("T 0"), ["RPRT 0"])
        expect("radio receiving", lambda: r.fake.state["tx"], 0)
        c.close()
    finally:
        r.close()


def test_ptt_and_meters():
    print("\n# PTT through Station.key; TX meters; strict key/unkey order")
    r = Rig()
    try:
        c = r.client()
        m = r.fake.mark()
        check("T 1", c.cmd("T 1"), ["RPRT 0"])
        expect("radio transmitting", lambda: r.fake.state["tx"], 1)
        # The meters are cached from TX polls: wait until the server has read
        # both (answered while keyed) and handled them.
        k = r.fake.mark()
        check("server read SW and PC while keyed", r.told(k, "SW", 1) and r.told(k, "PC", 1), True)
        check("rigctl t", c.cmd("t"), ["1"])
        swr = float(c.cmd("l SWR")[0])
        check_true("SWR 1.50 reads ~1.5", 1.4 <= swr <= 1.6, f"{swr}")
        po = float(c.cmd("l RFPOWER_METER")[0])
        check_true("4.5 W of 5 reads as most of full scale", 0.75 <= po <= 1.0, f"{po}")
        check("T 0", c.cmd("T 0"), ["RPRT 0"])
        expect("radio receiving", lambda: r.fake.state["tx"], 0)
        check("PTT on the wire", [v for _, v in r.fake.ptt(m)], [1, 0])
        m = r.fake.mark()
        for line in ("T 1", "T 0", "T 1", "T 0"):
            c.cmd(line)
        wait_for(lambda: len(r.fake.ptt(m)) >= 4 and r.fake.state["tx"] == 0, 10)
        time.sleep(0.3)                  # quiet window: nothing more after the fourth
        check("back-to-back PTT keeps order", [v for _, v in r.fake.ptt(m)], [1, 0, 1, 0])
        check("ends unkeyed", r.fake.state["tx"], 0)
        c.close()
    finally:
        r.close()


def test_tune_mode_blocks_key():
    print("\n# radio in SWR tune mode (MD8, started at the radio): no CAT key")
    r = Rig()
    try:
        m = r.fake.inject(md="8")
        check("server read IF (MD8) since the change", r.told(m, "IF"), True)
        c = r.client()
        m = r.fake.mark()
        check("T 1 refused while tuning", c.cmd("T 1")[0].startswith("RPRT -"), True)
        time.sleep(0.3)                  # quiet window: nothing may be written in it
        check("no key on the wire", r.fake.ptt(m), [])
        c.close()
        r.fake.inject(md="6")
    finally:
        r.close()


def test_split():
    print("\n# real split: SP1, TX frequency to VFO B")
    r = Rig()
    try:
        c = r.client()
        m = r.fake.mark()
        check("S 1 VFOB", c.cmd("S 1 VFOB"), ["RPRT 0"])
        check("I 14076000", c.cmd("I 14076000"), ["RPRT 0"])
        wait_for(lambda: {"SP1", "FB00014076000"} <= set(r.fake.sent(m))
                 and (r.fake.state["sp"], r.fake.state["fb"]) == (1, 14_076_000))
        sent = r.fake.sent(m)
        check("SP1 written", "SP1" in sent, True)
        check("FB written", "FB00014076000" in sent, True)
        check("radio split, B on TX freq", (r.fake.state["sp"], r.fake.state["fb"]), (1, 14_076_000))
        check("rigctl s", c.cmd("s", 2), ["1", "VFOB"])
        check("VFO A untouched", r.fake.state["fa"], 14_074_000)
        c.close()
    finally:
        r.close()


def test_sigterm_while_keyed():
    print("\n# shutdown while we're keyed: unkey written")
    r = Rig()
    try:
        c = r.client()
        c.cmd("T 1")
        expect("transmitting", lambda: r.fake.state["tx"], 1)
        t = time.monotonic()
        r.end()
        ptt = r.fake.ptt()
        check("last PTT is OFF, written after SIGTERM", (ptt[-1][1], ptt[-1][0] >= t), (0, True))
        check("radio receiving", r.fake.state["tx"], 0)
    finally:
        r.close()


def test_manual_tx_left_alone():
    print("\n# shutdown during a transmission started at the radio: left alone")
    r = Rig()
    try:
        m = r.fake.inject(tx=1)
        check("server read IF (keyed at the radio) since", r.told(m, "IF"), True)
        t = time.monotonic()
        r.end()
        check("no unkey written", r.fake.ptt(t), [])
    finally:
        r.close()


def test_server_killed_radio_unkeys_itself():
    print(f"\n# server SIGKILLed while keyed: the QMX's CAT timeout ({CAT_TIMEOUT_S} s) unkeys it")
    r = Rig()
    try:
        c = r.client()
        c.cmd("T 1")
        expect("transmitting", lambda: r.fake.state["tx"], 1)
        r.end(signal.SIGKILL)
        # The fake unkeys CAT_TIMEOUT_S after the last command: wait for that,
        # with room for a slow runner, rather than sleeping a guess past it.
        expect("radio back on receive by itself", lambda: (r.fake.state["tx"], r.fake.timeout_unkeys), (0, 1),
               timeout=CAT_TIMEOUT_S + 10)
    finally:
        r.close()


def ws_send(w, **m):
    import json
    return w.send_str(json.dumps(m))


async def until(pred, timeout=10.0):
    """wait_for() off the event loop: the page's WebSocket stays serviced."""
    return await asyncio.to_thread(wait_for, pred, timeout)


def test_winlink_ssb_source():
    print("\n# Winlink session (--winlink-mode USB): USB mode, SSB audio from USB, then back")
    import aiohttp
    lane = free_port()
    r = Rig("--winlink-rigctl-port", str(lane), "--winlink-mode", "USB", lane=True)
    try:
        check("starts in DiGi with the mic as SSB source", (r.fake.state["md"], r.fake.state["ss"]), ("6", "2"))
        mode_ss = lambda: (r.fake.state["md"], r.fake.state["ss"])  # noqa: E731

        async def session():
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(f"http://127.0.0.1:{r.web}/ws") as w:
                    await w.receive()
                    n = r.log_mark()
                    await ws_send(w, t="winlink_arm", v=True)
                    # Armed when the server says so; SS0 is queued just before
                    # that line, so the radio may still be getting it.
                    armed = await asyncio.to_thread(r.wait_log, "winlink: session armed", n, 15)
                    await until(lambda: mode_ss() == ("2", "0"))
                    during = (armed, mode_ss())
                    c = Client(lane)
                    keyed = c.cmd("T 1")
                    await until(lambda: r.fake.state["tx"] == 1)
                    tx = r.fake.state["tx"]
                    c.cmd("T 0")
                    c.close()
                    await until(lambda: r.fake.state["tx"] == 0)
                    await ws_send(w, t="winlink_arm", v=False)
                    await until(lambda: mode_ss() == ("6", "2"))
                    return during, keyed, tx, mode_ss()
        during, keyed, tx, after = asyncio.run(session())
        check("armed: USB (MD2), SSB audio from USB (SS0)", during, (True, ("2", "0")))
        check("the lane keys it", (keyed, tx), (["RPRT 0"], 1))
        check("disarmed: DiGi and the mic again", after, ("6", "2"))
    finally:
        r.close()


def test_page_voice_ssb_source():
    print("\n# page voice in plain USB: SSB audio from USB (SS0) while keyed, then back")
    # Review 2026-10-18 (Sol, verified): page voice keyed with SS still 2 (the
    # external mic): 0 W from the page. Winlink already switched it; voice now does.
    import aiohttp
    r = Rig()
    try:
        tx_ss = lambda: (r.fake.state["tx"], r.fake.state["ss"])  # noqa: E731

        async def hold(w, pred, mic, timeout=12.0):
            """Hold the page's PTT (key-down re-sent every 0.5 s, the page's
            keep-alive) until pred() holds or timeout; True if it held."""
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                await ws_send(w, t="set", k="tx", v=True, mic=mic)
                if await until(pred, 0.5):
                    return True
            return pred()

        async def go():
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(f"http://127.0.0.1:{r.web}/ws") as w:
                    await w.receive()
                    await ws_send(w, t="set", k="mode", v="USB")
                    await ws_send(w, t="set", k="data", v=False)
                    await until(lambda: r.fake.state["md"] == "2")
                    m = r.fake.mark()
                    told = await asyncio.to_thread(r.told, m, "IF")
                    before = (told, r.fake.state["md"], r.fake.state["ss"])
                    await hold(w, lambda: tx_ss() == (1, "0"), mic=True)
                    during = tx_ss()
                    await ws_send(w, t="set", k="tx", v=False)
                    await until(lambda: tx_ss() == (0, "2"))
                    after = tx_ss()
                    await hold(w, lambda: r.fake.state["tx"] == 1, mic=False)
                    no_mic = tx_ss()
                    await ws_send(w, t="set", k="tx", v=False)
                    await until(lambda: r.fake.state["tx"] == 0)
                    return before, during, after, no_mic
        before, during, after, no_mic = asyncio.run(go())
        check("plain USB with the mic jack as SSB source", before, (True, "2", "2"))
        check("keyed with SSB audio from USB (once the switch lands)", during, (1, "0"))
        check("released: SS2 again", after, (0, "2"))
        check("page mic off: keys, source left on the mic jack", no_mic, (1, "2"))
    finally:
        r.close()


TESTS = [test_connect_and_hello, test_freq_and_modes, test_front_panel_change_seen, test_poll_rate_capped,
         test_ptt_and_meters, test_tune_mode_blocks_key, test_split,
         test_sigterm_while_keyed, test_manual_tx_left_alone,
         test_server_killed_radio_unkeys_itself, test_winlink_ssb_source,
         test_page_voice_ssb_source]

if __name__ == "__main__":
    only = os.environ.get("ONLY")
    picked = [t for t in TESTS if not only or only in t.__name__]
    if not picked:   # a filter that runs nothing must not read as a pass
        print(f"FAIL  ONLY={only!r} matches no scenario in {[t.__name__ for t in TESTS]}")
        sys.exit(2)
    for t in picked:
        t()
    print("\nPASS" if not fails else f"\n{fails} FAILED")
    sys.exit(1 if fails else 0)
