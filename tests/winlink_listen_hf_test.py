"""Attended HF listening (winlink.listen_hf). Nothing goes on air: the radio
is server.py --sim behind a station, Pat is tests/fake_pat.py, ARDOP is
tests/fake_ardopcf.py.

An attended window arms an HF radio's Winlink lane the way a session does
(QSY to the DIAL frequency, 1.5 kHz below the listed centre), relaunches Pat
with --listen <modem>, and ends when its page closes, when nobody answers
"still there?", at max_minutes, on STOP TX, or when the lane is disarmed.
The lane may key ONLY while the window is open.

  H-1   plan: channels, dial, 97.221(b) segment flag; bad blocks refused
        (sub-band edge, modem, presence > max, a VARA channel without xara);
        render passes it to station.json
  H-2   start: before, the ARDOP lane's PTT is refused; after, the radio is
        on the dial, Pat lists [ardop], the lane may key; the window reads back
  H-3   while listening: a second window, a connect, a bad channel refused;
        "still there?" answered with I'm here (in process)
  H-4   Stop listening: Pat drops --listen, the lane disarms (PTT refused again)
  H-5   the page that opened it closes: the window ends, PTT refused
  H-6   an inbound session when it ends: the caller is disconnected, then the lane ends
  H-7   (in process) unanswered "still there?" ends it; max_minutes ends it;
        STOP TX ends it; a lane disarmed under it ends it; I'm here keeps it
  H-8   a stale HF record at start: a listening Pat that outlived its server is
        ended, the record dropped (the radio disarmed with the old socket)

    .venv/bin/python tests/winlink_listen_hf_test.py      (ONLY="H-2 H-7" to pick by id)
"""
import asyncio
import copy
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import listen_plan as LP  # noqa: E402
import testkit  # noqa: E402
import winlink_test_lib as L  # noqa: E402
from testkit import free_port  # noqa: E402

ONLY = os.environ.get("ONLY", "")
fails = 0
PLAN = {"channels": [{"khz": 7102.5, "modem": "ardop"}, {"khz": 3590, "modem": "ardop"}],
        "max_minutes": 60, "presence_minutes": 15}


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:66} got {got!r}" + ("" if ok else f" want {want!r}"))


def picked(tid):
    return not ONLY or tid in ONLY.split()


