"""Winlink P2P and Pat occupancy: server.py --sim and the engine in process,
with tests/fake_pat.py as Pat (inbound sessions announced as Pat does, via its
log line and Status). Nothing transmits.

  A-4   packet URLs: never 144.390 or its guard; peer: one valid call, not ours
  A-5   gateway session, outbox {plain, P2P only}: left 1, no "still in the outbox"
  A-6   peer session: sent = what Pat moved to sent/, whatever its addressing
  A-7   inbound: "Answering N0XYZ"; outbox DELETE/move 409 during, 200 after
  A-8   inbound during admission (after ready, the mailbox read, arm): undone, not dialed
  A-9a  cooperative inbound past --winlink-session-max: a graceful disconnect only
  A-9b  stubborn inbound: graceful, SIGTERM, SIGKILL, in order; Pat respawned
  A-10  Pat's WebSocket drops mid-inbound: outbox 409 in the gap; `since` kept
  A-11  preflight: a config that would listen/schedule/beacon, or unreadable: Pat not launched
  A-12  lifecycle: concurrent restarts share a respawn; restart while Pat is dead
        waits for the new one; stop during start leaves no child; argv from want

    .venv/bin/python tests/winlink_p2p_test.py      (ONLY=A-7 to pick by id)
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import types

import aiohttp

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.pop("FAKE_PAT_LOG", None)

import testkit  # noqa: E402
import winlink_test_lib as lib  # noqa: E402
from testkit import free_port, wait_for  # noqa: E402
from winlink_test_lib import WebPage as Page  # noqa: E402

ONLY = os.environ.get("ONLY", "")
if os.path.splitext(os.path.basename(__file__))[0] in ONLY.split():
    ONLY = ""
fails = 0
URL = "ardop:///N0DAJ?bw=2000MAX&freq=3588.5"


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:64} got {got!r}" + ("" if ok else f" want {want!r}"))
    return ok


def picked(tid):
    return not ONLY or ONLY == tid


def fake_log(path):
    try:
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except OSError:
        return []


def recs(path, route, **match):
    return [r for r in fake_log(path) if r.get("route") == route
            and all(r.get(k) == v for k, v in match.items())]


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ------------------------------------------------------------ server harness

class Server:
    """The radio behind a station running Pat (winlink_test_lib.start), on
    fresh ports each time."""

    def __init__(self, env=None, extra=(), pat_cmd=None, wait_pat=True):
        lib.APPS, lib.LANE, lib.PATP, lib.WEB, lib.RWEB = (free_port() for _ in range(5))
        self.web, self.apps, self.lane, self.patp = lib.WEB, lib.APPS, lib.LANE, lib.PATP
        self.flog = tempfile.NamedTemporaryFile(prefix="fakepat-", suffix=".jsonl", delete=False).name
        state = os.path.join(tempfile.mkdtemp(prefix="wlp2p-"), "addons.json")
        e = {"FAKE_PAT_LOG": self.flog, **(env or {})}
        self.p, _ = lib.start(e, "--addons-state", state, *(["--winlink-pat", pat_cmd] if pat_cmd else []),
                              *extra, wait_pat=wait_pat)
        self.page = Page(self.web)
        if wait_pat:
            wait_for(lambda: self.page.snapshot().get("pat_busy") is False, 10)
            time.sleep(1.2)            # past Pat's log replay (winlink.REPLAY_S)

    @staticmethod
    def _open(port):
        import socket
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return True
        except OSError:
            return False

    def fake(self, path, body=None):
        async def go():
            async with self.page.sess.post(f"http://127.0.0.1:{self.patp}{path}", json=body or {}) as r:
                return r.status
        return self.page.run(go())

    def compose(self, to, subject, p2ponly=False):
        form = aiohttp.FormData()
        for k, v in (("to", to), ("subject", subject), ("body", "b")):
            form.add_field(k, v)
        if p2ponly:
            form.add_field("p2ponly", "true")
        return self.page.http("POST", "/winlink/api/mailbox/out", data=form)[0]

    def mids(self, box):
        st, body = self.page.http("GET", f"/winlink/api/mailbox/{box}")
        return sorted(m["MID"] for m in json.loads(body)) if st == 200 else f"HTTP {st}"

    def stop(self):
        try:
            self.page.close()
        except Exception:      # noqa: BLE001
            pass
        self.p.terminate()
        try:
            self.out = self.p.communicate(timeout=40)[0]
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.out = self.p.communicate()[0]
        return self.out


def ended(e):
    return e.get("t") == "winlink" and e.get("session") == "ended"


def inbound_is(call):
    return lambda e: e.get("t") == "winlink" and "inbound" in e and (
        (e["inbound"] or {}).get("call") == call if call else e["inbound"] is None)


# ------------------------------------------------------------ A-4: URLs

if picked("A-4"):
    import winlink as W

    def band_of(hz):
        return "80m" if 3.5e6 <= hz <= 4e6 else "20m" if 14e6 <= hz <= 14.35e6 else None
    check("A-4 refused: 144.390, naming the APRS channel",
          "APRS" in (W.check_url("ax25+agwpe:///N0DAJ?freq=144390", band_of) or ""), True)
    for url in ("ax25:///N0DAJ?freq=144395", "ax25+agwpe:///N0DAJ?freq=144050"):
        check(f"A-4 refused: {url}", W.check_url(url, band_of) is not None, True)


# ------------------------------------------------------------ in-process engine

class St:
    """What PatEngine reaches pages through, recording what it says."""

    def __init__(self):
        self.notes, self.sent = [], []

    def notice(self, msg, level="info", ws=None):
        self.notes.append((level, msg))

    async def _send(self, ws, payload):
        self.sent.append(json.loads(payload))

    def _broadcast(self, payload):
        self.sent.append(json.loads(payload))

    def band_of(self, hz):
        return "80m" if 3.5e6 <= hz <= 4e6 else None


def engine(lane_for=None):
    import winlink as W
    flog = tempfile.NamedTemporaryFile(prefix="fakepat-", suffix=".jsonl", delete=False).name
    os.environ.update({"FAKE_PAT_LOG": flog, "FAKE_PAT_NO_PTT": "1", "FAKE_PAT_RIGCTL": "0"})
    args = types.SimpleNamespace(winlink_pat=testkit.fake_pat_cmd(), winlink_pat_port=free_port(),
                                 winlink_session_max=60, winlink_pat_config=None)
    st = St()
    eng = W.PatEngine(st, args, lane_for=lane_for)
    return eng, st, flog


async def await_for(pred, timeout=10.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(step)
    return bool(pred())


def first_status(eng):
    """Pat's event link is up, its Status in, past the log replay."""
    return eng.pat_ws is not None and bool(eng.status) and (eng.live_at or 0) <= time.monotonic()


