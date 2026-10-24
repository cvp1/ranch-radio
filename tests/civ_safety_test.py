"""Transmit-safety tests at the wire: the real CivRadio code against a fake radio.

Each scenario starts tests/fake_civ.py on a pseudo-terminal and server.py with
--port pointing at it (TX allowed; nothing here ever opens real hardware), then
drives the server over rigctl and asserts on the CI-V frames the fake received.

    .venv/bin/python tests/civ_safety_test.py [g90|ic7300]
"""
import asyncio
import os
import random
import signal
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from fake_civ import FakeCiv  # noqa: E402
from testkit import free_port, wait_for  # noqa: E402
import rigs  # noqa: E402

RIG = sys.argv[1] if len(sys.argv) > 1 else "g90"
TX_TIMEOUT = 8
fails = 0
logs = []


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


class Client:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.f = self.s.makefile("r")

    def cmd(self, line, nlines=1):
        self.s.sendall((line + "\n").encode())
        return [self.f.readline().strip() for _ in range(nlines)]

    def burst(self, lines):
        """Send several commands in one write; read one reply line each."""
        self.s.sendall(("".join(l + "\n" for l in lines)).encode())
        return [self.f.readline().strip() for _ in lines]

    def close(self):
        self.f.close()
        self.s.close()


class Rig:
    """fake radio + server.py on it, for one scenario."""

    def __init__(self, fake_kw=None, extra=()):
        self.fails_at_start = fails
        self.fake = FakeCiv(RIG, **(fake_kw or {})).start()
        self.rport, self.web = free_port(), free_port()
        self.proc = subprocess.Popen(
            [sys.executable, "server.py", "--rig", RIG, "--port", self.fake.path, "--no-audio",
             "--listen", str(self.web), "--host", "127.0.0.1", "--rigctl-host", "127.0.0.1",
             "--rigctl-port", str(self.rport),
             "--tx-timeout", str(TX_TIMEOUT), *extra],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.lines = []                    # the server's log, read as it is written
        self._log_reader = threading.Thread(target=self._read_log, daemon=True)
        self._log_reader.start()
        # Ready = listening for rigctl and the radio has answered it: waited
        # for, not slept on (Python + aiohttp start slowly on a 2-CPU runner).
        up = (self.wait_log("rigctl listener on", timeout=30)
              and wait_for(lambda: len(self.fake.answered) >= 6, 30) and self.fake.caught_up())
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

    def client(self):
        return Client(self.rport)

    def told(self, since, n=3, timeout=15.0, poll=(0x1C, b"\x00")):
        """Wait until the fake has answered n polls of one kind (default PTT
        status; TUNER, SPLIT, MODE below) after `since`, a mark taken right
        after a change: the server has been told the new state n times. Its
        'our key is over' guard needs two receive replies in a row; 3 covers it."""
        return fake_told(self.fake, since, n, poll, timeout, web=self.web)

    def keyed(self, c, grace_s=1.3):
        """T 1 on client c, then wait until the radio is keyed, the server has
        been told so, and grace_s has passed since the key: the server
        ignores a 'not transmitting' reply for 1 s after keying (a reply
        that crossed the key command), so a test of what comes after must
        start past it. A minimum wait, not a guess at readiness."""
        t0 = time.monotonic()
        ok = c.cmd("T 1") == ["RPRT 0"] and wait_for(lambda: self.fake.state["tx"] == 1, 5)
        t = self.fake.mark()
        ok = ok and self.told(t)
        time.sleep(max(0.0, grace_s - (time.monotonic() - t0)))
        return ok

    def stale_reply(self):
        """One 'not transmitting' reply that isn't true; returns once the
        server has had it and three true replies after it."""
        self.fake.stale_tx_reply_once()
        wait_for(lambda: self.fake._stale_tx == 0, 10)
        return self.told(self.fake.mark())

    def settle(self, c, hold_s=0.8, timeout=15.0):
        """Wait until the server's view (read through rigctl client c: f, t)
        matches the fake radio and keeps matching for hold_s, and every
        frame sent by then is answered (no out-of-date reply left to land
        late). The barrier to use instead of a sleep
        before taking a mark or checking state: with the fake's ack delay a
        reply queued before a change lands after it (CI, 2026-10-23).
        Reads only; never opens a client of its own (a client leaving
        clears emulated split). Not while keyed on an emulated split: f
        then reads the RX frequency while the radio sits on TX. False if
        it never settles; check it, don't ignore it."""
        end, since = time.monotonic() + timeout, None
        while time.monotonic() < end:
            st = self.fake.state           # waits until everything sent is answered
            want = [str(st["freq"]), str(st["tx"])]
            same = [c.cmd("f")[0], c.cmd("t")[0]] == want
            if not same:
                since = None
            elif since is None:
                since = time.monotonic()
            elif time.monotonic() - since >= hold_s:
                return self.fake.caught_up()
            time.sleep(0.05)
        return False

    def sigterm(self, timeout=10):
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self._log_reader.join(timeout=5)
        self.out = "".join(self.lines)
        return self.out

    def close(self):
        if getattr(self, "out", None) is None:
            self.sigterm()
        out = self.out
        self.fake.stop()
        logs.append(out)
        if fails > self.fails_at_start:    # the context a failing check needs, in CI's log too
            lines = [l.rstrip() for l in out.splitlines() if "aiohttp.access" not in l]
            print("      server log (last 40 lines):\n" + "\n".join("      | " + l for l in lines[-40:]))
        if "Traceback" in out:
            check_true("no Traceback in server log", False, out[-800:])
        return out


def browser(web, msgs, wait=0.6, until=None):
    """Open the browser WebSocket, send msgs, return the notices it got.
    The page stays connected for `wait` s, or with `until` (a predicate)
    until it holds (up to `wait`): check what the page's messages did
    while it is still connected, since closing the page ends what it
    started (a Winlink session disarms). until="notice": until the first
    notice arrives (a refusal is checked by its notice, so wait for it).
    browser.held says whether `until` held before the page closed."""
    import aiohttp, json

    async def go():
        notes = []
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(f"http://127.0.0.1:{web}/ws") as ws:
                await ws.receive(timeout=3)              # hello
                for m in msgs:
                    await ws.send_str(json.dumps(m))
                end = time.monotonic() + wait
                browser.held = False
                while time.monotonic() < end:
                    if until == "notice" and notes or (callable(until) and await asyncio.to_thread(until)):
                        browser.held = True
                        break
                    try:
                        msg = await ws.receive(timeout=max(0.05, min(0.1, end - time.monotonic())) if until
                                               else max(0.05, end - time.monotonic()))
                    except asyncio.TimeoutError:
                        if until is None:
                            break
                        continue                 # quiet for 0.1 s: look at `until` again
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        break
                    d = json.loads(msg.data)
                    if d.get("t") == "notice":
                        notes.append(d["msg"])
        return notes
    return asyncio.run(go())


TUNER, SPLIT, MODE = (0x1C, b"\x01"), (0x0F, b""), (0x26, b"\x00")   # polls, for Rig.told


def fake_ready(fake):
    """The server has polled the fake and every frame so far is answered."""
    return wait_for(lambda: len(fake.answered) >= 6, 30) and fake.caught_up()


def data_poll(rig):
    """The poll that reads a rig's DATA mode: 26 00 where the profile has it
    (G90), else 1A 06 (IC-7300) -- see civ.poll_plan."""
    return MODE if rigs.get(rig).get("mode_cmd_26") else (0x1A, b"\x06")


POLL_KEYS = {(0x1C, b"\x00"): ("tx",), (0x1C, b"\x01"): ("tuner",), (0x0F, b""): ("split",),
             (0x26, b"\x00"): ("mode", "data"), (0x1A, b"\x06"): ("mode", "data")}


def _same(k, server_v, fake_v):
    if k in ("tx", "split", "data"):
        return bool(server_v) == bool(fake_v)
    return server_v == fake_v


def fake_told(fake, since, n=1, poll=None, timeout=15.0, web=None):
    """The server has had, and handled, n replies to one poll (default the
    DATA-mode read) since `since`. Answered isn't handled: the reply may
    still be in the pty. The server's pump writes its next frame only once
    a reply is handled (or after 80 ms), so three more frames from it after
    the n-th answer mean the reply was taken in (seen 2026-10-23: armed
    before the plain-USB reply it had been sent was read)."""
    poll = poll or data_poll(fake.rig)
    if not wait_for(lambda: fake.status_replies_since(since, *poll) >= n, timeout):
        return False
    k = len(fake.frames)
    if not (wait_for(lambda: len(fake.frames) >= k + 3, timeout) and fake.caught_up()):
        return False
    if web is None:
        return True
    # The causal proof: the server's own state (what a page sees) says so.
    # Frames alone aren't: under load the pump stops waiting for a reply
    # after 80 ms and sends on, so frames can pass a reply still unread
    # (armed in plain USB under a loaded hunt, 2026-10-23).
    keys = POLL_KEYS.get(tuple(poll), ())
    return wait_for(lambda: (lambda st: all(_same(k, st.get(k), fake.state[k]) for k in keys))(server_state(web)),
                    timeout, step=0.1)


def expect(label, get, want, timeout=8.0):
    """check() that waits (up to timeout) for get() to equal want: for a
    state the server reaches on its own time. Never a sleep-then-look."""
    wait_for(lambda: get() == want, timeout)
    check(label, get(), want)


def keyed_retunes(seq):
    """Frequency sets written while the wire says the radio is keyed."""
    keyed, bad = False, []
    for kind, v in seq:
        if kind == "ptt":
            keyed = bool(v)
        elif keyed:
            bad.append(v)
    return bad


# --- F1: shutdown must leave the radio unkeyed ------------------------------

def test_f1_sigterm_while_keyed():
    print("\n# F1 shutdown while keyed (SIGTERM)")
    r = Rig()
    try:
        c = r.client()
        check("T 1 keys", c.cmd("T 1"), ["RPRT 0"])
        wait_for(lambda: r.fake.state["tx"] == 1, 5)
        check("fake radio is transmitting", r.fake.state["tx"], 1)
        t_term = time.monotonic()
        r.sigterm()
        r.fake.quiesce()
        ptt = r.fake.ptt_frames()
        check("last PTT frame on the wire is OFF", ptt[-1][1] if ptt else None, 0)
        check("OFF written after SIGTERM", any(v == 0 and t >= t_term for t, v in ptt), True)
        check("fake radio not transmitting after shutdown", r.fake.state["tx"], 0)
        c.close()
    finally:
        r.close()


def test_f1_unit_unkey_then_stop():
    print("\n# F1 unit: Station.unkey() then CivRadio.stop() writes OFF")
    import rigs
    from civ import CivRadio

    async def run():
        radio = CivRadio(rigs.get(RIG), "/nonexistent")
        rfd, wfd = os.pipe()
        os.set_blocking(rfd, False)
        radio.fd = wfd
        await radio.set("tx", True)        # queued, as it is between pump slots
        await radio.set("tx", False)       # what Station.unkey() does at cleanup
        await radio.stop()
        try:
            data = os.read(rfd, 4096)
        except BlockingIOError:
            data = b""
        os.close(rfd)
        return data

    data = asyncio.run(run())
    check("an OFF frame (1C 00 00) was written", b"\x1c\x00\x00\xfd" in data, True)

    async def run_sent():
        radio = CivRadio(rigs.get(RIG), "/nonexistent")
        rfd, wfd = os.pipe()
        os.set_blocking(rfd, False)
        radio.fd = wfd
        radio._write(radio._frame(0x1C, b"\x00\x01"))   # ON already on the wire
        os.read(rfd, 4096)
        radio._update(tx=False)            # e.g. a stale poll said 'not transmitting'
        await radio.stop()
        try:
            data = os.read(rfd, 4096)
        except BlockingIOError:
            data = b""
        os.close(rfd)
        return data

    try:
        data = asyncio.run(run_sent())
        check("ON sent, state says rx: stop() still writes OFF", b"\x1c\x00\x00\xfd" in data, True)
    except AttributeError as e:
        check_true("ON sent, state says rx: stop() still writes OFF", False, f"({e})")


def test_f1_manual_tx_left_alone_on_shutdown():
    print("\n# F1 + the operator's rule: shutdown never cuts a manual (front-panel/mic) TX")
    r = Rig()
    try:
        t = r.fake.inject(tx=1)                # keyed at the radio; no CAT key from us
        check("server told of the manual TX", r.told(t), True)
        m = r.fake.mark()
        r.sigterm()
        r.fake.quiesce()
        check("no OFF written for a manual transmission", [v for _, v in r.fake.ptt_frames(m)], [])
        check("radio still transmitting (operator's call)", r.fake.state["tx"], 1)
    finally:
        r.close()


def test_f1_manual_tx_after_our_key_left_alone():
    print("\n# F1 + the operator's rule: our key ended at the radio, then a manual TX -> not cut")
    r = Rig()
    try:
        c = r.client()
        check("T 1 keys", c.cmd("T 1"), ["RPRT 0"])
        check("server sees our TX", r.settle(c), True)
        time.sleep(1.0)                    # past the 1 s stale-reply grace after our key
        n = r.log_mark()
        t = r.fake.inject(tx=0)                # unkeyed at the front panel
        check("server told of the unkey", r.told(t) and r.settle(c), True)
        check("server released our key (after its confirm window)",
              r.wait_log("unkeyed at the radio; releasing PTT", n), True)
        t = r.fake.inject(tx=1)                # operator keys the mic by hand
        check("server told of the manual TX", r.told(t) and r.settle(c), True)
        m = r.fake.mark()
        r.sigterm()
        r.fake.quiesce()
        check("no OFF written at shutdown", [v for _, v in r.fake.ptt_frames(m)], [])
        check("radio still transmitting (operator's call)", r.fake.state["tx"], 1)
        c.close()
    finally:
        r.close()


# --- F2: PTT frames are never coalesced or reordered ------------------------

def test_f2_back_to_back_ptt():
    print("\n# F2 back-to-back T 1 / T 0 / T 1 / T 0")
    r = Rig(fake_kw={"ack_delay_s": 0.2})
    try:
        c = r.client()
        m = r.fake.mark()
        check("burst replies", c.burst(["T 1", "T 0", "T 1", "T 0"]), ["RPRT 0"] * 4)
        # Wait for the fake to apply all four (each waits ack_delay_s); a fixed
        # 1.5 s sleep raced its worker under load (review 2026-10-18, both).
        wait_for(lambda: [v for k, v in r.fake.sets_since(m) if k == "ptt"] == [1, 0, 1, 0]
                 and r.fake.state["tx"] == 0, 6)
        seq = [v for k, v in r.fake.sets_since(m) if k == "ptt"]
        check("wire PTT order", seq, [1, 0, 1, 0])
        check("radio ends unkeyed", r.fake.state["tx"], 0)
        c.close()
    finally:
        r.close()


def test_f2_split_hop_order():
    if RIG != "g90":
        return
    print("\n# F2 emulated split: TX hop before ON, RX restore after OFF")
    r = Rig(fake_kw={"ack_delay_s": 0.2})
    try:
        c = r.client()
        c.cmd("S 1 VFOB")
        c.cmd("F 7074000")
        c.cmd("I 7076000")
        check("RX settled on 7074000 before keying", r.settle(c) and r.fake.state["freq"] == 7074000, True)
        m = r.fake.mark()
        ptts = lambda: [v for k, v in r.fake.sets_since(m) if k == "ptt"]
        check("burst replies", c.burst(["T 1", "T 0", "I 7075000", "T 1"]), ["RPRT 0"] * 4)
        wait_for(lambda: ptts() == [1, 0, 1] and r.fake.state["tx"] == 1, 10)
        check("T 0", c.cmd("T 0"), ["RPRT 0"])
        wait_for(lambda: ptts() == [1, 0, 1, 0] and r.fake.state["freq"] == 7074000, 10)
        r.fake.caught_up()
        seq = r.fake.sets_since(m)
        print("      wire:", seq)
        check("wire PTT order", [v for k, v in seq if k == "ptt"], [1, 0, 1, 0])
        check("no retune while keyed", keyed_retunes(seq), [])
        first_on = next((i for i, x in enumerate(seq) if x == ("ptt", 1)), None)
        check("TX hop (7076000) precedes the first ON",
              first_on is not None and ("freq", 7076000) in seq[:first_on], True)
        check("radio back on RX, unkeyed", (r.fake.state["tx"], r.fake.state["freq"]), (0, 7074000))
        c.close()
    finally:
        r.close()


# --- F3: no retune while transmitting ---------------------------------------

def test_f3_no_retune_while_keyed():
    print("\n# F3 frequency/mode change refused while keyed")
    r = Rig()
    try:
        a, b = r.client(), r.client()
        a.cmd("F 14074000")
        check("settled on 14074000", r.settle(a), True)
        m = r.fake.mark()
        check("A: T 1", a.cmd("T 1"), ["RPRT 0"])
        check("server sees the key", r.settle(a), True)
        check("B: F 7074000 while keyed -> refused", b.cmd("F 7074000"), ["RPRT -11"])
        check("B: F to the current RX (no change) -> ok", b.cmd("F 14074000"), ["RPRT 0"])
        check("B: M LSB while keyed -> refused", b.cmd("M LSB 0"), ["RPRT -11"])
        notes = browser(r.web, [{"t": "set", "k": "freq", "v": 7074000}], wait=10, until="notice")
        check_true("browser freq set while keyed -> refused with a notice",
                   any("transmit" in n.lower() for n in notes), str(notes))
        check("radio still on 14074000 USB", (r.fake.state["freq"], r.fake.state["mode"]), (14074000, "USB"))
        check("A: T 0", a.cmd("T 0"), ["RPRT 0"])
        check("radio unkeyed, and the server knows", r.settle(a), True)
        check("no frequency set on the wire while keyed", keyed_retunes(r.fake.sets_since(m)), [])
        check("after unkey: F 7074000 accepted", b.cmd("F 7074000"), ["RPRT 0"])
        expect("radio moved to 7074000", lambda: r.fake.state["freq"], 7074000)
        a.close(); b.close()
    finally:
        r.close()


def test_f3_jtdx_sequence_still_works():
    print("\n# F3 JTDX-shaped split cycle still passes")
    r = Rig()
    try:
        c = r.client()
        seq = ["S VFOA 1 VFOB", "F VFOA 14074000", "I VFOA 14075500", "T VFOA 1", "t VFOA",
               "T VFOA 0", "F VFOA 14074000", "M VFOA PKTUSB 0"]
        got = []
        for line in seq:
            got += c.cmd(line)
            time.sleep(0.4 if line.startswith("T") else 0.1)
        check("every reply ok", [g for g in got if g.startswith("RPRT") and g != "RPRT 0"], [])
        c.cmd("S VFOA 0 VFOA")
        c.close()
    finally:
        r.close()


def test_f3_band_change_during_tune_waits():
    print("\n# F3 (the operator 2026-10-14): an app's band change during a tune waits for the tune to end")
    r = Rig(fake_kw={"tune_s": None})      # the test ends the tune, not a timer
    try:
        c = r.client()
        c.cmd("F 14074000")
        check("settled on 14074000", r.settle(c), True)
        check("G TUNE", c.cmd("G TUNE"), ["RPRT 0"])
        t = r.fake.mark()
        check("server told the radio is tuning", wait_for(lambda: r.fake.state["tuner"] == 2)
              and r.told(t, n=1, poll=TUNER), True)
        m = r.fake.mark()
        check("F 7074000 during the tune -> accepted", c.cmd("F 7074000"), ["RPRT 0"])
        check("f reads the requested frequency", c.cmd("f"), ["7074000"])
        check("T 1 while the change is pending -> refused", c.cmd("T 1"), ["RPRT -11"])
        time.sleep(0.5)                    # quiet window: no retune may go out mid-tune
        check("radio not retuned while tuning", r.fake.state["freq"], 14074000)
        r.fake.end_tune()
        ok = wait_for(lambda: r.fake.state["freq"] == 7074000, timeout=10)
        check("radio moves to 7074000 after the tune", ok, True)
        end = r.fake.tune_ended_at
        early = [f for f in r.fake.frames if f.t >= m and f.is_freq_set() and (end is None or f.t < end)]
        check("no retune frame before the tune ended", (end is not None, early), (True, []))
        expect("f reads 7074000", lambda: c.cmd("f"), ["7074000"])
        c.close()
    finally:
        r.close()


# --- F4: radio's own split on (G90) is turned off, never keyed through -------

def test_f4_g90_radio_split_on():
    if RIG != "g90":
        return
    print("\n# F4 G90 split turned on at the radio")
    r = Rig()
    try:
        c = r.client()
        c.cmd("F 14074000")
        check("settled on 14074000", r.settle(c), True)
        t = r.fake.inject(split=1)
        check("server told of the radio's split", r.told(t, n=1, poll=SPLIT), True)
        m = r.fake.mark()
        check("T 1 with the radio's split on -> refused", c.cmd("T 1"), ["RPRT -11"])
        split_off = lambda: any(f.cmd == 0x0F and f.data == b"\x00" for f in r.fake.frames if f.t >= m)
        wait_for(split_off)
        check("no ON written", [v for _, v in r.fake.ptt_frames(m)], [])
        check_true("split OFF (0F 00) written", split_off())
        wait_for(lambda: r.fake.state["split"] == 0)
        t = r.fake.mark()
        check("server told split is off", r.told(t, n=1, poll=SPLIT), True)
        check("after split is off: T 1 keys", c.cmd("T 1"), ["RPRT 0"])
        c.cmd("T 0")
        expect("unkeyed", lambda: r.fake.state["tx"], 0)
        # hold: split turned on at the panel while an app is connected
        m = r.fake.mark()
        r.fake.inject(split=1)
        ok = wait_for(lambda: any(f.cmd == 0x0F and f.data == b"\x00"
                                  for f in r.fake.frames if f.t >= m), timeout=5)
        check("hold turns the radio's split back off", ok, True)
        c.close()
    finally:
        r.close()


# --- F5: PTT refused during a tune leaves no dial movement or stuck state ------

def test_f5_key_during_tune():
    if RIG != "g90":
        return
    print("\n# F5 split PTT during an ATU tune")
    r = Rig(fake_kw={"tune_s": None})      # the test ends the tune, not a timer
    try:
        c = r.client()
        c.cmd("S 1 VFOB")
        c.cmd("F 14074000")
        c.cmd("I 14076000")
        check("settled on 14074000", r.settle(c), True)
        m = r.fake.mark()
        check("G TUNE", c.cmd("G TUNE"), ["RPRT 0"])
        t = r.fake.mark()
        check("server told the radio is tuning (tuner=2, tx=1)",
              wait_for(lambda: r.fake.state["tuner"] == 2) and r.told(t, n=1, poll=TUNER) and r.told(t), True)
        check("T 1 during the tune -> refused", c.cmd("T 1"), ["RPRT -11"])
        t = r.fake.end_tune()
        check("server told the tune ended", r.told(t) and r.told(t, n=1, poll=TUNER), True)
        check("dial never left RX during the tune",
              [v for k, v in r.fake.sets_since(m) if k == "freq" and v != 14074000], [])
        m2 = r.fake.mark()
        r.fake.inject(freq=14100000)       # front-panel dial: the hold must still work
        ok = wait_for(lambda: ("freq", 14074000) in r.fake.sets_since(m2), timeout=5)
        check("hold still restores RX afterwards (no stuck split state)", ok, True)
        c.cmd("S 0 VFOA")
        c.close()
    finally:
        r.close()


# --- F6: browser values are validated before the safety checks ---------------

def test_f6_string_values():
    print("\n# F6 string tuner/tx values from the browser")
    r = Rig(extra=("--no-tx",))
    try:
        m = r.fake.mark()
        notes = browser(r.web, [{"t": "set", "k": "tuner", "v": "2"}], wait=10, until="notice")
        time.sleep(0.4)
        check("--no-tx: tuner '2' writes no tune frame",
              [f for f in r.fake.frames if f.t >= m and f.cmd == 0x1C and f.data == b"\x01\x02"], [])
        check_true("--no-tx: tuner '2' refused with a notice", bool(notes), str(notes))
    finally:
        r.close()
    r = Rig()
    try:
        m = r.fake.mark()
        browser(r.web, [{"t": "set", "k": "tx", "v": "0"}])
        time.sleep(0.4)
        check("tx '0' does not key", [v for _, v in r.fake.ptt_frames(m) if v == 1], [])
    finally:
        r.close()


# --- F7: a tune we started is timed out; a manual transmission never is -----

TUNE_TIMEOUT = 3


def _tune_cut(r, m, label):
    """An OFF reached the wire after mark m, within the tune timeout + slack."""
    ok = wait_for(lambda: any(v == 0 for _, v in r.fake.ptt_frames(m)), timeout=TUNE_TIMEOUT + 3)
    check(f"{label}: OFF written after the tune timeout", ok, True)
    first = next((t for t, v in r.fake.ptt_frames(m) if v == 0), None)
    check_true(f"{label}: not cut before the timeout",
               first is not None and first - m >= TUNE_TIMEOUT - 0.3,
               f"after {first - m:.2f}s" if first else "")
    wait_for(lambda: (r.fake.state["tuner"], r.fake.state["tx"]) == (1, 0), 5)   # OFF applied
    check(f"{label}: fake radio stopped tuning", (r.fake.state["tuner"], r.fake.state["tx"]), (1, 0))


def test_f7_rigctl_tune_times_out():
    print("\n# F7 rigctl G TUNE that never finishes is cut after --tune-timeout")
    r = Rig(fake_kw={"tune_s": None}, extra=("--tune-timeout", str(TUNE_TIMEOUT)))
    try:
        c = r.client()
        c.cmd("F 14074000")
        time.sleep(0.6)
        m = r.fake.mark()
        check("G TUNE", c.cmd("G TUNE"), ["RPRT 0"])
        _tune_cut(r, m, "rigctl tune")
        c.close()
    finally:
        out = r.close()
    logged = "ATU tune" in out and "unkeyed" in out
    check_true("timeout logged", logged, "" if logged else out[-400:])


def test_f7_browser_tune_times_out():
    print("\n# F7 browser tune that never finishes is cut after --tune-timeout")
    r = Rig(fake_kw={"tune_s": None}, extra=("--tune-timeout", str(TUNE_TIMEOUT)))
    try:
        m = r.fake.mark()
        browser(r.web, [{"t": "set", "k": "tuner", "v": 2}])
        _tune_cut(r, m, "browser tune")
    finally:
        r.close()


def test_f7_auto_tune_times_out():
    print("\n# F7 auto-tune that never finishes is cut after --tune-timeout")
    r = Rig(fake_kw={"tune_s": None},
            extra=("--auto-tune", "--tune-timeout", str(TUNE_TIMEOUT)))
    try:
        time.sleep(3.2)                    # past AUTO_TUNE_ARM_S
        c = r.client()
        m = r.fake.mark()
        c.cmd("F 7074000")                 # band change -> auto-tune after settling
        tune_on = lambda: [f.t for f in r.fake.frames if f.t >= m and f.cmd == 0x1C and f.data == b"\x01\x02"]
        ok = wait_for(lambda: bool(tune_on()), timeout=6)
        check("auto-tune started", ok, True)
        if ok:
            _tune_cut(r, tune_on()[0], "auto-tune")
        c.close()
    finally:
        r.close()


def test_f7_manual_tune_not_cut():
    print("\n# F7 + the operator's rule: a tune started at the radio is never timed out")
    r = Rig(extra=("--tune-timeout", str(TUNE_TIMEOUT)))
    try:
        m = r.fake.mark()
        r.fake.inject(tuner=2, tx=1)       # TUNE pressed on the front panel
        time.sleep(TUNE_TIMEOUT + 2)
        check("no OFF written for a front-panel tune", [v for _, v in r.fake.ptt_frames(m)], [])
        check("radio still tuning (operator's call)", r.fake.state["tuner"], 2)
        r.fake.inject(tuner=1, tx=0)
    finally:
        r.close()


def test_f7_manual_tx_past_tx_timeout_not_cut():
    print("\n# F7 + the operator's rule: a front-panel TX longer than --tx-timeout is never cut")
    r = Rig()
    try:
        m = r.fake.mark()
        r.fake.inject(tx=1)
        time.sleep(TX_TIMEOUT + 1.5)
        check("no OFF written for a manual transmission", [v for _, v in r.fake.ptt_frames(m)], [])
        r.fake.inject(tx=0)
    finally:
        r.close()


# --- F8: one stale "not transmitting" reply must not drop PTT ownership ------

def test_f8_stale_reply_keeps_split_tx():
    if RIG != "g90":
        return
    print("\n# F8 one stale 'not transmitting' reply during a split TX")
    r = Rig()
    try:
        c = r.client()
        c.cmd("S 1 VFOB")
        c.cmd("F 14074000")
        c.cmd("I 14076000")
        check("settled on 14074000", r.settle(c), True)
        m = r.fake.mark()
        check("T 1 keys, past the 1 s grace", r.keyed(c), True)
        check("stale reply delivered", r.stale_reply(), True)
        seq = r.fake.sets_since(m)
        check("no frequency change while keyed", keyed_retunes(seq), [])
        check("radio still transmitting on the TX freq",
              (r.fake.state["tx"], r.fake.state["freq"]), (1, 14076000))
        c2 = r.client()
        check("ownership kept: another app can't key", c2.cmd("T 1"), ["RPRT -11"])
        c2.close()
        m2 = r.fake.mark()
        check("T 0", c.cmd("T 0"), ["RPRT 0"])
        expect("unkey then back to RX, once", lambda: r.fake.sets_since(m2), [("ptt", 0), ("freq", 14074000)])
        c.cmd("S 0 VFOA")
        c.close()
    finally:
        r.close()


def test_f8_stale_reply_then_shutdown_unkeys():
    print("\n# F8 one stale reply, then SIGTERM: our key is still unkeyed")
    r = Rig()
    try:
        c = r.client()
        check("T 1 keys, past the 1 s grace", r.keyed(c), True)
        check("stale reply delivered", r.stale_reply(), True)
        check("fake radio still transmitting", r.fake.state["tx"], 1)
        m = r.fake.mark()
        r.sigterm()
        r.fake.quiesce()
        check("OFF written at shutdown", any(v == 0 for _, v in r.fake.ptt_frames(m)), True)
        check("fake radio not transmitting after shutdown", r.fake.state["tx"], 0)
        c.close()
    finally:
        r.close()


def test_f8_real_panel_unkey_releases():
    print("\n# F8 a real unkey at the radio still releases ownership (and restores RX)")
    r = Rig()
    try:
        c = r.client()
        c.cmd("S 1 VFOB")
        c.cmd("F 14074000")
        c.cmd("I 14076000")
        check("settled on 14074000", r.settle(c), True)
        check("T 1 keys, past the 1 s grace", r.keyed(c), True)
        m = r.fake.inject(tx=0)            # unkeyed at the front panel, and stays so
        if RIG == "g90":                   # emulated split: the dial comes back to RX
            ok = wait_for(lambda: ("freq", 14074000) in r.fake.sets_since(m), timeout=8)
            check("back to RX after the panel unkey", ok, True)
        check("server released our key (after its confirm window)",
              r.wait_log("unkeyed at the radio; releasing PTT", timeout=10), True)
        if RIG == "g90":
            time.sleep(1.0)                # quiet window: a second restore would show
            check("RX restored exactly once",
                  [v for k, v in r.fake.sets_since(m) if k == "freq"].count(14074000), 1)
        check("no OFF written for an unkey made at the radio",
              [v for _, v in r.fake.ptt_frames(m)], [])
        c2 = r.client()
        check("ownership released: another app may key", c2.cmd("T 1"), ["RPRT 0"])
        c2.cmd("T 0")
        c2.close()
        c.cmd("S 0 VFOA")
        c.close()
    finally:
        r.close()


# --- F9: a leftover emulated split TX frequency is never keyed on -----------

def test_f9_split_cleared_on_last_disconnect():
    if RIG != "g90":
        return
    print("\n# F9 emulated split is cleared when the last rigctl client leaves")
    r = Rig()
    try:
        c = r.client()
        c.cmd("S 1 VFOB")
        c.cmd("F 14074000")
        c.cmd("I 14076000")
        n = r.log_mark()
        c.close()
        check("server saw the last client leave", r.wait_log("emulated split cleared", n), True)
        c2 = r.client()
        check("new client reads split off", c2.cmd("s", 2), ["0", "VFOA"])
        m = r.fake.mark()
        check("T 1 keys", c2.cmd("T 1"), ["RPRT 0"])
        wait_for(lambda: r.fake.state["tx"] == 1, 5)
        check("keyed on RX, no hop to the old TX freq",
              [v for k, v in r.fake.sets_since(m) if k == "freq"], [])
        c2.cmd("T 0")
        c2.close()
    finally:
        r.close()


def test_f9_cross_band_hop_refused():
    if RIG != "g90":
        return
    print("\n# F9 split TX on another band than RX is refused, the dial never moves")
    r = Rig()
    try:
        c = r.client()
        c.cmd("S 1 VFOB")
        c.cmd("I 14076000")
        c.cmd("F 7074000")
        check("settled on 7074000", r.settle(c), True)
        m = r.fake.mark()
        check("T 1 cross-band -> refused", c.cmd("T 1"), ["RPRT -11"])
        time.sleep(0.4)                    # quiet window: nothing may follow a refusal
        check("no ON and no retune written", r.fake.sets_since(m), [])
        check("radio receiving on 7074000", (r.fake.state["tx"], r.fake.state["freq"]), (0, 7074000))
        c.cmd("I 7076000")
        check("same-band split still keys", c.cmd("T 1"), ["RPRT 0"])
        expect("keyed on 7076000", lambda: (r.fake.state["tx"], r.fake.state["freq"]), (1, 7076000))
        c.cmd("T 0")
        c.cmd("S 0 VFOA")
        c.close()
    finally:
        r.close()


# --- F12: a same-band F around an auto-tune never loses or doubles the tune ---

def _f12_case(label, first_f, second_f):
    r = Rig(fake_kw={"tune_s": 1.0}, extra=("--auto-tune",))
    try:
        time.sleep(3.2)                    # past AUTO_TUNE_ARM_S
        c = r.client()
        m = r.fake.mark()
        tunes = lambda: [f.t for f in r.fake.frames if f.t >= m and f.cmd == 0x1C and f.data == b"\x01\x02"]
        c.cmd(f"F {first_f}")
        # A same-band move just as the 2.0 s settle ends: if anything between
        # recording the band and queueing the tune ever yields, this cancel
        # lands there and the band is marked tuned with no tune sent.
        time.sleep(2.15)
        c.cmd(f"F {second_f}")
        ok = wait_for(lambda: bool(tunes()), timeout=8)
        check(f"{label}: tune frame sent", ok, True)
        time.sleep(3.0)                    # any second (duplicate) tune would show by now
        check(f"{label}: exactly one tune frame", len(tunes()), 1)
        c.close()
    finally:
        out = r.close()
    n = out.count("auto-tune: ATU tuning")
    check(f"{label}: one auto-tune logged", n, 1)


def test_f12_same_band_moves_around_auto_tune():
    print("\n# F12 same-band dial moves around an auto-tune: tune sent once, never lost")
    _f12_case("band change", 7074000, 7075000)
    _f12_case("first use", 14075000, 14076000)


# --- F13: the G90 really ends up in DATA (U-D), and we report what it is in ---

def test_f13_g90_data_mode_applied():
    if RIG != "g90":
        return
    print("\n# F13 G90: DATA set through 26 00 (1A 06 is acked, ignored, and read back as an echo)")
    r = Rig(fake_kw={"g90_1a06_echo": True})
    try:
        t = r.fake.inject(data=0)                  # radio in plain USB, as on linux-station 2026-10-14
        check("server told of plain USB", r.told(t, n=1, poll=data_poll(RIG)), True)
        c = r.client()
        c.cmd("F 14075000")                    # an app setting: the hold wants DATA on
        ok = wait_for(lambda: r.fake.state["data"] == 1, timeout=5)
        check("radio put in DATA after the app connects", ok, True)
        expect("server reports what the radio is in", lambda: (c.cmd("m", 2)[0], r.fake.state["data"]),
               ("PKTUSB", 1))
        r.fake.inject(data=0)                  # front panel back to plain USB
        ok = wait_for(lambda: r.fake.state["data"] == 1, timeout=6)
        check("front-panel USB noticed and DATA restored", ok, True)
        c.close()
    finally:
        r.close()


# --- F16: G90 SWR reply is binary 8.8 fixed point (00 00 at a match) ---------

ICOM_SWR = [(0, 1.0), (48, 1.5), (80, 2.0), (120, 3.0), (255, 9.9)]   # static/app.js swrVal


def swr_ratio(meter):
    """What the page shows for a meter value (mirror of static/app.js swrVal)."""
    for (x0, y0), (x1, y1) in zip(ICOM_SWR, ICOM_SWR[1:]):
        if meter <= x1:
            return y0 + (meter - x0) / (x1 - x0) * (y1 - y0)
    return 9.9


def server_state(web):
    import aiohttp, json

    async def go():
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(f"http://127.0.0.1:{web}/ws") as ws:
                return json.loads((await ws.receive(timeout=3)).data)["state"]
    return asyncio.run(go())


def until_state(web, pred, timeout=8.0):
    """The first page state that satisfies pred (or the last one read). Keep
    that reading: a second read can land on the brief 'not transmitting' a
    stale reply shows just after a key (the server's 1 s grace covers it)."""
    end = time.monotonic() + timeout
    while True:
        st = server_state(web)
        if pred(st) or time.monotonic() > end:
            return st
        time.sleep(0.2)


def test_f16_swr_decode():
    # G90 replies seen on linux-station 2026-10-13/14 and what its own meter showed:
    # ~1.0 at 00 00 (the operator, 13:22-13:24), never above 1.5 on 10 m FT8 (01 04 / 01 4c).
    if RIG == "g90":
        cases = [(b"\x00\x00", 1.0), (b"\x01\x04", 1.02), (b"\x01\x4c", 1.30),
                 (b"\x01\x84", 1.52), (b"\x02\x2c", 2.17), (b"\x02\x5f", 2.37)]
    else:                                   # Icom: BCD on the documented scale, unchanged
        cases = [(b"\x00\x00", 1.0), (b"\x00\x48", 1.5), (b"\x00\x80", 2.0), (b"\x01\x20", 3.0)]
    print(f"\n# F16 SWR meter decode ({RIG})")
    r = Rig()
    try:
        c = r.client()
        c.cmd("F 14074000")
        check("settled on 14074000", r.settle(c), True)
        for raw, want in cases:              # key per case: stays well inside --tx-timeout
            r.fake.inject(swr_raw=raw)
            c.cmd("T 1")
            shown = lambda: server_state(r.web)
            ok = lambda st: st.get("tx") and abs(swr_ratio(st.get("swr") or 0) - want) < 0.02
            st = until_state(r.web, ok)      # TX meters are polled in rotation
            check_true(f"raw {raw.hex(' ')} -> SWR {want:.2f}", ok(st),
                       f"page shows {swr_ratio(st.get('swr') or 0):.2f}, tx={st.get('tx')}")
            c.cmd("T 0")
            check("unkeyed", r.settle(c), True)
        c.close()
    finally:
        r.fake.inject(swr_raw=b"\x00\x00")
        r.close()


def test_tx_meters_shown():
    # Xiegu CI-V reference v1.0, command 0x15: 0x11 power and 0x13 ALC are
    # 0-255 meters like SWR. The G90 profile listed only SWR (2026-10-15).
    print(f"\n# PO and ALC meters polled while keyed ({RIG})")
    caps = rigs.PROFILES[RIG]
    check("profile lists PO, SWR and ALC", [m for m in ("po", "swr", "alc") if m in caps["meters"]],
          ["po", "swr", "alc"])
    r = Rig()
    try:
        c = r.client()
        c.cmd("F 14074000")
        check("settled on 14074000", r.settle(c), True)
        r.fake.inject(po_raw=b"\x01\x28", alc_raw=b"\x00\x64", swr_raw=b"\x00\x00")
        c.cmd("T 1")
        meters = lambda st: (bool(st.get("tx")), st.get("po"), st.get("alc"))
        st = until_state(r.web, lambda st: meters(st) == (True, 128, 64))   # three TX meters in rotation
        tx, po, alc = meters(st)
        check("keyed", tx, True)
        check("PO on the page (raw 01 28)", po, 128)
        check("ALC on the page (raw 00 64)", alc, 64)
        c.cmd("T 0")
        st = until_state(r.web, lambda st: meters(st)[1:] == (0, 0))
        check("unkeyed: PO and ALC cleared", meters(st)[1:], (0, 0))
        c.close()
    finally:
        r.fake.inject(po_raw=b"\x00\x00", alc_raw=b"\x00\x00")
        out = r.close()
    # A calibration aid for the G90 only: an IC-7300's PO changes on nearly
    # every poll and logged ~50 lines/s while keyed (mac-shack, 2026-10-15).
    check("raw PO/ALC replies logged only on the G90",
          ("po raw" in out, "alc raw" in out), (RIG == "g90", RIG == "g90"))


def test_winlink_mode_usb_data():
    """--winlink-mode USB+DATA on the G90: arming a session from plain USB puts
    the radio in USB-D, and disarming puts plain USB back. Plain USB carries no
    Digirig audio, so a session there keys at 0 W (linux-station, 2026-10-17). The
    fake keeps the real G90's quirk (a 1A 06 data set is acked, never applied),
    so this passes only over CI-V 26 00."""
    print("\n# Winlink session from plain USB: USB-D while armed, plain USB after")
    if RIG != "g90":
        print("  (G90 only)")
        return
    lane = free_port()
    r = Rig(fake_kw={"g90_1a06_echo": True},
            extra=("--winlink-rigctl-port", str(lane), "--winlink-mode", "USB+DATA"))
    try:
        t = r.fake.inject(mode="USB", data=0)               # left in plain USB at the panel
        check("server told of plain USB", r.told(t, n=1, poll=data_poll(RIG)), True)
        usb_d = lambda: (r.fake.state["mode"], r.fake.state["data"]) == ("USB", 1)
        browser(r.web, [{"t": "winlink_arm", "v": True}], wait=10, until=usb_d)
        check("armed: the radio is in USB-D while the page is open", browser.held, True)
        browser(r.web, [{"t": "winlink_arm", "v": False}], wait=1.5)
        back = wait_for(lambda: r.fake.state["data"] == 0)
        check("disarmed: plain USB again", (back, r.fake.state["mode"], r.fake.state["data"]),
              (True, "USB", 0))
    finally:
        r.close()


def test_winlink_arm_refused_while_another_transmits():
    """Arming a Winlink session while someone else holds the transmitter is
    refused, and nothing on the radio moves: before, arming switched the mode
    (and the codec's drive) in the middle of that transmission (bug bash
    2026-10-17, Sol #5; review 2026-10-18, Grok). Once the key is released,
    arming works."""
    print("\n# Winlink arm while an app transmits: refused, mode untouched")
    lane = free_port()
    r = Rig(fake_kw={"g90_1a06_echo": True} if RIG == "g90" else None,
            extra=("--winlink-rigctl-port", str(lane), "--winlink-mode", "USB+DATA"))
    try:
        t = r.fake.inject(mode="USB", data=0)
        check("server told of plain USB", r.told(t, n=1, poll=data_poll(RIG)), True)
        app = r.client()
        check("app keys", app.cmd("T 1"), ["RPRT 0"])
        notes = browser(r.web, [{"t": "winlink_arm", "v": True}], wait=10, until="notice")
        check("arm refused with a notice", any("transmitting" in n for n in notes), True)
        check("mode untouched mid-transmission", (r.fake.state["mode"], r.fake.state["data"]),
              ("USB", 0))
        check("not armed: the lane can't key", Client(lane).cmd("T 1"), ["RPRT -11"])
        check("app unkeys", app.cmd("T 0"), ["RPRT 0"])
        app.close()
        wait_for(lambda: not server_state(r.web).get("tx"))   # the server has polled the unkey
        browser(r.web, [{"t": "winlink_arm", "v": True}], wait=10, until=lambda: r.fake.state["data"] == 1)
        check("released: arming works", browser.held, True)
        browser(r.web, [{"t": "winlink_arm", "v": False}], wait=1.5)
    finally:
        r.close()


def test_ic7300_voice_usb_source():
    """The IC-7300's page voice in plain USB: DATA OFF MOD goes to USB (03)
    before the key and back to what it was after the unkey; in USB-D, and
    for an app keying over rigctl, it is never touched. Its default MIC/ACC
    sent nothing from the page, near 0 W (mac-shack, 2026-10-18)."""
    print("\n# IC-7300: page voice in plain USB switches DATA OFF MOD to USB, then back")
    fake = FakeCiv("ic7300").start()
    web, rport = free_port(), free_port()
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--rig", "ic7300", "--port", fake.path, "--no-audio",
         "--listen", str(web), "--host", "127.0.0.1", "--rigctl-host", "127.0.0.1",
         "--rigctl-port", str(rport)], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        check("server polling the radio", fake_ready(fake), True)
        t = fake.inject(mode="USB", data=0)
        check("server told of plain USB", fake_told(fake, t, web=web), True)

        async def hold(seconds, msgs_before=(), want_key=True, want_badges=2):
            import aiohttp, json
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(f"http://127.0.0.1:{web}/ws") as ws:
                    await ws.receive(timeout=3)
                    for m in msgs_before:
                        await ws.send_str(json.dumps(m))
                    await asyncio.sleep(0.5)
                    during = []
                    keyed = lambda: fake.state["tx"] == 1
                    for i in range(int(seconds / 0.5)):
                        await ws.send_str(json.dumps({"t": "set", "k": "tx", "v": True}))
                        if i == 0 and want_key:      # sample only once the key is on the wire
                            await asyncio.to_thread(wait_for, keyed, 8)
                        await asyncio.sleep(0.5)
                        during.append((fake.state["tx"], fake.state["data_off_mod"]))
                    await ws.send_str(json.dumps({"t": "set", "k": "tx", "v": False}))
                    end = time.monotonic() + 8
                    while time.monotonic() < end and len(badge) < want_badges:   # badge events, in order
                        try:
                            msg = await ws.receive(timeout=0.2)
                        except asyncio.TimeoutError:
                            continue
                        if msg.type == aiohttp.WSMsgType.TEXT and '"t": "voice"' in msg.data:
                            badge.append(json.loads(msg.data)["usb_src"])
                    await asyncio.to_thread(wait_for, lambda: fake.state["tx"] == 0, 8)
                    return during
        badge = []
        during = asyncio.run(hold(2.0))
        check("keyed with DATA OFF MOD at USB throughout", set(during), {(1, 0x03)})
        check("the page's USB AUDIO badge: on, then off", badge, [True, False])
        expect("after: unkeyed, back to MIC/ACC", lambda: (fake.state["tx"], fake.state["data_off_mod"]), (0, 0x02))
        seq = [(f.cmd, f.data.hex()) for f in fake.frames if f.cmd in (0x1A, 0x1C)
               and (f.data[:3] == bytes([5, 0, 0x66]) and len(f.data) == 4 or f.is_ptt())]
        order = [("src", d[-2:]) if c == 0x1A else ("ptt", d[-2:]) for c, d in seq]
        check("wire order: source USB, key, unkey, source back",
              order[-4:], [("src", "03"), ("ptt", "01"), ("ptt", "00"), ("src", "02")])

        n = len([f for f in fake.frames if f.cmd == 0x1A and f.data[:3] == bytes([5, 0, 0x66]) and len(f.data) == 4])
        t = fake.inject(data=1)
        check("server told of USB-D", fake_told(fake, t, web=web), True)
        badge = []
        asyncio.run(hold(1.0, want_badges=0))
        check("USB-D: DATA OFF MOD never written",
              len([f for f in fake.frames if f.cmd == 0x1A and f.data[:3] == bytes([5, 0, 0x66]) and len(f.data) == 4]), n)

        t = fake.inject(data=0)
        check("server told of plain USB again", fake_told(fake, t, web=web), True)
        n = len([f for f in fake.frames if f.cmd == 0x1A and f.data[:3] == bytes([5, 0, 0x66]) and len(f.data) == 4])

        async def hold_no_mic():
            import aiohttp, json
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(f"http://127.0.0.1:{web}/ws") as ws:
                    await ws.receive(timeout=3)
                    for _ in range(3):
                        await ws.send_str(json.dumps({"t": "set", "k": "tx", "v": True, "mic": False}))
                        await asyncio.sleep(0.5)
                    await asyncio.to_thread(wait_for, lambda: fake.state["tx"] == 1, 8)
                    keyed = fake.state["tx"]
                    await ws.send_str(json.dumps({"t": "set", "k": "tx", "v": False}))
                    await asyncio.to_thread(wait_for, lambda: fake.state["tx"] == 0, 8)
                    return keyed
        check("page mic off: keys", asyncio.run(hold_no_mic()), 1)
        check("page mic off: DATA OFF MOD never written (the radio's own mic)",
              len([f for f in fake.frames if f.cmd == 0x1A and f.data[:3] == bytes([5, 0, 0x66]) and len(f.data) == 4]), n)
        c = Client(rport)
        check("rigctl keys", c.cmd("T 1"), ["RPRT 0"])
        wait_for(lambda: fake.state["tx"] == 1, 8)
        time.sleep(0.5)                    # quiet window: no source switch may follow
        check("an app's PTT leaves DATA OFF MOD alone", fake.state["data_off_mod"], 0x02)
        c.cmd("T 0")
        c.close()
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            out = proc.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.communicate()[0]
        fake.stop()
        check_true("no Traceback in server log", "Traceback" not in out, out[-800:])
        check_true("the voice line logs hold time and mic audio", "voice: page PTT released after" in out, out[-600:])
