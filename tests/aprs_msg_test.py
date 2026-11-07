"""APRS messaging (aprs_msg.py) in process, on a compressed clock.

A real tnc_hub.Hub and its APRS store (a temp SQLite file), with the radio
replaced at its write boundary: every frame the hub hands the radio is kept
as bytes and decoded here, and inbound frames go in through
Hub.on_radio_frame exactly as the radio's bytes would. The clock is a
variable: tnc_hub.now and the engine's clock both read it, and the engine's
pass (Engine.tick) is called by the test instead of its 0.5 s loop.

Covers the October M3 test list with the 2026-11-07 plan's deltas: acking
(full only), not acking, wrapped inbound, matching, retry slots, one in
flight per addressee, the gate's reserves with acks, pauses and races, a
restart mid-retry, the send/full split, the firmware-ack readout, and IS
copies never acked or matched. The wire versions (HTTP, the real pty, a
crash, APRS-IS) are in tests/aprs_msg_wire_test.py.

    .venv/bin/python tests/aprs_msg_test.py
"""
import asyncio
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.pop("CC_OBS_TOKENS_FILE", None)

import aprs_msg  # noqa: E402
import aprs_store  # noqa: E402
import tnc_hub  # noqa: E402
from ax25 import Frame  # noqa: E402
from ax25_link import TxRefused  # noqa: E402

fails = 0
T0 = 1_800_000_000.0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:70} got {got!r}" + ("" if ok else f" want {want!r}"))
    return ok


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


clock = Clock()
tnc_hub.now = clock                     # the gate's window, the logs, the store's receive times


class Radio:
    """RadioPort at its write boundary: what the hub hands the radio."""

    def __init__(self):
        self.up, self.held, self.frames = True, False, []
        self.spec = "fake"
        self.opened_at = self.last_rx = self.error = None

    def write(self, raw):
        self.frames.append((clock(), raw))

    def write_control(self, frame):
        pass

    def texts(self, since=0):
        return [Frame.decode(r).tnc2() for _, r in self.frames[since:]]


class Named:
    def __init__(self, name, mon=False, raw=False):
        self.name, self.mon, self.raw = name, mon, raw

    def monitor(self, f, raw):          # an AGWPE client's monitor feed (unused here)
        pass

    def send_frame(self, raw):          # a KISS client's feed
        pass


class Rig:
    """One hub + store + engine. close() stops the store's worker."""

    async def open(self, mode="full", fw="false", logdir=None, extra=(), cooldown=60):
        self.logdir = logdir or tempfile.mkdtemp(prefix="aprsmsg-")
        args = tnc_hub.parse_args(["--tnc", "/dev/null", "--mycall", "N9ABC", "--tx", "--log-dir", self.logdir,
                                   "--aprs-messaging", mode, "--aprs-firmware-acks", fw,
                                   "--aprs-cooldown", str(cooldown), *extra])
        self.hub = h = tnc_hub.Hub(args)
        h.radio = self.radio = Radio()
        loop = asyncio.get_running_loop()
        h.aprs = await loop.run_in_executor(None, lambda: aprs_store.AprsStore(os.path.join(self.logdir, "aprs.db")).start())
        self.eng = aprs_msg.Engine(h, mode, args.aprs_owned_calls, args.aprs_firmware_acks, args.aprs_path,
                                   cooldown_s=cooldown, clock=clock)
        h.aprs_msg = self.eng
        await self.eng.start(run=False)
        return self

    async def close(self, keep=False):
        await self.eng.settle()
        await asyncio.get_running_loop().run_in_executor(None, self.hub.aprs.stop)
        if not keep:
            shutil.rmtree(self.logdir, ignore_errors=True)

    def hear(self, src, info, dest="APRS", digis=(), used=0):
        f = Frame.ui(src, dest, info.encode("latin-1"), digis=digis)
        for d in f.digis[:used]:
            d.bit7 = True
        self.hub.on_radio_frame(f.encode())

    async def tick(self, n=1):
        for _ in range(n):
            await self.eng.tick()
            await self.eng.settle()

    async def run_until(self, t, step=1.0):
        """Advance the clock to t, a pass every step seconds."""
        while clock.t + step <= t:
            clock.t += step
            await self.tick()
        clock.t = t
        await self.tick()

    async def row(self, mid):
        return await self.hub.aprs.call("msg_get", mid)

    async def settle_store(self):
        await self.hub.aprs.call("rev")


