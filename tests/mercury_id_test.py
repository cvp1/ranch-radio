"""The station's own CW ID on Mercury HF's lane (mercury_id.py, plan
winlink-mercury M0), in process: a real server.Station on the simulator, its
safety loop running, Mercury as an in-process client of its lane
(rigctl_server.RigctlSession, modem "mercury"). Nothing here transmits.
Time limits are shortened per case (gate, bound, audio window).

  - negative control: the station ID's own T 1 unarmed -> RPRT -11, no PTT;
    armed but not scheduled -> -11; in an ARDOP session -> -11
  - cross-lane: xara's lane refuses during a Mercury session; Mercury's lane
    refuses during an ARDOP session
  - end of session: the ID goes after Mercury's key is released, never under
    it; on air (source "station"), not owed; nothing owed -> no ID
  - a refused Mercury T 1 (fenced) opens the audio window: the ID waits for
    that client's T 0, or the window's end without one
  - 10-minute gate: Mercury keyed past the gate (busy) -> the ID goes at its
    release; idle -> at once; Mercury keys again after it
  - collision: Mercury's T 1 during the ID cuts it at once, owed, no retry,
    the session ends; nothing keys afterwards
  - the session bound: end_request to the arming socket, Mercury fenced, the
    station's winlink_end -> ID -> disarm; with no answer the backstop does it
  - STOP TX, owner loss (disarm) and audio failure mid-ID: cut, owed, never
    keyed again; a task from before a re-arm never keys in the new session
  - failed connect then a late retry: the end ID, then -11, no ID
  - the station's RemoteLane turns end_request into its winlink.abort
  - panel p59d35d3ea8: TUNE refused on every Winlink lane (main port still
    tunes); a queued CI-V key-down dropped on disarm and owner loss; the
    rig's TX OFF before Mercury hears its refusal; a keepalive cut or a
    disconnect opens the full window; per-client windows kept across arms;
    no arm over a Mercury session; the timing budget from the constants;
    Mercury asking again and again after the fence; playback raising;
    exact ledger order for the station's own ID

    python3 tests/mercury_id_test.py
"""
import argparse
import asyncio
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.pop("CC_OBS_TOKENS_FILE", None)
import mercury_id  # noqa: E402
import rigctl_server  # noqa: E402
import rigs  # noqa: E402
import server  # noqa: E402
from sim import SimRadio  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:70} got {got!r}" + ("" if ok else f" want {want!r}"))


class Args(argparse.Namespace):
    def __getattr__(self, k):          # every flag not set here: off
        return None


class Audio:
    """Counts chunks; fails from chunk `fail_at` on (None: never)."""
    def __init__(self, fail_at=None):
        self.chunks, self.fail_at = 0, fail_at

    async def play(self, pcm):
        self.chunks += 1
        return self.fail_at is None or self.chunks < self.fail_at


class WS:
    """The arming socket (the station's RemoteLane)."""
    closed = False

    def __init__(self):
        self.sent = []

    async def send_str(self, m):
        self.sent.append(dict(json.loads(m), _at=time.monotonic()))

    async def send_bytes(self, m):
        pass


def make(gate_s=600, bound_s=600, audio=None, sim_args=None):
    a = Args(no_tx=False, sim=True, tx_timeout=60, tune_timeout=10, winlink_rigctl_port=1,
             winlink_vara_modem="mercury", winlink_mercury_rigctl_port=2, winlink_mercury_call="N0CALL",
             winlink_vara_log="/nonexistent/xara.log", winlink_vara_rigctl_port=3,
             winlink_mercury_test_gate_s=gate_s, winlink_mercury_test_bound_s=bound_s, **(sim_args or {}))
    st = server.Station(SimRadio(rigs.get("ic7300")), audio if audio is not None else Audio(), a)
    return st


async def up(st):
    await st.radio.start()
    st._safety = asyncio.ensure_future(st.safety_loop())
    await asyncio.sleep(0.1)


async def down(st):
    await st.winlink_disarm("test over")
    st._safety.cancel()
    await st.radio.stop()


def lane(st, modem="mercury", peer="mercury"):
    return rigctl_server.RigctlSession(st, peer, lane="winlink", modem=modem)


async def T(sess, on):
    r, _ = await sess.handle(f"T {1 if on else 0}")
    return r.strip()