# --- F17: a held browser PTT never rekeys after the server let it go -------

def held_ptt(web, hold_s, other_unkey_at=None, then_rekey=False):
    """A browser that holds PTT (key + a keep-alive every 0.5 s, as app.js
    does) for hold_s. other_unkey_at: a second browser sends tx=false that
    many seconds in. then_rekey: afterwards release and press again, as a
    person would. Returns the message types the holder received."""
    import aiohttp, json

    async def go():
        got = []
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(f"http://127.0.0.1:{web}/ws") as a:
                await a.receive(timeout=3)

                async def drain():
                    async for msg in a:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            got.append(json.loads(msg.data).get("t"))
                reader = asyncio.ensure_future(drain())
                on = json.dumps({"t": "set", "k": "tx", "v": True})
                await a.send_str(on)
                start = time.monotonic()
                unkeyed_by_other = False
                while time.monotonic() - start < hold_s:
                    await asyncio.sleep(0.5)
                    await a.send_str(on)                  # the keep-alive
                    if (other_unkey_at is not None and not unkeyed_by_other
                            and time.monotonic() - start >= other_unkey_at):
                        async with sess.ws_connect(f"http://127.0.0.1:{web}/ws") as b:
                            await b.receive(timeout=3)
                            await b.send_str(json.dumps({"t": "set", "k": "tx", "v": False}))
                            await asyncio.sleep(0.2)
                        unkeyed_by_other = True
                if then_rekey:
                    await a.send_str(json.dumps({"t": "set", "k": "tx", "v": False}))
                    await asyncio.sleep(0.5)
                    await a.send_str(on)
                    await asyncio.sleep(1.0)
                    await a.send_str(json.dumps({"t": "set", "k": "tx", "v": False}))
                    await asyncio.sleep(0.5)
                reader.cancel()
        return got
    return asyncio.run(go())