def after_cooldown():
    clock.t = T0 + 61


async def rig(**kw):
    clock.t = T0
    r = await Rig().open(**kw)
    after_cooldown()
    return r


ACK = "N9ABC>APZRR0,WIDE1-1,WIDE2-1::{:<9}:ack{}"


# ================================================================== acking (full)

async def t_acking():
    r = await rig()
    try:
        for msgno in ("1", "12", "ABCDE"):
            n = len(r.radio.frames)
            r.hear("KF7ABC-9", f":N9ABC    :hello{{{msgno}")
            await r.tick()
            check(f"full: a message with msgno {msgno!r} gets one ack, verbatim id, path, TOCALL, padding",
                  r.radio.texts(n), [ACK.format("KF7ABC-9", msgno)])
            clock.t += 3
        n = len(r.radio.frames)
        r.hear("KF7ABC-9", ":N9ABC    :cr ended{77\r")
        await r.tick()
        check("  a CR-terminated msgno: acked as 77", r.radio.texts(n), [ACK.format("KF7ABC-9", "77")])
        clock.t += 3
        n = len(r.radio.frames)
        r.hear("KF7ABC-9", ":N9ABC    :hello{12")
        await r.tick()
        check("a duplicate within 30 s of our ack: none", r.radio.texts(n), [])
        clock.t += 30
        r.hear("KF7ABC-9", ":N9ABC    :hello{12")
        await r.tick()
        check("  after 30 s: acked again (the sender didn't hear us)", r.radio.texts(n), [ACK.format("KF7ABC-9", "12")])
        clock.t += 3
        r.hub.gate.ack_per_min = 0                  # the gate refuses the next ack
        n = len(r.radio.frames)
        r.hear("W7TST", ":N9ABC    :refused{55")
        await r.tick()
        check("a refused ack: nothing on air", r.radio.texts(n), [])
        r.hub.gate.ack_per_min = 6
        clock.t += 3
        r.hear("W7TST", ":N9ABC    :refused{55")
        await r.tick()
        check("  the next copy is acked (a refusal doesn't start the 30 s slot)", r.radio.texts(n),
              [ACK.format("W7TST", "55")])
        clock.t += 61                               # a fresh minute: the ack cap (6/min) is not the test here
        n = len(r.radio.frames)
        r.hear("AA7JB", "}W7TST-9>APRS,TCPIP,AA7JB*::N9ABC    :hi{7", dest="APDW18")
        await r.tick()
        check("wrapped inbound (}...TCPIP...): acked by its inner source and id", r.radio.texts(n),
              [ACK.format("W7TST-9", "7")])
    finally:
        await r.close()


async def t_not_acked():
    r = await rig()
    try:
        cases = [("no msgno", "KF7ABC", ":N9ABC    :no number"), ("an ack", "KF7ABC", ":N9ABC    :ack12"),
                 ("a rej", "KF7ABC", ":N9ABC    :rej12"), ("a bulletin", "KF7ABC", ":BLN1     :news{3"),
                 ("PARM. (telemetry to itself)", "N9ABC-5", ":N9ABC-5  :PARM.A,B"),
                 ("to N9ABC-7 (not an owned call-SSID)", "KF7ABC", ":N9ABC-7  :hello{12"),
                 ("to W1AW", "KF7ABC", ":W1AW     :hello{12")]
        for name, src, info in cases:
            n = len(r.radio.frames)
            r.hear(src, info)
            await r.tick()
            clock.t += 3
            check(f"not acked: {name}", r.radio.texts(n), [])
        n = len(r.radio.frames)
        r.hub.on_is_line(clock(), "KF7ABC>APRS,TCPIP*,qAC,T2TEST::N9ABC    :from the internet{44")
        await r.run_until(clock.t + 20)
        check("not acked: an APRS-IS copy (port is)", r.radio.texts(n), [])
    finally:
        await r.close()
    clock.t = T0
    r = await Rig().open()
    try:
        clock.t = T0 + 30
        r.hear("KF7ABC", ":N9ABC    :early{12")
        await r.run_until(T0 + 75)
        check("not acked: a message heard during the start cooldown (not even after it)", r.radio.texts(), [])
        r.hear("KF7ABC", ":N9ABC    :early{12")
        await r.tick()
        check("  positive control: its next copy, after the cooldown, is", r.radio.texts(),
              [ACK.format("KF7ABC", "12")])
    finally:
        await r.close()


