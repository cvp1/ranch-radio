#!/usr/bin/env python3
"""igate_watch.py and the store's igate_days tally: is the drop in iGates that
carry APRS-IS traffic onto the air here noticed, once, and only when the radio
was hearing?

    python3 tests/igate_watch_test.py
"""
import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.pop("CC_OBS_TOKENS_FILE", None)

import aprs_store  # noqa: E402
import igate_watch  # noqa: E402
import tnc_hub  # noqa: E402
from aprs_store import Db  # noqa: E402

fails = 0
TMP = tempfile.mkdtemp(prefix="igate-watch-")


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:66} got {got!r}" + ("" if ok else f" want {want!r}"))


def fresh(name, clock=time.time):
    p = os.path.join(TMP, name + ".db")
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(p + ext)
        except OSError:
            pass
    return Db(p, clock=clock)


def noon(day):
    return igate_watch.day_start(day) + 12 * 3600


def wrapped(igate, inner_src="WXBOT", to="N9ABC", text="hello", n=1):
    """An APRS-IS message an iGate sent onto the air, as heard on RF."""
    return (f"{igate}>APRS,WIDE2-1:}}{inner_src}>APRS,TCPIP,{igate}*::{to.ljust(9)}:{text}{{{n}")


POS = "KF7ABC-9>APRS,WIDE1-1:!4049.00N/10510.00W>hi"


def fill(db, day, igates, rf=250):
    """A day with rf frames heard, and each iGate delivering its count of packets."""
    t = noon(day)
    k = 0
    for call, n in igates.items():
        for i in range(n):
            k += 1
            db.ingest(t + k, "rf", wrapped(call, text=f"m{k}", n=k))
    for i in range(max(0, rf - k)):
        db.ingest(t + 1000 + i * 31, "rf", f"KF7ABC-{i % 15}>APRS,WIDE1-1:>status {i}")


# ------------------------------------------------------------------ verdict

def test_verdict():
    day = "2026-11-01"
    base = {"day": day, "since": 0}
    v = lambda igates, rf=500, **kw: igate_watch.verdict({**base, "igates": igates, "rf_frames": rf, **kw})[0]
    check("two iGates with 3+ each: ok", v({"N9IGA": 5, "N9IGC": 3}), "ok")
    check("one iGate: low", v({"N9IGA": 40}), "low")
    check("none: low", v({}), "low")
    check("a second iGate with 2 (under 3) doesn't count: low", v({"N9IGA": 9, "N9IGC": 2}), "low")
    check("too few RF frames: unknown, even with no iGates", v({}, rf=150), "unknown")
    check("the tally started mid-day: unknown",
          v({}, since=igate_watch.day_start(day) + 3600), "unknown")
    check("the tally started before the day: judged", v({}, since=igate_watch.day_start(day) - 1), "low")


# ------------------------------------------------------------------ the tally

def test_tally():
    db = fresh("tally")
    day = igate_watch.day_of(time.time() - 86400)
    t = noon(day)
    line = wrapped("N9IGA")
    db.ingest(t, "rf", line)
    db.ingest(t + 2, "rf", line.replace("N9IGA", "N9IGB-2"))     # the same packet, a second iGate
    db.ingest(t + 3, "rf", line)                                   # N9IGA's copy again, via a digi
    rows = db.db.execute("SELECT COUNT(*) FROM packets").fetchone()[0]
    check("positive control: the packets table keeps one row for the three copies", rows, 1)
    got = db.igate_day(day)
    check("every copy counts: both iGates are credited", got["igates"], {"N9IGA": 2, "N9IGB-2": 1})
    check("RF frames: all three", got["rf_frames"], 3)
    db.ingest(t + 10, "is", wrapped("N9IGC"))
    check("an APRS-IS copy is not an iGate on the air", "N9IGC" in db.igate_day(day)["igates"], False)
    db.ingest(t + 11, "rf", "N9IGE>APRS,WIDE2-1:}KF7ABC>APRS,WIDE1-1::N9ABC    :rf only{3")
    check("a third-party packet without TCPIP is not an APRS-IS delivery",
          "N9IGE" in db.igate_day(day)["igates"], False)
    db.ingest(t + 12, "rf", POS)
    check("a plain packet counts as an RF frame only", db.igate_day(day)["rf_frames"], 5)
    other = igate_watch.day_of(t + 86400)
    db.ingest(igate_watch.day_start(other) + 5, "rf", wrapped("N9IGD-10"))
    check("the next local day is its own row", db.igate_day(other)["igates"], {"N9IGD-10": 1})
    check("and leaves this one alone", db.igate_day(day)["igates"], {"N9IGA": 2, "N9IGB-2": 1})


