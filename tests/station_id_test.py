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

# --- unit: xara's CW ID, matched to the VARA lane's burst (plan winlink-vara A3) ---
# Each falsifier fails on the ARDOP rule ("a lane PTT accepted between the
# ID's start and end, none refused") or on "the first lane release after
# the log line".
LEAD = station_id.XARA_CW_LEAD_S


def vara(clock_at=0.0):
    clock[0] = 2000.0 + clock_at
    return station_id.IdLedger({"ardop", "varahf"}, clock=lambda: clock[0])


def at(s):
    return 2000.0 + s


def xid(L):
    return L.last("xara")


# (i) our QRT: the CW rides the QRT frame's key; T 1 is ~1.4 s before "tx cw"
L = vara()
L.ptt(True, at(0), "varahf")
clock[0] = at(1.4)
L.id_started("CW ID (Closing)", at(1.4), "xara")
check("A3 (i) CW inside the key: in progress until the key ends", (L.in_progress("xara"), xid(L)["on_air"]),
      (True, None))
clock[0] = at(3.0)
L.ptt_released(at(3.0), "varahf", "normal")
check("A3 (i) own T 0 ends it: on air, source xara",
      (xid(L)["on_air"], xid(L)["source"], xid(L)["end"]), (True, "xara", at(3.0)))
check("A3 (i) nothing owed", L.owed(), False)

# (ii) their QRT: "tx cw", then a fresh T 1 within the lead window, then T 0
L = vara()
L.ptt(True, at(-20), "varahf")
L.ptt_released(at(-15), "varahf", "normal")           # an earlier, unrelated burst
L.id_started("CW ID (Closing)", at(0), "xara")
check("A3 (ii) line first, no key yet: in progress", L.in_progress("xara"), True)
L.ptt(True, at(LEAD - 0.2), "varahf")
check("A3 (ii) key after the line: matched, still keyed", xid(L)["on_air"], None)
L.ptt_released(at(LEAD + 7), "varahf", "normal")
check("A3 (ii) its T 0: on air", (xid(L)["on_air"], xid(L)["end"]), (True, at(LEAD + 7)))

# (iii) "tx cw", T 1, the socket drops mid-CW (a forced release): owed
L = vara()
L.ptt(True, at(-5), "varahf")
L.ptt_released(at(-4), "varahf", "normal")
L.id_started("CW ID (Closing)", at(0), "xara")
L.ptt(True, at(0.05), "varahf")
L.ptt_released(at(2.0), "varahf", "forced")
check("A3 (iii) disconnect mid-CW: not on air", xid(L)["on_air"], False)
check("A3 (iii) ... and an ID is owed", L.owed(), True)

# (iv) an open xara ID while an ARDOP-lane burst releases: still open
L = vara()
L.ptt(True, at(-1), "varahf")
L.id_started("CW ID (Closing)", at(0), "xara")
L.ptt(True, at(0.5), "ardop")
L.ptt_released(at(1.0), "ardop", "normal")
check("A3 (iv) ARDOP lane's release does not end xara's ID", (L.in_progress("xara"), xid(L)["end"]),
      (True, None))
L.ptt_released(at(6.0), "varahf", "normal")
check("A3 (iv) the VARA lane's own release does", xid(L)["on_air"], True)

# (v) the log line arrives after its burst was released (the tail polls every 250 ms)
L = vara()
L.ptt(True, at(0), "varahf")
L.ptt_released(at(3.0), "varahf", "normal")
clock[0] = at(3.3)
L.id_started("CW ID (Closing)", at(1.4), "xara")
check("A3 (v) late log line: matched to its released burst, on air", (xid(L)["on_air"], xid(L)["end"]),
      (True, at(3.0)))

# refused: the CW's T 1 refused (no VARA session armed): not on air at once
L = vara()
L.ptt(True, at(-30), "varahf")
L.ptt_released(at(-25), "varahf", "normal")
L.id_started("CW ID (Closing)", at(0), "xara")
L.ptt(False, at(0.05), "varahf")
check("xara: refused key -> not on air, owed", (xid(L)["on_air"], L.owed()), (False, True))

# a refused T 1 on the VARA lane inside the CW's burst: not on air
L = vara()
L.ptt(True, at(0), "varahf")
L.id_started("CW ID (Closing)", at(0.02), "xara")
L.ptt(False, at(1.0), "varahf")
L.ptt_released(at(4.0), "varahf", "normal")
check("xara: a refused T 1 during the CW -> not on air", xid(L)["on_air"], False)

