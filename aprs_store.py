"""APRS store: what the hub has heard, in one SQLite file (APRS plan M2).

One worker thread owns the database, behind a bounded queue; the hub never
runs SQL on its event loop. Frames arrive as TNC2 lines (the same text the hub
writes to rx.jsonl), so the live path and the backfill from the logs are one
code path. Receive only: nothing here can reach messaging or transmit.

    store = AprsStore(path).start()
    store.ingest(t, "rf", "KF7ABC-9>APRS,WIDE1-1:!4049.00N/10510.00W>hi")   # from the loop, never blocks
    rows = await store.call("stations", 0)                                 # any Db method, off the loop
    store.stop()

The rules (dups per port, who counts as heard on RF, RF-vs-IS positions,
object owners, retention with tombstones) are in the plan's M2 section.
"""
import asyncio
import json
import logging
import math
import os
import queue
import sqlite3
import threading
import time

import aprs

log = logging.getLogger("aprs_store")

SCHEMA_VERSION = 1
DUP_WINDOW_S = 30
IS_KEEPS_RF_S = 600               # an untimestamped IS fix never replaces an RF fix this fresh
RETENTION = {"packets": 30 * 86400, "positions": 7 * 86400, "stations": 90 * 86400,
             "killed_objects": 86400, "tombstones": 30 * 86400}
TRACK_MAX = 500                   # positions kept per station
RETENTION_EVERY_S = 3600
QUEUE_MAX = 20000
BATCH_MAX = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS packets (
  id INTEGER PRIMARY KEY, t REAL, port TEXT, src TEXT, dest TEXT, path TEXT, info BLOB,
  type TEXT, third_party INTEGER, inner_src TEXT, dup_of INTEGER, decoded TEXT,
  dupkey TEXT, dup_port TEXT, dups INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS packets_t ON packets (t);
CREATE INDEX IF NOT EXISTS packets_src ON packets (src, t);
CREATE INDEX IF NOT EXISTS packets_inner ON packets (inner_src, t);
CREATE INDEX IF NOT EXISTS packets_dup ON packets (dup_port, dupkey, t);
CREATE TABLE IF NOT EXISTS stations (
  call TEXT PRIMARY KEY, first REAL, last REAL, count INTEGER,
  rf_last REAL, direct_last REAL, is_last REAL, rf_path TEXT,
  lat REAL, lon REAL, pos_t REAL, pos_reported_t REAL, pos_port TEXT,
  ambiguity INTEGER, box_deg REAL, symbol TEXT, comment TEXT, status TEXT, device TEXT,
  weather TEXT, telem_meta TEXT, text TEXT, last_type TEXT, rev INTEGER);
CREATE INDEX IF NOT EXISTS stations_rev ON stations (rev);
CREATE INDEX IF NOT EXISTS stations_last ON stations (last);
CREATE TABLE IF NOT EXISTS positions (
  call TEXT, t REAL, lat REAL, lon REAL, course INTEGER, speed REAL, alt REAL, port TEXT);
CREATE INDEX IF NOT EXISTS positions_call ON positions (call, t);
CREATE TABLE IF NOT EXISTS objects (
  name TEXT, owner TEXT, kind TEXT, live INTEGER, t REAL, lat REAL, lon REAL, ambiguity INTEGER,
  symbol TEXT, comment TEXT, killed_by TEXT, rev INTEGER, PRIMARY KEY (name, owner));
CREATE INDEX IF NOT EXISTS objects_rev ON objects (rev);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, t REAL, dir TEXT, port TEXT, src TEXT, dst TEXT, text TEXT,
  msgno TEXT, reply_ack TEXT, peer_replyack INTEGER, state TEXT,
  first_sent REAL, tries INTEGER, next_try REAL, deadline REAL, acked_t REAL, why TEXT);
CREATE INDEX IF NOT EXISTS messages_src ON messages (src, t);
CREATE INDEX IF NOT EXISTS messages_dst ON messages (dst, t);
CREATE TABLE IF NOT EXISTS tombstones (kind TEXT, key TEXT, rev INTEGER);
CREATE INDEX IF NOT EXISTS tombstones_rev ON tombstones (rev);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