async def fake_post(eng, path, body=None):
    async with aiohttp.ClientSession() as s, s.post(f"{eng.base}{path}", json=body or {}) as r:
        return r.status


# ------------------------------------------------------------ A-4 (peer), A-8: admission

class TLane:
    """A lane that keys nothing, recording arm and end (as NoLane, observed)."""
    name = "test"
    armed = False
    extra_grace_s = 0.0

    def __init__(self, on_arm=None):
        self.arms, self.ends, self.on_arm = 0, [], on_arm

    def state(self):
        return {"armed": self.armed, "codec_db": None, "id": None}

    async def arm(self, ws, url):
        self.arms += 1
        if self.on_arm:
            await self.on_arm()
        return True

    async def end(self, started, ok, reason, closing, notify):
        self.ends.append(reason)


async def admission_cases():
    lanes = []

    def lane_for(url, via):
        return lanes[-1], None
    eng, st, flog = engine(lane_for=lane_for)
    await eng.start()
    res = {}
    try:
        await await_for(lambda: first_status(eng) and eng.mycall, 10)

        async def inbound_now():
            await fake_post(eng, "/fake/inbound", {"remote": "AX.25:N0XYZ", "mode": "cooperative"})
            await await_for(lambda: eng.status.get("connected"), 5)

        async def inbound_over():
            await fake_post(eng, "/fake/inbound/end")
            await await_for(lambda: not eng.status.get("connected") and not getattr(eng, "inbound", None), 5)

        # an SSID of our own call is another station
        for name, url in () if not picked("A-4") else (("own exact MyCall", "telnet://N9ABC:pw@127.0.0.1:9/N9ABC"),
                          ("malformed call", "telnet://N9ABC:pw@127.0.0.1:9/N9_BC"),
                          ("SSID 16", "telnet://N9ABC:pw@127.0.0.1:9/N0XYZ-16"),
                          ("a packet peer with no freq=", "ax25+agwpe:///N0XYZ")):
            lanes.append(TLane())
            n0, notes0 = len(recs(flog, "connect")), len(st.notes)
            await asyncio.wait_for(eng.connect(None, url, peer=True), 10)
            res[f"peer refused: {name}"] = (len(recs(flog, "connect")) - n0, lanes[-1].arms,
                                            any(lvl == "warn" for lvl, _ in st.notes[notes0:]))
        if picked("A-4"):
            lanes.append(TLane())
            n0 = len(recs(flog, "connect"))
            await asyncio.wait_for(eng.connect(None, "telnet://N9ABC:pw@127.0.0.1:9/N9ABC-5", peer=True), 10)
            await await_for(lambda: len(recs(flog, "connect")) > n0, 10)
            res["peer accepted: N9ABC-5 with MyCall N9ABC"] = [r["url"] for r in recs(flog, "connect")[n0:]]
            await await_for(lambda: not (eng.session and not eng.session.done()), 15)
        if not picked("A-8"):
            return res

        # A-8: an inbound call arriving at each await of the admission.
        orig_ready = eng._ready
        orig_box = eng._outbox
        stage = {"at": None}

        async def ready_hook():
            r = await orig_ready()
            if stage["at"] == "ready":
                stage["at"] = None
                await inbound_now()
            return r

        async def box_hook(*a):
            r = await orig_box(*a)
            if stage["at"] == "mailbox":
                stage["at"] = None
                await inbound_now()
            return r
        eng._ready, eng._outbox = ready_hook, box_hook

        async def arm_hook():
            if stage["at"] == "arm":
                stage["at"] = None
                await inbound_now()
        for at in ("ready", "mailbox", "arm"):
            lane = TLane(on_arm=arm_hook)
            lanes.append(lane)
            stage["at"] = at
            n0, notes0 = len(recs(flog, "connect")), len(st.notes)
            await asyncio.wait_for(eng.connect(None, "telnet://N9ABC:CMSTelnet@127.0.0.1:9/wl2k"), 15)
            await asyncio.sleep(1.0)       # negative check: a window for a dial to reach Pat
            told = [m for lvl, m in st.notes[notes0:] if lvl == "warn" and "N0XYZ" in m]
            res[f"inbound after {at}"] = (len(recs(flog, "connect")) - n0, bool(told),
                                          lane.arms == len(lane.ends))
            await asyncio.wait_for(eng.stop_session(), 5)
            await await_for(lambda: not (eng.session and not eng.session.done()), 15)
            await inbound_over()
    finally:
        await eng.stop()
    return res