# no key at all: stale after STALE_ID_S
L = vara()
L.ptt(True, at(-60), "varahf")
L.ptt_released(at(-55), "varahf", "normal")
L.id_started("CW ID (Closing)", at(0), "xara")
clock[0] = at(station_id.STALE_ID_S + 1)
L.tick()
check("xara: never keyed -> stale, not on air, owed", (xid(L)["on_air"], L.owed()), (False, True))

# a key well after the lead window is not the CW's
L = vara()
L.id_started("CW ID (Closing)", at(0), "xara")
L.ptt(True, at(LEAD + 2), "varahf")
L.ptt_released(at(LEAD + 5), "varahf", "normal")
check("xara: a key past the lead window is not its burst", xid(L)["on_air"], None)

# cross-source: an open ardopcf ID is not completed by a xara end, nor the reverse
L = vara()
L.id_started("10 minute ID", at(0), "ardopcf")
L.ptt(True, at(0.1), "varahf")
L.id_started("CW ID (Closing)", at(0.2), "xara")
L.ptt_released(at(4.0), "varahf", "normal")
check("cross-source: xara's ID done, ardopcf's still open",
      (xid(L)["on_air"], L.last("ardopcf")["end"]), (True, None))
L.id_ended(at(5.0), "ardopcf")
check("cross-source: ardopcf's ID judged by the ARDOP lane only (no ARDOP key: not on air)",
      L.last("ardopcf")["on_air"], False)
L = vara()
L.id_started("CW ID (Closing)", at(0), "xara")
check("cross-source: an ardopcf end does not touch xara's open ID",
      (L.id_ended(at(1.0), "ardopcf"), xid(L)["end"]), (None, None))

# panel p2b05e28fee F1: a lead-window match must not cross a session boundary.
# "tx cw" at t, the page closes (disarm), xara's T 1 is refused at t+0.1, a NEW
# session is armed and keys at t+0.8, its own T 0: that CW never went out.
# The log line read before the new key (the tail polls every 250 ms) ...
L = vara()
L.ptt(True, at(-20), "varahf")
L.ptt_released(at(-15), "varahf", "normal")
L.id_started("CW ID (Closing)", at(0), "xara")
L.new_generation()                                   # disarm (page closed)
L.ptt(False, at(0.1), "varahf")                      # xara's CW key refused
L.new_generation()                                   # a new session armed
L.ptt(True, at(0.8), "varahf")
L.ptt_released(at(4.0), "varahf", "normal")
check("F1 CW refused, a new session's key in the lead window: not on air, owed",
      (xid(L)["on_air"], L.owed()), (False, True))
# ... and read after it: the ID's generation is the one current when its line was
# written, so the tail stamps it with the generation at the line's wall time.
L = vara()
L.ptt(True, at(-20), "varahf")
L.ptt_released(at(-15), "varahf", "normal")
L.new_generation(at(-1))                             # the session whose CW this is
L.new_generation(at(0.05))                           # disarm (page closed)
L.ptt(False, at(0.1), "varahf")                      # xara's CW key refused
L.new_generation(at(0.5))                            # a new session armed
L.ptt(True, at(0.8), "varahf")
L.ptt_released(at(4.0), "varahf", "normal")
clock[0] = at(4.2)
L.id_started("CW ID (Closing)", at(0), "xara")       # the line, delivered late
check("F1 the same with the log line delivered late: not on air, owed",
      (xid(L)["on_air"], L.owed()), (False, True))
L = vara()
L.id_started("CW ID (Closing)", at(0), "xara")
L.new_generation(at(0.2))                            # disarmed
L.new_generation(at(0.5))                            # the next session armed
L.ptt(True, at(0.8), "varahf")                       # no refusal seen, but another session's key
L.ptt_released(at(4.0), "varahf", "normal")
check("F1 a lead-window key from another arm generation is not the CW's", xid(L)["on_air"] is True, False)

# panel p2b05e28fee F2: an ardopcf ID cut by a forced release on the ARDOP lane
L = vara()
L.ptt(True, at(100), "ardop")
L.id_started("Rcvd END in IDLE ProtocolState", at(100.1), "ardopcf")
L.ptt_released(at(101), "ardop", "forced")           # disconnect / limit / disarm mid-ID
L.id_ended(at(103), "ardopcf")
check("F2 ardopcf ID with a forced release inside it: not on air, owed",
      (L.last("ardopcf")["on_air"], L.owed()), (False, True))