def test_f17_keepalive_after_timeout_does_not_rekey():
    print("\n# F17 held browser PTT: keep-alives after the TX time limit don't key again")
    r = Rig()
    try:
        m = r.fake.mark()
        got = held_ptt(r.web, TX_TIMEOUT + 3, then_rekey=True)
        # key, time-limit unkey, then the deliberate release/press/release.
        expect("PTT on the wire: on, off (limit), on, off (pressed again)",
               lambda: [v for _, v in r.fake.ptt_frames(m)], [1, 0, 1, 0])
        check_true("the page was told to release its button", "ptt_released" in got, str(got))
    finally:
        r.close()


def test_f17_keepalive_after_other_unkey_does_not_rekey():
    print("\n# F17 held browser PTT: another operator's unkey sticks")
    r = Rig()
    try:
        m = r.fake.mark()
        got = held_ptt(r.web, 4, other_unkey_at=1.5)
        r.fake.quiesce()
        check("PTT on the wire: on, off (other operator), nothing after",
              [v for _, v in r.fake.ptt_frames(m)], [1, 0])
        check("radio not transmitting", r.fake.state["tx"], 0)
        check_true("the page was told to release its button", "ptt_released" in got, str(got))
    finally:
        r.close()


# --- F18: split and VFO swap can't move the transmitter while keyed --------

