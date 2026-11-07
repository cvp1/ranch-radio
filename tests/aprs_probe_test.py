"""The APRS round-trip probe (aprs_probe.py) in process, on a compressed clock.

A real tnc_hub.Hub, its store, the messaging engine and the probe, with the
radio replaced at its write boundary (as tests/aprs_msg_test.py) and the
APRS-IS observer standing in as an object with its login and gaps; IS lines
go in through Hub.on_is_line as the observer would hand them over.

  - happy path: digi copy (L2), IS copy qAR,K7RDG-1 (L3), a }-wrapped ack of
    this msgno from the target via AA7JB (L4, L5): five yes with evidence
  - a direct iGate, no digi copy: L2 "no digipeated copy observed"
  - wrong evidence is never yes: an IS line with H-bits, qAC, our own
    login, wrong info or dest, an earlier probe's msgno, an ack before
    t_first, unrelated target traffic, an IS copy of the target's ack
  - coverage: observer down for part of the window -> L3 unknown; held off
    channel for part of it without an ack -> L4, L5 unknown; evidence seen
    before a gap stays yes
  - late replies at t_first + 931 and + 1,409 s are inside the window
  - refusals; a version 1 store file migrates; a probe survives a restart

    .venv/bin/python tests/aprs_probe_test.py
"""
import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.pop("CC_OBS_TOKENS_FILE", None)

import aprs_msg  # noqa: E402
import aprs_probe  # noqa: E402
import aprs_store  # noqa: E402
import tnc_hub  # noqa: E402
from ax25 import Frame  # noqa: E402

fails = 0
T0 = 1_800_000_000.0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:70} got {got!r}" + ("" if ok else f" want {want!r}"))
    return ok


class Clock:
    t = T0

    def __call__(self):
        return self.t


clock = Clock()
tnc_hub.now = clock


class Radio:
    def __init__(self):
        self.up, self.held, self.frames, self.spec = True, False, [], "fake"
        self.opened_at = self.last_rx = self.error = None

    def write(self, raw):
        self.frames.append((clock(), raw))

    def texts(self):
        return [Frame.decode(r).tnc2() for _, r in self.frames]


class Observer:
    """The iGate as the probe reads it: its login and the observer's gaps."""

    def __init__(self, gaps=None):
        self.login, self.observe = "N9ABC-10", "b/N9ABC* g/N9ABC*"
        self.gaps = gaps if gaps is not None else [[T0 - 100, T0 - 99]]

    def offer(self, f):
        pass


class Rig:
    async def open(self, mode="send", fw="null", logdir=None, target="WXBOT", text="wx 85602", observer=True):
        self.logdir = logdir or tempfile.mkdtemp(prefix="probe-")
        args = tnc_hub.parse_args(["--tnc", "/dev/null", "--mycall", "N9ABC", "--tx", "--log-dir", self.logdir,
                                   "--aprs-messaging", mode, "--aprs-firmware-acks", fw, "--aprs-cooldown", "0"])
        self.hub = h = tnc_hub.Hub(args)
        h.radio = self.radio = Radio()
        h.igate = Observer() if observer else None
        loop = asyncio.get_running_loop()
        h.aprs = await loop.run_in_executor(None, lambda: aprs_store.AprsStore(os.path.join(self.logdir, "aprs.db")).start())
        self.eng = h.aprs_msg = aprs_msg.Engine(h, mode, args.aprs_owned_calls, args.aprs_firmware_acks, args.aprs_path,
                                                cooldown_s=0, clock=clock)
        await self.eng.start(run=False)
        self.probe = h.probe = aprs_probe.Probe(h, self.eng, target, text, clock=clock)
        await self.probe.start(run=False)
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

    async def tick(self):
        await self.eng.tick()
        await self.eng.settle()
        await self.probe.tick()

    async def run_until(self, t, step=5.0):
        while clock.t + step <= t:
            clock.t += step
            await self.tick()
        clock.t = t
        await self.tick()

    async def launch(self):
        p = await self.probe.begin(armed=True)
        clock.t += 1
        await self.tick()
        return p, self.radio.frames[-1][0]

    def info(self, p):
        return Frame.decode(self.radio.frames[0][1]).info.decode()

    async def stored(self, pid):
        return next(x for x in await self.hub.aprs.call("probes", 50) if x["id"] == pid)


def legs(p):
    return {k: v["v"] for k, v in p["legs"].items()}


async def rig(**kw):
    clock.t = T0
    return await Rig().open(**kw)