if picked("A-4") or picked("A-8"):
    try:
        r = asyncio.run(admission_cases())
    except Exception as e:                    # noqa: BLE001 - a failed check, not an abort
        r = {"error": f"{type(e).__name__}: {e}"}
        print("      ", r)
    if picked("A-4"):
        for name in ("own exact MyCall", "malformed call", "SSID 16", "a packet peer with no freq="):
            check(f"A-4 peer refused ({name}): nothing dialed, lane untouched, page told",
                  r.get(f"peer refused: {name}"), (0, 0, True))
        check("A-4 peer accepted: N9ABC-5 with MyCall N9ABC, dialed as given",
              r.get("peer accepted: N9ABC-5 with MyCall N9ABC"), ["telnet://N9ABC:pw@127.0.0.1:9/N9ABC-5"])
    if picked("A-8"):
        for at in ("ready", "mailbox", "arm"):
            check(f"A-8 inbound after {at}: nothing dialed, page told (N0XYZ), lane undone",
                  r.get(f"inbound after {at}"), (0, True, True))


# ------------------------------------------------------------ A-5, A-6: what a session sent

if picked("A-5"):
    s = Server({"FAKE_PAT_TX_S": "0.5"})
    try:
        s.compose("N0DAJ", "plain")
        s.compose("N0XYZ", "p2p only", p2ponly=True)
        s.page.events.clear()
        s.page.send({"t": "winlink_connect", "url": URL})
        e = s.page.wait(ended, 20)
        check("A-5 gateway session, outbox {plain, P2P only}: ok, sent 1, left 1, unsent 0",
              e and (e.get("ok"), e.get("sent"), e.get("left"), e.get("unsent")), (True, 1, 1, 0))
        check("A-5   the P2P-only message is still in the outbox", s.mids("out"), ["OUT1"])
    finally:
        out = s.stop()
    check("A-5   no \"still in the outbox\" warning", "still in the outbox" in out, False)