async def arm(st, scheme="varahf"):
    ws = WS()
    await st.winlink_arm(ws, True, scheme=scheme)
    return ws


def tx(st):
    return bool(st.radio.state.get("tx"))


def last(st):
    i = st.id_ledger.last("station")
    return i and (i["on_air"], i["end"] is not None)


async def until(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


async def key_watch(st, out):
    """Records (monotonic, who) every time the radio goes into TX."""
    was = False
    while True:
        now = tx(st)
        if now and not was:
            o = st.ptt_owner
            out.append((time.monotonic(), "id" if getattr(o, "station_id", False) else "mercury"))
        was = now
        await asyncio.sleep(0.01)


async def negative_controls():
    st = make()
    await up(st)
    ident = st.mercury_ident
    ident._allow = True
    check("unarmed: the station ID's own T 1 -> RPRT -11", await T(ident.sess, True), "RPRT -11")
    check("unarmed: no PTT", (tx(st), st.ptt_owner), (False, None))
    ident._allow = False
    m = lane(st)
    check("unarmed: Mercury's T 1 -> RPRT -11", await T(m, True), "RPRT -11")
    await arm(st)
    check("armed (Mercury): the backend is mercury", st.winlink_lane_modem, "mercury")
    check("armed, not scheduled: the station ID's T 1 -> RPRT -11", await T(ident.sess, True), "RPRT -11")
    check("armed, not scheduled: no PTT", tx(st), False)
    x = lane(st, "varahf", "xara")
    check("Mercury session: xara's lane T 1 -> RPRT -11", await T(x, True), "RPRT -11")
    check("Mercury session: ARDOP's lane T 1 -> RPRT -11", await T(lane(st, "ardop", "pat"), True), "RPRT -11")
    check("Mercury session: Mercury's lane T 1 -> RPRT 0", await T(m, True), "RPRT 0")
    await T(m, False)
    await st.winlink_disarm("next")
    await arm(st, "ardop")
    check("ARDOP session: Mercury's lane T 1 -> RPRT -11", await T(m, True), "RPRT -11")
    ident._allow = True
    check("ARDOP session: the station ID's T 1 -> RPRT -11", await T(ident.sess, True), "RPRT -11")
    ident._allow = False
    check("ARDOP session: no PTT", tx(st), False)
    await down(st)


async def end_of_session():
    st = make()
    await up(st)
    keys = []
    w = asyncio.ensure_future(key_watch(st, keys))
    ws = await arm(st)
    m = lane(st)
    check("end: Mercury keys", await T(m, True), "RPRT 0")
    started = time.time() - 1
    end = asyncio.ensure_future(st._winlink_end(ws, started, True, None))
    await asyncio.sleep(1.0)
    check("end: no ID under Mercury's key (still Mercury's)", [k[1] for k in keys], ["mercury"])
    check("end: fenced: Mercury's next T 1 refused", st.mercury_ident.fence, "the end of the session")
    released = time.monotonic()
    await T(m, False)
    await asyncio.wait_for(end, 15)
    check("end: the ID keyed after Mercury's release", [k[1] for k in keys], ["mercury", "id"])
    check("end: ID keyed promptly after the release (< 1 s)", keys[-1][0] - released < 1.0, True)
    check("end: on air, source station", last(st), (True, True))
    check("end: not owed", st.id_ledger.owed(), False)
    check("end: disarmed after the ID, radio in RX", (st.winlink_armed, tx(st)), (False, False))
    # nothing owed: no ID
    ws = await arm(st)
    n = len(keys)
    await asyncio.wait_for(st._winlink_end(ws, time.time(), True, None), 5)
    check("end, Mercury never keyed: no ID", len(keys), n)
    w.cancel()
    await down(st)


async def audio_window():
    mercury_id.WINDOW_S = 1.5
    mercury_id.WAIT_MAX_S = 2 * 1.5 + 1
    st = make()
    await up(st)
    keys = []
    w = asyncio.ensure_future(key_watch(st, keys))
    ws = await arm(st)
    m = lane(st)
    await T(m, True)
    await asyncio.sleep(0.2)
    await T(m, False)
    end = asyncio.ensure_future(st._winlink_end(ws, time.time() - 1, True, None))
    await asyncio.sleep(0.05)
    check("window: fenced Mercury T 1 refused", await T(m, True), "RPRT -11")
    await asyncio.sleep(0.6)
    check("window: no ID while the refused frame may play", [k[1] for k in keys], ["mercury"])
    t0 = time.monotonic()
    await T(m, False)                     # its frame is over
    await asyncio.wait_for(end, 15)
    check("window: the ID keyed after that client's T 0", [k[1] for k in keys], ["mercury", "id"])
    check("window: ... at once (< 1 s)", keys[-1][0] - t0 < 1.0, True)
    # no T 0 after the refusal: the window's end
    ws = await arm(st)
    await T(m, True)
    await asyncio.sleep(0.2)
    await T(m, False)
    end = asyncio.ensure_future(st._winlink_end(ws, time.time() - 1, True, None))
    await asyncio.sleep(0.05)
    t0 = time.monotonic()
    await T(m, True)                      # refused; Mercury then says nothing
    await asyncio.wait_for(end, 15)
    check("window, no T 0: the ID waits out WINDOW_S", keys[-1][0] - t0 >= 1.4, True)
    check("window, no T 0: on air", last(st), (True, True))
    w.cancel()
    await down(st)
    mercury_id.WINDOW_S = rigctl_server.WINLINK_MERCURY_KEEPALIVE_S
    mercury_id.WAIT_MAX_S = 2 * mercury_id.WINDOW_S + 1


async def gate_busy_and_idle():
    st = make(gate_s=1.0)
    await up(st)
    keys = []
    w = asyncio.ensure_future(key_watch(st, keys))
    await arm(st)
    m = lane(st)
    await T(m, True)                      # keyed through the gate
    await asyncio.sleep(1.5)
    check("gate busy: fenced while Mercury is keyed", st.mercury_ident.fence, "the 10-minute station ID")
    check("gate busy: Mercury's key not cut by the gate", st.ptt_owner is not None and not
          getattr(st.ptt_owner, "station_id", False), True)
    rel = time.monotonic()
    await T(m, False)
    await until(lambda: last(st) == (True, True), 10)
    check("gate busy: the ID went at Mercury's release", [k[1] for k in keys], ["mercury", "id"])
    check("gate busy: ... at once (< 1 s)", keys[-1][0] - rel < 1.0, True)
    check("gate busy: fence lifted, still armed", (st.mercury_ident.fence, st.winlink_armed), (None, True))
    check("gate busy: Mercury keys again after the ID", await T(m, True), "RPRT 0")
    await asyncio.sleep(0.2)
    await T(m, False)
    # idle: Mercury quiet when the gate comes due -> the ID at once
    n = len(st.mercury_ident.results)
    await asyncio.sleep(1.3)
    await until(lambda: len(st.mercury_ident.results) > n, 10)
    check("gate idle: the ID went without Mercury keying", st.mercury_ident.results[-1][1:], (True, None))
    w.cancel()
    await down(st)


async def collision():
    st = make(gate_s=0.8)
    await up(st)
    keys = []
    w = asyncio.ensure_future(key_watch(st, keys))
    await arm(st)
    m = lane(st)
    await T(m, True)
    await asyncio.sleep(0.2)
    await T(m, False)
    await until(lambda: st.mercury_ident.playing, 5)
    await asyncio.sleep(0.5)
    t0 = time.monotonic()
    check("collision: Mercury's T 1 mid-ID refused", await T(m, True), "RPRT -11")
    await until(lambda: not tx(st), 2)
    check("collision: the ID cut at once (< 0.2 s)", time.monotonic() - t0 < 0.2, True)
    await until(lambda: st.mercury_ident.results, 2)
    check("collision: recorded cut", st.mercury_ident.results[-1][1:], (False, "Mercury asked to key during it"))
    check("collision: owed", st.id_ledger.owed(), True)
    await until(lambda: not st.winlink_armed, 2)
    check("collision: the session ended (disarmed)", st.winlink_armed, False)
    n = len(keys)
    check("collision: Mercury's later T 1 refused", await T(m, True), "RPRT -11")
    await asyncio.sleep(2.0)
    check("collision: nothing keyed afterwards (no retry)", (len(keys), tx(st)), (n, False))
    w.cancel()
    await down(st)


async def tune_refused():
    """Panel #1: no Winlink lane starts an ATU tune, armed or not; the main port still does."""
    st = make()
    await up(st)
    for armed in (False, True):
        if armed:
            await arm(st)
        for modem in ("ardop", "varahf", "mercury"):
            sess = lane(st, modem, f"{modem}-tuner")
            r = [(await sess.handle(c))[0].strip() for c in ("G TUNE", "vfo_op TUNE")]
            check(f"TUNE on the {modem} lane ({'armed' if armed else 'unarmed'}): refused",
                  (r, st.radio.state.get("tuner") == 2), (["RPRT -11", "RPRT -11"], False))
    await st.winlink_disarm("x")
    main = rigctl_server.RigctlSession(st, "jtdx")
    check("TUNE on the main rigctl port: still accepted (positive control)",
          (await main.handle("G TUNE"))[0].strip(), "RPRT 0")
    await until(lambda: st.radio.state.get("tuner") != 2, 5)
    await down(st)


async def civ_queue_dropped():
    """Panel #2: a key-down still in the CI-V queue never reaches the rig
    after a disarm or the owner going away; its own T 0 keeps the order."""
    from civ import CivRadio

    def frames(r):
        return [(k, f[-2]) for k, f in r._ordered if k == "tx"]

    for how in ("disarm", "disconnect", "own T 0"):
        st = server.Station(CivRadio(rigs.get("ic7300"), "/dev/null"), Audio(), Args(
            no_tx=False, sim=False, tx_timeout=60, tune_timeout=10, winlink_rigctl_port=1))
        await st.winlink_arm(WS(), True, scheme="ardop")
        sess = lane(st, "ardop", "pat")
        check(f"CI-V {how}: T 1 accepted, TX ON queued (worker not running)",
              (await T(sess, True), frames(st.radio)), ("RPRT 0", [("tx", 1)]))
        if how == "disarm":
            await st.winlink_disarm("operator's page closed")
        elif how == "disconnect":
            await sess.release()
        else:
            await T(sess, False)
        want = [("tx", 1), ("tx", 0)] if how == "own T 0" else [("tx", 0)]
        check(f"CI-V {how}: queue {'keeps its order' if how == 'own T 0' else 'has no TX ON'}",
              frames(st.radio), want)


async def collision_order():
    """Panel #3: the ID's key is released before Mercury hears its refusal."""
    st = make(gate_s=0.6)
    await up(st)
    seq = []
    real_set = st.radio.set

    async def logged(k, v):
        await real_set(k, v)
        if k == "tx":
            seq.append(("tx", v))
    st.radio.set = logged
    await arm(st)
    m = lane(st)
    await T(m, True)
    await asyncio.sleep(0.1)
    await T(m, False)
    await until(lambda: st.mercury_ident.playing and tx(st), 5)
    await asyncio.sleep(0.3)
    seq.append(("reply", await T(m, True)))
    i_rep = seq.index(("reply", "RPRT -11"))
    i_on = max(i for i, e in enumerate(seq[:i_rep]) if e == ("tx", True))      # the ID's key
    check("collision order: the rig's TX OFF before Mercury's RPRT -11",
          ("tx", False) in seq[i_on:i_rep], True)
    check("collision order: unkeyed when Mercury hears no", tx(st), False)
    await down(st)


async def release_causes():
    """Panel #4/#5: a keepalive cut or a disconnect opens the full window;
    a T 0 closes only that client's own window; windows outlive a re-arm."""
    mercury_id.WINDOW_S = 1.5
    st = make()
    await up(st)
    keys = []
    w = asyncio.ensure_future(key_watch(st, keys))
    ws = await arm(st)
    m = lane(st)
    await T(m, True)
    m.owner.keepalive_s = 0.4                      # cut by the watchdog, like 33 s
    await until(lambda: not tx(st), 3)
    cut = time.monotonic()
    end = asyncio.ensure_future(st._winlink_end(ws, time.time() - 1, True, None))
    await asyncio.wait_for(end, 15)
    check("keepalive cut: the ID waits the full window", keys[-1][0] - cut >= 1.4, True)
    check("keepalive cut: then on air", last(st), (True, True))
    # disconnect
    ws = await arm(st)
    m = lane(st)
    await T(m, True)
    await asyncio.sleep(0.1)
    await m.release()
    gone = time.monotonic()
    await asyncio.wait_for(st._winlink_end(ws, time.time() - 1, True, None), 15)
    check("disconnect: the ID waits the full window", keys[-1][0] - gone >= 1.4, True)
    # per-client window: A refused, B's T 0 does not close A's window
    ws = await arm(st)
    a, b = lane(st, peer="mercury-a"), lane(st, peer="mercury-b")
    await T(a, True)
    await asyncio.sleep(0.1)
    await T(a, False)
    end = asyncio.ensure_future(st._winlink_end(ws, time.time() - 1, True, None))
    await asyncio.sleep(0.05)
    t0 = time.monotonic()
    check("per client: A refused (fenced)", await T(a, True), "RPRT -11")
    await T(b, False)                              # another client's idle T 0
    await asyncio.wait_for(end, 15)
    check("per client: B's T 0 did not close A's window", keys[-1][0] - t0 >= 1.4, True)
    # windows survive disarm and re-arm
    ws = await arm(st)
    await T(a, True)
    await asyncio.sleep(0.1)
    await T(a, False)
    end = asyncio.ensure_future(st._winlink_end(ws, time.time() - 1, True, None))
    await asyncio.sleep(0.05)
    await T(a, True)                               # refused: A's frame plays on
    await st.winlink_disarm("re-arm")
    await end
    ws = await arm(st)
    check("re-arm: A's open window kept", a.owner in st.mercury_ident.windows, True)
    w.cancel()
    await down(st)
    mercury_id.WINDOW_S = rigctl_server.WINLINK_MERCURY_KEEPALIVE_S


async def replacement_arm():
    """Panel #6: no arm over a live Mercury session."""
    st = make(gate_s=600)
    await up(st)
    ws = await arm(st)
    m = lane(st)
    await T(m, True)
    await asyncio.sleep(0.1)
    await T(m, False)
    ident = st.mercury_ident
    ident.fence, ident.latched = "x", True
    before = (ident.token, ident.armed_at, ident.unid_since, ident.fence, ident.latched)
    ws2 = WS()
    for scheme in ("varahf", "ardop"):
        r = await st.winlink_arm(ws2, True, scheme=scheme)
        check(f"replacement arm ({scheme}) during a Mercury session: refused", r, False)
    await asyncio.sleep(0.1)                       # the notice is sent from its own task
    check("replacement arm: told to disarm first", any("disarm it first" in x.get("msg", "") for x in ws2.sent), True)
    check("replacement arm: owner, clocks, fence and latch unchanged",
          (st.winlink_owner is ws, (ident.token, ident.armed_at, ident.unid_since, ident.fence, ident.latched)),
          (True, before))
    await down(st)


async def budget():
    """Panel #8: the timing budget, from the constants."""
    import cw_id
    check("longest call check_call accepts: twelve zeros, 17.4 s at 18 WPM",
          (cw_id.longest_call(), round(cw_id.longest_s(), 2)), ("000000000000", 17.4))
    g = mercury_id.worst_case_end_s(mercury_id.GATE_S)
    b = mercury_id.worst_case_end_s(mercury_id.BOUND_S)
    check(f"gate {mercury_id.GATE_S:g} s: worst-case ID end {g:.1f} s <= {mercury_id.ID_DEADLINE_S:g}",
          g <= mercury_id.ID_DEADLINE_S, True)
    check(f"bound {mercury_id.BOUND_S:g} s: worst-case ID end {b:.1f} s <= {mercury_id.ID_DEADLINE_S:g}",
          b <= mercury_id.ID_DEADLINE_S, True)
    check("the bound comes before the gate", mercury_id.BOUND_S < mercury_id.GATE_S, True)
    check("the wait covers Mercury's cut key, a full window and the guard",
          mercury_id.WAIT_MAX_S >= rigctl_server.WINLINK_MERCURY_KEEPALIVE_S + mercury_id.WINDOW_S
          + mercury_id.RELEASE_GUARD_S, True)
    check(f"the station's end wait {mercury_id.END_WAIT_S:.1f} s covers the wait and the longest ID",
          mercury_id.END_WAIT_S >= mercury_id.WAIT_MAX_S + mercury_id.id_s_max(), True)


async def gate_mercury_persists():
    """Panel #8: Mercury keeps asking after the fence; the ID never keys while
    a refused frame may play, and goes once Mercury is quiet."""
    st = make(gate_s=0.8)
    await up(st)
    keys = []
    w = asyncio.ensure_future(key_watch(st, keys))
    await arm(st)
    m = lane(st)
    await T(m, True)
    await asyncio.sleep(1.0)                       # the gate falls due under the key
    await T(m, False)
    frames = []
    for _ in range(3):                             # refused frames, T 0 after each, 0.1 s apart
        t1 = time.monotonic()
        r = await T(m, True)
        await asyncio.sleep(0.3)
        await T(m, False)
        frames.append((t1, time.monotonic(), r))
        await asyncio.sleep(0.1)
    await until(lambda: last(st) == (True, True), 10)
    ids = [k[0] for k in keys if k[1] == "id"]
    check("persistent Mercury: every T 1 after the fence refused", [f[2] for f in frames], ["RPRT -11"] * 3)
    check("persistent Mercury: the ID never keyed during a refused frame",
          bool(ids) and not any(a <= ids[0] <= b for a, b, _ in frames), True)
    check("persistent Mercury: then on air, after the last frame", bool(ids) and ids[0] > frames[-1][1], True)
    w.cancel()
    await down(st)


class RaisingAudio(Audio):
    async def play(self, pcm):
        self.chunks += 1
        if self.chunks >= 10:
            raise OSError("aplay vanished")
        return True


async def play_raises():
    """Panel #9: playback raising is a failed ID: owed, latched, clock kept, T 0 sent."""
    st = make(gate_s=0.6, audio=RaisingAudio())
    await up(st)
    await arm(st)
    m = lane(st)
    await T(m, True)
    await asyncio.sleep(0.1)
    await T(m, False)
    ident = st.mercury_ident
    unid = ident.unid_since
    await until(lambda: ident.results, 8)
    r = ident.results[-1]
    check("play raises: not on air", (r[1], "raised OSError" in (r[2] or "")), (False, True))
    check("play raises: owed, unkeyed", (st.id_ledger.owed(), tx(st), st.ptt_owner), (True, False, None))
    b = list(st.id_ledger.bursts.get("mercury", ()))[-1]
    check("play raises: its own T 0 released it (normal)", b[2], "normal")
    check("play raises: the 9:00 clock not cleared", ident.unid_since == unid and unid is not None, True)
    await until(lambda: not st.winlink_armed, 3)
    check("play raises: the session ended", st.winlink_armed, False)
    await down(st)


def ledger_exact():
    """Panel #10: station IDs are ordered exactly, no 0.5 s log slop."""
    import station_id
    L = station_id.IdLedger({"mercury"})
    L.ptt(True, wall=100.0, modem="mercury")           # Mercury
    L.id_started("CW ID", 101.0, "station")
    L.ptt(True, wall=101.0, modem="mercury")           # the ID's key
    L.id_done(105.0, True)
    check("ledger: after an on-air station ID, not owed", L.owed(), False)
    L.ptt(True, wall=105.2, modem="mercury")           # Mercury, 0.2 s later
    L.id_started("CW ID", 105.3, "station")
    L.ptt(True, wall=105.3, modem="mercury")
    L.id_done(105.6, False, "cut")
    check("ledger: Mercury 0.2 s after it, the next ID cut -> owed", L.owed(), True)
    X = station_id.IdLedger({"varahf"})
    X.id_started("CW ID", 200.0, "xara")
    X._finish(X.ids[-1], 201.0, True, None)
    X.ptt(True, wall=201.3, modem="varahf")
    check("ledger: a log-timed (xara) ID keeps its slop (unchanged)", X.owed(), False)


async def end_collision():
    """Regression (2026-11-25, this test): an end-of-session ID cut by a
    collision was sent a second time by end_of_session's next pass."""
    st = make()
    await up(st)
    keys = []
    w = asyncio.ensure_future(key_watch(st, keys))
    ws = await arm(st)
    m = lane(st)
    await T(m, True)
    await asyncio.sleep(0.2)
    await T(m, False)
    end = asyncio.ensure_future(st._winlink_end(ws, time.time() - 1, True, None))
    await until(lambda: st.mercury_ident.playing, 5)
    await asyncio.sleep(0.3)
    check("end collision: Mercury's T 1 mid-ID refused", await T(m, True), "RPRT -11")
    await T(m, False)
    await asyncio.wait_for(end, 15)
    await asyncio.sleep(1.0)
    r = st.mercury_ident.results
    check("end collision: one ID, cut, never sent again", [x[1:] for x in r],
          [(False, "Mercury asked to key during it")])
    check("end collision: keyed once (Mercury, then the cut ID)", [k[1] for k in keys], ["mercury", "id"])
    check("end collision: owed, disarmed", (st.id_ledger.owed(), st.winlink_armed), (True, False))
    w.cancel()
    await down(st)


async def latched_until_disarm():
    """Between a cut and the disarm, Mercury stays fenced (the latch)."""
    st = make(gate_s=0.8)
    await up(st)
    ident = st.mercury_ident
    await arm(st)
    m = lane(st)
    await T(m, True)
    await asyncio.sleep(0.1)
    await T(m, False)
    seen = []
    real = st.winlink_disarm

    async def slow_disarm(reason=None):
        seen.append((ident.latched, ident.fence, await T(m, True)))
        await real(reason)
    st.winlink_disarm = slow_disarm
    await until(lambda: ident.playing, 5)
    await T(m, True)                         # collision
    await until(lambda: seen, 3)
    st.winlink_disarm = real
    check("latch: after the cut, before the disarm: latched, fenced, T 1 refused",
          seen and (seen[0][0], bool(seen[0][1]), seen[0][2]), (True, True, "RPRT -11"))
    await down(st)


async def bound():
    st = make(bound_s=1.0)
    await up(st)
    keys = []
    w = asyncio.ensure_future(key_watch(st, keys))
    ws = await arm(st)
    m = lane(st)
    await T(m, True)
    await asyncio.sleep(0.2)
    await T(m, False)
    await until(lambda: any(s.get("end_request") for s in ws.sent), 3)
    req = next((s for s in ws.sent if s.get("end_request")), None)
    check("bound: end_request sent to the arming socket", bool(req), True)
    await until(lambda: last(st) == (True, True), 10)
    check("bound: the ID, on air", (last(st), [k[1] for k in keys]), ((True, True), ["mercury", "id"]))
    check("bound: end_request (Pat abort) went before the ID keyed", req and req["_at"] < keys[-1][0], True)
    check("bound: still armed after the ID (Pat ends it)", st.winlink_armed, True)
    check("bound: Mercury stays fenced", await T(m, True), "RPRT -11")
    await T(m, False)
    # the station: Pat returned -> winlink_end
    await asyncio.wait_for(st._winlink_end(ws, time.time() - 5, False, "aborted"), 15)
    check("bound: no second ID; then disarmed", ([k[1] for k in keys], st.winlink_armed),
          (["mercury", "id"], False))
    w.cancel()
    await down(st)
    # nobody answers: the backstop
    mercury_id.BOUND_BACKSTOP_S = 0.5
    st = make(bound_s=1.0)
    await up(st)
    ws = await arm(st)
    m = lane(st)
    await T(m, True)
    await asyncio.sleep(0.2)
    await T(m, False)
    await until(lambda: not st.winlink_armed, 12)
    check("bound backstop: ID on air, then disarmed", (last(st), st.winlink_armed), ((True, True), False))
    await down(st)
    mercury_id.BOUND_BACKSTOP_S = 20.0


async def cut_mid_id(how):
    audio = Audio(fail_at=30) if how == "audio" else Audio()
    st = make(gate_s=0.8, audio=audio)
    await up(st)
    keys = []
    w = asyncio.ensure_future(key_watch(st, keys))
    ws = await arm(st)
    m = lane(st)
    await T(m, True)
    await asyncio.sleep(0.1)
    await T(m, False)
    await until(lambda: st.mercury_ident.playing, 5)
    await asyncio.sleep(0.4)
    if how == "stop":
        await st.tx_stop(ws, "STOP TX pressed")
    elif how == "owner":
        await st.winlink_disarm("operator's page closed")
    await until(lambda: not tx(st), 3)
    await until(lambda: st.mercury_ident.results and not st.mercury_ident.playing, 3)
    r = st.mercury_ident.results[-1]
    check(f"{how} mid-ID: cut, not on air", (r[1], last(st)), (False, (False, True)))
    check(f"{how} mid-ID: owed", st.id_ledger.owed(), True)
    await until(lambda: not st.winlink_armed, 3)
    check(f"{how} mid-ID: disarmed", st.winlink_armed, False)
    if how == "stop":
        await st.tx_resume(ws)
    n = len(keys)
    await asyncio.sleep(2.0)
    check(f"{how} mid-ID: never keyed again", (len(keys), tx(st)), (n, False))
    w.cancel()
    await down(st)


async def stale_after_rearm():
    mercury_id.WINDOW_S = 1.0
    mercury_id.WAIT_MAX_S = 3.0
    st = make()
    await up(st)
    keys = []
    w = asyncio.ensure_future(key_watch(st, keys))
    ws = await arm(st)
    m = lane(st)
    await T(m, True)
    end = asyncio.ensure_future(st._winlink_end(ws, time.time() - 1, True, None))   # waits on Mercury
    await asyncio.sleep(0.3)
    await st.winlink_disarm("re-arm")                # Mercury's key cut by the disarm
    ws2 = await arm(st)                                # a new session at once
    await asyncio.wait({end}, timeout=5)
    await asyncio.sleep(3.5)
    check("stale: the old arm's ID never keyed in the new session", [k[1] for k in keys], ["mercury"])
    check("stale: the new session is untouched (armed, no fence)",
          (st.winlink_armed, st.winlink_owner is ws2, st.mercury_ident.fence), (True, True, None))
    w.cancel()
    await down(st)
    mercury_id.WINDOW_S = rigctl_server.WINLINK_MERCURY_KEEPALIVE_S
    mercury_id.WAIT_MAX_S = 2 * mercury_id.WINDOW_S + 1


async def failed_connect_late_retry():
    st = make()
    await up(st)
    keys = []
    w = asyncio.ensure_future(key_watch(st, keys))
    ws = await arm(st)
    m = lane(st)
    await T(m, True)                     # the CALL
    await asyncio.sleep(0.3)
    await T(m, False)
    await asyncio.wait_for(st._winlink_end(ws, time.time() - 1, False, "connect failed"), 15)
    check("failed connect: the ID after the CALL, on air", (last(st), [k[1] for k in keys]),
          ((True, True), ["mercury", "id"]))
    check("failed connect: disarmed", st.winlink_armed, False)
    check("late retry: Mercury's T 1 refused", await T(m, True), "RPRT -11")
    await asyncio.sleep(1.5)
    check("late retry: no ID for it", [k[1] for k in keys], ["mercury", "id"])
    w.cancel()
    await down(st)


async def remote_lane_end_request():
    import station

    class Msg:
        type = station.WSMsgType.TEXT

        def __init__(self, d):
            self.data = json.dumps(d)

    class FakeWS:
        close_code = None

        def __init__(self, msgs):
            self.msgs = msgs

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for m in self.msgs:
                yield m
                await asyncio.sleep(0.05)

    aborts = []

    class WL:
        async def abort(self, why):
            aborts.append(why)

    class St:
        winlink = WL()

        def notice(self, *a, **k):
            pass

    class R:
        name = "IC-7300"

    lane_ = station.RemoteLane.__new__(station.RemoteLane)
    lane_.station, lane_.radio = St(), R()
    lane_.radio_state = {"armed": True}
    lane_._armed, lane_._disarmed = asyncio.Event(), asyncio.Event()
    lane_._armed.set()
    lane_._ending = False
    lane_.ws = FakeWS([Msg({"t": "winlink", "end_request": "the 8.5-minute Mercury session bound"}),
                       Msg({"t": "winlink", "id": {}})])
    task = asyncio.ensure_future(lane_._read())
    await asyncio.sleep(0.3)
    task.cancel()
    check("RemoteLane: end_request -> the station's winlink.abort, the radio still armed",
          aborts[:1], ["the 8.5-minute Mercury session bound"])


async def main():
    await negative_controls()
    await end_of_session()
    await audio_window()
    await gate_busy_and_idle()
    ledger_exact()
    await budget()
    await tune_refused()
    await civ_queue_dropped()
    await collision()
    await collision_order()
    await end_collision()
    await release_causes()
    await replacement_arm()
    await gate_mercury_persists()
    await play_raises()
    await latched_until_disarm()
    await bound()
    for how in ("stop", "owner", "audio"):
        await cut_mid_id(how)
    await stale_after_rearm()
    await failed_connect_late_retry()
    await remote_lane_end_request()


asyncio.run(main())
print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
