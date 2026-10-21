"""Station ID (47 CFR 97.119(a)) for Winlink sessions, with tests/fake_pat.py
standing in for Pat + ardopcf (it writes ardopcf-style log lines and keys the
ID through the Winlink lane, as Pat does for ardopcf).

Unit:
  - ardopcf log lines -> ID start/end (with and without CW ID); ConReq and
    other frames are not IDs
  - ledger: on air only if the lane accepted the ID's keying; owed after any
    transmission that no on-air ID followed; not tracked -> owed is None
Server:
  - an ID sent before Pat returns: on air, nothing owed, page told
  - session cap (dirty abort) with Pat returning BEFORE ardopcf's ID: the
    session stays armed until the ID has gone out, then disarms
  - no ID at the end (stock ardopcf after a failed connect): the hold is
    bounded; the page is told an ID is owed; ardopcf's later unarmed
    "10 minute ID" is refused, logged as a WARNING naming the station ID,
    and the page is told it did not go on air
  - without --winlink-ardop-log: "not tracked", never "sent"
  - the page closing still disarms at once (operator gone: no ID wait)

    .venv/bin/python tests/station_id_test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import station_id  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:60} got {got!r}" + ("" if ok else f" want {want!r}"))


# --- unit: parser ---------------------------------------------------------

DATE = "20261015"
W = station_id.line_wall(DATE, "16", "50", "39", "974")
check("line time is UTC wall clock", W, 1792083039.974)  # 2026-10-15T16:50:39.974Z


def feed(p, lines):
    out = []
    for ln in lines:
        out += p.feed(ln, DATE)
    return out


p = station_id.ArdopLogParser()
ev = feed(p, [
    "16:50:39.974 D SendID  N0CALL:[FN31]  Rcvd END in IDLE ProtocolState",
    "16:50:39.974 D [AddTagToDataAndSendToHost] bytes=16 Tag IDF",
    "16:50:39.974 I Sending Frame Type IDFrame",
    "16:50:39.974 D [Main.KeyPTT]  PTT-TRUE",
    "16:50:41.986 D [Main.KeyPTT]  PTT-FALSE",
    "16:50:42.054 D Capture device USB Audio CODEC open",
    "16:50:42.054 D Sending CW ID of N0CALL",
    "16:50:42.054 D [Main.KeyPTT]  PTT-TRUE",
    "16:50:48.553 D [Main.KeyPTT]  PTT-FALSE",
])
check("parser: ID with CW -> start + end at the CW's PTT-FALSE",
      [(k, r, round(t - W, 3)) for k, r, t in ev],
      [("start", "Rcvd END in IDLE ProtocolState", 0.0), ("end", None, 8.579)])

p = station_id.ArdopLogParser()
ev = feed(p, [
    "16:40:23.105 I Sending Frame Type ConReq2000M",
    "16:40:23.105 D [Main.KeyPTT]  PTT-TRUE",
    "16:40:25.117 D [Main.KeyPTT]  PTT-FALSE",
    "16:40:27.127 D  Command Trace TO Host NEWSTATE DISC ",
])
check("parser: ConReq frames are not an ID", ev, [])

p = station_id.ArdopLogParser()
ev = feed(p, [
    "16:41:32.029 D SendID  N0CALL:[FN31]  10 minute ID",
    "16:41:32.029 D [Main.KeyPTT]  PTT-TRUE",
    "16:41:34.052 D [Main.KeyPTT]  PTT-FALSE",
])
check("parser: IDFrame only -> started, end not yet known", [k for k, _, _ in ev], ["start"])
end_frame = station_id.line_wall(DATE, "16", "41", "34", "052")
check("parser: no CW ID within 1.5 s -> ended with the frame",
      p.tick(end_frame + 2), [("end", None, end_frame)])
check("parser: garbage line ignored", p.feed("not a log line", DATE), [])

# --- unit: ledger ---------------------------------------------------------

clock = [1000.0]
L = station_id.IdLedger(True, clock=lambda: clock[0])
check("ledger: nothing sent -> nothing owed", L.owed(), False)
L.ptt(True, 1000.0)                       # a ConReq on air
check("ledger: transmitted -> ID owed", L.owed(), True)
L.id_started("ConReq not answered", 1010.0)
L.ptt(True, 1010.01)
L.ptt(True, 1012.1)
clock[0] = 1012.5
check("ledger: ID in progress", L.in_progress(), True)
L.id_ended(1018.6)
check("ledger: ID keyed through the lane -> on air", L.last()["on_air"], True)
check("ledger: ... and nothing owed", L.owed(), False)
L.ptt(True, 1100.0)
check("ledger: transmitting again -> owed again", L.owed(), True)
L.id_started("10 minute ID", 1700.0)
L.ptt(False, 1700.01)
L.ptt(False, 1702.1)
L.id_ended(1708.0)
check("ledger: ID refused by the lane -> not on air", L.last()["on_air"], False)
check("ledger: ... and still owed", L.owed(), True)
U = station_id.IdLedger(False)
U.ptt(True)
check("ledger: not tracked -> owed is None (never 'sent')", (U.owed(), U.snapshot()["tracked"]), (None, False))

# --- server ---------------------------------------------------------------

import winlink_test_lib as lib  # noqa: E402


def id_events(page):
    return [e["id"] for e in page.events if e.get("t") == "winlink" and "id" in e]


def wait_id(page, pred, timeout=15):
    return page.wait(lambda e: e.get("t") == "winlink" and "id" in e and pred(e["id"]), timeout)


def armed_events(page):
    return [(e["armed"], time.time()) for e in page.events if e.get("t") == "winlink" and "armed" in e]


# 1. ID before Pat returns (patched ardopcf + Pat waiting for DISC)
logdir = tempfile.mkdtemp(prefix="ardoplog-")
server, flog = lib.start({"FAKE_PAT_TX_S": "1", "FAKE_PAT_ARDOP_LOG": logdir, "FAKE_PAT_ID": "end"},
                         "--winlink-ardop-log", logdir)
try:
    page = lib.Page()
    snap = page.hello["winlink"].get("id") or {}
    check("snapshot: ID tracked from ardopcf's log", snap.get("tracked"), True)
    page.send({"t": "winlink_connect", "url": lib.URL})
    e = page.wait(lib.ended, 20)
    check("end ID: session ended ok", e and e.get("ok"), True)
    ids = [r for r in lib.fake_log(flog) if r["route"] == "id"]
    check("end ID: keyed through the lane while armed", [r["replies"] for r in ids], [["RPRT 0", "RPRT 0"]])
    e = wait_id(page, lambda i: i["last"] and i["last"]["on_air"] is not None, 5)
    check("end ID: page told it went on air, nothing owed",
          e and (e["id"]["last"]["on_air"], e["id"]["owed"], e["id"]["last"]["reason"]),
          (True, False, "Rcvd END in IDLE ProtocolState"))
    check("end ID: lane disarmed after", lib.lane_ptt_refused(), True)
    page.close()
finally:
    out = lib.stop(server)
check("end ID: server log says on air", any("station ID on air" in l for l in out.splitlines()), True)

# 2. session cap; Pat returns at once, ardopcf's ID follows 1 s later
logdir = tempfile.mkdtemp(prefix="ardoplog-")
server, flog = lib.start({"FAKE_PAT_TX_S": "30", "FAKE_PAT_ARDOP_LOG": logdir,
                          "FAKE_PAT_ID": "after_abort"},
                         "--winlink-ardop-log", logdir, "--winlink-session-max", "3")
try:
    page = lib.Page()
    page.send({"t": "winlink_connect", "url": lib.URL})
    e = page.wait(lib.ended, 30)
    check("cap: session ended not ok", e and e.get("ok"), False)
    time.sleep(0.5)
    ids = [r for r in lib.fake_log(flog) if r["route"] == "id"]
    check("cap: ardopcf's ID after the abort keyed while still armed",
          [r["replies"] for r in ids], [["RPRT 0", "RPRT 0"]])
    check("cap: nothing owed", (page.wait(lambda e: e.get("t") == "winlink" and "id" in e
                                          and e["id"]["owed"] is False, 3) or {}).get("id", {}).get("owed"), False)
    check("cap: radio unkeyed, lane disarmed", (lib.ptt(), lib.lane_ptt_refused()), ("0", True))
    page.close()
finally:
    out = lib.stop(server)

# 3. no ID at the end; ardopcf's 10-minute ID comes 12 s after the session (past
#    the bounded hold), unarmed
logdir = tempfile.mkdtemp(prefix="ardoplog-")
server, flog = lib.start({"FAKE_PAT_TX_S": "1", "FAKE_PAT_ARDOP_LOG": logdir, "FAKE_PAT_ID": "none",
                          "FAKE_PAT_LATE_ID_S": "12"},
                         "--winlink-ardop-log", logdir)
try:
    page = lib.Page()
    t0 = time.time()
    page.send({"t": "winlink_connect", "url": lib.URL})
    e = page.wait(lib.ended, 30)
    took = time.time() - t0
    check("no ID: the hold for an ID is bounded (< 15 s)", bool(e) and took < 15, True)
    e = wait_id(page, lambda i: i["owed"] is True, 3)
    check("no ID: page told an ID is owed", e is not None, True)
    e = wait_id(page, lambda i: i["last"] and i["last"]["on_air"] is False, 15)
    check("late ID refused: page told it did not go on air",
          e and (e["id"]["last"]["reason"], e["id"]["owed"]), ("10 minute ID", True))
    ids = [r for r in lib.fake_log(flog) if r["route"] == "id"]
    check("late ID: lane refused its PTT", [r["replies"] for r in ids], [["RPRT -11", "RPRT -11"]])
    check("late ID: radio never keyed", lib.ptt(), "0")
    page.close()
finally:
    out = lib.stop(server)
lines = out.splitlines()
warn = [l for l in lines if "WARNING" in l and "PTT refused" in l]
check("lane: refusal WARNING names the station ID", bool(warn) and all("station ID" in l for l in warn), True)
check("lane: one refusal WARNING per burst (hamlib retries)", len(warn), 1)
check("ledger: WARNING that the ID did NOT go on air",
      any("WARNING" in l and "did NOT go on air" in l and "10 minute ID" in l for l in lines), True)

# 4. not tracked
server, flog = lib.start({"FAKE_PAT_TX_S": "1"})
try:
    page = lib.Page()
    snap = page.hello["winlink"].get("id") or {}
    check("untracked: snapshot says not tracked, owed unknown",
          (snap.get("tracked"), snap.get("owed")), (False, None))
    page.send({"t": "winlink_connect", "url": lib.URL})
    e = page.wait(lib.ended, 20)
    check("untracked: session still ends ok", e and e.get("ok"), True)
    check("untracked: never reports an ID as sent",
          [i for i in id_events(page) if i.get("last") or i.get("owed") is not None], [])
    page.close()
finally:
    lib.stop(server)

# 5. page closed mid-session: disarmed at once, no ID wait
logdir = tempfile.mkdtemp(prefix="ardoplog-")
server, flog = lib.start({"FAKE_PAT_TX_S": "20", "FAKE_PAT_ARDOP_LOG": logdir, "FAKE_PAT_ID": "after_abort"},
                         "--winlink-ardop-log", logdir)
try:
    page = lib.Page()
    page.send({"t": "winlink_connect", "url": lib.URL})
    page.wait(lambda e: e.get("t") == "winlink" and e.get("armed") is True, 5)
    time.sleep(1.0)
    page.close()
    time.sleep(0.5)
    check("page closed: lane disarmed at once", lib.lane_ptt_refused(), True)
    time.sleep(2.0)
    ids = [r for r in lib.fake_log(flog) if r["route"] == "id"]
    check("page closed: ardopcf's ID afterwards is refused (operator gone)",
          [r["replies"] for r in ids], [["RPRT -11", "RPRT -11"]])
finally:
    lib.stop(server)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