# ================================================================== matching

async def compose_sent(r, to="W7TST", text="hello", msgno=None):
    if msgno is not None:
        idx = aprs_msg.MSGNO_CHARS.index(msgno[0]) * 36 + aprs_msg.MSGNO_CHARS.index(msgno[1])
        await r.hub.aprs.call("set_meta", "next_msgno", idx)
    row = await r.eng.compose(to, text)
    await r.tick()
    clock.t += 3
    return row


async def t_matching():
    for reply, name in (("ack{m}}", "ackMM}"), ("ack{m}", "ackMM"), ("ack{m}}AA", "ackMM}AA")):
        r = await rig()
        try:
            row = await compose_sent(r)
            r.hear("W7TST", ":N9ABC    :" + reply.replace("{m}", row["msgno"]))
            await r.tick()
            check(f"{name} completes our pending message", (await r.row(row["id"]))["state"], "acked")
        finally:
            await r.close()
    r = await rig()
    try:
        row = await compose_sent(r)
        r.hear("W7TST", f":N9ABC    :sure{{X1}}{row['msgno']}")
        await r.tick()
        check("an incoming {MM}AA completes our AA (a free ack)", (await r.row(row["id"]))["state"], "acked")
    finally:
        await r.close()
    r = await rig()
    try:
        row = await compose_sent(r, msgno="12")
        for name, src, info in (("ack1 does not complete 12", "W7TST", ":N9ABC    :ack1"),
                                ("an ack from the wrong SSID", "W7TST-1", ":N9ABC    :ack12"),
                                ("from the wrong sender", "K7XYZ", ":N9ABC    :ack12"),
                                ("addressed to someone else", "W7TST", ":N9ABC-7  :ack12")):
            r.hear(src, info)
            await r.tick()
            check(f"{name}: changes nothing", (await r.row(row["id"]))["state"], "pending")
        q = await r.eng.compose("W7TST", "second")
        r.hear("W7TST", f":N9ABC    :ack{q['msgno']}")
        await r.tick()
        check("an ack for a queued (unsent) message changes nothing", (await r.row(q["id"]))["state"], "queued")
        r.hear("W7TST", ":N9ABC    :rej12")
        await r.tick()
        check("a rej completes ours as rejected", (await r.row(row["id"]))["state"], "rejected")
        check("  and the queued one to the same addressee is next", (await r.row(q["id"]))["state"], "pending")
        n = len(r.radio.frames)
        clock.t += 3
        await r.tick()
        check("  and goes out", [t.split("::")[1] for t in r.radio.texts(n)], [f"W7TST    :second{{{q['msgno']}}}"])
        clock.t += 3
        r.hear("W7TST", f":N9ABC    :ack{q['msgno']}")
        await r.tick()
        n = len(r.radio.frames)
        r.hear("W7TST", f":N9ABC    :ack{q['msgno']}")
        await r.run_until(clock.t + 40)
        check("an ack is never acked", r.radio.texts(n), [])
    finally:
        await r.close()


# ================================================================== retries

async def t_retry_slots():
    r = await rig()
    try:
        row = await r.eng.compose("W7TST", "retry me")
        t0 = clock.t + 1
        await r.run_until(t0 + 1500)
        sent = [round(t - t0) for t, raw in r.radio.frames]
        check("sent at t0 + 0, 30, 90, 210, 450, 930 exactly (1 s passes)", sent, [0, 30, 90, 210, 450, 930])
        got = await r.row(row["id"])
        check("  failed at the deadline (t0 + 1410), 6 tries", (got["state"], got["tries"], round(got["deadline"] - t0)),
              ("failed", 6, 1410))
    finally:
        await r.close()
    r = await rig()
    try:
        row = await r.eng.compose("W7TST", "stop on ack")
        await r.run_until(clock.t + 35)
        r.hear("W7TST", f":N9ABC    :ack{row['msgno']}")
        await r.run_until(clock.t + 300)
        check("retries stop on the ack", (len(r.radio.frames), (await r.row(row["id"]))["state"]), (2, "acked"))
    finally:
        await r.close()