def vfo_moves(frames):
    """VFO swap (07 B0) and split sets (0F xx) written while keyed."""
    keyed, bad = False, []
    for f in frames:
        if f.is_ptt():
            keyed = bool(f.data[1])
        elif keyed and (f.cmd == 0x07 or (f.cmd == 0x0F and len(f.data) == 1)):
            bad.append(repr(f))
    return bad


def test_f18_no_split_or_swap_while_keyed():
    print("\n# F18 split / VFO swap refused while keyed (real split rigs)")
    if "vfo_b" not in rigs.get(RIG)["features"]:
        print("skip: this rig's split is emulated in the listener; it never reaches the radio")
        return
    r = Rig()
    try:
        a, b = r.client(), r.client()
        a.cmd("F 14074000")
        check("settled on 14074000", r.settle(a), True)
        m = r.fake.mark()
        check("A: T 1", a.cmd("T 1"), ["RPRT 0"])
        check("server sees the key", r.settle(a), True)
        check("B: S 1 VFOB while keyed -> refused", b.cmd("S 1 VFOB"), ["RPRT -11"])
        check("B: S 0 VFOA (no change) while keyed -> ok", b.cmd("S 0 VFOA"), ["RPRT 0"])
        notes = browser(r.web, [{"t": "swap"}], wait=10, until="notice")
        check_true("browser swap while keyed -> refused with a notice",
                   any("transmit" in n.lower() for n in notes), str(notes))
        check("A: T 0", a.cmd("T 0"), ["RPRT 0"])
        expect("unkeyed", lambda: r.fake.state["tx"], 0)
        with r.fake.lock:
            frames = [f for f in r.fake.frames if f.t >= m]
        check("no split or swap on the wire while keyed", vfo_moves(frames), [])
        check("radio split still off", r.fake.state["split"], 0)
        check("after unkey: S 1 VFOB accepted", b.cmd("S 1 VFOB"), ["RPRT 0"])
        expect("radio split on", lambda: r.fake.state["split"], 1)
        check("S 0 VFOA", b.cmd("S 0 VFOA"), ["RPRT 0"])
        a.close(); b.close()
    finally:
        r.close()