STATION_COLS = ("call", "first", "last", "count", "rf_last", "direct_last", "is_last", "rf_path",
                "lat", "lon", "pos_t", "pos_reported_t", "pos_port", "ambiguity", "box_deg", "symbol",
                "comment", "status", "device", "weather", "telem_meta", "text", "last_type", "rev")
OBJECT_COLS = ("name", "owner", "kind", "live", "t", "lat", "lon", "ambiguity", "symbol", "comment",
               "killed_by", "rev")
POSITION_TYPES = ("position", "mice", "weather")


def parse_line(line):
    """A TNC2 monitor line -> (src, dest, path, info), or None when it isn't a
    UI frame (connected-mode lines read 'A>B <I C S0R0> ...': a space before
    the first colon)."""
    head, sep, _ = line.partition(":")
    if not sep or " " in head:
        return None
    return aprs.split_tnc2(line)


def _finite(v):
    """NaN and Infinity aren't JSON; browsers refuse them. None instead."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _finite(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_finite(x) for x in v]
    return v


def _json(v):
    try:
        return json.dumps(v, separators=(",", ":"), default=str, allow_nan=False)
    except ValueError:
        return json.dumps(_finite(v), separators=(",", ":"), default=str)


def _load(v):
    try:
        return json.loads(v) if v else None
    except ValueError:
        return None


class Db:
    """The SQL. Runs on the store's worker thread (or directly, in tests)."""

    def __init__(self, path, clock=time.time):
        self.path = str(path)
        self.clock = clock
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.migrate()
        self.counts = {"ingested": 0, "dups": 0, "non_ui": 0, "unknown": 0}

    # ---------- schema

    def migrate(self):
        v = self.db.execute("PRAGMA user_version").fetchone()[0]
        if v > SCHEMA_VERSION:
            raise RuntimeError(f"{self.path}: schema {v} is newer than this code ({SCHEMA_VERSION})")
        self.db.executescript(SCHEMA)
        # Later versions migrate here, one step at a time: if v < 2: ALTER ...
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('rev', '0')")

    def meta(self, k, default=None):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def set_meta(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, str(v)))

    def next_rev(self):
        rev = int(self.meta("rev", 0)) + 1
        self.set_meta("rev", rev)
        return rev

    def close(self):
        self.db.close()

    # ---------- ingest

    def ingest_many(self, items):
        """[(t, port, line), ...] in one transaction."""
        self.db.execute("BEGIN")
        try:
            for t, port, line in items:
                self._ingest(t, port, line)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def ingest(self, t, port, line):
        self.ingest_many([(t, port, line)])

    def _ingest(self, t, port, line):
        parts = parse_line(line)
        if parts is None:
            # Not APRS (connected mode): still a station heard, nothing else.
            self.counts["non_ui"] += 1
            src = line.partition(">")[0].strip()
            if src and " " not in src and ":" not in src:
                head = line.partition(" ")[0]
                used = "*" in head
                self._heard(aprs.norm_call(src), t, port, line, used, head.partition(",")[2], None)
            return
        src, dest, path, info = parts
        d = aprs.decode(src, dest, path, info)
        self.counts["ingested"] += 1
        if d["type"] == "unknown":
            self.counts["unknown"] += 1
        inner = d.get("inner") if d.get("third_party") else None
        key_src, key_dest, key_info = ((inner["src"], inner["dest"], inner["info"]) if inner
                                       else (d["src"], d["dest"], d["info"]))
        dupkey = f"{key_src}>{key_dest}:{key_info}"
        path_s = ",".join(path)
        used = any(p.endswith("*") for p in path)
        # An IS copy never suppresses an RF copy: a packet an iGate wrapped
        # from APRS-IS is judged among the IS copies (review 2026-10-19, Codex).
        dup_port = "is" if inner and d.get("inner_tcpip") else port
        dup = self.db.execute(
            "SELECT id, path FROM packets WHERE dup_port=? AND dupkey=? AND t>=? ORDER BY t DESC LIMIT 1",
            (dup_port, dupkey, t - DUP_WINDOW_S)).fetchone()
        outer = d["src"]
        # The outer transmitter was heard either way (a digipeated copy, or the
        # same IS packet through a second iGate, is still that station on the air).
        self._heard(outer, t, port, line, used, path_s, d, count=dup is None)
        if dup is not None:
            self.counts["dups"] += 1
            if len(path) < len([p for p in (dup["path"] or "").split(",") if p]):
                self.db.execute("UPDATE packets SET path=?, dups=dups+1 WHERE id=?", (path_s, dup["id"]))
            else:
                self.db.execute("UPDATE packets SET dups=dups+1 WHERE id=?", (dup["id"],))
            return
        self.db.execute(
            "INSERT INTO packets (t, port, src, dest, path, info, type, third_party, inner_src, dup_of,"
            " decoded, dupkey, dup_port) VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?,?)",
            (t, port, d["src"], d["dest"], path_s, d["info"].encode("latin-1", "replace"), d["type"],
             1 if inner else 0, inner["src"] if inner else None, _json(d), dupkey, dup_port))
        if inner:
            subject_port = "is" if d.get("inner_tcpip") else port
            self._inner_heard(inner, t, subject_port, d.get("inner_tcpip"))
            self._apply(inner, t, subject_port)
        else:
            self._apply(d, t, port)

    def _station(self, call, t):
        r = self.db.execute("SELECT * FROM stations WHERE call=?", (call,)).fetchone()
        if r is None:
            self.db.execute("DELETE FROM tombstones WHERE kind='station' AND key=?", (call,))
            self.db.execute("INSERT INTO stations (call, first, last, count) VALUES (?,?,?,0)", (call, t, t))
            r = self.db.execute("SELECT * FROM stations WHERE call=?", (call,)).fetchone()
        return dict(r)

    def _save_station(self, s, changes):
        if not changes:
            return
        changes["rev"] = self.next_rev()
        unknown = changes.keys() - set(STATION_COLS)
        if unknown:
            raise ValueError(f"not station columns: {sorted(unknown)}")
        cols = ", ".join(f"{k}=?" for k in changes)
        # Column names are allow-listed against STATION_COLS above; values stay bound.
        self.db.execute(f"UPDATE stations SET {cols} WHERE call=?",  # nosec B608
                        (*changes.values(), s["call"]))

    def _heard(self, call, t, port, line, used, path_s, d, count=True):
        s = self._station(call, t)
        ch = {"last": max(t, s["last"] or 0), "text": line}
        if count:
            ch["count"] = (s["count"] or 0) + 1
        if d is not None:
            ch["last_type"] = d["type"]
        if port == "rf":
            ch["rf_last"] = max(t, s["rf_last"] or 0)
            if not used:
                ch["direct_last"] = max(t, s["direct_last"] or 0)
            # The shortest way it reaches us: keep a direct path over a digipeated one.
            if not used or not s["rf_path"] or (s["direct_last"] or 0) < t - 3600:
                ch["rf_path"] = path_s
        else:
            ch["is_last"] = max(t, s["is_last"] or 0)
        self._save_station(s, ch)

    def _inner_heard(self, inner, t, port, tcpip):
        s = self._station(inner["src"], t)
        ch = {"last": max(t, s["last"] or 0), "count": (s["count"] or 0) + 1,
              "text": f"{inner['src']}>{inner['dest']}:{inner['info']}", "last_type": inner["type"]}
        if tcpip:
            ch["is_last"] = max(t, s["is_last"] or 0)
        self._save_station(s, ch)

    # ---------- what a packet says

    def _apply(self, d, t, port):
        typ = d["type"]
        if typ in ("object", "item"):
            self._object(d, t)
        elif typ in POSITION_TYPES and "lat" in d:
            self._position(d, t, port)
        if typ == "weather" or d.get("weather"):
            s = self._station(d["src"], t)
            self._save_station(s, {"weather": _json({**d.get("weather", {}), "t": t})})
        if typ == "status":
            s = self._station(d["src"], t)
            self._save_station(s, {"status": d.get("text", "")})
        if d.get("device"):
            s = self._station(d["src"], t)
            if s["device"] != d["device"]:
                self._save_station(s, {"device": d["device"]})
        if typ == "telemetry_meta":
            self._telem_meta(d, t)
        elif typ == "message":
            self._message(d, t, port)
        elif typ in ("ack", "rej"):
            self.db.execute(
                "UPDATE messages SET state=?, acked_t=? WHERE dir='in' AND src=? AND dst=? AND msgno=?"
                " AND state='received'",
                ("acked" if typ == "ack" else "rejected", t, d["addressee"], d["src"], d["msgno"]))

    def _position(self, d, t, port):
        s = self._station(d["src"], t)
        rt = aprs.reported_time(d.get("timestamp"), t)
        if s["lat"] is not None:
            old_rt, old_t, old_port = s["pos_reported_t"], s["pos_t"] or 0, s["pos_port"]
            old_eff = old_rt or old_t
            if port == "is" and old_port == "rf":
                if (rt or t) < old_eff or (rt is None and t - old_t < IS_KEEPS_RF_S):
                    return
            if rt is not None:
                if rt <= old_eff:
                    return
            elif t < old_t:
                return
        ch = {"lat": d["lat"], "lon": d["lon"], "pos_t": t, "pos_reported_t": rt, "pos_port": port,
              "ambiguity": d.get("ambiguity", 0), "box_deg": d.get("box_deg", 0.0),
              "symbol": d.get("symbol")}
        if d.get("comment"):
            ch["comment"] = d["comment"][:200]
        self._save_station(s, ch)
        last = self.db.execute("SELECT lat, lon FROM positions WHERE call=? ORDER BY t DESC LIMIT 1",
                               (d["src"],)).fetchone()
        if last is None or (last["lat"], last["lon"]) != (d["lat"], d["lon"]):
            self.db.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?)",
                            (d["src"], rt or t, d["lat"], d["lon"], d.get("course"), d.get("speed_kt"),
                             d.get("alt_ft"), port))

    def _object(self, d, t):
        name, owner = d["name"].strip(), d["src"]
        if not name:
            return
        if not d.get("live"):
            # A kill from its owner applies; from anyone else it's shown on the
            # owner's object, not applied (and makes no object of its own).
            owners = [r["owner"] for r in self.db.execute("SELECT owner FROM objects WHERE name=?", (name,))]
            if owner not in owners:
                for o in owners:
                    self.db.execute("UPDATE objects SET killed_by=?, rev=? WHERE name=? AND owner=?",
                                    (owner, self.next_rev(), name, o))
                return
        self.db.execute("DELETE FROM tombstones WHERE kind='object' AND key=?", (_json([name, owner]),))
        self.db.execute(
            "INSERT OR REPLACE INTO objects VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?)",
            (name, owner, d["type"], 1 if d.get("live") else 0, t, d.get("lat"), d.get("lon"),
             d.get("ambiguity", 0), d.get("symbol"), (d.get("comment") or "")[:200], self.next_rev()))

    def _telem_meta(self, d, t):
        target = aprs.norm_call(d.get("addressee") or d["src"])
        s = self._station(target, t)
        meta = _load(s["telem_meta"]) or {}
        for k in ("names", "units", "eqns", "bits_sense", "project"):
            if k in d:
                meta[k] = d[k]
        self._save_station(s, {"telem_meta": _json(meta)})

    def _message(self, d, t, port):
        dst = aprs.norm_call(d.get("addressee", ""))
        if d.get("msgno"):
            # The sender retries the same message (same msgno and text) until
            # it's acked; keep one row.
            seen = self.db.execute(
                "SELECT id FROM messages WHERE dir='in' AND src=? AND dst=? AND msgno=? AND text=? AND t>=?",
                (d["src"], dst, d["msgno"], d.get("text", ""), t - 86400)).fetchone()
            if seen:
                return
        self.db.execute(
            "INSERT INTO messages (t, dir, port, src, dst, text, msgno, reply_ack, peer_replyack, state)"
            " VALUES (?, 'in', ?, ?, ?, ?, ?, ?, ?, 'received')",
            (t, port, d["src"], dst, d.get("text", ""), d.get("msgno"), d.get("reply_ack"),
             1 if d.get("replyack_capable") else 0))

    # ---------- retention

    def retain(self, now=None):
        now = self.clock() if now is None else now
        self.db.execute("BEGIN")
        try:
            r = {}
            r["packets"] = self.db.execute("DELETE FROM packets WHERE t < ?",
                                           (now - RETENTION["packets"],)).rowcount
            r["positions"] = self.db.execute("DELETE FROM positions WHERE t < ?",
                                             (now - RETENTION["positions"],)).rowcount
            r["positions"] += self.db.execute(
                "DELETE FROM positions WHERE rowid IN (SELECT rowid FROM (SELECT rowid, ROW_NUMBER() OVER"
                " (PARTITION BY call ORDER BY t DESC) AS n FROM positions) WHERE n > ?)", (TRACK_MAX,)).rowcount
            old = [x[0] for x in self.db.execute("SELECT call FROM stations WHERE last < ?",
                                                 (now - RETENTION["stations"],))]
            for call in old:
                self.db.execute("DELETE FROM stations WHERE call=?", (call,))
                self.db.execute("INSERT INTO tombstones VALUES ('station', ?, ?)", (call, self.next_rev()))
            r["stations"] = len(old)
            dead = self.db.execute("SELECT name, owner FROM objects WHERE live=0 AND t < ?",
                                   (now - RETENTION["killed_objects"],)).fetchall()
            for o in dead:
                self.db.execute("DELETE FROM objects WHERE name=? AND owner=?", (o["name"], o["owner"]))
                self.db.execute("INSERT INTO tombstones VALUES ('object', ?, ?)",
                                (_json([o["name"], o["owner"]]), self.next_rev()))
            r["objects"] = len(dead)
            # Old tombstones go too; a client older than the newest one dropped reloads in full.
            gone = self.db.execute("SELECT MAX(rev) FROM tombstones WHERE rowid IN (SELECT t.rowid FROM"
                                   " tombstones t ORDER BY rev LIMIT -1 OFFSET 5000)").fetchone()[0]
            if gone:
                self.db.execute("DELETE FROM tombstones WHERE rev <= ?", (gone,))
                self.set_meta("tomb_floor", gone)
            self.set_meta("retained", now)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return r

    # ---------- backfill

    def backfill(self, logdir):
        """Once, into an empty db: rx.jsonl.1 then rx.jsonl, in one
        transaction. Returns what it did. Feeds the store only."""
        if self.meta("backfill"):
            return {"skipped": "already done", **_load(self.meta("backfill"))}
        files = [os.path.join(logdir, n) for n in ("rx.jsonl.1", "rx.jsonl")]
        done = {"files": [], "lines": 0, "ingested": 0, "malformed": 0}
        items = []
        for fn in files:
            try:
                f = open(fn, encoding="utf-8", errors="replace")
            except OSError:
                continue
            with f:
                first = None
                for raw in f:
                    first = first if first is not None else raw.rstrip("\n")
                    done["lines"] += 1
                    try:
                        rec = json.loads(raw)
                        t, text = float(rec["t"]), rec["text"]
                        if rec.get("dir", "rx") != "rx" or not isinstance(text, str):
                            raise ValueError("not an rx record")
                    except (ValueError, KeyError, TypeError):
                        done["malformed"] += 1
                        continue
                    items.append((t, "rf", text))
                done["files"].append({"name": os.path.basename(fn), "size": os.path.getsize(fn),
                                      "first": (first or "")[:200]})
        items.sort(key=lambda x: x[0])
        self.db.execute("BEGIN")
        try:
            for t, port, line in items:
                self._ingest(t, port, line)
            done["ingested"] = len(items)
            self.set_meta("backfill", _json(done))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return done

    # ---------- reads (the API)

    def rev(self):
        return int(self.meta("rev", 0))

    def heard(self, n=100):
        """The page's heard list, as the hub kept it in memory."""
        out = []
        for r in self.db.execute("SELECT call, first, last, count, rf_last, direct_last, text FROM stations"
                                 " WHERE rf_last IS NOT NULL ORDER BY rf_last DESC LIMIT ?", (n,)):
            out.append({"station": r["call"], "first": r["first"], "last": r["rf_last"],
                        "count": r["count"], "direct": bool(r["direct_last"]) and r["direct_last"] >= r["rf_last"],
                        "text": r["text"] or ""})
        return out

    def _delta(self, table, cols, kind, since, key):
        since = int(since or 0)
        floor = int(self.meta("tomb_floor", 0))
        full = since <= 0 or since < floor
        # table and cols are only ever the module constants passed by stations()/objects().
        rows = self.db.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE rev > ? ORDER BY rev",  # nosec B608
                               (0 if full else since,)).fetchall()
        gone = [] if full else [_load(r[0]) if kind == "object" else r[0] for r in self.db.execute(
            "SELECT key FROM tombstones WHERE kind=? AND rev > ?", (kind, since))]
        return {"rev": self.rev(), "full": full, "rows": [key(dict(r)) for r in rows], "gone": gone}

    def stations(self, since=0):
        def shape(r):
            for k in ("weather", "telem_meta"):
                r[k] = _load(r[k])
            r["direct"] = bool(r["direct_last"]) and r["direct_last"] >= (r["rf_last"] or 0)
            return r
        return self._delta("stations", STATION_COLS, "station", since, shape)

    def objects(self, since=0):
        return self._delta("objects", OBJECT_COLS, "object", since, lambda r: r)

    def station(self, call, packets=50):
        call = aprs.norm_call(call)
        r = self.db.execute("SELECT * FROM stations WHERE call=?", (call,)).fetchone()
        if r is None:
            return None
        s = dict(r)
        for k in ("weather", "telem_meta"):
            s[k] = _load(s[k])
        pk = self.db.execute(
            "SELECT t, port, src, path, dups, decoded FROM packets WHERE src=? OR inner_src=?"
            " ORDER BY t DESC LIMIT ?", (call, call, packets)).fetchall()
        s["packets"] = [{"t": p["t"], "port": p["port"], "src": p["src"], "path": p["path"], "dups": p["dups"],
                         "decoded": _load(p["decoded"])} for p in pk]
        s["track"] = [dict(x) for x in self.db.execute(
            "SELECT t, lat, lon, course, speed, alt, port FROM positions WHERE call=? ORDER BY t", (call,))]
        s["paths"] = [{"path": x[0], "count": x[1], "last": x[2]} for x in self.db.execute(
            "SELECT path, COUNT(*), MAX(t) FROM packets WHERE src=? AND port='rf' GROUP BY path"
            " ORDER BY COUNT(*) DESC LIMIT 10", (call,))]
        s["telemetry"] = self._telemetry(call, s["telem_meta"])
        s["direct"] = bool(s["direct_last"]) and s["direct_last"] >= (s["rf_last"] or 0)
        return s

    def _telemetry(self, call, meta):
        row = self.db.execute("SELECT t, decoded FROM packets WHERE src=? AND type='telemetry'"
                              " ORDER BY t DESC LIMIT 1", (call,)).fetchone()
        if row is None:
            return None
        d = _load(row["decoded"]) or {}
        meta = meta or {}
        names, units, eqns = meta.get("names") or [], meta.get("units") or [], meta.get("eqns") or []
        channels = []
        for i, raw in enumerate(d.get("analog") or []):
            v = aprs.scale(eqns[i], raw) if i < len(eqns) and raw is not None else raw
            channels.append({"name": names[i] if i < len(names) else f"A{i + 1}",
                             "unit": units[i] if i < len(units) else "",
                             "raw": raw, "value": round(v, 3) if isinstance(v, float) else v})
        return {"t": row["t"], "seq": d.get("seq"), "channels": channels, "bits": d.get("bits")}

    def messages(self, with_call=None, n=200):
        if with_call:
            c = aprs.norm_call(with_call)
            rows = self.db.execute("SELECT * FROM messages WHERE src=? OR dst=? ORDER BY t DESC LIMIT ?",
                                   (c, c, n))
        else:
            rows = self.db.execute("SELECT * FROM messages ORDER BY t DESC LIMIT ?", (n,))
        return [dict(r) for r in rows][::-1]

    def messages_to(self, bases, n=100):
        """Messages to our calls (any SSID), oldest first: the page's
        'messages for you', reloaded at start."""
        out = []
        for r in self.db.execute("SELECT t, src, dst, text, msgno FROM messages WHERE dir='in'"
                                 " ORDER BY t DESC LIMIT 5000"):
            if aprs.base_call(r["dst"]) in bases:
                text = r["text"] + (f"{{{r['msgno']}" if r["msgno"] else "")
                out.append({"t": r["t"], "from": r["src"], "to": r["dst"], "text": text})
                if len(out) >= n:
                    break
        return out[::-1]

    def summary(self):
        q = lambda s: self.db.execute(s).fetchone()[0]  # noqa: E731
        return {"packets": q("SELECT COUNT(*) FROM packets"), "stations": q("SELECT COUNT(*) FROM stations"),
                "objects": q("SELECT COUNT(*) FROM objects WHERE live=1"),
                "messages": q("SELECT COUNT(*) FROM messages"), "rev": self.rev(),
                "backfill": _load(self.meta("backfill")), "retained": self.meta("retained"),
                **self.counts}