L = vara()
L.ptt(True, at(100), "ardop")
L.id_started("Rcvd END in IDLE ProtocolState", at(100.1), "ardopcf")
L.ptt_released(at(101), "ardop", "normal")
L.ptt(True, at(101.2), "ardop")
L.ptt_released(at(103), "ardop", "normal")
L.id_ended(at(103), "ardopcf")
check("F2 control: the same ID released normally is on air", L.last("ardopcf")["on_air"], True)

# 4.2 negative control: a transmitting session whose log has no "tx cw" line
L = vara()
L.ptt(True, at(0), "varahf")
L.ptt_released(at(5.0), "varahf", "normal")
check("no tx cw line: an ID is owed after transmitting", L.owed(), True)

# --- unit: VaraLogTail (real-shaped xara v0.1.3 --log lines) ---
import shutil  # noqa: E402

d = tempfile.mkdtemp(prefix="xaralog-")
xlog = os.path.join(d, "xara.log")


def xline(wall, text):
    """A line as xara's log.rs + tnc.rs echo write it: stamp, station seconds (10.3), text."""
    ms = int(round((wall % 1) * 1000))
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(int(wall))) + f".{ms:03d}Z {123.456:10.3f} {text}\n"


def append(text):
    with open(xlog, "a") as f:
        f.write(text)


W0 = 1792083039.0
append(xline(W0 - 100, "  tx cw Closing"))                 # history: not replayed
append(xline(W0 - 99, "< BITRATE (2)  138 bps TX"))
L = vara()
T = station_id.VaraLogTail(xlog, L)
T.poll(first=True)
check("tail: starts at the end (history not replayed)", (L.last("xara"), T.max_level(W0 - 200)), (None, None))
append(xline(W0, "< BITRATE (3)  273 bps TX") + xline(W0 + 1, "  tx c4 f0 Sending") +
       xline(W0 + 2, "< BITRATE (4)  537 bps RX") + xline(W0 + 3, "  tx cw Closing"))
T.poll()
check("tail: tx cw -> a xara ID started at the line's UTC time",
      (xid(L)["source"], xid(L)["reason"], xid(L)["start"]), ("xara", "CW ID (Closing)", W0 + 3))
check("tail: the highest speed level reported", T.max_level(W0 - 1, W0 + 10), 4)
before = sum(1 for i in L.ids if i["source"] == "xara")
append(xline(W0 + 4, "  tx cw Closing")[:-6])              # half a line: waits for its end
T.poll()
check("tail: half a line is not read yet", sum(1 for i in L.ids if i["source"] == "xara"), before)
append("osing\n")
T.poll()
check("tail: ... and is once it ends", sum(1 for i in L.ids if i["source"] == "xara"), before + 1)
with open(xlog, "w") as f:                                  # truncated
    f.write(xline(W0 + 10, "  tx cw Closing"))
T.poll()
check("tail: truncation -> read from the start", xid(L)["start"], W0 + 10)
os.rename(xlog, xlog + ".1")                               # rotated: a new file under the name
with open(xlog, "w") as f:
    f.write(xline(W0 + 20, "  tx cw Closing") + "x" * 400 + "\n")
T.poll()
check("tail: a new file under the name (inode) -> read from its start", xid(L)["start"], W0 + 20)
shutil.rmtree(d, ignore_errors=True)

# ArdopLogTail: the same name on a new file (inode change, size not smaller) is read from its start
d = tempfile.mkdtemp(prefix="ardoplog-")
af = os.path.join(d, f"ARDOPDebug8515_{DATE}.log")
with open(af, "w") as f:
    f.write("16:00:00.000 D [Main.KeyPTT]  PTT-TRUE\n")
L = station_id.IdLedger({"ardop"})
A = station_id.ArdopLogTail(d, L)
A.poll(first=True)
os.rename(af, af + ".old")
with open(af, "w") as f:
    f.write("16:50:39.974 D SendID  N0CALL:[FN31]  10 minute ID\n" + "x" * 200 + "\n")
A.poll()
check("ardop tail: a new file under the same name is followed (inode)",
      (L.last() or {}).get("reason"), "10 minute ID")
shutil.rmtree(d, ignore_errors=True)

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