async def t_happy():
    r = await rig()
    try:
        p, t1 = await r.launch()
        info = r.info(p)
        check("the probe message: target, text with a 3-character token, msgno",
              (r.radio.texts()[0].split("::")[1].split("{")[0], len(p["token"])), (f"WXBOT    :wx 85602 {p['token']}", 3))
        clock.t += 2
        r.hear("N9ABC", info, dest="APZRR0", digis=["K7RDG-1", "WIDE2-1"], used=1)
        clock.t += 1
        r.hub.on_is_line(clock(), f"N9ABC>APZRR0,K7RDG-1*,WIDE2-1,qAR,K7RDG-1:{info}")
        clock.t += 20
        r.hear("AA7JB", f"}}WXBOT>APRS,TCPIP,AA7JB*::N9ABC    :ack{p['msgno']}", dest="APDW18")
        await r.tick()
        live = r.probe.describe(r.probe.cur)
        check("while running: five yes as the evidence arrives", legs(live), {k: "yes" for k in aprs_probe.LEGS})
        await r.run_until(t1 + aprs_probe.WINDOW_S + 1, step=30)
        done = await r.stored(p["id"])
        check("after the window: stored, five yes", (done["state"], legs(done)), ("done", {k: "yes" for k in aprs_probe.LEGS}))
        ev = {k: v["evidence"] for k, v in done["legs"].items()}
        check("  L2: digipeated by K7RDG-1", ev["L2"].startswith("digipeated by K7RDG-1"), True)
        check("  L3: qAR by the iGate K7RDG-1", ev["L3"].startswith("qAR by the iGate K7RDG-1"), True)
        check("  L4: the ack, via the IS->RF iGate AA7JB", ev["L4"].startswith("ack from WXBOT via the IS->RF iGate AA7JB"), True)
        check("  window = t_first + 1,470 s", round(done["window_end"] - done["t_first"]), 1470)
    finally:
        await r.close()


async def t_direct_igate():
    r = await rig()
    try:
        p, t1 = await r.launch()
        info = r.info(p)
        r.hub.on_is_line(clock() + 1, f"N9ABC>APZRR0,WIDE1-1,WIDE2-1,qAO,AA7JB:{info}")
        clock.t += 15
        r.hear("WXBOT", f":N9ABC    :ack{p['msgno']}")
        await r.run_until(t1 + aprs_probe.WINDOW_S + 1, step=30)
        done = await r.stored(p["id"])
        check("a direct iGate: L2 not seen, L3-L5 yes", legs(done),
              {"L1": "yes", "L2": "not seen", "L3": "yes", "L4": "yes", "L5": "yes"})
        check("  worded 'no digipeated copy observed' (not an antenna diagnosis)", done["legs"]["L2"]["why"],
              "no digipeated copy observed")
    finally:
        await r.close()


async def t_wrong_evidence():
    r = await rig()
    try:
        # an earlier probe, its window run out: its msgno is not this one's
        old, t_old = await r.launch()
        await r.run_until(t_old + aprs_msg.DEADLINE_S + 1, step=60)
        p = await r.probe.begin(armed=True)                         # composed; the engine sends on its next pass
        r.hear("WXBOT", f":N9ABC    :ack{p['msgno']}")              # an ack before our first transmission
        await r.eng.settle()
        clock.t += 1
        await r.tick()
        t1 = r.radio.frames[-1][0]
        info = Frame.decode(r.radio.frames[-1][1]).info.decode()
        clock.t += 1
        r.hub.on_is_line(clock(), f"N9ABC>APZRR0,K7RDG-1*,WIDE2-1,qAC,T2TEST:{info}")      # H-bits on an IS line; qAC
        r.hub.on_is_line(clock(), f"N9ABC>APZRR0,WIDE1-1,qAO,N9ABC-10:{info}")              # our own login
        r.hub.on_is_line(clock(), f"N9ABC>APZRR0,WIDE1-1,qAR,K7RDG-1:{info}X")              # wrong info
        r.hub.on_is_line(clock(), f"N9ABC>APRS,WIDE1-1,qAR,K7RDG-1:{info}")                 # wrong dest
        r.hear("N9ABC", info + "X", dest="APZRR0", digis=["K7RDG-1"], used=1)               # wrong info on RF
        r.hear("N9ABC", info, dest="APRS", digis=["K7RDG-1"], used=1)                       # wrong dest on RF
        r.hear("N9ABC", info, dest="APZRR0", digis=["WIDE1-1"], used=0)                     # no H-bit
        r.hear("WXBOT", f":N9ABC    :ack{old['msgno']}")                                    # the earlier probe's msgno
        r.hear("WXBOT", ":N9ABC    :unrelated weather for you")                             # unrelated target traffic
        r.hub.on_is_line(clock(), f"WXBOT>APRS,TCPIP*,qAC,WXBOT::N9ABC    :ack{p['msgno']}")  # the target's ack over IS
        await r.tick()
        live = r.probe.describe(r.probe.cur)
        check("none of it is yes (L1 only)", legs(live),
              {"L1": "yes", "L2": "waiting", "L3": "waiting", "L4": "waiting", "L5": "waiting"})
        notes = r.probe.cur["notes"]
        check("  IS copies not via RF are noted, with why",
              (any("qAC" in n for n in notes), any("our own login" in n for n in notes)), (True, True))
        check("  the unrelated target message is noted 'not correlated'",
              any(n.startswith("target traffic seen, not correlated") for n in notes), True)
        check("  the early ack didn't complete the message", (await r.hub.aprs.call("msg_get", p["msg_id"]))["state"],
              "pending")
        await r.run_until(t1 + aprs_probe.WINDOW_S + 1, step=60)
        done = await r.stored(p["id"])
        check("after the window: L2-L5 not seen (coverage complete), L5 never from IS", legs(done),
              {"L1": "yes", "L2": "not seen", "L3": "not seen", "L4": "not seen", "L5": "not seen"})
    finally:
        await r.close()