def test_ic7300_sigterm_restores_source():
    """Review 2026-10-18 (Sol, 3/3 runs): SIGTERM during page voice unkeyed
    the IC-7300 but left DATA OFF MOD on USB; stop() dropped the queued
    restore. It must go out after the unkey."""
    print("\n# IC-7300: SIGTERM during page voice puts DATA OFF MOD back")
    fake = FakeCiv("ic7300").start()
    web, rport = free_port(), free_port()
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--rig", "ic7300", "--port", fake.path, "--no-audio",
         "--listen", str(web), "--host", "127.0.0.1", "--rigctl-host", "127.0.0.1",
         "--rigctl-port", str(rport)], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = ""
    try:
        check("server polling the radio", fake_ready(fake), True)
        t = fake.inject(mode="USB", data=0)
        check("server told of plain USB", fake_told(fake, t, web=web), True)

        async def key_and_kill():
            import aiohttp, json
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(f"http://127.0.0.1:{web}/ws") as ws:
                    await ws.receive(timeout=3)
                    for _ in range(3):
                        await ws.send_str(json.dumps({"t": "set", "k": "tx", "v": True, "mic": True}))
                        await asyncio.sleep(0.5)
                    await asyncio.to_thread(wait_for, lambda: (fake.state["tx"], fake.state["data_off_mod"]) == (1, 3), 8)
                    keyed = (fake.state["tx"], fake.state["data_off_mod"])
                    proc.send_signal(signal.SIGTERM)
                    await asyncio.sleep(0.1)
                    return keyed
        check("keyed on USB before the SIGTERM", asyncio.run(key_and_kill()), (1, 0x03))
        try:
            out = proc.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.communicate()[0]
        wait_for(lambda: fake.state["data_off_mod"] == 0x02 and fake.state["tx"] == 0, 10)
        check("after SIGTERM: unkeyed, DATA OFF MOD back to MIC/ACC",
              (fake.state["tx"], fake.state["data_off_mod"]), (0, 0x02))
    finally:
        if proc.poll() is None:
            proc.kill()
        fake.stop()
        check_true("no Traceback in server log", "Traceback" not in out, out[-800:])