if picked("A-6"):
    s = Server({"FAKE_PAT_TX_S": "0.5", "FAKE_PAT_P2P": "N0XYZ"})
    try:
        s.compose("N0XYZ", "to the peer")                     # OUT0: goes (only receiver)
        s.compose("N0XYZ K7ABC", "to the peer and another")   # OUT1: stays (not the only receiver)
        s.compose("N0XYZ", "p2p only to the peer", p2ponly=True)   # OUT2: goes
        s.compose("someone@example.com", "an email")          # OUT3: stays
        s.page.events.clear()
        s.page.send({"t": "winlink_connect", "url": f"telnet://N9ABC:pw@127.0.0.1:{free_port()}/N0XYZ",
                     "via": "telnet", "peer": True})
        e = s.page.wait(ended, 20)
        sent_box = s.mids("sent")
        check("A-6 peer session: Pat moved OUT0 and OUT2 to sent", sent_box, ["OUT0", "OUT2"])
        check("A-6   sent = those MIDs, left 2, no gateway count, peer",
              e and (e.get("ok"), e.get("sent"), e.get("left"), e.get("unsent"), e.get("peer")),
              (True, len(sent_box), 2, None, True))
        st = s.page.wait(lambda x: x.get("t") == "winlink" and x.get("session") == "started", 1)
        check("A-6   the page never sees the telnet password", st and st.get("url", "").count("pw"), 0)
    finally:
        out = s.stop()
    check("A-6   no \"still in the outbox\" warning", "still in the outbox" in out, False)
    check("A-6   the log never has the telnet password", ":pw@" in out, False)


# ------------------------------------------------------------ A-7, A-10: inbound and the outbox