async def t_hold_skips_slots():
    r = await rig()
    try:
        row = await r.eng.compose("W7TST", "through a listen window")
        clock.t += 1
        await r.tick()
        t0 = r.radio.frames[0][0]
        r.hub.set_hold("winlink-qsy", "listen window on 145.090", "channel 3")
        await r.run_until(t0 + 925, step=5)
        check("a listen-length hold over slots 1-4 (t0+30..930): nothing sent in them", len(r.radio.frames), 1)
        got = await r.row(row["id"])
        check("  still pending, one try: a blocked slot is not a try", (got["state"], got["tries"]), ("pending", 1))
        await r.run_until(t0 + 929.5, step=0.5)
        check("  to the end of slot 4's window (a blocked slot may go late only within it)", len(r.radio.frames), 1)
        clock.t = t0 + 930
        r.hub.release_hold("winlink-qsy", "test")
        await r.tick()
        check("released as slot 5 opens: the passed slots are not caught up", len(r.radio.frames), 2)
        check("  they were logged as missed (slots 2-5 in the log's 1-based count)", r.eng.counts["slots_missed"], 4)
        await r.run_until(t0 + 931, step=1)
        check("  slot 5 goes on time (t0+930)", [round(t - t0) for t, _ in r.radio.frames], [0, 930])
        await r.run_until(t0 + 1411, step=10)
        check("  then failed at the deadline, which never moved", ((await r.row(row["id"]))["state"],
              round((await r.row(row["id"]))["deadline"] - t0)), ("failed", 1410))
    finally:
        await r.close()


async def t_blocked_slot_later():
    r = await rig()
    try:
        await r.eng.compose("W7TST", "late in its slot")
        clock.t += 1
        await r.tick()
        t0 = r.radio.frames[0][0]
        await r.run_until(t0 + 29)
        k = Named("127.0.0.1:5000")
        r.hub.kiss_clients.add(k)
        await r.run_until(t0 + 40)
        check("a KISS client over t0+30: slot 1 not sent", len(r.radio.frames), 1)
        check("  the page names the client", "KISS client is connected (127.0.0.1:5000)" in (r.eng.pause_why() or ""), True)
        r.hub.kiss_clients.discard(k)
        await r.run_until(t0 + 41)
        check("gone at t0+40: slot 1 sent then, within its own slot", [round(t - t0) for t, _ in r.radio.frames], [0, 41])
        await r.run_until(t0 + 91)
        check("  slot 2 still on time (t0+90): one transmission per slot", [round(t - t0) for t, _ in r.radio.frames],
              [0, 41, 90])
    finally:
        await r.close()


async def t_t0_is_first_send():
    r = await rig()
    try:
        row = await r.eng.compose("W7TST", "t0 later")
        r.hub.set_hold("winlink-qsy", "x", "y")
        await r.run_until(clock.t + 100, step=5)
        check("composed, then held: nothing sent", len(r.radio.frames), 0)
        r.hub.release_hold("winlink-qsy", "t")
        await r.tick()
        t0 = r.radio.frames[0][0]
        got = await r.row(row["id"])
        check("t0 is the first actual transmission, not the compose", (got["first_sent"] == t0, got["t"] < t0 - 90),
              (True, True))
        await r.run_until(t0 + 31)
        check("  the next slot counts from it", [round(t - t0) for t, _ in r.radio.frames], [0, 30])
    finally:
        await r.close()