class AprsStore:
    """The Db on one worker thread. ingest() never blocks (a full queue drops
    and counts); call() runs any Db method there and returns its result to the
    awaiting coroutine."""

    def __init__(self, path, clock=time.time, retention_every=RETENTION_EVERY_S):
        self.path = str(path)
        self.clock = clock
        self.retention_every = retention_every
        self.q = queue.Queue(maxsize=QUEUE_MAX)
        self.dropped = 0
        self.errors = 0
        self.db = None
        self._thread = None
        self._ready = threading.Event()
        self._open_error = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="aprs-store", daemon=True)
        self._thread.start()
        self._ready.wait(30)
        if self._open_error:
            raise self._open_error
        return self

    def stop(self, timeout=10):
        if self._thread and self._thread.is_alive():
            self.q.put(("stop", None, None))
            self._thread.join(timeout)

    def ingest(self, t, port, line):
        try:
            self.q.put_nowait(("ingest", (t, port, line), None))
        except queue.Full:
            self.dropped += 1
            if self.dropped in (1, 10, 100) or self.dropped % 1000 == 0:
                log.warning("APRS store queue full: %d frames not stored", self.dropped)

    def submit(self, name, *args):
        """A concurrent Future for Db.<name>(*args), run on the worker."""
        import concurrent.futures
        fut = concurrent.futures.Future()
        self.q.put_nowait(("call", (name, args), fut))
        return fut

    async def call(self, name, *args):
        return await asyncio.wrap_future(self.submit(name, *args))

    def qsize(self):
        return self.q.qsize()

    def _run(self):
        try:
            self.db = Db(self.path, self.clock)
        except Exception as e:     # noqa: BLE001
            self._open_error = e
            self._ready.set()
            return
        self._ready.set()
        next_retain = time.monotonic() + 60
        while True:
            try:
                kind, arg, fut = self.q.get(timeout=max(0.1, next_retain - time.monotonic()))
            except queue.Empty:
                kind = None
            if kind is None or time.monotonic() >= next_retain:
                next_retain = time.monotonic() + self.retention_every
                try:
                    r = self.db.retain()
                    if any(r.values()):
                        log.info("APRS retention: %s", r)
                except Exception:     # noqa: BLE001
                    self.errors += 1
                    log.exception("APRS retention failed")
                if kind is None:
                    continue
            if kind == "stop":
                self.db.close()
                return
            if kind == "ingest":
                batch = [arg]
                pending = None
                while len(batch) < BATCH_MAX:
                    try:
                        k2, a2, f2 = self.q.get_nowait()
                    except queue.Empty:
                        break
                    if k2 != "ingest":
                        pending = (k2, a2, f2)
                        break
                    batch.append(a2)
                self._safe_ingest(batch)
                if pending is None:
                    continue
                kind, arg, fut = pending
                if kind == "stop":
                    self.db.close()
                    return
            name, args = arg
            # A caller cancelled before we got here (a page closed): skip it.
            # Once running, the future can't be cancelled, so setting its
            # result can't race a cancel (review 2026-10-19, Codex).
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(getattr(self.db, name)(*args))
            except Exception as e:     # noqa: BLE001
                fut.set_exception(e)

    def _safe_ingest(self, batch):
        try:
            self.db.ingest_many(batch)
        except Exception:     # noqa: BLE001
            # One bad frame must not lose the batch: store them one by one.
            log.exception("APRS store batch failed; storing one at a time")
            for item in batch:
                try:
                    self.db.ingest_many([item])
                except Exception:     # noqa: BLE001
                    self.errors += 1
                    log.exception("APRS store: frame not stored: %r", item[2][:200])
