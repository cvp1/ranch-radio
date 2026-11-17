"""A Winlink session the station has to end itself, against tests/fake_pat.py:

  stuck Pat: Pat's /api/connect never returns and a disconnect can't reach it
    (Pat stuck on a wedged ardopcf, a Mac station 2026-11-17). Stop twice, or the
    page closing, ends the session ~10 s later anyway, saying the modem may be
    stuck; the radio's lane is disarmed. Control: without the guard the
    session would still be open (the wait below would time out).
  no sound card: ardopcf logs "cannot open playback device ..." at key-up.
    The session ends at once with "ARDOP is not on the air", by a dirty
    disconnect, not a 30 s "connect timeout". Control: the same session with
    a working sound card runs to a good end.

    .venv/bin/python tests/winlink_stuck_test.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import winlink_test_lib as L  # noqa: E402
from winlink_test_lib import URL, ended, fake_log, lane_ptt_refused, stop  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


class Page(L.WebPage):
    def __init__(self):
        super().__init__(L.WEB)


def armed(e):
    return e.get("t") == "winlink" and e.get("armed") is True


# --- stuck Pat: Stop twice ---
server, flog = L.start({"FAKE_PAT_HANG": "1"})
try:
    page = Page()
    page.send({"t": "winlink_connect", "url": URL})
    check("stuck: armed", bool(page.wait(armed, 10)), True)
    time.sleep(0.5)
    page.send({"t": "winlink_stop"})
    time.sleep(0.5)
    page.send({"t": "winlink_stop"})
    t0 = time.time()
    e = page.wait(ended, 25)
    check("stuck, Stop twice: the session ends anyway", bool(e), True)
    check("  ...after the grace, not at once", e is not None and 8 <= time.time() - t0 <= 20, True)
    check("  ...saying Pat didn't return and the modem may be stuck",
          bool(e) and "did not return" in (e.get("reason") or "") and "modem may be stuck" in e["reason"], True)
    check("  ...a dirty disconnect was sent first",
          any(r["route"] == "disconnect" and r.get("dirty") == "true" for r in fake_log(flog)), True)
    check("  ...the lane is disarmed", lane_ptt_refused(), True)

    # --- stuck Pat: the page closes (abort) ---
    page.events.clear()
    other = Page()
    page.send({"t": "winlink_connect", "url": URL})
    check("stuck again: armed", bool(page.wait(armed, 10)), True)
    page.close()
    e = other.wait(ended, 25)
    check("stuck, page closed: the session ends anyway", bool(e) and "did not return" in (e.get("reason") or ""),
          True)
    st, body = other.http("GET", "/winlink/api/status")
    check("  ...the station shows no session", (st, json.loads(body).get("session")), (200, None))
    other.close()
finally:
    stop(server)

# --- ardopcf can't open its sound card ---
logs = tempfile.mkdtemp(prefix="ardoplogs-")
server, flog = L.start({"FAKE_PAT_NO_AUDIO": "1", "FAKE_PAT_ARDOP_LOG": logs, "FAKE_PAT_TX_S": "30"},
                       "--winlink-ardop-logs", logs)
try:
    page = Page()
    page.send({"t": "winlink_connect", "url": URL})
    check("no audio: armed", bool(page.wait(armed, 10)), True)
    t0 = time.time()
    e = page.wait(ended, 20)
    check("no audio: the session ends", bool(e), True)
    check("  ...within seconds, not the 30 s transmission", e is not None and time.time() - t0 < 10, True)
    check("  ...saying ARDOP is not on the air", bool(e) and "ARDOP is not on the air" in (e.get("reason") or ""),
          True)
    check("  ...naming ardopcf's error", bool(e) and "cannot open playback device" in e["reason"], True)
    check("  ...by a dirty disconnect",
          any(r["route"] == "disconnect" and r.get("dirty") == "true" for r in fake_log(flog)), True)
    check("  ...the lane is disarmed", lane_ptt_refused(), True)
    page.close()
finally:
    stop(server)

# --- control: a working sound card, same log folder watched ---
logs = tempfile.mkdtemp(prefix="ardoplogs-")
server, flog = L.start({"FAKE_PAT_ARDOP_LOG": logs, "FAKE_PAT_TX_S": "2"}, "--winlink-ardop-logs", logs)
try:
    page = Page()
    page.send({"t": "winlink_connect", "url": URL})
    e = page.wait(ended, 30)
    check("control: a working sound card ends ok", bool(e) and e.get("ok"), True)
    check("  ...no 'not on the air'", "not on the air" in ((e or {}).get("reason") or ""), False)
    page.close()
finally:
    stop(server)

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