async def t_one_per_addressee():
    r = await rig()
    try:
        a = await r.eng.compose("W7TST", "first")
        b = await r.eng.compose("W7TST", "second")
        c = await r.eng.compose("K7XYZ", "other")
        await r.run_until(clock.t + 10)
        texts = [t.split("::")[1].split(":")[1] for t in r.radio.texts()]
        check("one in flight per addressee; another addressee goes too",
              sorted(texts), sorted([f"first{{{a['msgno']}}}", f"other{{{c['msgno']}}}"]))
        check("  the second to the same addressee waits queued", (await r.row(b["id"]))["state"], "queued")
        check("  paced: 2 s apart", round(r.radio.frames[1][0] - r.radio.frames[0][0]) >= 2, True)
    finally:
        await r.close()


# ================================================================== the gate, with acks

async def t_gate_reserves():
    r = await rig(extra=("--max-digi-per-min", "12"))
    try:
        for i in range(12):
            r.hub.transmit(Frame.ui("KF7XYZ", "APRS", f">digi {i}".encode(), digis=["N9ABC-1*"]), "digi", digi=True)
        r.hear("W7TST", ":N9ABC    :after digis{01")
        clock.t += 3
        await r.tick()
        check("12 digi frames in the minute, then an ack still goes", r.radio.texts()[-1:], [ACK.format("W7TST", "01")])
    finally:
        await r.close()
    r = await rig()
    try:
        sent = 0
        for i in range(5):
            try:
                r.hub.transmit(Frame.ui("N9ABC-7", "APRS", (">" + "x" * 370 + str(i)).encode()), "kiss:app")
                sent += 1
            except TxRefused:
                pass
        check("five max-length app frames: the non-ack airtime stops them at 3", sent, 3)
        r.hear("W7TST", ":N9ABC    :after big apps{02")
        clock.t += 3
        await r.tick()
        check("  then an ack still goes (its airtime reserve)", r.radio.texts()[-1:], [ACK.format("W7TST", "02")])
    finally:
        await r.close()
    r = await rig()
    try:
        from ax25 import Address
        ok = refused = 0
        for i in range(297):
            f = Frame(Address.from_text("W7TST-10"), Address.from_text("N9ABC-10"), kind="I", ns=i % 8, pid=0xF0,
                      info=b"x")
            try:
                r.hub.transmit(f, "agw:pat")
                ok += 1
            except TxRefused as e:
                refused += 1
                why = str(e)
        check("296 connected frames in a minute go; the 297th is refused", (ok, refused), (296, 1))
        check("  refusal names the cap and the reserve", "300 frames/min, the last 4 kept for acks" in why, True)
        r.hear("W7TST", ":N9ABC    :after a transfer{03")
        clock.t += 3
        await r.tick()
        check("  then an ack still goes", r.radio.texts()[-1:], [ACK.format("W7TST", "03")])
    finally:
        await r.close()
    r = await rig()
    try:
        for i in range(6):
            r.hub.transmit(Frame.ui("N9ABC-7", "APRS", f">app {i}".encode()), "kiss:app")
        await r.eng.compose("W7TST", "m1")
        await r.eng.compose("K7XYZ", "m2")
        await r.run_until(clock.t + 6)
        check("6 app + 2 msg sent", sum(1 for t in r.radio.texts() if "::" in t), 2)
        try:
            r.hub.transmit(Frame.ui("N9ABC-7", "APRS", b">ninth"), "kiss:app")
            ninth = "sent"
        except TxRefused as e:
            ninth = str(e)
        check("  a 9th non-ack UI frame is refused (4 kept for acks)", ninth,
              "UI total rate limit (12/min, the last 4 kept for acks)")
        n = len(r.radio.frames)
        for i, src in enumerate(("KA1A", "KB1B", "KC1C", "KD1D")):
            r.hear(src, f":N9ABC    :hi{{{10 + i}")
            clock.t += 2.1
            await r.tick()
        check("  while 4 acks still go", len(r.radio.texts(n)), 4)
    finally:
        await r.close()
    r = await rig()
    try:
        for i in range(20):
            r.hear(f"KA{i}AA", f":N9ABC    :burst{{{i}")
            clock.t += 2.1
            await r.tick()
        check("20 acks asked for in a minute: 6 sent (the ack cap)", len(r.radio.frames), 6)
        check("  the rest refused at the gate, counted", r.eng.counts["acks_refused"], 14)
    finally:
        await r.close()


