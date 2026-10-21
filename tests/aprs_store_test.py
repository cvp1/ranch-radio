"""aprs_store.py (APRS plan M2): the rules, on a scratch database.

  - schema create, reopen and refusing a newer schema
  - dups per port (RF after IS, IS after RF); wrapped dups across two iGates
  - shortest path kept, direct_last only when no digi has repeated it
  - a wrapped packet never updates the inner station's RF times
  - the IS-vs-RF position rules and newer-fix rules
  - object ownership: a kill from another owner is shown, not applied
  - retention writes tombstones; deltas carry them; a client older than the
    pruned tombstones reloads in full
  - backfill once, idempotent, across a rotation, with malformed lines and
    non-UI lines, never calling transmit
  - reload after reopen (heard list, our messages)
  - the worker under load: 10,000 frames through the queue, then queries

    python3 tests/aprs_store_test.py
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import aprs_store  # noqa: E402
from aprs_store import AprsStore, Db  # noqa: E402

fails = 0
TMP = tempfile.mkdtemp(prefix="aprs-store-")
T0 = 1792400000.0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


def fresh(name):
    p = os.path.join(TMP, name + ".db")
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(p + ext)
        except OSError:
            pass
    return Db(p)


def station(db, call):
    r = db.db.execute("SELECT * FROM stations WHERE call=?", (call,)).fetchone()
    return dict(r) if r else None


def npackets(db):
    return db.db.execute("SELECT COUNT(*) FROM packets").fetchone()[0]


POS = "KF7ABC-9>APRS,WIDE1-1:!4049.00N/10510.00W>hi"


def test_schema():
    db = fresh("schema")
    check("schema version set", db.db.execute("PRAGMA user_version").fetchone()[0], aprs_store.SCHEMA_VERSION)
    check("WAL mode", db.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
    db.ingest(T0, "rf", POS)
    db.close()
    db = Db(db.path)
    check("reopen keeps the data", npackets(db), 1)
    db.db.execute(f"PRAGMA user_version={aprs_store.SCHEMA_VERSION + 1}")
    db.close()
    try:
        Db(db.path)
        check("a newer schema is refused", False, True)
    except RuntimeError:
        check("a newer schema is refused", True, True)


def test_dups():
    db = fresh("dups")
    db.ingest(T0, "rf", POS)
    db.ingest(T0 + 5, "rf", "KF7ABC-9>APRS,HELIO*,WIDE1:!4049.00N/10510.00W>hi")
    check("same packet on RF within 30 s: one row", npackets(db), 1)
    check("  counted as a dup", db.db.execute("SELECT dups FROM packets").fetchone()[0], 1)
    check("  station count is packets, not copies", station(db, "KF7ABC-9")["count"], 1)
    db.ingest(T0 + 40, "rf", POS)
    check("again after 30 s: a new row", npackets(db), 2)

    db = fresh("dups2")
    db.ingest(T0, "is", POS)
    db.ingest(T0 + 1, "rf", POS)
    check("RF after IS: an IS copy never suppresses RF", npackets(db), 2)
    db.ingest(T0 + 2, "is", POS)
    check("IS after RF and IS: a dup on IS only", npackets(db), 2)

    db = fresh("wrapped")
    inner = "}K4PC-9>APWW11,TCPIP,W9EN-10*::K7XYG-9  :hello{12"
    db.ingest(T0, "rf", "W9EN-10>APDW18,HELIO*,WIDE2:" + inner)
    db.ingest(T0 + 3, "rf", "K7OTHER-10>APDW18,WIDE2-1:" + inner.replace("W9EN-10", "K7OTHER-10"))
    check("the same IS packet via two iGates: one row", npackets(db), 1)
    check("  both iGates heard on RF", (bool(station(db, "W9EN-10")["rf_last"]),
                                       bool(station(db, "K7OTHER-10")["rf_last"])), (True, True))
    k4 = station(db, "K4PC-9")
    check("  the inner station: IS time, no RF times", (bool(k4["is_last"]), k4["rf_last"], k4["direct_last"]),
          (True, None, None))
    check("  one message stored, port is", [(m["src"], m["dst"], m["port"]) for m in db.messages()],
          [("K4PC-9", "K7XYG-9", "is")])


def test_paths():
    db = fresh("paths")
    db.ingest(T0, "rf", "KF7ABC-9>APRS,HELIO*,WIDE2-1:!4049.00N/10510.00W>hi")
    s = station(db, "KF7ABC-9")
    check("digipeated: rf_last set, direct_last not", (s["rf_last"], s["direct_last"]), (T0, None))
    db.ingest(T0 + 2, "rf", POS)
    check("then heard direct (a dup): direct_last set", station(db, "KF7ABC-9")["direct_last"], T0 + 2)
    check("  shortest path kept on the packet", db.db.execute("SELECT path FROM packets").fetchone()[0], "WIDE1-1")
    db.ingest(T0 + 100, "rf", "KF7ABC-9>APRS,HELIO*,WIDE2:>status")
    s = station(db, "KF7ABC-9")
    check("  a later digipeated packet keeps the direct path", s["rf_path"], "WIDE1-1")
    check("  and the station isn't direct now", db.heard()[0]["direct"], False)
    check("status text kept", s["status"], "status")


def test_positions():
    db = fresh("pos")
    rf = "N0ABC>APRS:!4049.00N/10510.00W-rf"
    db.ingest(T0, "rf", rf)
    db.ingest(T0 + 60, "is", "N0ABC>APRS,TCPIP*:!4050.00N/10510.00W-is")
    check("untimestamped IS fix within 10 min of RF: ignored", station(db, "N0ABC")["lat"], 40.816667)
    db.ingest(T0 + 700, "is", "N0ABC>APRS,TCPIP*:!4051.00N/10510.00W-is")
    s = station(db, "N0ABC")
    check("  after 10 min: taken", (s["lat"], s["pos_port"]), (40.85, "is"))
    db.ingest(T0 + 800, "rf", "N0ABC>APRS:!4052.00N/10510.00W-rf")
    check("a newer RF fix replaces it", station(db, "N0ABC")["lat"], 40.866667)
    # Timestamped: an older report arriving later doesn't move the station.
    t = time.gmtime(T0 + 800)
    old = f"{t.tm_mday:02d}{t.tm_hour:02d}{max(t.tm_min - 5, 0):02d}z"
    db.ingest(T0 + 900, "is", f"N0ABC>APRS,TCPIP*:/{old}4053.00N/10510.00W-old")
    check("an IS report timestamped before the RF fix: ignored", station(db, "N0ABC")["lat"], 40.866667)
    db.ingest(T0 + 950, "rf", "N0ABC>APRS:!4052.00N/10510.00W-rf")
    track = db.station("N0ABC")["track"]
    check("track: one point per move, none for a repeat", [p["lat"] for p in track], [40.816667, 40.85, 40.866667])
    db.ingest(T0 + 960, "rf", "N0AMB>APRS:!3320.  N/11010.  W-amb")
    s = station(db, "N0AMB")
    check("ambiguity kept with its box", (s["ambiguity"], s["box_deg"] > 0), (2, True))


def test_objects():
    db = fresh("obj")
    db.ingest(T0, "rf", "K7OWN>APRS:;REPEATER *111111z4049.00N/10510.00Wr146.52")
    db.ingest(T0 + 1, "rf", "K7EVIL>APRS:;REPEATER _111111z4049.00N/10510.00Wr")
    rows = {(o["name"], o["owner"]): o for o in db.objects(0)["rows"]}
    check("a kill from another owner: the owner's object stays live",
          rows[("REPEATER", "K7OWN")]["live"], 1)
    check("  and shows who tried", rows[("REPEATER", "K7OWN")]["killed_by"], "K7EVIL")
    rev = db.rev()
    db.ingest(T0 + 2, "rf", "K7OWN>APRS:;REPEATER _111111z4049.00N/10510.00Wr")
    d = db.objects(rev)
    check("the owner's kill applies, and comes as a delta",
          [(o["name"], o["owner"], o["live"]) for o in d["rows"]], [("REPEATER", "K7OWN", 0)])
    check("an object doesn't move its owner", station(db, "K7OWN")["lat"], None)


def test_retention():
    db = fresh("ret")
    db.ingest(T0, "rf", POS)
    db.ingest(T0 - 10, "rf", "K7OWN>APRS:;GONE     *111111z4049.00N/10510.00Wr")
    db.ingest(T0, "rf", "K7OWN>APRS:;GONE     _111111z4049.00N/10510.00Wr")
    db.ingest(T0 + 91 * 86400, "rf", "N0NEW>APRS:>still here")
    rev = db.rev()
    r = db.retain(now=T0 + 91 * 86400)
    check("retention removes old packets, stations, killed objects",
          (r["packets"] >= 1, r["stations"], r["objects"]), (True, 2, 1))
    d = db.stations(rev)
    check("tombstones come as deltas", sorted(d["gone"]), ["K7OWN", "KF7ABC-9"])
    check("  object tombstone", db.objects(rev)["gone"], [["GONE", "K7OWN"]])
    check("  a full load has no tombstones", db.stations(0)["gone"], [])
    db.set_meta("tomb_floor", db.rev())
    check("a client older than the pruned tombstones reloads in full", db.stations(rev)["full"], True)
    db = fresh("ret2")
    for i in range(aprs_store.TRACK_MAX + 20):
        db.ingest(T0 + i * 40, "rf", f"N0MOV>APRS:!31{i // 60:02d}.{i % 60:02d}N/10510.00W>")
    db.retain(now=T0 + 600 * 40)
    check("track capped per station", len(db.station("N0MOV")["track"]), aprs_store.TRACK_MAX)


def write_log(path, lines):
    with open(path, "w") as f:
        for l in lines:
            f.write(l if isinstance(l, str) else json.dumps(l))
            f.write("\n")


def test_backfill():
    logdir = os.path.join(TMP, "logs")
    os.makedirs(logdir, exist_ok=True)
    write_log(os.path.join(logdir, "rx.jsonl.1"), [
        {"t": T0, "dir": "rx", "text": POS},
        "not json",
        {"t": T0 + 1, "dir": "rx"},
    ])
    write_log(os.path.join(logdir, "rx.jsonl"), [
        {"t": T0 + 100, "dir": "rx", "text": "N0ABC>APRS:>after the rotation"},
        {"t": T0 + 101, "dir": "rx", "text": "N9ABC>K7RMS-10,HELIO* <SABM C P>"},
    ])
    called = []
    db = fresh("backfill")
    db.transmit = lambda *a: called.append(a)          # nothing in the store may call this
    r = db.backfill(logdir)
    check("backfill: both files, the rotated one first",
          [f["name"] for f in r["files"]], ["rx.jsonl.1", "rx.jsonl"])
    check("  lines, ingested, malformed", (r["lines"], r["ingested"], r["malformed"]), (5, 3, 2))
    check("  packets stored (the SABM is a station, not a packet)", npackets(db), 2)
    check("  the connected-mode caller is heard", bool(station(db, "N9ABC")), True)
    check("  again: skipped, nothing added", (db.backfill(logdir).get("skipped"), npackets(db)), ("already done", 2))
    check("  never transmitted", called, [])
    check("no logs: an empty backfill", fresh("nolog").backfill(os.path.join(TMP, "none"))["lines"], 0)


def test_reload():
    db = fresh("reload")
    db.ingest(T0, "rf", "KF7XYZ>APRS::N9ABC-7  :are you there{5")
    db.ingest(T0 + 1, "rf", "KF7XYZ>APRS::N9ABC-7  :are you there{5")   # a retry: one row
    db.ingest(T0 + 2, "rf", "KF7XYZ>APRS::W1AW     :not for us")
    db.ingest(T0 + 3, "rf", "N9ABC-7>APRS::KF7XYZ   :ack5")
    path = db.path
    db.close()
    db = Db(path)
    check("heard list reloads", [h["station"] for h in db.heard()], ["N9ABC-7", "KF7XYZ"])
    check("our messages reload, with the msgno", [(m["from"], m["text"]) for m in db.messages_to({"N9ABC"})],
          [("KF7XYZ", "are you there{5")])
    check("an ack marks the message acked", [m["state"] for m in db.messages("KF7XYZ") if m["msgno"] == "5"],
          ["acked"])


def test_worker_under_load():
    path = os.path.join(TMP, "load.db")
    store = AprsStore(path).start()
    n = 10000

    async def run():
        t = time.monotonic()
        for i in range(n):
            store.ingest(T0 + i, "rf", f"N{i % 300:04d}>APRS,WIDE1-1:!31{(i // 300) % 60:02d}.00N/10510.00W>{i}")
            if i % 1000 == 0:
                await asyncio.sleep(0)
        enq = time.monotonic() - t
        s = await store.call("summary")
        st = await store.call("stations", 0)
        one = await store.call("station", "N0007")
        bad = None
        try:
            await store.call("no_such_method")
        except AttributeError as e:
            bad = type(e).__name__
        return enq, s, st, one, bad

    enq, s, st, one, bad = asyncio.run(run())
    check("10,000 frames enqueued without blocking (< 1 s)", enq < 1.0, True)
    check("  all stored, none dropped", (s["ingested"], store.dropped), (n, 0))
    check("  300 stations", len(st["rows"]), 300)
    check("  a station's detail", (one["call"], one["count"]), ("N0007", 34))
    check("  a failing call raises in the caller, the worker lives on", bad, "AttributeError")
    store.stop()
    check("  worker stopped", store._thread.is_alive(), False)


def test_queue_full_drops():
    store = AprsStore(os.path.join(TMP, "full.db"))
    store.q = aprs_store.queue.Queue(maxsize=2)       # not started: nothing drains it
    for i in range(5):
        store.ingest(T0, "rf", POS)
    check("a full queue drops and counts, never blocks", store.dropped, 3)


sys.path.insert(0, os.path.join(ROOT, "tests"))


def test_corpus():
    import corpus
    src = corpus.sample()
    if src is None:
        print("skip  corpus: no receive sample (tests/corpus.py)")
        return
    logdir = os.path.join(TMP, "corpus")
    os.makedirs(logdir, exist_ok=True)
    shutil.copy(src, os.path.join(logdir, "rx.jsonl"))
    db = fresh("corpus")
    r = db.backfill(logdir)
    s = db.summary()
    check("the heard sample backfills with nothing malformed", r["malformed"], 0)
    check("  every line ingested", s["ingested"], r["lines"])
    check("  stations and positions found", (s["stations"] > 20, db.db.execute(
        "SELECT COUNT(*) FROM stations WHERE lat IS NOT NULL").fetchone()[0] > 10), (True, True))
    sqlite3.connect(db.path).execute("PRAGMA integrity_check")


def test_review_round2():
    """Code review 2026-10-19 (Codex, round 2)."""
    db = fresh("wrapped_then_rf")
    inner = "}N0ABC>APRS,TCPIP,K7IG*:!4049.00N/10510.00W-from IS"
    db.ingest(T0, "rf", "K7IG>APDW18,WIDE2-1:" + inner)
    db.ingest(T0 + 2, "rf", "N0ABC>APRS:!4049.00N/10510.00W-from IS")
    s = station(db, "N0ABC")
    check("a wrapped IS copy doesn't suppress the direct RF copy", (npackets(db), s["pos_port"], s["direct_last"]),
          (2, "rf", T0 + 2))

    db = fresh("resurrect")
    db.ingest(T0, "rf", "K7OWN>APRS:;BACK     *111111z4049.00N/10510.00Wr")
    db.ingest(T0 + 1, "rf", "K7OWN>APRS:;BACK     _111111z4049.00N/10510.00Wr")
    rev = db.rev()
    db.retain(now=T0 + 2 * 86400)
    db.ingest(T0 + 2 * 86400 + 5, "rf", "K7OWN>APRS:;BACK     *111111z4049.00N/10510.00Wr")
    d = db.objects(rev)
    check("an object back after its tombstone: a row, no tombstone",
          ([o["name"] for o in d["rows"]], d["gone"]), (["BACK"], []))

    store = AprsStore(os.path.join(TMP, "cancel.db")).start()
    orig = Db.summary
    Db.summary = lambda self: (time.sleep(0.3), orig(self))[1]
    try:
        slow = store.submit("summary")
        queued = store.submit("summary")
        check("a call cancelled while queued is cancelled", queued.cancel(), True)
        slow.result(5)
        check("  the worker skips it and lives on", store.submit("rev").result(5) >= 0, True)
        running = store.submit("summary")
        time.sleep(0.1)
        check("  a running call can't be cancelled", running.cancel(), False)
        check("  and still answers", isinstance(running.result(5), dict), True)
    finally:
        Db.summary = orig
        store.stop()


for test in [test_schema, test_dups, test_paths, test_positions, test_objects, test_retention, test_backfill,
             test_reload, test_worker_under_load, test_queue_full_drops, test_corpus, test_review_round2]:
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