async def t_coverage():
    r = await rig()
    try:
        p, t1 = await r.launch()
        info = r.info(p)
        r.hear("N9ABC", info, dest="APZRR0", digis=["K7RDG-1", "WIDE2-1"], used=1)
        r.hub.igate.gaps.append([t1 + 100, t1 + 200])                                       # observer down for a while
        await r.run_until(t1 + 300)
        r.hub.set_hold("winlink-qsy", "Winlink on 145.090", "channel 1")                     # off channel for a while
        await r.run_until(t1 + 600)
        r.hub.release_hold("winlink-qsy", "t")
        await r.run_until(t1 + aprs_probe.WINDOW_S + 1, step=30)
        done = await r.stored(p["id"])
        check("observer down part of the window: L3 unknown; held part of it, no ack: L4, L5 unknown", legs(done),
              {"L1": "yes", "L2": "yes", "L3": "unknown", "L4": "unknown", "L5": "unknown"})
        check("  L2, seen before the gap, stays yes", done["legs"]["L2"]["v"], "yes")
        check("  the reasons", (done["legs"]["L3"]["why"], done["legs"]["L4"]["why"]),
              ("observer coverage incomplete", "RF coverage incomplete (off channel (held))"))
    finally:
        await r.close()
    r = await rig(observer=False)
    try:
        p, t1 = await r.launch()
        await r.run_until(t1 + aprs_probe.WINDOW_S + 1, step=60)
        done = await r.stored(p["id"])
        check("no observer at all: L3 unknown, never 'not seen'", (done["legs"]["L3"]["v"], done["legs"]["L3"]["why"]),
              ("unknown", "no APRS-IS observer"))
    finally:
        await r.close()


async def t_late_replies():
    for late in (931, 1409):
        r = await rig()
        try:
            p, t1 = await r.launch()
            await r.run_until(t1 + late, step=7)
            r.hear("WXBOT", f":N9ABC    :ack{p['msgno']}")
            await r.run_until(t1 + aprs_probe.WINDOW_S + 1, step=30)
            done = await r.stored(p["id"])
            check(f"an ack at t_first + {late} s is inside the window: L4, L5 yes",
                  (done["legs"]["L4"]["v"], done["legs"]["L5"]["v"]), ("yes", "yes"))
        finally:
            await r.close()