if picked("A-7"):
    s = Server()
    try:
        s.compose("N0DAJ", "queued")
        check("A-7 precondition: OUT0 in the outbox, Pat idle", (s.mids("out"), s.page.snapshot().get("pat_busy")),
              (["OUT0"], False))
        s.fake("/fake/inbound", {"remote": "AX.25:N0XYZ", "method": "ax25+agwpe", "receive": 1})
        e = s.page.wait(inbound_is("N0XYZ"), 10)
        check("A-7 the page is told Pat is answering N0XYZ (from Pat's log line)",
              e and (e["inbound"].get("call"), e["inbound"].get("from")), ("N0XYZ", "log"))
        st, body = s.page.http("DELETE", "/winlink/api/mailbox/out/OUT0")
        check("A-7 during: DELETE out/OUT0 -> 409, saying who", (st, "N0XYZ" in body), (409, True))
        st, _ = s.page.http("POST", "/winlink/api/mailbox/archive", headers={"X-Pat-SourcePath": "/api/mailbox/out/OUT0"})
        check("A-7 during: move from out -> 409", st, 409)
        check("A-7   neither reached Pat", [r for r in recs(s.flog, "req") if "OUT0" in r.get("path", "")
                                            or "OUT0" in (r.get("headers") or {}).get("X-Pat-SourcePath", "")], [])
        s.fake("/fake/inbound/end")
        check("A-7 the inbound session ended: the page told", bool(s.page.wait(inbound_is(None), 10)), True)
        check("A-7 after: DELETE out/OUT0 -> 200", s.page.http("DELETE", "/winlink/api/mailbox/out/OUT0")[0], 200)
        # telnet: remote_addr is tcp:ip:port, the call only in the log line
        s.fake("/fake/inbound", {"remote": "tcp:127.0.0.1:5555", "method": "telnet", "call": "K7ABC"})
        check("A-7 telnet inbound: the call comes from Pat's log line", bool(s.page.wait(inbound_is("K7ABC"), 10)), True)
        s.fake("/fake/inbound/end")
        s.page.wait(inbound_is(None), 10)
    finally:
        s.stop()

if picked("A-10"):
    s = Server()
    try:
        s.compose("N0DAJ", "queued")
        s.fake("/fake/inbound", {"remote": "AX.25:N0XYZ"})
        s.page.wait(inbound_is("N0XYZ"), 10)
        since = (s.page.snapshot().get("inbound") or {}).get("since")
        check("A-10 precondition: inbound with a since", since is not None, True)
        n_status = sum(1 for e in s.page.events if "status" in e)
        s.fake("/fake/ws/drop", {"refuse_s": 3})
        check("A-10 the gap: Pat's state unknown (pat_busy)",
              wait_for(lambda: s.page.snapshot().get("pat_busy") is True and s.page.snapshot().get("status") is not None, 3),
              True)
        check("A-10 in the gap: DELETE out/OUT0 -> 409", s.page.http("DELETE", "/winlink/api/mailbox/out/OUT0")[0], 409)
        check("A-10 the event link comes back (Pat's full Status again)",
              bool(s.page.wait(lambda e: "status" in e and sum(1 for x in s.page.events if "status" in x) > n_status, 15)),
              True)
        snap = s.page.snapshot()
        check("A-10 after reconnect, still connected: since unchanged",
              ((snap.get("inbound") or {}).get("since"), snap.get("inbound", {}) and snap["inbound"].get("call")),
              (since, "N0XYZ"))
        check("A-10   outbox still 409", s.page.http("DELETE", "/winlink/api/mailbox/out/OUT0")[0], 409)
        s.fake("/fake/inbound/end")
        s.page.wait(inbound_is(None), 10)
        check("A-10 after it ends: DELETE -> 200", s.page.http("DELETE", "/winlink/api/mailbox/out/OUT0")[0], 200)
    finally:
        s.stop()


# ------------------------------------------------------------ A-9: the cap on inbound sessions

if picked("A-9a"):
    s = Server({}, ("--winlink-session-max", "2"))
    try:
        s.fake("/fake/inbound", {"remote": "AX.25:N0XYZ", "mode": "cooperative"})
        s.page.wait(inbound_is("N0XYZ"), 10)
        e = s.page.wait(inbound_is(None), 20)
        check("A-9a cooperative past 2 s: ended", bool(e), True)
        check("A-9a   by a graceful disconnect", [r["dirty"] for r in recs(s.flog, "disconnect")], ["false"])
        check("A-9a   no signal, no respawn", (recs(s.flog, "sigterm"), len(recs(s.flog, "start"))), ([], 1))
    finally:
        out = s.stop()
    check("A-9a   logged as past the limit", "ran past the 2s limit" in out, True)