TESTS = [test_f1_sigterm_while_keyed, test_f1_unit_unkey_then_stop,
         test_f1_manual_tx_left_alone_on_shutdown, test_f1_manual_tx_after_our_key_left_alone,
         test_f2_back_to_back_ptt, test_f2_split_hop_order,
         test_f3_no_retune_while_keyed, test_f3_jtdx_sequence_still_works,
         test_f3_band_change_during_tune_waits,
         test_f4_g90_radio_split_on, test_f5_key_during_tune, test_f6_string_values,
         test_f7_rigctl_tune_times_out, test_f7_browser_tune_times_out,
         test_f7_auto_tune_times_out, test_f7_manual_tune_not_cut,
         test_f7_manual_tx_past_tx_timeout_not_cut,
         test_f8_stale_reply_keeps_split_tx, test_f8_stale_reply_then_shutdown_unkeys,
         test_f8_real_panel_unkey_releases,
         test_f9_split_cleared_on_last_disconnect, test_f9_cross_band_hop_refused,
         test_f12_same_band_moves_around_auto_tune, test_f13_g90_data_mode_applied,
         test_f16_swr_decode, test_tx_meters_shown, test_winlink_mode_usb_data,
         test_winlink_arm_refused_while_another_transmits,
         test_ic7300_voice_usb_source, test_ic7300_sigterm_restores_source,
         test_f17_keepalive_after_timeout_does_not_rekey,
         test_f17_keepalive_after_other_unkey_does_not_rekey,
         test_f18_no_split_or_swap_while_keyed]

if __name__ == "__main__":
    only = os.environ.get("ONLY")
    ran = 0
    for t in TESTS:
        if only and only not in t.__name__:
            continue
        ran += 1
        t()
    # A filter that matches nothing must not read as a pass (a gate that ran
    # nothing said PASS, 2026-10-23).
    check_true(f"ONLY={only!r} selects at least one scenario", ran > 0, "")
    print("\nPASS" if not fails else f"\n{fails} FAILED")
    sys.exit(1 if fails else 0)
