"""The logbook sync's ledger: every QSO once, and where each destination
stands with it (PLAN v3 §5). SQLite, one file, shared by the station's
logsync and `ranch-radio logsync` (WAL, short transactions).

Per destination a QSO is in one state:

    held_backfill  imported from the logbook on first start; no sender reads it,
                   only `ranch-radio logsync backfill --apply HASH` releases it
    pending        waiting for its sender
    attempting     payload committed, the send is in flight
    sent           Wavelog, QRZ: the destination has it
    submitted      LoTW: TQSL signed and uploaded it (exit 0)
    accepted       LoTW: it is in LoTW's own list (qso_qsl=no)
    confirmed      LoTW or QRZ: the other station confirmed it
    unknown        the result was lost (a restart mid-send, TQSL 8/9): checked
                   against the destination before anything is sent again
    held           stopped, with a reason; an alert was raised

Nothing here talks to a network or holds a credential: the ledger stores
ADIF records and reasons. Reasons are written by logsync.py, which strips
anything that could be a credential first.
"""

import calendar
import hashlib
import json
import os
import sqlite3
import time

import logsync_adif as adif
from logsync_config import DESTS

STATES = ("held_backfill", "pending", "attempting", "sent", "submitted", "accepted", "confirmed",
          "unknown", "held")
DONE = ("sent", "submitted", "accepted", "confirmed")
NEAR_S = 120                      # a near miss: same call/band/mode this close, a different second

SCHEMA = """
CREATE TABLE IF NOT EXISTS qso(
    id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, own TEXT, call TEXT, band TEXT,
    mode TEXT, submode TEXT, ts TEXT, adif TEXT NOT NULL, source TEXT, created REAL,
    near INTEGER DEFAULT 0, backfill INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS qso_call ON qso(call, band, mode, submode);
CREATE TABLE IF NOT EXISTS revision(
    id INTEGER PRIMARY KEY, qso_id INTEGER NOT NULL, adif TEXT NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS dest(
    qso_id INTEGER NOT NULL, dest TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, detail TEXT,
    payload TEXT, misses INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0, updated REAL,
    correction INTEGER DEFAULT 0, edited INTEGER DEFAULT 0, PRIMARY KEY(qso_id, dest));
CREATE INDEX IF NOT EXISTS dest_state ON dest(dest, state);
CREATE TABLE IF NOT EXISTS cursor(name TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS alert(id INTEGER PRIMARY KEY, dest TEXT, cls TEXT, msg TEXT, created REAL);
"""


def _epoch(ts):
    return calendar.timegm(time.strptime(ts, "%Y%m%d%H%M%S")) if ts else 0