# ================================================================== pauses and races

async def t_pauses():
    r = await rig()
    try:
        cases = [("an AGWPE client with monitor on", Named("pat", mon=True)),
                 ("an AGWPE client with raw on", Named("yaac", raw=True))]
        for name, c in cases:
            r.hub.agw_clients.add(c)
            n = len(r.radio.frames)
            r.hear("W7TST", f":N9ABC    :{name}{{1{len(name) % 10}")
            await r.tick()
            check(f"{name}: no ack, and the page names it",
                  (r.radio.texts(n), c.name in (r.eng.pause_why() or "")), ([], True))
            r.hub.agw_clients.discard(c)
            clock.t += 3
        r.hub.agw_clients.add(Named("pat"))
        n = len(r.radio.frames)
        r.hear("W7TST", ":N9ABC    :pat only{99")
        await r.tick()
        check("an AGWPE client with neither (Pat): acks continue", r.radio.texts(n), [ACK.format("W7TST", "99")])
    finally:
        await r.close()


async def t_races():
    for name, act in (("Off", lambda h: h.switch_tx(False, "test")),
                      ("a KISS client connecting", lambda h: h.kiss_clients.add(Named("k"))),
                      ("an off-channel hold", lambda h: h.set_hold("winlink-qsy", "x", "y")),
                      (None, lambda h: None)):
        r = await rig()
        try:
            row = await r.eng.compose("W7TST", "race")
            go = asyncio.Event()

            async def hook(what, item):
                if what == "before_transmit":
                    await go.wait()
            r.eng.hook = hook
            clock.t += 1
            task = asyncio.create_task(r.eng.tick())
            await asyncio.sleep(0.05)               # the pass is held after its decision, before transmit()
            act(r.hub)
            go.set()
            await task
            got = await r.row(row["id"])
            if name is None:
                check("race positive control: nothing in the gap, the frame goes", (len(r.radio.frames), got["tries"]),
                      (1, 1))
            else:
                check(f"race: {name} between the decision and transmit(): nothing written",
                      (len(r.radio.frames), got["tries"], got["first_sent"], got["state"]), (0, 0, None, "pending"))
        finally:
            await r.close()


async def t_restart_mid_retry():
    logdir = tempfile.mkdtemp(prefix="aprsmsg-")
    r = await rig(logdir=logdir)
    row = await r.eng.compose("W7TST", "crash")
    clock.t += 1
    await r.tick()
    t0 = r.radio.frames[0][0]
    clock.t = t0 + 30

    class Killed(BaseException):
        pass

    def die(name):
        if name == "aprs-after-write":
            raise Killed()
    saved = aprs_msg._test_point
    aprs_msg._test_point = die
    try:
        await r.eng.tick()
    except Killed:
        pass
    finally:
        aprs_msg._test_point = saved
    check("killed after radio.write in slot 1: two frames on air", len(r.radio.frames), 2)
    check("  the row says sending", (await r.row(row["id"]))["state"], "sending")
    await r.close(keep=True)
    clock.t = t0 + 35
    r2 = await Rig().open(logdir=logdir, cooldown=0)
    try:
        got = await r2.row(row["id"])
        check("the next process counts slot 1 as tried", (got["state"], got["tries"]), ("pending", 2))
        await r2.run_until(t0 + 89)
        check("  and does not send slot 1 again", len(r2.radio.frames), 0)
        await r2.run_until(t0 + 91)
        check("  positive control: slot 2 goes at t0+90", [round(t - t0) for t, _ in r2.radio.frames], [90])
    finally:
        await r2.close(keep=True)
    clock.t = t0 + 2000
    r3 = await Rig().open(logdir=logdir, cooldown=0)
    try:
        got = await r3.row(row["id"])
        check("a restart past the deadline: failed (restart)", (got["state"], got["why"]), ("failed", "restart: past its deadline"))
    finally:
        await r3.close()


