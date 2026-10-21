"""Station identification for Winlink over ARDOP (47 CFR 97.119(a)).

The rule: identify at the end of each communication and at least every 10
minutes during one. ardopcf sends the ID (an IDFrame, then CW ID) itself; Pat
keys the radio for it through the Winlink lane like any other frame. The lane
lets the radio key only while a session is armed from the page, so an ID that
ardopcf sends after the session was disarmed (its "10 minute ID", ~9 minutes
after an attempt that ended without one) is refused and never goes on air,
while ardopcf's log says it was sent (2026-10-15 09:06 and 09:41).

This module keeps the ledger that tells the two apart:
  - every lane PTT request (accepted = on air, refused = not), wall time
  - every ID ardopcf started and finished, read from ardopcf's own debug log
    (--winlink-ardop-log, its --logdir): "SendID <call> <reason>", the PTT
    lines, "Sending CW ID"
An ID went on air if the lane accepted its keying and refused none of it.
An ID is owed if the station transmitted after the end of the last ID that
went on air. Without the log the ledger is "not tracked": it never claims an
ID was sent (no data must not read as good data).
"""

import asyncio
import calendar
import glob
import logging
import os
import re
import time
from collections import deque

log = logging.getLogger("station_id")

SLOP_S = 0.5            # log line vs lane decision timing (same host clock)
NO_CW_S = 1.5           # after the IDFrame, a CW ID starts within this or there is none
STALE_ID_S = 30.0       # an ID that started but never ended stops counting as in progress
POLL_S = 0.25

_LINE = re.compile(r"^(\d\d):(\d\d):(\d\d)\.(\d{3}) \S (.*)$")
_SENDID = re.compile(r"^SendID\s+(\S+)\s+(.*?)\s*$")
_FILE_DATE = re.compile(r"_(\d{8})\.log$")


def line_wall(date, hh, mm, ss, ms):
    """ardopcf logs UTC time of day; its file name carries the UTC date."""
    y, mo, d = int(date[:4]), int(date[4:6]), int(date[6:])
    return calendar.timegm((y, mo, d, int(hh), int(mm), int(ss), 0, 0, 0)) + int(ms) / 1000