def test_migration_and_retention():
    p = os.path.join(TMP, "v2.db")
    c = sqlite3.connect(p)
    c.executescript(aprs_store.SCHEMA + aprs_store.PROBES_SCHEMA)
    c.execute("PRAGMA user_version=2")
    c.execute("INSERT INTO meta VALUES ('rev', '0')")
    c.commit()
    c.close()
    now = 1794100000.0
    db = Db(p, clock=lambda: now)
    check("a version 2 file migrates to 3", db.db.execute("PRAGMA user_version").fetchone()[0], 3)
    check("  and starts the tally now (no partial day judged)", db.igate_day("2026-11-01")["since"], now)
    db.close()
    new = fresh("v0")
    check("a new file counts from the start (the backfill replays the logs)",
          new.igate_day("2026-11-01")["since"], 0.0)
    old_day = igate_watch.day_of(time.time() - 40 * 86400)
    new.ingest(noon(old_day), "rf", wrapped("N9IGA"))
    keep_day = igate_watch.day_of(time.time() - 2 * 86400)
    new.ingest(noon(keep_day), "rf", wrapped("N9IGA"))
    r = new.retain()
    check("retention drops tally days older than 30 days", r.get("igate_days", 0) >= 1, True)
    check("  and keeps recent ones", new.igate_day(keep_day)["igates"], {"N9IGA": 1})
    check("  the old one is gone", new.igate_day(old_day)["igates"], {})


# ------------------------------------------------------------------ the watch

class Harness:
    """A Watch over a real Db, with a clock and a recorded notifier."""

    def __init__(self, db, clock):
        self.db, self.clock, self.sent = db, clock, []

    async def call(self, name, *args):
        return getattr(self.db, name)(*args)

    async def notify(self, title, text):
        self.sent.append(title)

    def watch(self):
        return igate_watch.Watch(self.call, self.notify, clock=lambda: self.clock[0])


def run(coro):
    return asyncio.run(coro)


def test_watch_edges():
    days = ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"]
    clock = [noon(days[0])]
    db = fresh("edges", clock=lambda: 0)
    fill(db, days[0], {"N9IGA": 5, "N9IGB-2": 4})            # ok
    fill(db, days[1], {"N9IGA": 6})                            # low
    fill(db, days[2], {}, rf=50)                               # unknown: the radio wasn't hearing
    fill(db, days[3], {"N9IGA": 3, "N9IGC": 3})                # ok again
    h = Harness(db, clock)
    w = h.watch()

    clock[0] = noon(days[1])
    j = run(w.check())
    check("day 1 ok, first day judged: silent", (j["verdict"], h.sent), ("ok", []))
    check("the same day isn't judged twice", run(w.check()), None)

    clock[0] = noon(days[2])
    j = run(w.check())
    check("day 2 low: one notification", (j["verdict"], h.sent), ("low", ["APRS: fewer iGates carrying messages here"]))
    check("  naming the one iGate left", j["delivering"], ["N9IGA"])
    run(w.check())
    check("  checking again sends nothing more", len(h.sent), 1)
    w2 = h.watch()                                             # a hub restart: a new Watch, the same store
    check("  a restart doesn't repeat it", (run(w2.check()), len(h.sent)), (None, 1))

    clock[0] = noon(days[3])
    j = run(w2.check())
    check("day 3 unknown (50 frames): silent, state stays low", (j["verdict"], j["state"], len(h.sent)),
          ("unknown", "low", 1))

    clock[0] = noon(days[4])
    j = run(w2.check())
    check("day 4 ok: the recovery is told once", (j["verdict"], h.sent[-1], len(h.sent)),
          ("ok", "APRS: iGates carrying messages again", 2))


def test_watch_first_day_low():
    days = ["2026-09-10", "2026-09-11"]
    clock = [noon(days[1])]
    db = fresh("firstlow", clock=lambda: 0)
    fill(db, days[0], {"N9IGA": 9})
    h = Harness(db, clock)
    j = run(h.watch().check())
    check("a first day judged low notifies (no earlier ok needed)", (j["verdict"], len(h.sent)), ("low", 1))


def test_hub_flags():
    a = tnc_hub.parse_args(["--tnc", "tcp:127.0.0.1:1", "--mycall", "N9ABC"])
    check("the hub watches by default, 2 iGates", a.igate_watch_min, 2)
    a = tnc_hub.parse_args(["--tnc", "tcp:127.0.0.1:1", "--mycall", "N9ABC", "--igate-watch-min", "0"])
    check("--igate-watch-min 0 turns it off", a.igate_watch_min, 0)


for test in [test_verdict, test_tally, test_migration_and_retention, test_watch_edges, test_watch_first_day_low,
             test_hub_flags]:
    print(f"--- {test.__name__}")
    try:
        test()
    except Exception as e:     # noqa: BLE001
        fails += 1
        import traceback
        traceback.print_exc()
        print(f"FAIL  {test.__name__} raised {type(e).__name__}: {e}")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