async def t_restart_cooldown():
    logdir = tempfile.mkdtemp(prefix="aprsmsg-")
    r = await rig(logdir=logdir)
    row = await r.eng.compose("W7TST", "resume")
    clock.t += 1
    await r.tick()
    t0 = r.radio.frames[0][0]
    await r.close(keep=True)
    clock.t = t0 + 50                         # restarted between slot 1 (30) and slot 2 (90): slot 1 missed
    r2 = await Rig().open(logdir=logdir)      # 60 s cooldown: to t0+110
    try:
        await r2.run_until(t0 + 109)
        check("restart: nothing during the cooldown (not at once)", len(r2.radio.frames), 0)
        await r2.run_until(t0 + 111)
        check("  resumes in its current slot after the cooldown", [round(t - t0) for t, _ in r2.radio.frames], [110])
        got = await r2.row(row["id"])
        check("  pending, not failed", got["state"], "pending")
    finally:
        await r2.close()


# ================================================================== send vs full

async def t_modes():
    r = await rig(mode="send", fw="null")
    try:
        check("send mode: effective send", r.eng.status()["effective"], "send")
        r.hear("W7TST", ":N9ABC    :hello{AB}")
        await r.run_until(clock.t + 40)
        check("send: a peer's {AB} message is never acked", r.radio.texts(), [])
        row = await r.eng.compose("W7TST", "reply")
        await r.tick()
        check("send: our next message carries {MM} only, never {MM}AB", r.radio.texts()[-1].split("::")[1],
              f"W7TST    :reply{{{row['msgno']}}}")
    finally:
        await r.close()
    r = await rig(mode="full", fw="false")
    try:
        r.hear("W7TST", ":N9ABC    :hello{AB}")
        await r.tick()
        check("full (positive control): the same frame is acked", r.radio.texts(), [ACK.format("W7TST", "AB")])
        clock.t += 3
        row = await r.eng.compose("W7TST", "reply")
        await r.tick()
        check("full: our next message to them carries the free ack {MM}AB", r.radio.texts()[-1].split("::")[1],
              f"W7TST    :reply{{{row['msgno']}}}AB")
    finally:
        await r.close()
    r = await rig(mode="full", fw="null")
    try:
        st = r.eng.status()
        check("full with firmware_acks null: starts as send, says why",
              (st["mode"], st["effective"], "firmware_acks" in (st["why"] or "")), ("full", "send", True))
        r.hear("W7TST", ":N9ABC    :hello{12")
        await r.run_until(clock.t + 5)
        check("  and sends no ack", r.radio.texts(), [])
    finally:
        await r.close()


async def t_firmware_readout():
    r = await rig(mode="send", fw="null")
    try:
        r.hear("W7TST", ":N9ABC    :are you there{42")
        clock.t += 4
        r.hear("N9ABC", ":W7TST    :ack42", digis=["K7RDG-1", "WIDE2-1"], used=1)
        ev = r.eng.fw_readout()
        check("an RF message, then a digi copy of N9ABC's ack not sent by us: 'ack seen, not ours'",
              [(e["from"], e["msgno"], e["verdict"], e["ack"]["via"]) for e in ev], [("W7TST", "42", "ack seen, not ours", "RF")])
        r.hear("K7XYZ", ":N9ABC    :anyone{43")
        clock.t += 121
        check("  no ack in 120 s: 'none seen'", r.eng.fw_readout()[0]["verdict"], "none seen")
        r.hub.on_is_line(clock(), "K7Q>APRS,TCPIP*,qAC,T2X::N9ABC    :is only{44")
        check("  an IS-only inbound message is not evidence", [e["msgno"] for e in r.eng.fw_readout()], ["43", "42"])
        check("  the readout is in status", len(r.eng.status()["firmware_ack_evidence"]), 2)
    finally:
        await r.close()