class ArdopLogParser:
    """Turns ardopcf debug-log lines into ID events:
    ("start", reason, wall) and ("end", None, wall)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.in_id = False
        self.frame_end = None     # wall time the IDFrame's PTT dropped
        self.cw = False           # CW ID started after the IDFrame

    def feed(self, line, date):
        m = _LINE.match(line.rstrip("\r\n"))
        if not m:
            return []
        t = line_wall(date, *m.groups()[:4])
        msg = m.group(5).strip()
        ev = []
        s = _SENDID.match(msg)
        if s:
            ev += self._finish()
            self.in_id = True
            ev.append(("start", s.group(2), t))
            return ev
        if not self.in_id:
            return ev
        if msg.startswith("Sending CW ID"):
            self.cw = True
        elif msg.endswith("PTT-FALSE"):
            if self.cw:
                ev.append(("end", None, t))
                self.reset()
            else:
                self.frame_end = t
        elif msg.endswith("PTT-TRUE") and self.frame_end is not None and not self.cw:
            ev += self._finish()   # something else keyed: that ID had no CW part
        return ev

    def tick(self, wall):
        """No CW ID followed the IDFrame in time: the ID ended with the frame."""
        if self.in_id and not self.cw and self.frame_end is not None and wall - self.frame_end > NO_CW_S:
            return self._finish()
        return []

    def _finish(self):
        ev = [("end", None, self.frame_end)] if self.in_id and self.frame_end is not None else []
        self.reset()
        return ev


class IdLedger:
    def __init__(self, tracked, on_change=None, clock=time.time):
        self.tracked = tracked
        self.on_change = on_change
        self.clock = clock
        self.ptts = deque(maxlen=1000)    # (wall, accepted)
        self.last_tx = None               # wall time of the last accepted PTT
        self.ids = deque(maxlen=20)       # {"reason", "start", "end", "on_air"}

    # --- inputs ---

    def ptt(self, accepted, wall=None):
        t = self.clock() if wall is None else wall
        self.ptts.append((t, accepted))
        if accepted:
            was = self.owed()
            self.last_tx = t
            if self.owed() != was:
                self._changed()

    def id_started(self, reason, wall):
        self.ids.append({"reason": reason, "start": wall, "end": None, "on_air": None})
        self._changed()

    def id_ended(self, wall):
        cur = self.ids[-1] if self.ids else None
        if cur is None or cur["end"] is not None:
            return None
        cur["end"] = wall
        acc = ref = 0
        for t, ok in self.ptts:
            if cur["start"] - SLOP_S <= t <= wall + SLOP_S:
                acc += ok
                ref += not ok
        cur["on_air"] = acc > 0 and ref == 0
        if cur["on_air"]:
            log.info("station ID on air (%s)", cur["reason"])
        else:
            log.warning("station ID (%s) did NOT go on air: the Winlink lane %s; "
                        "an ID is owed for the last transmission",
                        cur["reason"],
                        "refused its PTT (no session armed)" if ref else "never saw it key")
        self._changed()
        return cur

    # --- views ---

    def last(self):
        return self.ids[-1] if self.ids else None

    def last_on_air_end(self):
        for i in reversed(self.ids):
            if i["on_air"]:
                return i["end"]
        return None

    def owed(self):
        """True: transmitted since the last ID that went on air. None: not tracked."""
        if not self.tracked:
            return None
        if self.last_tx is None:
            return False
        end = self.last_on_air_end()
        return end is None or self.last_tx > end + SLOP_S

    def in_progress(self):
        cur = self.last()
        return bool(cur and cur["end"] is None and self.clock() - cur["start"] < STALE_ID_S)

    def last_ptt(self):
        return self.ptts[-1][0] if self.ptts else None

    def snapshot(self):
        cur = self.last()
        return {"tracked": self.tracked, "owed": self.owed(), "in_progress": self.in_progress(),
                "last": None if cur is None else {
                    "reason": cur["reason"], "start": cur["start"], "end": cur["end"],
                    "on_air": cur["on_air"]}}

    def _changed(self):
        if self.on_change:
            self.on_change(self.snapshot())


class ArdopLogTail:
    """Follows the newest ARDOPDebug*.log in ardopcf's --logdir (read only)
    and feeds the ledger. Starts at the end of the file: history is not replayed."""

    def __init__(self, logdir, ledger):
        self.logdir = logdir
        self.ledger = ledger
        self.parser = ArdopLogParser()
        self.path = None
        self.pos = 0
        self.part = ""
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self._run())

    def stop(self):
        if self.task:
            self.task.cancel()

    def _newest(self):
        files = glob.glob(os.path.join(self.logdir, "ARDOPDebug*_*.log"))
        return max(files, key=os.path.getmtime) if files else None

    def poll(self, first=False):
        path = self._newest()
        if path is None:
            return
        if path != self.path:
            self.path, self.part = path, ""
            self.pos = os.path.getsize(path) if first else 0
            self.parser.reset()
        m = _FILE_DATE.search(path)
        date = m.group(1) if m else time.strftime("%Y%m%d", time.gmtime())
        size = os.path.getsize(path)
        if size < self.pos:           # truncated / replaced
            self.pos, self.part = 0, ""
        if size > self.pos:
            with open(path, "rb") as f:
                f.seek(self.pos)
                data = f.read(size - self.pos)
            self.pos = size
            text = self.part + data.decode("utf-8", "replace")
            lines = text.split("\n")
            self.part = lines.pop()
            for line in lines:
                self._apply(self.parser.feed(line, date))
        self._apply(self.parser.tick(time.time()))

    def _apply(self, events):
        for kind, reason, t in events:
            if kind == "start":
                self.ledger.id_started(reason, t)
            else:
                self.ledger.id_ended(t)

    async def _run(self):
        first = True
        while True:
            try:
                self.poll(first)
                first = False
            except OSError as e:
                log.debug("ardopcf log: %s", e)
            await asyncio.sleep(POLL_S)