if picked("A-9b"):
    s = Server({}, ("--winlink-session-max", "2"))
    try:
        pid0 = recs(s.flog, "start")[-1]["pid"]
        s.fake("/fake/inbound", {"remote": "AX.25:N0XYZ", "mode": "stubborn"})
        s.page.wait(inbound_is("N0XYZ"), 10)
        e = s.page.wait(inbound_is(None), 60)
        log = [r["route"] for r in fake_log(s.flog) if r["route"] in ("disconnect", "sigterm", "start")]
        check("A-9b stubborn: ended", bool(e), True)
        check("A-9b   graceful, then SIGTERM (Pat ran on), then a new Pat", log, ["start", "disconnect", "sigterm", "start"])
        check("A-9b   the SIGTERM was ignored (stubborn)", [r.get("kept_running") for r in recs(s.flog, "sigterm")], [True])
        check("A-9b   the old Pat is gone (SIGKILL), a new one runs",
              (alive(pid0), alive(recs(s.flog, "start")[-1]["pid"])), (False, True))
        check("A-9b   the owner's Pat answers again", wait_for(lambda: s.page.snapshot().get("pat_busy") is False, 15), True)
    finally:
        out = s.stop()
    check("A-9b   logged: SIGTERM, then SIGKILL",
          ("stopping Pat (SIGTERM)" in out, "killing Pat (SIGKILL)" in out), (True, True))


# ------------------------------------------------------------ A-11: preflight

if picked("A-11"):
    bad_json = tempfile.NamedTemporaryFile(prefix="patcfg-", suffix=".json", delete=False).name
    with open(bad_json, "w") as f:
        f.write("{not json")
    cases = [("listen [telnet]", {"listen": ["telnet"]}, "listen"),
             ("schedule non-empty", {"schedule": {"@hourly": "connect telnet"}}, "schedule"),
             ("beacon every 600", {"ax25": {"engine": "agwpe", "beacon": {"every": 600}}}, "beacon")]

    def refused(s, label, word):
        # preflight sets `blocked` before any spawn, so once it shows Pat never started
        check(f"A-11 {label}: Winlink says why",
              wait_for(lambda: word in (s.page.snapshot().get("blocked") or ""), 10), True)
        check(f"A-11 {label}: Pat not launched", recs(s.flog, "start"), [])

    for name, over, word in cases:
        s = Server(pat_cmd=testkit.fake_pat_cmd(**over), wait_pat=False)
        try:
            refused(s, name, word)
        finally:
            s.stop()
    s = Server(pat_cmd=f"{sys.executable} tests/fake_pat.py --config {bad_json}", wait_pat=False)
    try:
        refused(s, "unreadable config", "can't be read")
    finally:
        s.stop()
    clean = testkit.pat_config(tempfile.NamedTemporaryFile(suffix=".json", delete=False).name)
    listen = testkit.pat_config(tempfile.NamedTemporaryFile(suffix=".json", delete=False).name, listen=["telnet"])
    s = Server(pat_cmd=f"{sys.executable} tests/fake_pat.py --config {clean}",
               extra=("--winlink-pat-config", listen), wait_pat=False)
    try:
        refused(s, "--winlink-pat-config wins over the command's --config", "listen")
    finally:
        s.stop()
    s = Server()
    try:
        check("A-11 clean config: launched, listening on nothing",
              [r.get("listen") for r in recs(s.flog, "start")], [[]])
        check("A-11   not blocked", s.page.snapshot().get("blocked"), None)
    finally:
        s.stop()


# ------------------------------------------------------------ A-12: one lifecycle owner

