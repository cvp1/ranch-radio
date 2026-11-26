"""Winlink over Mercury HF, the radio server's side (plan winlink-mercury M0):
server.py --sim with tests/fake_mercury_owner.py as Mercury's PTT (T 1,
silence, T 0). Sessions are armed from the radio's own page, which also
plays the station's part (winlink_end). Nothing here transmits.

  - off: no Mercury flags -> the hello is as before (no vara_backend), and
    Mercury flags with the xara backend -> Mercury's lane does not listen
  - the shorten-only test knobs are refused without --sim, and above 9:00/8:30
  - on: modems ARDOP and VARA HF, vara_backend mercury; unarmed Mercury
    T 1 -> RPRT -11, no PTT; armed -> backend mercury; xara's lane and
    ARDOP's lane refuse during it
  - keepalive: a silent 30 s key holds, a 34 s one is cut at 33 s (negative
    control), the lane disarms nothing by itself
  - end of session, F9: Mercury still keyed 10 s after winlink_end; the lane
    stays armed past ID_START_S (8 s), the station ID goes after the release,
    on air (source station), then disarm
  - 10-minute gate (shortened): Mercury keyed through the gate, the ID at its
    release, Mercury keys again after it
  - collision: Mercury's next T 1 during the ID -> cut, owed, disarmed; the
    keys after it refused; no ID later
  - the session bound (shortened): end_request to the arming page, Mercury
    fenced, the page's winlink_end -> ID on air -> disarm

    .venv/bin/python tests/winlink_mercury_test.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import testkit  # noqa: E402
import winlink_test_lib as lib  # noqa: E402
from rigctl_server import WINLINK_MERCURY_KEEPALIVE_S  # noqa: E402

os.environ.pop("CC_OBS_TOKENS_FILE", None)
fails = 0
lib.APPS, lib.LANE, lib.RWEB = (testkit.free_port() for _ in range(3))
MLANE, VLANE = testkit.free_port(), testkit.free_port()
XLOG = os.path.join(tempfile.mkdtemp(prefix="xara-"), "xara.log")
open(XLOG, "w").close()


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:70} got {got!r}" + ("" if ok else f" want {want!r}"))


def base():
    return [sys.executable, "server.py", "--sim", "--host", "127.0.0.1", "--listen", str(lib.RWEB),
            "--rigctl-host", "127.0.0.1", "--rigctl-port", str(lib.APPS),
            "--winlink-rigctl-port", str(lib.LANE), "--addons-state", tempfile.mkdtemp(prefix="addons-")]


MERCURY = ["--winlink-vara-modem", "mercury", "--winlink-mercury-rigctl-port", str(MLANE),
           "--winlink-mercury-call", "N0CALL",
           "--winlink-vara-log", XLOG, "--winlink-vara-rigctl-port", str(VLANE)]


def start(*extra, sim=True):
    argv = base() + list(extra)
    if not sim:
        argv.remove("--sim")
    p = subprocess.Popen(argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if not testkit.wait_for(lambda: p.poll() is not None or listening(lib.RWEB), 40):
        p.kill()
        raise SystemExit("server did not start")
    return p


def stop(p):
    p.terminate()
    try:
        out, _ = p.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
    return out or ""


def listening(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
        return True
    except OSError:
        return False


def owner(key_s=2.0, count=1, gap_s=1.0, delay_s=0.0):
    ev = tempfile.NamedTemporaryFile(prefix="mercury-", suffix=".jsonl", delete=False).name
    p = subprocess.Popen([sys.executable, "tests/fake_mercury_owner.py", "--port", str(MLANE),
                          "--key-s", str(key_s), "--count", str(count), "--gap-s", str(gap_s),
                          "--delay-s", str(delay_s), "--t0-first", "--events", ev], cwd=ROOT)
    return p, ev


def events(ev, kind=None):
    try:
        with open(ev) as f:
            out = [json.loads(ln) for ln in f if ln.strip()]
    except OSError:
        return []
    return [e for e in out if kind is None or e["ev"] == kind]


def lane_t1(port):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        f = s.makefile("r")
        s.sendall(b"T 1\n")
        v = f.readline().strip()
        s.sendall(b"T 0\n")               # as Mercury does after a frame, keyed or not
        f.readline()
        return v


def tx():
    return lib.ptt() == "1"


def armed_ev(page):
    return page.wait(lambda e: e.get("t") == "winlink" and e.get("armed") is True, 10)


def disarmed(page, timeout):
    return page.wait(lambda e: e.get("t") == "winlink" and e.get("armed") is False, timeout)


# --- off ---------------------------------------------------------------------

p = start()
try:
    page = lib.WebPage(lib.RWEB)
    wl = page.hello["winlink"]
    check("off: hello unchanged (modems ARDOP, no vara_backend)", (wl["modems"], "vara_backend" in wl),
          (["ardop"], False))
    check("off: nothing on Mercury's lane port", listening(MLANE), False)
    page.close()
finally:
    stop(p)
p = start("--winlink-mercury-rigctl-port", str(MLANE), "--winlink-mercury-call", "N0CALL",
          "--winlink-vara-log", XLOG, "--winlink-vara-rigctl-port", str(VLANE))
try:
    page = lib.WebPage(lib.RWEB)
    wl = page.hello["winlink"]
    check("xara backend + Mercury flags: modems ARDOP and VARA HF, no vara_backend",
          (wl["modems"], "vara_backend" in wl), (["ardop", "varahf"], False))
    check("xara backend: Mercury's lane does not listen (a stray Mercury can't key)", listening(MLANE), False)
    check("xara backend: xara's lane listens", listening(VLANE), True)
    page.close()
finally:
    out = stop(p)
check("xara backend: says Mercury is off", "Mercury HF is off" in out, True)

for argv, what in ((["--winlink-mercury-test-gate-s", "5"], "gate knob without --sim"),
                   (["--winlink-mercury-test-bound-s", "5"], "bound knob without --sim")):
    r = subprocess.run(base()[:2] + ["--host", "127.0.0.1", "--listen", str(testkit.free_port()), *argv],
                       cwd=ROOT, capture_output=True, text=True, timeout=30)
    check(f"{what}: refused at startup", (r.returncode, "is for tests: it needs --sim" in r.stderr), (2, True))
r = subprocess.run(base() + ["--winlink-mercury-test-gate-s", "600"], cwd=ROOT, capture_output=True,
                   text=True, timeout=30)
check("gate knob above 9:00: refused (shorten only)", (r.returncode, "at most 540" in r.stderr), (2, True))
r = subprocess.run(base() + ["--winlink-mercury-test-bound-s", "511"], cwd=ROOT, capture_output=True,
                   text=True, timeout=30)
check("bound knob above 8:30: refused (shorten only)", (r.returncode, "at most 510" in r.stderr), (2, True))

# --- on: lanes, keepalive, end of session ----------------------------------------

p = start(*MERCURY)
try:
    page = lib.WebPage(lib.RWEB)
    wl = page.hello["winlink"]
    check("on: modems ARDOP and VARA HF, vara_backend mercury", (wl["modems"], wl.get("vara_backend")),
          (["ardop", "varahf"], "mercury"))
    check("on: Mercury's lane listens", listening(MLANE), True)
    check("unarmed: Mercury T 1 -> RPRT -11 (negative control)", lane_t1(MLANE), "RPRT -11")
    check("unarmed: no PTT", tx(), False)

    page.send({"t": "winlink_arm", "v": True, "scheme": "varahf"})
    a = armed_ev(page)
    check("armed varahf: backend mercury", a and a.get("backend"), "mercury")
    check("Mercury session: xara's lane T 1 -> RPRT -11", lane_t1(VLANE), "RPRT -11")
    check("Mercury session: ARDOP's lane T 1 -> RPRT -11", lane_t1(lib.LANE), "RPRT -11")
    check("Mercury session: no PTT from either", tx(), False)

    # keepalive: one silent 34 s key; held at 30 s, cut by 34 s
    m, ev = owner(key_s=34.0)
    testkit.wait_for(lambda: events(ev, "key"), 10)
    check("keepalive: Mercury's key accepted", [e["reply"] for e in events(ev, "key")], ["RPRT 0"])
    t0 = events(ev, "key")[0]["t"]
    time.sleep(max(0, t0 + 30.0 - time.time()))
    check("keepalive: a silent key still held at 30 s", tx(), True)
    testkit.wait_for(lambda: not tx(), 6)
    cut = time.time() - t0
    check(f"keepalive: cut at {WINLINK_MERCURY_KEEPALIVE_S:g} s (negative control: 34 s key)",
          (not tx(), WINLINK_MERCURY_KEEPALIVE_S <= cut < WINLINK_MERCURY_KEEPALIVE_S + 1.5), (True, True))
    m.wait(10)
    check("keepalive: the session stays armed", page.wait(lambda e: e.get("armed") is False, 0.5), None)

    # F9: Mercury still keyed 10 s after the end; the ID after its release
    m, ev = owner(key_s=10.0)
    testkit.wait_for(lambda: events(ev, "key"), 10)
    page.events.clear()
    page.send({"t": "winlink_end", "started": time.time() - 60, "ok": True})
    time.sleep(9.0)
    check("F9: still armed 9 s after the end (no 8 s give-up)", disarmed(page, 0.2), None)
    testkit.wait_for(lambda: events(ev, "unkey"), 5)
    rel = events(ev, "unkey")[0]["t"]
    keyed_again = testkit.wait_for(tx, 3)
    check("F9: the station ID keys after Mercury's release", (keyed_again, time.time() - rel < 3), (True, True))
    d = disarmed(page, 20)
    last = d and d["id"]["last"]
    check("F9: disarmed after the ID", bool(d), True)
    check("F9: ID on air, source station", last and (last["source"], last["on_air"]), ("station", True))
    check("F9: not owed", d and d["id"]["owed"], False)
    check("F9: RX after", tx(), False)
    m.wait(5)
    page.close()
finally:
    out = stop(p)
check("log: the Mercury lane listens on loopback", f"winlink listener on 127.0.0.1:{MLANE}" in out, True)
check("log: the keepalive cut is named", "went quiet" in out, True)

# --- gate and collision (shortened gate) ------------------------------------------

p = start(*MERCURY, "--winlink-mercury-test-gate-s", "3")
try:
    page = lib.WebPage(lib.RWEB)
    page.send({"t": "winlink_arm", "v": True, "scheme": "varahf"})
    armed_ev(page)
    # keys at 0-2 s and 2.5-4.5 s: the gate falls due under the second
    m, ev = owner(key_s=2.0, count=2, gap_s=0.5)
    m.wait(15)
    r = [e["reply"] for e in events(ev, "key")]
    check("gate: Mercury keyed through the gate (both keys accepted)", r, ["RPRT 0", "RPRT 0"])
    rel = events(ev, "unkey")[-1]["t"]
    check("gate: the ID keys at Mercury's release", testkit.wait_for(tx, 3), True)
    check("gate: ... within 1 s of it", time.time() - rel < 1.5, True)
    testkit.wait_for(lambda: not tx(), 12)
    time.sleep(0.5)
    m, ev = owner(key_s=0.5)
    m.wait(10)
    check("gate: Mercury keys again after the ID", [e["reply"] for e in events(ev, "key")], ["RPRT 0"])
    check("gate: still armed", disarmed(page, 0.3), None)
    # collision: the gate falls due, then Mercury keys during the ID
    time.sleep(3.0)
    testkit.wait_for(tx, 5)                    # the 10-minute ID (Mercury quiet)
    time.sleep(1.0)
    m, ev = owner(key_s=0.5, count=2, gap_s=0.5)
    m.wait(10)
    check("collision: Mercury's T 1 during the ID refused, and the next",
          [e["reply"] for e in events(ev, "key")], ["RPRT -11", "RPRT -11"])
    d = disarmed(page, 5)
    check("collision: the session ended", bool(d), True)
    check("collision: owed", d and d["id"]["owed"], True)
    time.sleep(2.0)
    check("collision: nothing keys later", tx(), False)
    page.close()
finally:
    out = stop(p)
check("log: the cut is named", "Mercury asked to key during the ID" in out, True)

# --- the session bound (shortened) ---------------------------------------------------

p = start(*MERCURY, "--winlink-mercury-test-bound-s", "4")
try:
    page = lib.WebPage(lib.RWEB)
    page.send({"t": "winlink_arm", "v": True, "scheme": "varahf"})
    armed_ev(page)
    m, ev = owner(key_s=1.0)
    m.wait(10)
    req = page.wait(lambda e: e.get("t") == "winlink" and e.get("end_request"), 8)
    check("bound: end_request to the arming page (Pat abort first)", bool(req), True)
    check("bound: still armed for the ID", disarmed(page, 0.2), None)
    check("bound: Mercury fenced", lane_t1(MLANE), "RPRT -11")
    page.send({"t": "winlink_end", "started": time.time() - 30, "ok": False, "reason": "aborted"})
    d = disarmed(page, 20)
    last = d and d["id"]["last"]
    check("bound: then the ID on air (station), then disarm",
          (bool(d), last and (last["source"], last["on_air"])), (True, ("station", True)))
    page.close()
finally:
    stop(p)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