def wait_for(pred, timeout=15, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


def rig(cmd):
    with socket.create_connection(("127.0.0.1", L.APPS), timeout=3) as s:
        f = s.makefile("r")
        s.sendall(cmd.encode() + b"\n")
        v = f.readline().strip()
        f.close()
        return v


def lane_may_key():
    """The ARDOP lane's PTT: True if it may key now (it is un-keyed at once)."""
    return not L.lane_ptt_refused()


# ------------------------------------------------------------ H-1: the plan and render

if picked("H-1"):
    p = LP.HfListenPlan.from_block(PLAN)
    d = p.describe()
    check("H-1 channels: centre, dial 1.5 kHz below, band, segment",
          [(c["khz"], c["dial_khz"], c["band"], c["segment"]) for c in d["channels"]],
          [(7102.5, 7101.0, "40 m", True), (3590.0, 3588.5, "80 m", True)])
    check("H-1 a channel outside every 97.221(b) segment is still allowed (attended), flagged",
          LP.HfListenPlan.from_block({"channels": [{"khz": 7080, "modem": "ardop"}]}).describe()["channels"][0]["segment"],
          False)
    for name, block in [("edge: 7124 +-1.5 kHz crosses 7125", {"channels": [{"khz": 7124, "modem": "ardop"}]}),
                        ("phone band 7200", {"channels": [{"khz": 7200, "modem": "ardop"}]}),
                        ("80 m below General data (3520)", {"channels": [{"khz": 3520, "modem": "ardop"}]}),
                        ("60 m (channelised, left out)", {"channels": [{"khz": 5357, "modem": "ardop"}]}),
                        ("modem pactor", {"channels": [{"khz": 7102.5, "modem": "pactor"}]}),
                        ("no channels", {"channels": []}),
                        ("a channel twice", {"channels": [{"khz": 7102.5, "modem": "ardop"}] * 2}),
                        ("presence > max", {**PLAN, "max_minutes": 10, "presence_minutes": 15}),
                        ("max 500 min", {**PLAN, "max_minutes": 500}),
                        ("unknown key", {**PLAN, "unattended": True})]:
        try:
            LP.HfListenPlan.from_block(block)
            check(f"H-1 refused: {name}", "accepted", "refused")
        except LP.ListenError:
            check(f"H-1 refused: {name}", "refused", "refused")

    from ranch_radio import render
    mac = json.load(open(os.path.join(ROOT, "examples/hosts/mac-shack.json")))
    h = copy.deepcopy(mac)
    h["winlink"]["listen_hf"] = PLAN
    render.validate(h)
    check("H-1 render: station.json keeps winlink.listen_hf", render.station_json(h)["winlink"]["listen_hf"], PLAN)
    h2 = copy.deepcopy(h)
    h2["winlink"]["listen_hf"] = {"channels": [{"khz": 14108, "modem": "varahf"}]}
    h2["winlink"].pop("xara", None)
    try:
        render.validate(h2)
        check("H-1 render refuses a VARA HF channel without winlink.xara", "accepted", "refused")
    except render.HostError:
        check("H-1 render refuses a VARA HF channel without winlink.xara", "refused", "refused")


# ------------------------------------------------------------ H-2..H-6: the stack

def start_stack():
    state = os.path.join(tempfile.mkdtemp(prefix="wlhf-"), "addons.json")
    return L.start({"FAKE_PAT_TX_S": "2"}, "--addons-state", state, "--winlink-listen-hf", json.dumps(PLAN))


def window(page):
    return ((page.snapshot().get("listen") or {}).get("window"))


def listen_busy(page):
    return (page.snapshot().get("listen") or {}).get("busy")


def listeners(page):
    return (page.snapshot().get("status") or {}).get("active_listeners")


def open_window(page, ch=0):
    page.events.clear()
    page.send({"t": "winlink_listen", "hf": ch})
    return wait_for(lambda: (window(page) or {}).get("kind") == "hf" and listen_busy(page) is None, 25)


def idle(page, timeout=40):
    return wait_for(lambda: window(page) is None and listen_busy(page) is None, timeout)


def notices(page, level=None):
    return [e.get("msg", "") for e in list(page.events)
            if e.get("t") == "notice" and (level is None or e.get("level") == level)]


if any(picked(t) for t in ("H-2", "H-3", "H-4", "H-5", "H-6")):
    server, flog = start_stack()
    page = None
    try:
        page = L.WebPage(L.WEB)
        wait_for(lambda: page.snapshot().get("mycall") and page.snapshot().get("pat_busy") is False, 30)
        st = page.snapshot().get("listen") or {}
        check("H-2 the page is offered attended HF (plan, no packet plan)",
              ((st.get("hf") or {}).get("attended"), st.get("plan")), (True, None))
        check("H-2 before: the ARDOP lane may NOT key", lane_may_key(), False)
        freq0 = rig("f")

        if picked("H-2") or picked("H-3") or picked("H-4"):
            check("H-2 a window opens", bool(open_window(page)), True)
            w = window(page) or {}
            check("H-2 window: kind, modem, centre, dial, segment",
                  (w.get("kind"), w.get("method"), w.get("centre_hz"), w.get("hz"), w.get("segment")),
                  ("hf", "ardop", 7102500, 7101000, True))
            check("H-2 the radio is on the DIAL frequency", rig("f"), "7101000")
            check("H-2 Pat lists exactly [ardop]", listeners(page), ["ardop"])
            check("H-2 while listening: the ARDOP lane may key", lane_may_key(), True)
            check("H-2 the page was told it is attended and to stay open",
                  any("Keep this page open" in n for n in notices(page)), True)

            if picked("H-3"):
                page.events.clear()
                page.send({"t": "winlink_listen", "hf": 1})
                check("H-3 a second window refused", bool(page.wait(
                    lambda e: e.get("t") == "notice" and "already open" in e.get("msg", ""), 5)), True)
                page.send({"t": "winlink_connect", "url": "ardop:///N0XYZ?freq=7101", "peer": True})
                check("H-3 a connect refused while listening", bool(page.wait(
                    lambda e: e.get("t") == "notice" and e.get("level") == "warn"
                    and "listen" in e.get("msg", "").lower(), 5)), True)
                page.send({"t": "winlink_listen_here"})
                time.sleep(0.5)
                check("H-3 I'm here with no question asked: the window is unchanged",
                      (window(page) or {}).get("ask_until"), None)

            if picked("H-4"):
                page.events.clear()
                page.send({"t": "winlink_listen_stop"})
                check("H-4 Stop listening ends the window", bool(idle(page)), True)
                check("H-4 Pat drops --listen", wait_for(lambda: listeners(page) == [], 10), True)
                check("H-4 after: the ARDOP lane may NOT key", wait_for(lambda: not lane_may_key(), 15), True)
                check("H-4 the page is told it ended",
                      any(n.startswith("Listening ended: stopped from the page") for n in notices(page)), True)

        if picked("H-5"):
            other = L.WebPage(L.WEB)
            other.send({"t": "winlink_listen", "hf": 1})
            ok = wait_for(lambda: (window(page) or {}).get("kind") == "hf" and listen_busy(page) is None, 25)
            check("H-5 a window opened from a second page (80 m channel)",
                  (bool(ok), rig("f")), (True, "3588500"))
            check("H-5 it may key while that page is open", lane_may_key(), True)
            other.close()
            check("H-5 that page closes: the window ends", bool(idle(page, 30)), True)
            check("H-5 ...saying why", wait_for(lambda: any("page that opened it closed" in n for n in notices(page)), 5)
                  is not None and any("page that opened it closed" in n for n in notices(page)), True)
            check("H-5 after: the ARDOP lane may NOT key", wait_for(lambda: not lane_may_key(), 15), True)

        if picked("H-6"):
            check("H-6 a window opens", bool(open_window(page)), True)
            L.fake_post = getattr(L, "fake_post", None)
            import urllib.request
            urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{L.PATP}/fake/inbound",
                data=json.dumps({"remote": "ARDOP:N0XYZ", "method": "ardop"}).encode(), method="POST",
                headers={"Content-Type": "application/json"}), timeout=5).read()
            check("H-6 Pat answers N0XYZ", bool(wait_for(lambda: (page.snapshot().get("inbound") or {}).get("call")
                                                         == "N0XYZ", 10)), True)
            page.events.clear()
            page.send({"t": "winlink_listen_stop"})
            check("H-6 the window ends", bool(idle(page)), True)
            log = L.fake_log(flog)
            disc = [r["t"] for r in log if r.get("route") == "disconnect"]
            check("H-6 Pat was asked to disconnect the caller", bool(disc), True)
            check("H-6 no exchange left", page.snapshot().get("inbound"), None)
            check("H-6 after: the ARDOP lane may NOT key", wait_for(lambda: not lane_may_key(), 15), True)
        check("H-2 the radio may be anywhere after; it is not left transmitting", L.ptt(), "0")
        del freq0
    finally:
        if page:
            page.close()
        L.stop(server)