async def t_refusals():
    async def why(r, armed=True):
        try:
            await r.probe.begin(armed)
            return "started"
        except aprs_probe.ProbeError as e:
            return str(e)
    r = await rig()
    try:
        check("not armed", await why(r, armed=False), "arm TX on the page first")
        r.hub.set_hold("winlink-qsy", "x", "y")
        check("held", (await why(r)).startswith("not now: off-channel hold"), True)
        r.hub.release_hold("winlink-qsy", "t")
        k = Observer()                    # any hashable object with a name stands in for a KISS client
        k.name = "k"
        r.hub.kiss_clients.add(k)
        check("paused (a KISS client)", (await why(r)).startswith("not now: a KISS client"), True)
        r.hub.kiss_clients.discard(k)
        r.hub.switch_tx(False, "t")
        check("transmit Off", await why(r), "not now: transmit is stopped by the operator")
        r.hub.switch_tx(True, "t")
        check("positive control: then it starts", await why(r), "started")
        clock.t += 1
        await r.tick()
        clock.t += aprs_msg.DEADLINE_S - 10
        check("a second probe before the previous deadline", (await why(r)).startswith("the previous probe's deadline"), True)
        clock.t += 11
        check("  after it: starts", await why(r), "started")
    finally:
        await r.close()
    r = await rig(target=None)
    try:
        check("no target", (await why(r)).startswith("no probe target"), True)
    finally:
        await r.close()
    r = await rig(mode="off")
    try:
        check("messaging off", await why(r), "messaging is off")
    finally:
        await r.close()


async def t_schema_and_restart():
    d = tempfile.mkdtemp(prefix="probe-v1-")
    v1 = os.path.join(d, "aprs_store_v1.py")
    path = os.path.join(d, "v1.db")
    src = subprocess.run(["git", "show", "origin/main:aprs_store.py"], cwd=ROOT, capture_output=True, text=True)
    if src.returncode == 0 and "SCHEMA_VERSION = 1" in src.stdout:
        with open(v1, "w") as f:
            f.write(src.stdout)
        made = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(1, sys.argv[2]);"
                               "import aprs_store_v1 as s; db = s.Db(sys.argv[3]);"
                               "db.ingest(1.0, 'rf', 'W7TST>APRS::N9ABC    :old{1'); db.close()", d, ROOT, path],
                              capture_output=True, text=True)
        check("a version 1 file made by main's aprs_store", (made.returncode, made.stderr[-200:]), (0, ""))
    else:
        # A shallow checkout (CI) has no main: version 1's own DDL, the same tables.
        print("note  origin/main not here: the version 1 file is built from schema 1's DDL")
        con = sqlite3.connect(path)
        con.executescript(aprs_store.SCHEMA)
        con.execute("PRAGMA user_version=1")
        con.execute("INSERT INTO messages (t, dir, port, src, dst, text, msgno, state) VALUES "
                    "(1.0, 'in', 'rf', 'W7TST', 'N9ABC', 'old', '1', 'received')")
        con.commit()
        con.close()
    if True:
        con = sqlite3.connect(path)
        check("  it is version 1, without probes", (con.execute("PRAGMA user_version").fetchone()[0],
              con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='probes'").fetchone()[0]), (1, 0))
        con.close()
        db = aprs_store.Db(path)
        check("opened by this code: the current version, probes added, its messages kept",
              (db.db.execute("PRAGMA user_version").fetchone()[0], db.probes(),
               [m["text"] for m in db.messages()]), (aprs_store.SCHEMA_VERSION, [], ["old"]))
        db.close()
    shutil.rmtree(d, ignore_errors=True)
    logdir = tempfile.mkdtemp(prefix="probe-")
    r = await rig(logdir=logdir)
    p, t1 = await r.launch()
    info = r.info(p)
    r.hear("N9ABC", info, dest="APZRR0", digis=["K7RDG-1"], used=1)
    await r.run_until(t1 + 100)
    await r.close(keep=True)
    clock.t = t1 + 400                                  # down for 300 s
    r2 = await Rig().open(logdir=logdir)
    try:
        cur = r2.probe.cur
        check("after a restart the probe resumes, its L2 kept", (cur["id"], cur["state"], cur["legs"]["L2"]["v"]),
              (p["id"], "running", "yes"))
        r2.hear("WXBOT", f":N9ABC    :ack{p['msgno']}")
        await r2.run_until(t1 + aprs_probe.WINDOW_S + 1, step=30)
        done = await r2.stored(p["id"])
        check("  the downtime is an RF gap; the ack after it still counts", legs(done),
              {"L1": "yes", "L2": "yes", "L3": "unknown", "L4": "yes", "L5": "yes"})
        check("  listed by api/aprs/probes' query, newest first", [x["id"] for x in await r2.hub.aprs.call("probes", 20)][:1],
              [p["id"]])
    finally:
        await r2.close()


async def main():
    for t in (t_happy, t_direct_igate, t_wrong_evidence, t_coverage, t_late_replies, t_refusals, t_schema_and_restart):
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
