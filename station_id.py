"""Station identification for Winlink over ARDOP and VARA HF (47 CFR 97.119(a)).

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

VARA HF (xara, plan winlink-vara): xara keys the radio itself through its
own lane and sends a CW ID at every close for a US call. Its --log
(--winlink-vara-log) gives only the moment the ID was queued ("tx cw"); the
VARA lane's own record of that burst (accepted T 1, how it was released)
says whether it went out whole. Each ID carries its source ("ardopcf",
"xara"), and an ID of one source is never completed by the other's lane.
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
BURST_KEEP_S = 120.0    # a lane's burst history: this long...
BURST_KEEP_N = 256      # ...and at most this many per lane
# xara v0.1.3: when the other station sends QRT, xara queues its CW ID with
# earliest = now (crates/xara/src/station/hf.rs drain_at, after on_ctrl ->
# close(blanks) in crates/xara-arq/src/hf/session.rs), logs "tx cw" as it
# queues it, and keys on the next audio block (scheduler.rs next -> key).
# The T 1 follows the line by one audio block plus the PTT thread's hop and
# the rigctl round trip: tens of milliseconds. 1 s is that with margin for
# a loaded machine; the blanks before the call (QRT_CWID_BLANKS, up to 12
# word spaces, ~3.6 s) are inside the keyed burst, not before it.
XARA_CW_LEAD_S = 1.0
NO_CW_S = 1.5           # after the IDFrame, a CW ID starts within this or there is none
STALE_ID_S = 30.0       # an ID that started but never ended stops counting as in progress
POLL_S = 0.25

_LINE = re.compile(r"^(\d\d):(\d\d):(\d\d)\.(\d{3}) \S (.*)$")
# xara's --log (crates/xara/src/log.rs + tnc.rs echo): "<UTC ISO, ms>Z <secs> <text>";
# a queued CW ID is the trace "tx cw <state>", a speed level the host line
# "< BITRATE (<level>)  <bps> bps TX|RX".
_XARA_LINE = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)\.(\d{3})Z (.*)$")
_XARA_CW = re.compile(r"^\s*-?[\d.]+\s+tx cw\b\s*(\S*)")
_XARA_RATE = re.compile(r"<\s*BITRATE\s*\((\d+)\)\s+(\d+)\s*bps\s+(TX|RX)\b")
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


# Which modem's log tells of which lane's IDs: the lane a modem keys through
# is named after its URL scheme; its log after the program.
SOURCE_OF = {"ardop": "ardopcf", "varahf": "xara"}
MODEM_OF = {v: k for k, v in SOURCE_OF.items()}


class IdLedger:
    """tracked: the lane modems whose ID log this server reads ({"ardop"},
    {"ardop", "varahf"}, ...); True means every modem, False none."""

    def __init__(self, tracked, on_change=None, clock=time.time):
        if tracked is True:
            tracked = set(SOURCE_OF)
        self.tracked_modems = set(tracked or ())
        self.on_change = on_change
        self.clock = clock
        self.ptts = deque(maxlen=1000)    # (wall, accepted, modem): every lane PTT request
        self.last_tx = None               # wall time of the last accepted PTT
        self.ids = deque(maxlen=20)       # {"reason", "start", "end", "on_air", "source"}
        # Per lane modem, each transmission: [accepted T 1 wall, release wall
        # or None (still keyed), release kind ("normal": the owner's own T 0;
        # "forced": a disconnect, the keepalive or time limit, a disarm, the
        # radio)]. Bounded: BURST_KEEP_S and BURST_KEEP_N.
        self.bursts = {}

    @property
    def tracked(self):
        return bool(self.tracked_modems)

    def tracked_for(self, modem):
        return modem in self.tracked_modems

    # --- inputs ---

    def ptt(self, accepted, wall=None, modem="ardop"):
        t = self.clock() if wall is None else wall
        self.ptts.append((t, accepted, modem))
        if accepted:
            b = self.bursts.setdefault(modem, deque(maxlen=BURST_KEEP_N))
            if not (b and b[-1][1] is None):     # a repeated T 1 while keyed is the same burst
                b.append([t, None, None])
            while b and b[0][1] is not None and b[0][1] < t - BURST_KEEP_S:
                b.popleft()
            was = self.owed()
            self.last_tx = t
            if self.owed() != was:
                self._changed()
        self._resolve()

    def ptt_released(self, wall, modem, kind):
        """A Winlink lane's transmission ended (Station.unkey, exactly once
        per burst). kind: "normal" (the owner's own T 0) or "forced"."""
        b = self.bursts.get(modem)
        if b and b[-1][1] is None:
            b[-1][1], b[-1][2] = wall, kind
        log.info("station ID: %s lane PTT released (%s)", modem, kind)
        self._resolve()

    def id_started(self, reason, wall, source="ardopcf"):
        self.ids.append({"reason": reason, "start": wall, "end": None, "on_air": None, "source": source})
        self._changed()
        self._resolve()

    def id_ended(self, wall, source="ardopcf"):
        """ardopcf's log says its ID ended: completes only ardopcf's open ID,
        judged by the ARDOP lane's PTT requests alone."""
        cur = self._open(source)
        if cur is None:
            return None
        cur["end"] = wall
        modem = MODEM_OF.get(source, "ardop")
        acc = ref = 0
        for t, ok, m in self.ptts:
            if m == modem and cur["start"] - SLOP_S <= t <= wall + SLOP_S:
                acc += ok
                ref += not ok
        self._finish(cur, wall, acc > 0 and ref == 0,
                     "refused its PTT (no session armed)" if ref else "never saw it key")
        return cur

    def tick(self, wall=None):
        """Time passes: a xara ID that never found its keying goes stale."""
        self._resolve(self.clock() if wall is None else wall)

    # --- xara: its log gives the start of a CW ID; the lane's bursts, the rest

    def _resolve(self, now=None):
        """Match each open xara CW ID to the VARA lane burst it rode (plan
        winlink-vara A3). The burst open at the "tx cw" line's time t (our
        own QRT: the CW is queued behind the QRT frame, inside its key); else
        the first burst starting within XARA_CW_LEAD_S after t (their QRT:
        a fresh key right after the line). Either may arrive first, the log
        line or the release. On air only if that burst ended with the
        owner's own T 0 and no refused T 1 on the lane fell inside it."""
        now = self.clock() if now is None else now
        modem = MODEM_OF["xara"]
        for cur in list(self.ids):
            if cur["source"] != "xara" or cur["end"] is not None:
                continue
            t = cur["start"]
            bursts = list(self.bursts.get(modem, ()))
            b = next((b for b in reversed(bursts)
                      if b[0] <= t + SLOP_S and (b[1] is None or b[1] >= t)), None)
            if b is None:
                b = next((b for b in bursts if t <= b[0] <= t + XARA_CW_LEAD_S), None)
            if b is None:
                refused = any(not ok and m == modem and t - SLOP_S <= w <= t + XARA_CW_LEAD_S
                              for w, ok, m in self.ptts)
                if refused:
                    self._finish(cur, t, False, "refused its PTT (no VARA session armed)")
                elif now - t > STALE_ID_S:
                    self._finish(cur, t, False, "never saw it key")
                continue
            if b[1] is None:
                continue                            # still keyed: in progress
            refused = any(not ok and m == modem and b[0] <= w <= b[1] for w, ok, m in self.ptts)
            if b[2] != "normal":
                why = "released it before it ended (a disconnect, a limit or a disarm)"
            elif refused:
                why = "refused a PTT request during it"
            else:
                why = None
            self._finish(cur, b[1], why is None, why)

    def _finish(self, cur, end, on_air, why):
        cur["end"], cur["on_air"] = end, on_air
        name = cur.get("source", "ardopcf")
        if on_air:
            log.info("station ID on air (%s: %s)", name, cur["reason"])
        else:
            log.warning("station ID (%s: %s) did NOT go on air: the Winlink lane %s; "
                        "an ID is owed for the last transmission", name, cur["reason"], why)
        self._changed()

    def _open(self, source):
        for i in reversed(self.ids):
            if i["source"] == source:
                return i if i["end"] is None else None
        return None

    # --- views ---

    def last(self, source=None):
        for i in reversed(self.ids):
            if source is None or i["source"] == source:
                return i
        return None

    def last_on_air_end(self):
        ends = [i["end"] for i in self.ids if i["on_air"]]
        return max(ends) if ends else None

    def owed(self):
        """True: transmitted since the last ID that went on air. None: not tracked."""
        if not self.tracked:
            return None
        if self.last_tx is None:
            return False
        end = self.last_on_air_end()
        return end is None or self.last_tx > end + SLOP_S

    def in_progress(self, source=None):
        """An ID (of that source, or any) started and not yet ended, and not stale."""
        now = self.clock()
        return any(i["end"] is None and now - i["start"] < STALE_ID_S
                   for i in self.ids if source is None or i["source"] == source)

    def last_ptt(self):
        return self.ptts[-1][0] if self.ptts else None

    def snapshot(self):
        cur = self.last()
        return {"tracked": self.tracked, "tracked_modems": sorted(self.tracked_modems),
                "owed": self.owed(), "in_progress": self.in_progress(),
                "last": None if cur is None else {
                    "reason": cur["reason"], "start": cur["start"], "end": cur["end"],
                    "on_air": cur["on_air"], "source": cur["source"]}}

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
        self.ino = None
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
        st = os.stat(path)
        if path != self.path or st.st_ino != self.ino:
            # A new file, or the same name on a new file (replaced): from its start.
            new_name = path != self.path
            self.path, self.ino, self.part = path, st.st_ino, ""
            self.pos = st.st_size if (first and new_name) else 0
            self.parser.reset()
        m = _FILE_DATE.search(path)
        date = m.group(1) if m else time.strftime("%Y%m%d", time.gmtime())
        size = st.st_size
        if size < self.pos:           # truncated
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


def xara_wall(y, mo, d, hh, mm, ss, ms):
    return calendar.timegm((int(y), int(mo), int(d), int(hh), int(mm), int(ss), 0, 0, 0)) + int(ms) / 1000


class VaraLogTail:
    """Follows xara's --log file (one append-only file, read only) and feeds
    the ledger: each "tx cw" line starts a xara ID, which the ledger matches
    to the VARA lane's burst (IdLedger._resolve). Also keeps the speed
    levels xara reports (BITRATE), for the session record. Starts at the end
    of the file: history is not replayed. A truncated file, or a new file
    under the name (log rotated, xara reinstalled), is read from its start."""

    def __init__(self, path, ledger):
        self.path = os.path.expanduser(path)
        self.ledger = ledger
        self.ino = None
        self.pos = 0
        self.part = ""
        self.task = None
        self.levels = deque(maxlen=2000)      # (wall, level, "TX"|"RX")

    def start(self):
        self.task = asyncio.create_task(self._run())

    def stop(self):
        if self.task:
            self.task.cancel()

    def max_level(self, since, until=None):
        """The highest speed level xara reported between since and until (None: none seen)."""
        lv = [lvl for t, lvl, _ in self.levels if t >= since - SLOP_S and (until is None or t <= until + SLOP_S)]
        return max(lv) if lv else None

    def poll(self, first=False):
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            self.ledger.tick()
            return
        if st.st_ino != self.ino:
            fresh = self.ino is None and first
            self.ino, self.part = st.st_ino, ""
            self.pos = st.st_size if fresh else 0
        if st.st_size < self.pos:             # truncated
            self.pos, self.part = 0, ""
        if st.st_size > self.pos:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read(st.st_size - self.pos)
            self.pos = st.st_size
            text = self.part + data.decode("utf-8", "replace")
            lines = text.split("\n")
            self.part = lines.pop()
            for line in lines:
                self.feed(line)
        self.ledger.tick()

    def feed(self, line):
        m = _XARA_LINE.match(line.rstrip("\r"))
        if not m:
            return
        t = xara_wall(*m.groups()[:7])
        rest = m.group(8)
        cw = _XARA_CW.match(rest)
        if cw:
            self.ledger.id_started(f"CW ID{f' ({cw.group(1)})' if cw.group(1) else ''}", t, "xara")
            return
        r = _XARA_RATE.search(rest)
        if r:
            self.levels.append((t, int(r.group(1)), r.group(3)))

    async def _run(self):
        first = True
        while True:
            try:
                self.poll(first)
                first = False
            except OSError as e:
                log.debug("xara log: %s", e)
            await asyncio.sleep(POLL_S)