# ------------------------------------------------------------ H-7, H-8: in process

class Host:
    """What PatEngine reaches pages through (a stand-in station)."""

    def __init__(self):
        self.notes, self.sent, self.tx_stopped = [], [], False

    def notice(self, msg, level="info", ws=None):
        self.notes.append((level, msg))

    def _send(self, *a):
        pass

    def _broadcast(self, payload):
        pass

    def band_of(self, hz):
        return "40m" if 7.0e6 <= hz <= 7.3e6 else None


class FakeLane:
    """RemoteLane's contract: arm(ws, url) -> bool, armed, end(...)."""

    def __init__(self):
        self.name, self.armed, self.arms, self.ends = "IC-7300", False, [], []

    async def arm(self, ws, url):
        self.arms.append(url)
        self.armed = True
        return True

    async def end(self, started, ok, reason, closing, notify):
        self.ends.append(reason)
        self.armed = False


class FakeWs:
    closed = False


async def until(pred, t=20.0):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return bool(pred())


async def in_process(res):
    import winlink as W
    tmp = tempfile.mkdtemp(prefix="wlhf-ip-")
    flog = os.path.join(tmp, "pat.jsonl")
    os.environ["FAKE_PAT_LOG"] = flog
    host = Host()
    lane = FakeLane()
    plan = LP.HfListenPlan((LP.HfChannel(7102.5, "ardop"),), max_minutes=60, presence_minutes=15)
    cfg = testkit.pat_config(os.path.join(tmp, "patcfg.json"))     # never the user's own (scratch HOME)
    args = types.SimpleNamespace(winlink_pat=f"{sys.executable} tests/fake_pat.py --config {cfg}",
                                 winlink_pat_port=free_port(),
                                 winlink_session_max=60, winlink_pat_config=None, winlink_listen=None,
                                 winlink_listen_hf=plan, addons_state=os.path.join(tmp, "addons.json"))
    eng = W.PatEngine(host, args, lane_for=lambda url, via: (lane, None), hf_modems=["ardop"])
    await eng.start()
    try:
        await until(lambda: eng.pat_ws is not None and eng._status_live and eng.mycall
                    and (eng.live_at or 1e18) <= time.monotonic(), 15)

        async def open_one():
            ws = FakeWs()
            await eng.listen_hf_start(ws, 0)
            await until(lambda: eng.listening is not None and eng._listen_busy is None, 15)
            return ws

        async def ended():
            return await until(lambda: eng.listening is None and eng._listen_busy is None, 30)

        W.LISTEN_POLL_S = 0.1
        # presence: asked, answered (kept), asked again, unanswered (ended)
        plan_fast = LP.HfListenPlan((LP.HfChannel(7102.5, "ardop"),), max_minutes=60, presence_minutes=0.02)
        eng.listen_hf_plan = plan_fast
        W.HF_ASK_GRACE_S = 1.0
        await open_one()
        res["armed url"] = lane.arms[-1]
        asked = await until(lambda: (eng.listening or {}).get("ask_until") is not None, 5)
        eng.listen_here()
        res["answered"] = (asked, (eng.listening or {}).get("ask_until"), eng.listening is not None)
        asked2 = await until(lambda: (eng.listening or {}).get("ask_until") is not None, 5)
        res["unanswered"] = (asked2, await ended(), lane.armed, lane.ends[-1:])
        # max_minutes
        eng.listen_hf_plan = plan
        await open_one()
        eng.listening["until"] = time.time() + 0.5
        res["ran out"] = (await ended(), lane.armed, lane.ends[-1:])
        # STOP TX
        await open_one()
        host.tx_stopped = True
        res["stop tx"] = (await ended(), lane.armed, lane.ends[-1:])
        host.tx_stopped = False
        # STOP TX already on: refused, nothing armed
        host.tx_stopped = True
        n = len(lane.arms)
        await eng.listen_hf_start(FakeWs(), 0)
        res["refused under stop tx"] = (len(lane.arms) - n, eng.listening, [m for lv, m in host.notes][-1])
        host.tx_stopped = False
        # the lane disarmed under the window (the radio's page, a restart of the radio's server)
        await open_one()
        lane.armed = False
        res["lane lost"] = (await ended(), [m for lv, m in host.notes if "Listening ended" in m][-1:])
        # a channel that is no channel
        n = len(lane.arms)
        await eng.listen_hf_start(FakeWs(), 5)
        res["bad channel"] = (len(lane.arms) - n, eng.listening)
        # H-8: a record left by a server that died mid-window, with a listening Pat
        pat = subprocess.Popen([sys.executable, "tests/fake_pat.py", "--listen", "ardop", "http", "--addr",
                                f"127.0.0.1:{eng.port}"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        res["stale pid"] = pat.pid
    finally:
        await eng.stop()
    return eng, tmp


if picked("H-7") or picked("H-8"):
    res = {}
    eng, tmp = asyncio.run(in_process(res))
    if picked("H-7"):
        check("H-7 the lane is armed with the dial in the URL", res["armed url"], "ardop:///N9ABC?freq=7101")
        check("H-7 asked \"still there?\", answered: kept open", res["answered"], (True, None, True))
        check("H-7 asked again, unanswered: ended, lane ended",
              res["unanswered"][:3], (True, True, False))
        check("H-7   ...saying why", "answered" in (res["unanswered"][3] or [""])[0]
              or "still there" in (res["unanswered"][3] or [""])[0], True)
        check("H-7 max_minutes: ended, lane ended", res["ran out"], (True, False, ["the window ran out"]))
        check("H-7 STOP TX: ended, lane ended", res["stop tx"], (True, False, ["transmit was stopped (STOP TX)"]))
        check("H-7 STOP TX on: Listen refused, nothing armed",
              (res["refused under stop tx"][0], res["refused under stop tx"][1],
               "STOP TX" in res["refused under stop tx"][2]), (0, None, True))
        check("H-7 the lane disarmed under it: ended", res["lane lost"][0], True)
        check("H-7   ...saying why", "lane was disarmed" in (res["lane lost"][1] or [""])[0], True)
        check("H-7 a channel that is not in the plan: refused, nothing armed", res["bad channel"], (0, None))
    if picked("H-8"):
        import winlink as W
        pid = res["stale pid"]
        rec = {"kind": "hf", "method": "ardop", "hz": 7101000, "since": time.time() - 60, "until": time.time() + 600,
               "server_pid": 999999, "stage": "listening", "hub": None, "name": "IC-7300", "call": "N9ABC",
               "pat_port": eng.port, "pid": pid}
        path = os.path.join(tmp, W.LISTEN_FILE)
        with open(path, "w") as f:
            json.dump(rec, f)

        async def recover():
            e2 = W.PatEngine(Host(), types.SimpleNamespace(
                winlink_pat=f"{sys.executable} tests/fake_pat.py --config {os.path.join(tmp, 'patcfg.json')}",
                winlink_pat_port=eng.port, winlink_session_max=60,
                winlink_pat_config=None, winlink_listen=None, winlink_listen_hf=None,
                addons_state=os.path.join(tmp, "addons.json")))
            await e2._recover_listen()
        time.sleep(1.0)
        asyncio.run(recover())
        gone = wait_for(lambda: subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode != 0, 15)
        check("H-8 the listening Pat that outlived its server is ended", bool(gone), True)
        check("H-8 the record is dropped", os.path.exists(path), False)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