async def lifecycle_cases():
    res = {}
    eng, st, flog = engine()
    await eng.start()
    try:
        await await_for(lambda: first_status(eng), 10)
        n0 = len(recs(flog, "start"))
        pid0 = recs(flog, "start")[-1]["pid"]
        out = await asyncio.wait_for(asyncio.gather(eng.restart_pat("one"), eng.restart_pat("two"),
                                                    return_exceptions=True), 30)
        await asyncio.sleep(2.5)       # negative check: a second respawn would show by now (RESTART_S 2)
        res["two concurrent restarts"] = ([type(x).__name__ for x in out if x is not None],
                                          len(recs(flog, "start")) - n0, alive(pid0))
        # Pat dies: restart_pat must not return before a new Pat is up
        pid1 = recs(flog, "start")[-1]["pid"]
        os.kill(pid1, signal.SIGKILL)
        await await_for(lambda: eng.proc is not None and eng.proc.returncode is not None, 5)
        try:
            await asyncio.wait_for(eng.restart_pat("dead"), 30)
            err = None
        except Exception as e:         # noqa: BLE001
            err = type(e).__name__
        p = eng.proc
        res["restart while dead"] = (err, p is not None and p.returncode is None and alive(p.pid),
                                     recs(flog, "start")[-1]["pid"] != pid1, eng.pat_ws is not None)
        eng.want["listen"] = "telnet"
        await asyncio.wait_for(eng.restart_pat("listen"), 30)
        r = recs(flog, "start")[-1]
        argv = r.get("argv") or []
        res["argv from want"] = (r.get("listen"), "--listen" in argv and argv.index("--listen") < argv.index("http"))
        eng.want["listen"] = None
        await asyncio.wait_for(eng.restart_pat("no listen"), 30)
        res["argv from want, cleared"] = recs(flog, "start")[-1].get("listen")
        testkit.pat_config(eng.config_path, listen=["telnet"])
        n1 = len(recs(flog, "start"))
        try:
            await asyncio.wait_for(eng.restart_pat("bad config"), 30)
            err = None
        except Exception as e:         # noqa: BLE001
            err = type(e).__name__
        await asyncio.sleep(1.0)       # negative check
        res["restart with a bad config"] = (err, len(recs(flog, "start")) - n1)
        testkit.pat_config(eng.config_path)
        await asyncio.wait_for(eng.restart_pat("good again"), 40)
        res["good again"] = recs(flog, "start")[-1].get("listen")
    finally:
        await eng.stop()
    pids = [r["pid"] for r in recs(flog, "start")]
    res["after stop, no Pat left"] = [p for p in pids if alive(p)]

    for when in ("child spawned", "at once"):
        eng, st, flog = engine()
        await eng.start()
        if when == "child spawned":
            await await_for(lambda: recs(flog, "start"), 10, 0.005)
        await asyncio.wait_for(eng.stop(), 20)
        await asyncio.sleep(2.5)       # negative check: longer than RESTART_S
        pids = [r["pid"] for r in recs(flog, "start")]
        res[f"stop during start ({when})"] = ([p for p in pids if alive(p)], len(pids) <= 1)
    return res


if picked("A-12"):
    try:
        r = asyncio.run(lifecycle_cases())
    except Exception as e:                    # noqa: BLE001
        r = {"error": f"{type(e).__name__}: {e}"}
        print("      ", r)
    check("A-12 two concurrent restart_pat: both return, ONE respawn, old Pat gone",
          r.get("two concurrent restarts"), ([], 1, False))
    check("A-12 restart_pat while Pat is dead: returns with a new Pat up and its events live",
          r.get("restart while dead"), (None, True, True, True))
    check("A-12 argv from want at spawn: --listen telnet, before http", r.get("argv from want"), (["telnet"], True))
    check("A-12   and without it again", r.get("argv from want, cleared"), [])
    check("A-12 restart_pat with a config that would listen: raises, Pat not started",
          r.get("restart with a bad config"), ("PatRestartError", 0))
    check("A-12   config fixed: restart_pat starts it", r.get("good again"), [])
    check("A-12 stop(): no Pat left", r.get("after stop, no Pat left"), [])
    check("A-12 stop during start (child spawned): no orphan, no respawn",
          r.get("stop during start (child spawned)"), ([], True))
    check("A-12 stop during start (at once): no orphan, no respawn", r.get("stop during start (at once)"), ([], True))


print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