class Ledger:
    def __init__(self, path, dests=DESTS, clock=time.time):
        self.path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.dests = tuple(dests)
        self.clock = clock
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def close(self):
        self.db.close()

    # ---------------------------------------------------------------- QSOs

    def find(self, q):
        """The ledger row for an identity, matched exactly; a remote record
        whose TIME_ON has no seconds matches the one row in that minute (or
        none, if two)."""
        if q["secs"]:
            r = self.db.execute("SELECT * FROM qso WHERE key=?", (adif.key(q),)).fetchone()
            if r is not None or q["own"]:
                return r
        rows = self.db.execute(
            "SELECT * FROM qso WHERE call=? AND band=? AND mode=? AND submode=? AND substr(ts,1,12)=?",
            (q["call"], q["band"], q["mode"], q["submode"], q["ts"][:12])).fetchall()
        if q["own"]:
            rows = [r for r in rows if r["own"] == q["own"]]
        if q["secs"]:
            rows = [r for r in rows if r["ts"] == q["ts"]]
        return rows[0] if len(rows) == 1 else None

    def add(self, fields, source, states, own=None):
        """Record a QSO. states: {dest: initial state} for the ledger's
        destinations. Returns (qso_id, "new" | "same" | "revision")."""
        q = adif.qso(fields, own)
        rec = adif.record(fields)
        k = adif.key(q)
        now = self.clock()
        with self._tx():
            row = self.db.execute("SELECT * FROM qso WHERE key=?", (k,)).fetchone()
            if row is not None:
                old = adif.parse(self.latest_adif(row["id"]), strict=False)[1][0]
                if adif.content(old) == adif.content(fields):
                    return row["id"], "same"
                self.db.execute("INSERT INTO revision(qso_id, adif, created) VALUES(?,?,?)", (row["id"], rec, now))
                # Not yet sent anywhere: the new fields simply replace the old.
                # Sent somewhere: that destination is marked edited; nothing goes
                # out until the operator sends the correction (PLAN §5).
                # (Senders always send latest_adif(), so a pending destination
                # gets the new fields.)
                self.db.execute("UPDATE dest SET edited=1 WHERE qso_id=? AND state NOT IN "
                                "('pending', 'held_backfill')", (row["id"],))
                return row["id"], "revision"
            cur = self.db.execute(
                "INSERT INTO qso(key, own, call, band, mode, submode, ts, adif, source, created, backfill) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (k, q["own"], q["call"], q["band"], q["mode"], q["submode"], q["ts"], rec, source, now,
                 1 if source == "backfill" else 0))
            qid = cur.lastrowid
            for d in self.dests:
                self.db.execute("INSERT INTO dest(qso_id, dest, state, updated) VALUES(?,?,?,?)",
                                (qid, d, states.get(d, "pending"), now))
            self._flag_near(qid, q)
        return qid, "new"

    def _flag_near(self, qid, q):
        t = _epoch(q["ts"])
        for r in self.db.execute("SELECT id, ts FROM qso WHERE own=? AND call=? AND band=? AND mode=? AND submode=? "
                                 "AND id<>?", (q["own"], q["call"], q["band"], q["mode"], q["submode"], qid)):
            if abs(_epoch(r["ts"]) - t) <= NEAR_S:
                self.db.execute("UPDATE qso SET near=1 WHERE id IN (?,?)", (qid, r["id"]))

    def siblings(self, qid, window_s):
        """Other QSOs with the same own call, call, band and mode within window_s."""
        q = self.qso(qid)
        t = _epoch(q["ts"])
        return [r["id"] for r in self.db.execute(
            "SELECT id, ts FROM qso WHERE own=? AND call=? AND band=? AND mode=? AND id<>?",
            (q["own"], q["call"], q["band"], q["mode"], qid)) if abs(_epoch(r["ts"]) - t) <= window_s]

    def qso(self, qid):
        return self.db.execute("SELECT * FROM qso WHERE id=?", (qid,)).fetchone()

    def latest_adif(self, qid):
        r = self.db.execute("SELECT adif FROM revision WHERE qso_id=? ORDER BY id DESC LIMIT 1", (qid,)).fetchone()
        return r["adif"] if r else self.qso(qid)["adif"]

    # ---------------------------------------------------------------- states

    def state(self, qid, dest):
        r = self.db.execute("SELECT state FROM dest WHERE qso_id=? AND dest=?", (qid, dest)).fetchone()
        return r["state"] if r else None

    def row(self, qid, dest):
        return self.db.execute("SELECT * FROM dest WHERE qso_id=? AND dest=?", (qid, dest)).fetchone()

    def set(self, qids, dest, state, reason=None, detail=None, only_from=None):
        """Move qids at dest to state. only_from: only rows now in one of these."""
        qids = [qids] if isinstance(qids, int) else list(qids)
        now = self.clock()
        with self._tx():
            n = 0
            for qid in qids:
                sql = "UPDATE dest SET state=?, reason=?, detail=?, updated=? WHERE qso_id=? AND dest=?"
                args = [state, reason, detail, now, qid, dest]
                if only_from:
                    sql += f" AND state IN ({','.join('?' * len(only_from))})"
                    args += list(only_from)
                n += self.db.execute(sql, args).rowcount
        return n

    def attempting(self, qids, dest, payload):
        """Commit the payload and the attempting mark before a send (PLAN §5)."""
        now = self.clock()
        with self._tx():
            for qid in qids:
                self.db.execute("UPDATE dest SET state='attempting', payload=?, attempts=attempts+1, updated=? "
                                "WHERE qso_id=? AND dest=?", (payload, now, qid, dest))

    def recover(self):
        """At start: a send whose result was lost is unknown, never retried
        blind. held_backfill is not touched, here or anywhere but release()."""
        with self._tx():
            return self.db.execute("UPDATE dest SET state='unknown', reason='restart' "
                                   "WHERE state='attempting'").rowcount

    def pending(self, dest, limit=1, backfill=None):
        sql = ("SELECT q.*, d.correction, d.edited FROM dest d JOIN qso q ON q.id=d.qso_id "
               "WHERE d.dest=? AND d.state='pending'")
        args = [dest]
        if backfill is not None:
            sql += " AND q.backfill=?"
            args.append(1 if backfill else 0)
        return self.db.execute(sql + " ORDER BY q.ts, q.id LIMIT ?", (*args, limit)).fetchall()

    def in_state(self, dest, *states):
        return self.db.execute(
            f"SELECT q.*, d.state, d.reason, d.misses, d.detail, d.updated FROM dest d JOIN qso q ON q.id=d.qso_id "
            f"WHERE d.dest=? AND d.state IN ({','.join('?' * len(states))}) ORDER BY q.ts, q.id",
            (dest, *states)).fetchall()

    def miss(self, qid, dest):
        with self._tx():
            self.db.execute("UPDATE dest SET misses=misses+1 WHERE qso_id=? AND dest=?", (qid, dest))
        return self.row(qid, dest)["misses"]

    def requeue(self, qid, dest):
        """A lost send LoTW never listed goes back to its sender (PLAN §5:
        absent -> pending), its miss count reset."""
        now = self.clock()
        with self._tx():
            return self.db.execute("UPDATE dest SET state='pending', reason=NULL, misses=0, updated=? "
                                   "WHERE qso_id=? AND dest=? AND state='unknown'", (now, qid, dest)).rowcount

    def set_detail(self, qids, dest, detail):
        with self._tx():
            for qid in qids:
                self.db.execute("UPDATE dest SET detail=? WHERE qso_id=? AND dest=?", (detail, qid, dest))

    def correction(self, qid, dest):
        """Queue the latest revision for dest (the operator's Send correction)."""
        now = self.clock()
        with self._tx():
            return self.db.execute(
                "UPDATE dest SET state='pending', correction=1, edited=0, reason=NULL, detail=NULL, updated=? "
                "WHERE qso_id=? AND dest=? AND edited=1", (now, qid, dest)).rowcount

    # ---------------------------------------------------------------- cursors and alerts

    def cursor(self, name, default=None):
        r = self.db.execute("SELECT value FROM cursor WHERE name=?", (name,)).fetchone()
        return r["value"] if r else default

    def set_cursor(self, name, value):
        with self._tx():
            self.db.execute("INSERT INTO cursor(name, value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=?",
                            (name, str(value), str(value)))

    def alert(self, dest, cls, msg):
        with self._tx():
            self.db.execute("INSERT INTO alert(dest, cls, msg, created) VALUES(?,?,?,?)",
                            (dest, cls, msg, self.clock()))

    def alerts(self, limit=10):
        return self.db.execute("SELECT * FROM alert ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ---------------------------------------------------------------- views

    def counts(self):
        out = {d: {} for d in self.dests}
        for r in self.db.execute("SELECT dest, state, count(*) n FROM dest GROUP BY dest, state"):
            out.setdefault(r["dest"], {})[r["state"]] = r["n"]
        return out

    def recent(self, limit=15):
        rows = self.db.execute("SELECT * FROM qso ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for q in rows:
            ds = {d["dest"]: d for d in self.db.execute("SELECT * FROM dest WHERE qso_id=?", (q["id"],))}
            out.append((q, ds))
        return out

    # ---------------------------------------------------------------- backfill (PLAN §7)

    def backfill_set(self):
        """[(qso_id, dest)] still held_backfill, in order."""
        return [(r["qso_id"], r["dest"]) for r in self.db.execute(
            "SELECT d.qso_id, d.dest FROM dest d JOIN qso q ON q.id=d.qso_id WHERE d.state='held_backfill' "
            "ORDER BY q.ts, q.id, d.dest")]

    @staticmethod
    def set_hash(pairs):
        return hashlib.sha256(json.dumps(sorted(pairs)).encode()).hexdigest()[:16]

    def release(self, want_hash):
        """Move exactly the reported set to pending. Refuses (returns None)
        if the set changed since the report: a stale hash."""
        with self._tx():
            pairs = self.backfill_set()
            if self.set_hash(pairs) != want_hash:
                return None
            now = self.clock()
            for qid, d in pairs:
                self.db.execute("UPDATE dest SET state='pending', updated=? WHERE qso_id=? AND dest=? "
                                "AND state='held_backfill'", (now, qid, d))
            return len(pairs)

    def _tx(self):
        return _Tx(self.db)


class _Tx:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")

    def __exit__(self, et, e, tb):
        self.db.execute("COMMIT" if et is None else "ROLLBACK")
        return False