async def t_is_never_acked():
    r = await rig()
    try:
        r.hear("W7TST", ":N9ABC    :over RF{61")
        await r.tick()
        check("positive control (effective full): an RF message with msgno is acked",
              (r.eng.effective, r.radio.texts()), ("full", [ACK.format("W7TST", "61")]))
        n = len(r.radio.frames)
        clock.t += 31
        r.hub.on_is_line(clock(), "W7TST>APRS,TCPIP*,qAC,T2TEST::N9ABC    :over RF{61")
        r.hub.on_is_line(clock(), "W7TST>APRS,TCPIP*,qAC,T2TEST::N9ABC    :new one{62")
        await r.run_until(clock.t + 120, step=2)
        check("the same message as an APRS-IS line: nothing on the radio, ever", r.radio.texts(n), [])
        row = await r.eng.compose("K7XYZ", "match test")
        await r.tick()
        r.hub.on_is_line(clock(), f"K7XYZ>APRS,TCPIP*,qAC,T2TEST::N9ABC    :ack{row['msgno']}")
        await r.tick()
        check("an IS ack never completes our message", (await r.row(row["id"]))["state"], "pending")
        await r.settle_store()
        rows = await r.hub.aprs.call("thread", "W7TST", ["N9ABC"])
        check("  the IS copy is stored as port 'is' (shown 'via internet')", sorted({x["port"] for x in rows}), ["is", "rf"])
    finally:
        await r.close()


async def t_contract():
    r = await rig()
    try:
        for name, text in (("non-ASCII", "héllo"), ("68 characters", "x" * 68), ("{", "a{b"), ("|", "a|b"), ("empty", "  ")):
            try:
                await r.eng.compose("W7TST", text)
                got = "accepted"
            except aprs_msg.MsgError as e:
                got = "refused" if str(e).startswith("text") else str(e)
            check(f"compose refuses {name}", got, "refused")
        try:
            await r.eng.compose("NOT A CALL", "x")
        except aprs_msg.MsgError as e:
            check("compose refuses a bad callsign", str(e).startswith("to:"), True)
        text = "A" * 30 + " 0123456789 !\"#$%&'()*+,-./:;<=>?@[]^_`" [:37]
        check("  (the boundary text is 67 characters)", len(text), 67)
        row = await r.eng.compose("w7tst-9", text)
        await r.tick()
        stored = await r.row(row["id"])
        check("67 characters: stored exactly as sent, to the normalised call", (stored["text"], stored["dst"]),
              (text, "W7TST-9"))
        f = Frame.decode(r.radio.frames[-1][1])
        check("  the frame carries exactly that text and msgno", f.info.decode(),
              f":W7TST-9  :{text}{{{stored['msgno']}}}")
        check("  from the owned call, TOCALL, path", (str(f.src), str(f.dest), [str(d) for d in f.digis]),
              ("N9ABC", "APZRR0", ["WIDE1-1", "WIDE2-1"]))
        r.hub.switch_tx(False, "t")
        try:
            await r.eng.compose("K7XYZ", "while off")
            got = "accepted"
        except aprs_msg.MsgError as e:
            got = str(e)
        check("compose while transmit is off: refused, saying why", got.startswith("not now: transmit is stopped"), True)
    finally:
        await r.close()


async def t_msgno():
    r = await rig()
    try:
        await r.hub.aprs.call("set_meta", "next_msgno", 36 * 36 - 1)
        a = await r.eng.compose("W7TST", "a")
        b = await r.eng.compose("K7XYZ", "b")
        check("msgno: two characters [0-9A-Z], wrapping", (a["msgno"], b["msgno"]), ("ZZ", "00"))
        await r.hub.aprs.call("set_meta", "next_msgno", 36 * 36 - 1)
        c = await r.eng.compose("KA1AAA", "c")
        check("  not reused while one with it is pending", c["msgno"], "01")
    finally:
        await r.close()


async def main():
    for t in (t_acking, t_not_acked, t_matching, t_retry_slots, t_hold_skips_slots, t_blocked_slot_later,
              t_t0_is_first_send, t_one_per_addressee, t_gate_reserves, t_pauses, t_races, t_restart_mid_retry,
              t_restart_cooldown, t_modes, t_firmware_readout, t_is_never_acked, t_contract, t_msgno):
        if os.environ.get("ONLY") and os.environ["ONLY"] not in t.__name__:
            continue
        print(f"--- {t.__name__}")
        try:
            await t()
        except Exception as e:     # noqa: BLE001
            global fails
            fails += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL  {t.__name__} raised {type(e).__name__}: {e}")


asyncio.run(main())
print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
