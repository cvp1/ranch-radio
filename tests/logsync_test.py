"""The logbook sync (logsync.py, PLAN v3 §9): every case against fakes only.
No real Wavelog, QRZ or LoTW is called and the real TQSL never runs: the
destinations are tests/fake_wavelog.py, fake_qrz.py and fake_lotw.py on
OS-assigned ports, TQSL is tests/fake_tqsl.py behind a wrapper script, and
JTDX is tests/fake_jtdx.py on a private UDP port (or a private multicast
group per run).

  protocol    a hand-assembled QSO Logged datagram (NetworkMessage.hpp field
              order, written byte by byte here) parses to the right ADIF
  pairing     type 5 + type 12 for one QSO -> one row, one send per destination;
              type 5 alone -> one send (after the pair wait)
  identity    two QSOs with one station, same band and mode, 90 s apart -> two
              rows, two sends each (Wavelog's 15-minute duplicate refuses the
              second: held, never counted as sent), both flagged near misses;
              FT4 as MODE=FT4 (type 5) and MODE=MFSK SUBMODE=FT4 (type 12) -> one row
  revision    a re-log with a changed report -> a revision, nothing sent until
              Send correction; LoTW refuses a correction (no report there)
  crash       a crash after Wavelog's 201 and before "sent" -> unknown at the
              restart -> the reconcile finds it -> no second send
              (and QRZ's: the reconcile FETCHes by call); a QSO QRZ already has
              -> FAIL duplicate -> sent, no alert
  TQSL        exit 9 on a backfill batch with one out-of-range QSO -> that QSO
              held(not on LoTW) with TQSL's reason, the rest accepted (and all
              three unknown, not submitted, straight after the run); exit 0 but
              absent from three qso_qsl=no pulls -> held and alerted; a hang ->
              killed (with its child) at the deadline, held, Wavelog and QRZ keep
              sending, shutdown is clean; a TQSL waiting on stdin gets EOF
  backfill    an accepted-but-unconfirmed QSO (qso_qsl=no, not yes) is never
              queued; nothing leaves held_backfill across restarts without
              `ranch-radio logsync backfill --apply HASH`; a stale hash is refused
  pulls       a LoTW HTML login page or truncated ADIF is a failure: cursor
              unchanged, no misses counted
  outage      one destination down -> the other two keep sending; it catches up
              in order
  UDP         multicast: logsync and a GridTracker both hear JTDX, GridTracker's
              Reply reaches JTDX, logsync sends JTDX nothing, a forward target
              is fed one way. Unicast relay: GridTracker's Reply reaches JTDX;
              Reply from anyone else, or with another id, or a non-reply type,
              is dropped; nothing goes back to a forward target; idle clients expire
  station     logsync failing to start (or a bad block) -> the station, its
              radio and the Logbook tab are up; switching the Logbook tab off
              doesn't stop logging; its snapshot carries the status
  secrets     no key, password or LoTW URL in any log line, TQSL's argv, the
              snapshot, an alert or reason, the ledger or the safety copy
  wiring      config refusals name the field; render puts the block (with the
              host's call) in station.json and refuses a bad one; doctor's
              checks; catalog, manifest and the CLI

    python3 tests/logsync_test.py
"""
import asyncio
import datetime as dt
import json
import logging
import os
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import fake_jtdx as J  # noqa: E402
import testkit  # noqa: E402
from fake_lotw import FakeLotw  # noqa: E402
from fake_qrz import FakeQrz  # noqa: E402
from fake_wavelog import FakeWavelog  # noqa: E402

import logsync  # noqa: E402
import logsync_adif as A  # noqa: E402
import logsync_udp as U  # noqa: E402

fails = 0
WL_KEY, QRZ_KEY, LOTW_PW = "WLKEY-s3cret-7f1e", "QRZKEY-s3cret-22ab", "LOTWPW-s3cret-90cd"
SECRETS = (WL_KEY, QRZ_KEY, LOTW_PW)
T0 = dt.datetime(2026, 11, 13, 12, 3, 15, tzinfo=dt.UTC)


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"), flush=True)


# ---------------------------------------------------------------- log capture (the secrets case reads it)

LOG_LINES = []


class Keep(logging.Handler):
    def emit(self, record):
        LOG_LINES.append(self.format(record))


_h = Keep()
_h.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
logging.getLogger().addHandler(_h)
logging.getLogger().setLevel(logging.DEBUG)


# ---------------------------------------------------------------- the event loop the engine and fakes run on

class Bg:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coro, timeout=30):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def call(self, fn, *a):
        async def f():
            return fn(*a)
        return self.run(f())


BG = Bg()


async def _serve(app):
    runner = web.AppRunner(app, access_log=None)      # the fakes' own access log is the destination's, not ours
    await runner.setup()
    port = testkit.free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, port


def free_udp():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class H:
    """One scenario: fakes on free ports, credential files, a fake TQSL, and
    a logsync engine on a fresh ledger."""

    def __init__(self, dests=("wavelog", "qrz", "lotw"), tqsl=None, udp=None, timing=None, forward=(),
                 start=True):
        self.tmp = Path(tempfile.mkdtemp(prefix="logsync-test-"))
        self.files = {}
        for name, val in (("wavelog-key", WL_KEY), ("qrz-key", QRZ_KEY), ("lotw-password", LOTW_PW)):
            p = self.tmp / name
            p.write_text(val + "\n")
            os.chmod(p, 0o600)
            self.files[name] = p
        self.wl = FakeWavelog(WL_KEY)
        self.qrz = FakeQrz(QRZ_KEY, "N0CALL")
        self.store = self.tmp / "lotw-store.jsonl"
        self.lotw = FakeLotw("N0CALL", LOTW_PW, str(self.store))
        self.runners = []
        ports = {}
        for name, fake in (("wl", self.wl), ("qrz", self.qrz), ("lotw", self.lotw)):
            r, ports[name] = BG.run(_serve(fake.app()))
            self.runners.append(r)
        self.tqsl_log = self.tmp / "tqsl-log.jsonl"
        self.tqsl_conf = self.tmp / "tqsl.json"
        self.set_tqsl(tqsl or {"mode": "upload"})
        wrapper = self.tmp / "tqsl"
        wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {ROOT / 'tests' / 'fake_tqsl.py'} \"$@\"\n")
        os.chmod(wrapper, 0o755)
        os.environ["FAKE_TQSL_CONF"] = str(self.tqsl_conf)
        self.port = free_udp()
        t = {"pair_wait_s": 0.4, "tqsl_timeout_s": 3, "backoff_s": [0.1, 0.4], "http_timeout_s": 5,
             "lotw_every_s": 3600, "qrz_every_s": 3600, "wavelog_every_s": 3600}
        t.update(timing or {})
        self.block = {"udp": udp or {"bind": "127.0.0.1", "port": self.port}, "forward": list(forward),
                      "call": "N0CALL", "ledger": str(self.tmp / "state" / "logsync.db"), "timing": t}
        if "wavelog" in dests:
            self.block["wavelog"] = {"url": f"http://127.0.0.1:{ports['wl']}", "station_profile_id": 1,
                                     "key_file": str(self.files["wavelog-key"])}
        if "qrz" in dests:
            self.block["qrz"] = {"key_file": str(self.files["qrz-key"]), "url": f"http://127.0.0.1:{ports['qrz']}/api"}
        if "lotw" in dests:
            self.block["lotw"] = {"tqsl": str(wrapper), "location": "Home Station", "login": "N0CALL",
                                  "password_file": str(self.files["lotw-password"]),
                                  "url": f"http://127.0.0.1:{ports['lotw']}/lotwuser/lotwreport.adi"}
        self.alerts = []
        self.eng = None
        self.jtdx = J.FakeJtdx()
        if start:
            self.start()

    def set_tqsl(self, conf):
        conf = {"log": str(self.tqsl_log), "store": str(self.store), **conf}
        self.tqsl_conf.write_text(json.dumps(conf))

    def start(self, **attrs):
        self.eng = logsync.LogSync(dict(self.block), alert=self.alerts.append)
        for k, v in attrs.items():
            setattr(self.eng, k, v)
        BG.run(self.eng.start())
        want = {"pull_wavelog": "wavelog", "pull_lotw": "lotw", "pull_qrz": "qrz"}
        if self.block["timing"].get("pull_at_start", True):
            testkit.wait_for(lambda: all(self.eng.pulls_done.get(p, 0) >= 1 for p, d in want.items() if d in self.block),
                             timeout=10)

    def stop(self):
        if self.eng is not None and self.eng.started:
            BG.run(self.eng.stop())

    def close(self):
        self.stop()
        for r in self.runners:
            BG.run(r.cleanup())
        self.jtdx.close()

    def send(self, data):
        self.jtdx.send(data, ("127.0.0.1", self.port))

    def db(self, sql, *args):
        c = sqlite3.connect(self.block["ledger"], timeout=10)
        c.row_factory = sqlite3.Row
        try:
            return c.execute(sql, args).fetchall()
        finally:
            c.close()

    def state(self, call, dest, ts=None):
        rows = self.db("SELECT d.state FROM dest d JOIN qso q ON q.id=d.qso_id WHERE q.call=? AND d.dest=?"
                       + (" AND q.ts=?" if ts else "") + " ORDER BY q.ts", call, dest, *([ts] if ts else []))
        return [r["state"] for r in rows]

    def tqsl_runs(self):
        if not self.tqsl_log.exists():
            return []
        return [json.loads(x) for x in self.tqsl_log.read_text().splitlines() if x.strip()]

    def wl_posts(self, call=None):
        return [s for p, s in self.wl.calls if p == "qso" and (call is None or f">{call} " in s or f">{call}<" in s
                                                                 or s.find(f"<CALL:{len(call)}>{call}") >= 0)]


def ts(t):
    return t.strftime("%Y%m%d%H%M%S")


def done(h, call, t=None):
    want = {"wavelog": ["sent"], "qrz": ["sent"], "lotw": ["submitted"]}
    return all(h.state(call, d, ts(t) if t else None) == want[d] for d in ("wavelog", "qrz", "lotw") if d in h.block)


# ================================================================ protocol: a hand-built datagram

def u32(v):
    return struct.pack(">I", v)


def s8(text):
    return u32(len(text)) + text.encode()


jd = (dt.date(2026, 11, 13) - dt.date(1970, 1, 1)).days + 2440588
on_ms = (12 * 3600 + 3 * 60 + 15) * 1000 + 400
raw = (u32(0xADBCCBDA) + u32(2) + u32(5) + s8("JTDX")
       + struct.pack(">q", jd) + u32(on_ms + 75000) + b"\x01"          # time off, UTC
       + s8("N9ABC") + s8("EN50") + struct.pack(">Q", 21074000) + s8("FT4")
       + s8("-07") + s8("+02") + s8("50") + s8("") + s8("")
       + struct.pack(">q", jd) + u32(on_ms) + b"\x01"                   # time on
       + s8("") + s8("N0CALL") + s8("FN31"))
f = U.parse_logged(raw)
check("a hand-built QSO Logged parses: call, band, FT4 as MFSK/FT4, time to the second",
      (f.get("CALL"), f.get("BAND"), f.get("MODE"), f.get("SUBMODE"), f.get("QSO_DATE"), f.get("TIME_ON"), f.get("STATION_CALLSIGN")),
      ("N9ABC", "15m", "MFSK", "FT4", "20261113", "120315", "N0CALL"))
check("  and the fake JTDX builds the same bytes", J.logged(T0.replace(microsecond=400000), call="N9ABC", grid="EN50",
                                                             dial=21074000, mode="FT4", sent="-07", rcvd="+02",
                                                             t_off=T0.replace(microsecond=400000) + dt.timedelta(seconds=75)),
      raw)
check("header(): schema, type, id", U.header(J.reply()), (2, 4, "JTDX"))
check("a Logged ADIF parses to its one record", U.parse_adif_message(J.logged_adif(T0, mode="FT4", dial=21074000,
                                                                                  band="15m")).get("SUBMODE"), "FT4")
check("type 5 and type 12 of one QSO have one key",
      A.key(A.qso(U.parse_logged(J.logged(T0, mode="FT4", dial=21074000)))),
      A.key(A.qso(U.parse_adif_message(J.logged_adif(T0, mode="FT4", dial=21074000, band="15m")))))
for bad, why in (("<CALL:5>N9ABC <EOR>", "no <EOH>"), ("<EOH><CALL:5>N9A", "runs past"),
                 ("<EOH><CALL:5>N9ABC <BAND:3>20m", "no <EOR>"), ("<html><body>login</body></html>", "no <EOH>")):
    try:
        A.parse(bad, strict=True)
        check(f"strict ADIF refuses {why}", "accepted", "refused")
    except A.AdifError as e:
        check(f"strict ADIF refuses {why}", why in str(e), True)


# ================================================================ pairing

h = H()
h.send(J.logged(T0))
h.send(J.logged_adif(T0))
check("type 5 + 12: every destination done", testkit.wait_for(lambda: done(h, "N9ABC"), 15), True)
check("  one ledger row", len(h.db("SELECT * FROM qso")), 1)
check("  recorded from the type 12", h.db("SELECT source FROM qso")[0]["source"], "jtdx12")
check("  one send per destination", (len(h.wl_posts()), len(h.qrz.inserts), len(h.tqsl_runs())), (1, 1, 1))
check("  QRZ saw the identifiable User-Agent every time", h.qrz.bad_agents, 0)
check("  and never REPLACE", h.qrz.violations, [])
t1 = T0 + dt.timedelta(hours=1)
h.send(J.logged(t1, call="N9ABC"))
check("type 5 alone: recorded after the pair wait, sent everywhere", testkit.wait_for(lambda: done(h, "N9ABC", t1), 15), True)
check("  from the type 5", h.db("SELECT source FROM qso WHERE ts=?", ts(t1))[0]["source"], "jtdx5")
check("  one more send per destination", (len(h.wl_posts()), len(h.qrz.inserts), len(h.tqsl_runs())), (2, 2, 2))
argv = h.tqsl_runs()[0]["argv"]
check("TQSL argv: -x -d -a compliant -l LOCATION -u FILE, never -p",
      (argv[:7], argv[7].endswith("upload.adi"), "-p" in argv), (["-x", "-d", "-a", "compliant", "-l", "Home Station", "-u"],
                                                                 True, False))
check("  one QSO per live TQSL run", [len(J_r["adif"].split("<EOR>")) - 1 for J_r in h.tqsl_runs()], [1, 1])
check("the safety copy has both QSOs, written before the ledger",
      len((Path(h.block["ledger"]).parent / "logsync-safety.adi").read_text().splitlines()), 2)

# ---------------------------------------------------------------- identity: 90 s apart, and FT4
t2 = T0 + dt.timedelta(hours=2)
t3 = t2 + dt.timedelta(seconds=90)
h.send(J.logged_adif(t2, call="N9XYZ"))
h.send(J.logged_adif(t3, call="N9XYZ"))
check("two QSOs, one station, 90 s apart: two rows",
      testkit.wait_for(lambda: len(h.db("SELECT * FROM qso WHERE call='N9XYZ'")) == 2, 5), True)
check("  two sends to each destination",
      testkit.wait_for(lambda: (len(h.wl_posts("N9XYZ")), sum("N9XYZ" in a for a in h.qrz.inserts),
                                sum("N9XYZ" in r["adif"] for r in h.tqsl_runs())) == (2, 2, 2), 15), True)
check("  QRZ and LoTW have both", testkit.wait_for(lambda: (h.state("N9XYZ", "qrz"), h.state("N9XYZ", "lotw")) ==
                                                  (["sent", "sent"], ["submitted", "submitted"]), 10), True)
check("  Wavelog's 15-minute duplicate on the second is held, not counted as sent",
      h.state("N9XYZ", "wavelog"), ["sent", "held"])
check("  both flagged as near misses", [r["near"] for r in h.db("SELECT near FROM qso WHERE call='N9XYZ'")], [1, 1])
check("  and alerted", any(a.startswith("Wavelog refused N9XYZ") for a in h.alerts), True)

t4 = T0 + dt.timedelta(hours=3)
h.send(J.logged(t4, call="N9FT4", mode="FT4", dial=14080000))
h.send(J.logged_adif(t4, call="N9FT4", mode="FT4", dial=14080000))
check("FT4 type 5 + type 12: sent", testkit.wait_for(lambda: done(h, "N9FT4"), 15), True)
time.sleep(0.6)                       # past the pair wait: a type 5 that didn't pair would land now
check("FT4 as MODE=FT4 (type 5) and MFSK/FT4 (type 12): one row", len(h.db("SELECT * FROM qso WHERE call='N9FT4'")), 1)
check("  stored as MFSK/FT4", [tuple(r) for r in h.db("SELECT mode, submode FROM qso WHERE call='N9FT4'")], [("MFSK", "FT4")])
check("  one send per destination", (len(h.wl_posts("N9FT4")), sum("N9FT4" in a for a in h.qrz.inserts)), (1, 1))

# ---------------------------------------------------------------- revision
before = (len(h.wl_posts()), len(h.qrz.inserts), len(h.tqsl_runs()))
h.send(J.logged_adif(T0, rcvd="-03"))
check("a re-log with a changed report: a revision",
      testkit.wait_for(lambda: len(h.db("SELECT * FROM revision")) == 1, 5), True)
check("  every destination marked edited", [r["edited"] for r in h.db(
    "SELECT d.edited FROM dest d JOIN qso q ON q.id=d.qso_id WHERE q.ts=? AND q.call='N9ABC' ORDER BY d.dest", ts(T0))],
      [1, 1, 1])
time.sleep(1.0)
check("  nothing sent on its own", (len(h.wl_posts()), len(h.qrz.inserts), len(h.tqsl_runs())), before)
snap = BG.call(h.eng.status)
row = next(q for q in snap["recent"] if q["call"] == "N9ABC" and q["time"].endswith("12:03:15"))
check("  the page sees 'edited' per destination", [row["d"][d]["edited"] for d in ("wavelog", "qrz", "lotw")], [True, True, True])
qid = row["id"]
check("  LoTW refuses a correction, saying why", "no report or grid" in (BG.run(h.eng.correct(qid, "lotw")) or ""), True)
check("  Send correction to QRZ queues it", BG.run(h.eng.correct(qid, "qrz")), None)
check("  then QRZ gets the edited record",
      testkit.wait_for(lambda: len(h.qrz.inserts) == before[1] + 1 and "<RST_RCVD:3>-03" in h.qrz.inserts[-1], 5), True)
check("  QRZ kept the original (a duplicate): held, not sent",
      testkit.wait_for(lambda: h.state("N9ABC", "qrz", ts(T0)) == ["held"], 5), True)
check("  Wavelog untouched without its own Send correction", len(h.wl_posts()), before[0])
h.close()


# ================================================================ crash after 201, before "sent"

h = H(dests=("wavelog",), timing={"pull_at_start": False}, start=False)
h.block["timing"]["pull_at_start"] = True
h.start(crash_after_send="wavelog")
h.send(J.logged_adif(T0))
check("crash: Wavelog got the QSO", testkit.wait_for(lambda: len(h.wl.qsos) == 1, 10), True)
check("  the ledger still says attempting (the result was lost)",
      testkit.wait_for(lambda: h.state("N9ABC", "wavelog") == ["attempting"], 5), True)
h.stop()
h.block["timing"]["pull_at_start"] = False      # only the reconcile may find it
h.start()
check("  after the restart: reconciled to sent", testkit.wait_for(lambda: h.state("N9ABC", "wavelog") == ["sent"], 10), True)
check("  by the reconcile", h.db("SELECT d.detail FROM dest d")[0]["detail"], "found on reconcile")
check("  and never sent twice", len(h.wl_posts()), 1)
h.close()

# QRZ: the same lost result; the reconcile FETCHes by call and finds it.
h = H(dests=("qrz",), timing={"pull_at_start": False}, start=False)
h.start(crash_after_send="qrz")
h.send(J.logged_adif(T0))
check("QRZ crash: QRZ got the QSO", testkit.wait_for(lambda: len(h.qrz.inserts) == 1, 10), True)
check("  the ledger still says attempting (the result was lost)",
      testkit.wait_for(lambda: h.state("N9ABC", "qrz") == ["attempting"], 5), True)
h.stop()
h.start()
check("  after the restart: reconciled to sent", testkit.wait_for(lambda: h.state("N9ABC", "qrz") == ["sent"], 10), True)
check("  by the reconcile", h.db("SELECT d.detail FROM dest d")[0]["detail"], "found on reconcile")
check("  and never sent twice", len(h.qrz.inserts), 1)
h.close()

# QRZ already has the QSO (logged by hand, or by GridTracker in the parallel
# run): its FAIL "duplicate" counts as sent, never held or alerted.
from fake_wavelog import records as _wl_records  # noqa: E402
h = H(dests=("qrz",), timing={"pull_at_start": False}, start=False)
h.qrz.add(_wl_records("<EOH>" + J.adif_record(T0))[0])
h.start()
h.send(J.logged_adif(T0))
check("QRZ duplicate: one INSERT, answered duplicate", testkit.wait_for(lambda: len(h.qrz.inserts) == 1, 10), True)
check("  counts as sent", testkit.wait_for(lambda: h.state("N9ABC", "qrz") == ["sent"], 5), True)
check("  with no alert", h.alerts, [])
h.close()

# LoTW: a TQSL run whose result was lost. Uploaded -> the next qso_qsl=no pull
# finds it, no second run; never uploaded -> absent three pulls -> pending -> sent again.
for mode, want_runs in (({"mode": "upload"}, 1), ({"mode": "exit", "exit": 0}, 2)):
    h = H(dests=("lotw",), tqsl=mode, timing={"pull_at_start": False}, start=False)
    h.start(crash_after_send="lotw")
    h.send(J.logged_adif(T0))
    testkit.wait_for(lambda: len(h.tqsl_runs()) == 1 and h.state("N9ABC", "lotw") == ["attempting"], 10)
    h.stop()
    h.start()
    check(f"LoTW crash ({mode['mode']}): attempting -> unknown at the restart", h.state("N9ABC", "lotw"), ["unknown"])
    for _ in range(3):
        BG.run(h.eng.pull_lotw())
    if want_runs == 1:
        check("  LoTW lists it: accepted, no second TQSL run", (h.state("N9ABC", "lotw"), len(h.tqsl_runs())),
              (["accepted"], 1))
    else:
        check("  absent three pulls: sent again, no alert",
              (testkit.wait_for(lambda: len(h.tqsl_runs()) == 2 and h.state("N9ABC", "lotw") == ["submitted"], 10),
               h.alerts), (True, []))
    h.close()


# ================================================================ TQSL exit 9 on a backfill batch

h = H(dests=("wavelog", "lotw"), tqsl={"mode": "upload", "range_from": "20200101"}, start=False)
old = [dt.datetime(2025, 3, 1, 10, 0, 0), dt.datetime(2025, 3, 2, 11, 0, 0), dt.datetime(2019, 6, 1, 9, 0, 0)]
for i, t in enumerate(old):
    h.wl.add({"CALL": f"N9OL{i}", "BAND": "20m", "MODE": "FT8", "QSO_DATE": t.strftime("%Y%m%d"),
              "TIME_ON": t.strftime("%H%M%S"), "STATION_CALLSIGN": "N0CALL", "FREQ": "14.074000"})
h.start()
check("backfill import: QSOs held for LoTW, nothing uploaded",
      ([s for i in range(3) for s in h.state(f"N9OL{i}", "lotw")], h.tqsl_runs()), (["held_backfill"] * 3, []))
out = subprocess.run([sys.executable, str(ROOT / "ranch-radio"), "logsync", "backfill", "--ledger", h.block["ledger"]],
                     capture_output=True, text=True, timeout=30)
hsh = next(ln.split()[-1] for ln in out.stdout.splitlines() if ln.startswith("Hash:"))
out = subprocess.run([sys.executable, str(ROOT / "ranch-radio"), "logsync", "backfill", "--ledger", h.block["ledger"],
                      "--apply", hsh], capture_output=True, text=True, timeout=30)
check("  --apply HASH releases them", (out.returncode, "Released 3" in out.stdout), (0, True))
check("  one TQSL run for the batch, all three in it",
      testkit.wait_for(lambda: len(h.tqsl_runs()) == 1, 10) and h.tqsl_runs()[0]["adif"].count("<EOR>"), 3)
check("  exit 9: all three unknown (not submitted)",
      testkit.wait_for(lambda: [s for i in range(3) for s in h.state(f"N9OL{i}", "lotw")] == ["unknown"] * 3, 10), True)
for _ in range(3):
    BG.run(h.eng.pull_lotw())
check("  after three pulls: the two in range accepted", h.state("N9OL0", "lotw") + h.state("N9OL1", "lotw"),
      ["accepted", "accepted"])
check("  the out-of-range one held(not on LoTW)", [tuple(r) for r in h.db(
    "SELECT d.state, d.reason FROM dest d JOIN qso q ON q.id=d.qso_id WHERE q.call='N9OL2' AND d.dest='lotw'")],
      [("held", "not on LoTW")])
check("  with TQSL's reason", "out of range" in h.db(
    "SELECT d.detail FROM dest d JOIN qso q ON q.id=d.qso_id WHERE q.call='N9OL2' AND d.dest='lotw'")[0]["detail"], True)
h.close()


# ================================================================ TQSL exit 0, but LoTW never lists it

h = H(dests=("lotw",), tqsl={"mode": "exit", "exit": 0, "text": "Success"})
h.send(J.logged_adif(T0))
check("exit 0: submitted", testkit.wait_for(lambda: h.state("N9ABC", "lotw") == ["submitted"], 10), True)
BG.run(h.eng.pull_lotw())
BG.run(h.eng.pull_lotw())
check("  absent from two pulls: still submitted", h.state("N9ABC", "lotw"), ["submitted"])
BG.run(h.eng.pull_lotw())
check("  absent from three: held(not on LoTW)", [tuple(r) for r in h.db("SELECT state, reason FROM dest")],
      [("held", "not on LoTW")])
check("  and alerted", ([r["cls"] for r in h.db("SELECT cls FROM alert")], len(h.alerts)), (["not on LoTW"], 1))
h.close()


# ================================================================ TQSL hangs

h = H(tqsl={"mode": "hang"}, timing={"tqsl_timeout_s": 2.5})
h.send(J.logged_adif(T0))
check("TQSL hangs: Wavelog and QRZ sent meanwhile",
      testkit.wait_for(lambda: h.state("N9ABC", "wavelog") == ["sent"] and h.state("N9ABC", "qrz") == ["sent"], 2), True)
check("  while LoTW is still attempting", h.state("N9ABC", "lotw"), ["attempting"])
check("  killed at the deadline: held(tqsl hung)",
      testkit.wait_for(lambda: h.state("N9ABC", "lotw") == ["held"], 8)
      and h.db("SELECT reason FROM dest WHERE dest='lotw'")[0]["reason"], "tqsl hung")
run = h.tqsl_runs()[0]


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:                               # a zombie still answers kill 0: ask ps
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    except OSError:
        return False
    return st == "" or st.startswith("Z")


check("  TQSL and its child are gone (the group was killed)",
      testkit.wait_for(lambda: gone(run["pid"]) and gone(run["child"]), 5), True)
check("  alerted once", sum("TQSL hung" in a for a in h.alerts), 1)
h.send(J.logged_adif(t1))
check("  the next QSO: Wavelog and QRZ still send",
      testkit.wait_for(lambda: h.state("N9ABC", "wavelog", ts(t1)) == ["sent"] and h.state("N9ABC", "qrz", ts(t1)) == ["sent"], 5),
      True)
check("  LoTW holds it (no second hung TQSL)", (h.state("N9ABC", "lotw", ts(t1)), len(h.tqsl_runs())), (["pending"], 1))
t_stop = time.monotonic()
h.stop()
check("  shutdown is clean and quick", time.monotonic() - t_stop < 5, True)
h.close()

h = H(dests=("lotw",), tqsl={"mode": "stdin"})
h.send(J.logged_adif(T0))
check("a TQSL that waits on stdin gets EOF at once (exit 4: LoTW held, QSO pending)",
      testkit.wait_for(lambda: h.state("N9ABC", "lotw") == ["pending"] and "lotw" in h.eng.holds, 6), True)
check("  not killed by the deadline", any("hung" in a for a in h.alerts), False)
h.close()


# ================================================================ backfill

h = H(start=False)
qa, qb, qc = (dt.datetime(2024, 5, d, 14, 0, 0) for d in (1, 2, 3))


def rec(call, t):
    return {"CALL": call, "BAND": "20m", "MODE": "FT8", "QSO_DATE": t.strftime("%Y%m%d"), "TIME_ON": t.strftime("%H%M%S"),
            "STATION_CALLSIGN": "N0CALL", "FREQ": "14.074000"}


for call, t in (("N9AAA", qa), ("N9BBB", qb), ("N9CCC", qc)):
    h.wl.add(rec(call, t))
h.lotw.accept(rec("N9AAA", qa))                       # accepted, not confirmed
h.lotw.accept(rec("N9BBB", qb))
h.lotw.confirm("N9BBB", qb.strftime("%Y%m%d"), qb.strftime("%H%M%S"))
h.qrz.add(rec("N9AAA", qa))
h.start()
check("backfill: A on LoTW (accepted, unconfirmed)", h.state("N9AAA", "lotw"), ["accepted"])
check("  B confirmed", h.state("N9BBB", "lotw"), ["confirmed"])
check("  A on QRZ", h.state("N9AAA", "qrz"), ["sent"])
check("  the rest held_backfill", (h.state("N9BBB", "qrz"), h.state("N9CCC", "qrz"), h.state("N9CCC", "lotw")),
      (["held_backfill"], ["held_backfill"], ["held_backfill"]))
for _ in range(2):
    h.stop()
    h.start()
time.sleep(0.5)
check("  two restarts: still held, nothing sent",
      (h.state("N9CCC", "qrz"), h.state("N9CCC", "lotw"), len(h.qrz.inserts), len(h.tqsl_runs())),
      (["held_backfill"], ["held_backfill"], 0, 0))
hash1, text = logsync.report(h.block["ledger"])
check("  the report lists every held QSO and the hash", ("N9CCC" in text, "N9AAA" not in text.split("Held")[1].split("Near")[0],
                                                          hash1 in text), (True, True, True))
h.lotw.accept(rec("N9CCC", qc))                       # LoTW learns of C after the report
BG.run(h.eng.pull_lotw())
check("  C turns up on LoTW: accepted", h.state("N9CCC", "lotw"), ["accepted"])
out = subprocess.run([sys.executable, str(ROOT / "ranch-radio"), "logsync", "backfill", "--ledger", h.block["ledger"],
                      "--apply", hash1], capture_output=True, text=True, timeout=30)
check("  the old hash is stale: refused", (out.returncode, "stale" in out.stderr), (1, True))
check("  nothing released", (h.state("N9BBB", "qrz"), h.state("N9CCC", "qrz")), (["held_backfill"], ["held_backfill"]))
hash2, _ = logsync.report(h.block["ledger"])
check("  a new report, --apply its hash: released", logsync.apply(h.block["ledger"], hash2), 2)
check("  B and C go to QRZ", testkit.wait_for(lambda: h.state("N9BBB", "qrz") == ["sent"] and h.state("N9CCC", "qrz") == ["sent"], 10),
      True)
time.sleep(0.5)
check("  A (accepted, unconfirmed) never queued for LoTW, nothing re-sent to QRZ",
      (h.state("N9AAA", "lotw"), len(h.tqsl_runs()), sum("N9AAA" in a for a in h.qrz.inserts)), (["accepted"], 0, 0))
h.close()


# ================================================================ LoTW pull failures

h = H(dests=("lotw",), tqsl={"mode": "exit", "exit": 0})
h.lotw.accept(rec("N9OLD", qa), rx="2026-11-01 10:00:00")
BG.run(h.eng.pull_lotw())
cur = h.db("SELECT value FROM cursor WHERE name='lotw_qsorx'")[0]["value"]
h.send(J.logged_adif(T0))
testkit.wait_for(lambda: h.state("N9ABC", "lotw") == ["submitted"], 10)
h.lotw.html = True
check("LoTW's HTML login page: the pull fails", BG.run(h.eng.pull_lotw()), False)
check("  cursor unchanged", h.db("SELECT value FROM cursor WHERE name='lotw_qsorx'")[0]["value"], cur)
h.lotw.html, h.lotw.truncated = False, True
h.lotw.accept(rec("N9NEW", qb), rx="2026-11-12 10:00:00")
check("truncated ADIF: the pull fails", BG.run(h.eng.pull_lotw()), False)
check("  cursor unchanged", h.db("SELECT value FROM cursor WHERE name='lotw_qsorx'")[0]["value"], cur)
check("  no misses counted for the submitted QSO", h.db("SELECT misses FROM dest d JOIN qso q ON q.id=d.qso_id "
                                                      "WHERE q.call='N9ABC'")[0]["misses"], 0)
check("  the page sees the error class, no URL", "http" not in json.dumps(BG.call(h.eng.status)), True)
h.close()


# ================================================================ one destination down

h = H()
h.qrz.down = True
times = [T0 + dt.timedelta(hours=k) for k in (5, 6, 7)]
for t in times:
    h.send(J.logged_adif(t, call="N9DWN"))
check("QRZ down: Wavelog and LoTW keep sending",
      testkit.wait_for(lambda: h.state("N9DWN", "wavelog") == ["sent"] * 3 and h.state("N9DWN", "lotw") == ["submitted"] * 3, 15),
      True)
check("  QRZ has nothing yet", (len(h.qrz.log), set(h.state("N9DWN", "qrz")) <= {"pending", "attempting"}), (0, True))
h.qrz.down = False
check("  QRZ back: it catches up", testkit.wait_for(lambda: h.state("N9DWN", "qrz") == ["sent"] * 3, 15), True)
check("  in order", [A.parse("<EOH>" + a, strict=False)[1][0]["TIME_ON"] for a in h.qrz.inserts], [t.strftime("%H%M%S") for t in times])
h.close()


# ================================================================ multicast

def multicast_works():
    g, port = "239.255.%d.%d" % (os.getpid() % 250, 7), free_udp()
    try:
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rx.bind(("", port))
        rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, struct.pack("4s4s", socket.inet_aton(g),
                                                                              socket.inet_aton("127.0.0.1")))
        rx.settimeout(1)
        tx = J.FakeJtdx().multicast()
        tx.send(b"probe", (g, port))
        ok = rx.recvfrom(100)[0] == b"probe"
        rx.close()
        tx.close()
        return ok
    except OSError:
        return False


def listener(group, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, struct.pack("4s4s", socket.inet_aton(group),
                                                                          socket.inet_aton("127.0.0.1")))
    s.settimeout(0.3)
    return s


def drain(s):
    out = []
    while True:
        try:
            out.append(s.recvfrom(65536))
        except (TimeoutError, OSError):
            return out


if not multicast_works():
    print("SKIP multicast: this machine doesn't loop multicast back on 127.0.0.1 (the relay shape is tested below)")
else:
    group = "239.255.%d.%d" % (os.getpid() % 250, 1 + os.getpid() % 200)
    mport = free_udp()
    fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    fwd.bind(("127.0.0.1", 0))
    fwd.settimeout(0.3)
    gt = listener(group, mport)                       # GridTracker, beside logsync
    h = H(dests=("wavelog",), udp={"group": group, "port": mport, "iface": "127.0.0.1"},
          forward=[f"127.0.0.1:{fwd.getsockname()[1]}"])
    jt = J.FakeJtdx().multicast()
    for d in (J.heartbeat(), J.decode(), J.logged_adif(T0)):
        jt.send(d, (group, mport))
    check("multicast: logsync hears JTDX (the QSO is logged)", testkit.wait_for(lambda: h.state("N9ABC", "wavelog") == ["sent"], 10), True)
    seen = drain(gt)
    check("  GridTracker hears the same three, from JTDX's own address",
          ([J.type_of(d) for d, _ in seen], {a for _, a in seen}), ([0, 2, 12], {jt.addr}))
    gt.sendto(J.reply(), seen[0][1])                  # double-click: straight back to JTDX's socket
    got = []
    testkit.wait_for(lambda: bool(got.extend(jt.poll()) or got), 3)
    check("  GridTracker's Reply reaches JTDX", [J.type_of(d) for d, _ in got], [4])
    time.sleep(0.3)
    check("  logsync sent JTDX nothing", [J.type_of(d) for d, _ in jt.poll()], [4])
    fseen = drain(fwd)
    check("  the forward target gets JTDX's traffic one way", [J.type_of(d) for d, _ in fseen], [0, 2, 12])
    fwd.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
    fwd.sendto(J.decode(msg="FROM THE FEED"), (group, mport))     # the target talking back on the group
    time.sleep(0.4)
    check("  nothing from the forward target is forwarded back", drain(fwd), [])
    h.close()
    gt.close()
    fwd.close()
    jt.close()


# ================================================================ unicast relay

gt = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
gt.bind(("127.0.0.1", 0))
gt.settimeout(0.3)
fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
fwd.bind(("127.0.0.1", 0))
fwd.settimeout(0.3)
rogue = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rogue.bind(("127.0.0.1", 0))
port = free_udp()
h = H(dests=("wavelog",), udp={"bind": "127.0.0.1", "port": port, "relay": f"127.0.0.1:{gt.getsockname()[1]}"},
      forward=[f"127.0.0.1:{fwd.getsockname()[1]}"])
h.port = port
for d in (J.heartbeat(), J.decode()):
    h.send(d)
seen = []
testkit.wait_for(lambda: bool(seen.extend(drain(gt)) or len(seen) >= 2), 3)
check("relay: GridTracker gets JTDX's traffic", [J.type_of(d) for d, _ in seen], [0, 2])
up = seen[0][1]                                       # the client's own upstream socket
check("  from a socket of its own, not the intake port", up[1] != port, True)
gt.sendto(J.reply(), up)
got = []
testkit.wait_for(lambda: bool(got.extend(h.jtdx.poll()) or got), 3)
check("  GridTracker's Reply reaches JTDX", [(J.type_of(d), a[1]) for d, a in got], [(4, port)])
rogue.sendto(J.reply(), up)
rogue.sendto(J.reply(), ("127.0.0.1", port))
gt.sendto(J.reply(cid="WSJT-X"), up)                  # another client's id
gt.sendto(J.decode(), up)                             # not a reply type
gt.sendto(J.halt(), up)                               # Halt Tx: allowed
time.sleep(0.5)
check("  Reply from anyone else, another id or a non-reply type: dropped; Halt Tx passes",
      [J.type_of(d) for d, _ in h.jtdx.poll()[len(got):]], [8])
fseen = drain(fwd)
check("  the forward target gets JTDX's traffic", [J.type_of(d) for d, _ in fseen], [0, 2])
fwd.sendto(J.decode(msg="FROM THE FEED"), ("127.0.0.1", port))
fwd.sendto(J.reply(), ("127.0.0.1", port))
time.sleep(0.4)
check("  nothing from the forward target goes anywhere", (drain(fwd), drain(gt), len(h.jtdx.poll()) - len(got) - 1),
      ([], [], 0))
h.send(J.logged_adif(T0))
check("  a QSO through the relay is logged", testkit.wait_for(lambda: h.state("N9ABC", "wavelog") == ["sent"], 10), True)
check("  and GridTracker sees it too", [J.type_of(d) for d, _ in drain(gt)], [12])
intake = h.eng.intake
BG.call(lambda: intake.expire(BG.loop.time() + 301))
check("  a client silent 5 minutes is forgotten", len(intake.clients), 0)
h.close()
for s in (gt, fwd, rogue):
    s.close()


# ================================================================ secrets

LOG_LINES.clear()
h = H(tqsl={"mode": "upload"})
h.send(J.logged_adif(T0))
testkit.wait_for(lambda: done(h, "N9ABC"), 15)
h.lotw.accept(rec("N9OLD", qa))
BG.run(h.eng.pull_lotw())
BG.run(h.eng.pull_qrz())
BG.run(h.eng.pull_wavelog())
h.lotw.html = True
BG.run(h.eng.pull_lotw())
h.lotw.html = False
h.files["wavelog-key"].write_text("WRONG-key-value\n")
h.send(J.logged_adif(t1))
testkit.wait_for(lambda: "wavelog" in h.eng.holds, 10)
os.chmod(h.files["qrz-key"], 0o644)                   # too open: refused, named, never read
h.send(J.logged_adif(t2))
testkit.wait_for(lambda: "qrz" in h.eng.holds, 10)
snap = json.dumps(BG.call(h.eng.status))
h.stop()
ledger_bytes = b"".join(p.read_bytes() for p in Path(h.block["ledger"]).parent.iterdir() if p.is_file())
argvs = json.dumps([r["argv"] for r in h.tqsl_runs()])
leaky = [ln for ln in LOG_LINES if any(x in ln for x in (*SECRETS, "password=", "lotwreport.adi?"))]
if leaky:
    print("  leaking lines:", leaky[:3])
everything = {"log lines": "\n".join(LOG_LINES), "TQSL argv": argvs, "snapshot": snap,
              "alerts": "\n".join(h.alerts), "ledger and safety copy": ledger_bytes.decode("utf-8", "replace")}
for where, text in everything.items():
    check(f"no credential or LoTW URL in the {where}",
          [s for s in (*SECRETS, "WRONG-key-value", "password=", "lotwreport.adi?") if s in text], [])
check("  (the log did see the LoTW pulls and the holds)",
      (any("LoTW pull" in ln for ln in LOG_LINES), "wavelog" in snap and "auth" in snap), (True, True))
check("  the too-open key file is named in the alert", any("readable by others" in a for a in h.alerts), True)
check("scrub() takes a URL and a key out of an exception string",
      logsync.scrub(f"Cannot connect to https://lotw.arrl.org/lotwuser/lotwreport.adi?login=N0CALL&password={LOTW_PW} "
                    f"key={WL_KEY}", SECRETS), "Cannot connect to [URL] key=[REDACTED]")
h.close()


# ================================================================ station: a broken logsync is not fatal; the tab's switch

import aiohttp  # noqa: E402


def station_run(block, port_held=None):
    tmp = Path(tempfile.mkdtemp(prefix="logsync-station-"))
    web_p, radio_web, rig = testkit.free_port(), testkit.free_port(), testkit.free_port()
    radio = subprocess.Popen([sys.executable, "server.py", "--sim", "--rig", "ic7300", "--no-audio", "--host", "127.0.0.1",
                              "--listen", str(radio_web), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(rig)],
                             cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cfg = {"name": "test", "radios": [{"id": "ic7300", "name": "IC-7300", "web": radio_web, "rigctl": rig}],
           "addons": {"logbook": {"enabled": True, "url": "https://192.168.1.20:8087/"}},
           "addons_state": str(tmp / "addons.json"), "logsync": block}
    (tmp / "station.json").write_text(json.dumps(cfg))
    st = subprocess.Popen([sys.executable, "station.py", "--config", str(tmp / "station.json"), "--host", "127.0.0.1",
                           "--listen", str(web_p), "--rigctl-port", "0"],
                          cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return tmp, web_p, radio, st


async def hello_and(web_p, then=None, want=None):
    async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{web_p}/ws") as ws:
        first = json.loads((await ws.receive(timeout=10)).data)
        hello = first
        if want:
            end = time.monotonic() + 10
            while not want(hello) and time.monotonic() < end:
                await ws.close()
                await asyncio.sleep(0.3)
                async with s.ws_connect(f"http://127.0.0.1:{web_p}/ws") as ws2:
                    hello = json.loads((await ws2.receive(timeout=10)).data)
        if then:
            async with s.ws_connect(f"http://127.0.0.1:{web_p}/ws") as ws3:
                await ws3.receive(timeout=10)
                for m in then:
                    await ws3.send_str(json.dumps(m))
                await asyncio.sleep(0.5)
        return hello


def stop_proc(p):
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()


held = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
held.bind(("127.0.0.1", 0))                            # the port logsync wants is taken
tmp, web_p, radio, st = station_run({"udp": {"bind": "127.0.0.1", "port": held.getsockname()[1]},
                                     "ledger": str(Path(tempfile.mkdtemp()) / "l.db")})
def _port_open(p):
    try:
        socket.create_connection(("127.0.0.1", p), 0.2).close()
        return True
    except OSError:
        return False


check("station with a logsync that can't bind: the page is up", testkit.wait_for(lambda: _port_open(web_p), 30), True)
hello = BG.run(hello_and(web_p, want=lambda m: any(r["present"] for r in m.get("radios", []))))
check("  its radio is there", [(r["id"], r["present"]) for r in hello["radios"]], [("ic7300", True)])
check("  logsync reports why it isn't running",
      (hello["logsync"]["loaded"], hello["logsync"]["error"].startswith("did not start: OSError")), (False, True))
check("  the Logbook tab is on, its frame target intact, the reason riding along",
      (hello["addons"]["logbook"]["on"], hello["addons"]["logbook"]["url"], hello["addons"]["logbook"]["logsync"]["loaded"]),
      (True, "https://192.168.1.20:8087/", False))
stop_proc(st)
stop_proc(radio)
held.close()

tmp, web_p, radio, st = station_run({"udp": {"bind": "127.0.0.1", "port": "2237x"}})
check("a logsync block the station can't read: the page is up anyway", testkit.wait_for(lambda: _port_open(web_p), 30), True)
hello = BG.run(hello_and(web_p))
check("  logsync: not loaded, naming the field", hello["logsync"]["error"].startswith("not loaded: ValueError: logsync.udp.port"),
      True)
stop_proc(st)
stop_proc(radio)

lport = free_udp()
ldb = Path(tempfile.mkdtemp()) / "logsync.db"
tmp, web_p, radio, st = station_run({"udp": {"bind": "127.0.0.1", "port": lport}, "ledger": str(ldb), "call": "N0CALL"})
check("a working logsync in the station", testkit.wait_for(lambda: _port_open(web_p), 30), True)
hello = BG.run(hello_and(web_p, then=[{"t": "addon", "addon": "logbook", "do": "power", "on": False}]))
check("  its status in the hello and the Logbook snapshot",
      (hello["logsync"]["running"], hello["addons"]["logbook"]["logsync"]["intake"]), (True, "unicast"))
check("  the Logbook tab switched off", json.loads((tmp / "addons.json").read_text()).get("logbook"), False)
jt = J.FakeJtdx()
jt.send(J.logged_adif(T0), ("127.0.0.1", lport))


def logged_in_station():
    try:
        c = sqlite3.connect(ldb, timeout=5)
        try:
            return c.execute("SELECT call FROM qso").fetchall() == [("N9ABC",)]
        finally:
            c.close()
    except sqlite3.Error:
        return False


check("  logging carries on with the tab off", testkit.wait_for(logged_in_station, 10), True)
jt.close()
stop_proc(st)
out = st.stdout.read()
check("  the station stopped cleanly", "Traceback" in out, False)
stop_proc(radio)


# ================================================================ wiring: config, render, doctor, catalog, manifest

def refused(block):
    try:
        logsync.Config(block)
    except ValueError as e:
        return str(e)
    return None


base = {"udp": {"group": "224.0.0.1", "port": 2237}}
check("config: a good multicast block passes", refused(base), None)
check("  udp missing", refused({}).startswith("logsync.udp is required"), True)
check("  both group and bind", refused({"udp": {"group": "224.0.0.1", "bind": "127.0.0.1", "port": 2237}}).startswith("logsync.udp:"),
      True)
check("  a group that isn't multicast", "not a multicast" in refused({"udp": {"group": "192.168.1.5", "port": 2237}}), True)
check("  qrz without a call", "logsync.call is required" in refused({**base, "qrz": {"key_file": "~/k"}}), True)
check("  a plain-http LoTW URL off this machine",
      "must be https" in refused({**base, "lotw": {"tqsl": "t", "location": "L", "login": "N0CALL", "password_file": "p",
                                                    "url": "http://lotw.example/x"}}), True)
check("  lotw missing its password file", refused({**base, "lotw": {"tqsl": "t", "location": "L", "login": "N0CALL"}}),
      "logsync.lotw.password_file is required")

HEAVY = ("aiohttp", "sqlite3", "socket", "asyncio")
probe = ("import sys; before = set(sys.modules); {imp}; "
         "print(' '.join(sorted(m for m in {heavy!r} if m in sys.modules and m not in before)))")
loaded = {}
for name, imp in (("baseline", "pass"), ("logsync_config", "import logsync_config"),
                  ("logsync (the control)", "import logsync")):
    r = subprocess.run([sys.executable, "-c", probe.format(imp=imp, heavy=HEAVY)], cwd=ROOT, capture_output=True,
                       text=True, timeout=30)
    loaded[name] = (r.returncode, r.stdout.split())
check("logsync_config (what render and the CLI import) loads none of aiohttp, sqlite3, socket, asyncio",
      (loaded["baseline"], loaded["logsync_config"]), ((0, []), (0, [])))
check("  while logsync itself does (the probe can fail)", bool(loaded["logsync (the control)"][1]), True)
check("render.py and the CLI import logsync_config, never logsync",
      [(p, ln.strip()) for p in ("ranch_radio/render.py", "ranch_radio/cli.py")
       for ln in (ROOT / p).read_text().splitlines()
       if ln.strip().startswith(("import logsync", "from logsync")) and "logsync_config" not in ln], [])

from ranch_radio import catalog, doctor, manifest, render  # noqa: E402

ex = json.loads((ROOT / "examples/hosts/mac-shack.json").read_text())
check("the Mac example carries a logsync block", "logsync" in ex, True)
render.validate(ex)
sj = render.station_json(ex)
check("render: station.json gets the block, with the host's call when it names none",
      (sj["logsync"]["udp"], sj["logsync"].get("call")), (ex["logsync"]["udp"], ex.get("call", ex["logsync"].get("call"))))
bad = json.loads(json.dumps(ex))
bad["logsync"]["udp"]["group"] = "10.0.0.1"           # ranch-radio: allow (a refused, non-multicast value)
try:
    render.validate(bad)
    check("render refuses a bad logsync block", "rendered", "refused")
except render.HostError as e:
    check("render refuses a bad logsync block", "logsync.udp.group" in str(e), True)
bad = json.loads(json.dumps(ex))
bad["logsync"]["lotw"]["location"] = "Home; rm -rf /"
bad["logsync"]["lotw"]["tqsl"] = "tqsl --evil"
try:
    render.validate(bad)
    check("render refuses a TQSL path that isn't one word", "rendered", "refused")
except render.HostError as e:
    check("render refuses a TQSL path that isn't one word", "logsync.lotw.tqsl" in str(e), True)


def _paths(d, path=()):
    for k, v in (d.items() if isinstance(d, dict) else ()):
        yield path + (k,)
        yield from _paths(v, path + (k,))


# The radio server's codec save calls render.validate, so check_block must be
# total: any one field of the wrong type is a ValueError naming it, never a crash.
import logsync_config  # noqa: E402
escaped = []
for p in _paths(ex["logsync"]):
    for val in (None, 5, "x", [], {}, [1], True, 1.5, -1, {"a": 1}):
        b = json.loads(json.dumps(ex["logsync"]))
        t = b
        for k in p[:-1]:
            t = t[k]
        t[p[-1]] = val
        try:
            logsync_config.check_block(b)
        except ValueError:
            pass
        except Exception as e:
            escaped.append((".".join(p), repr(val), type(e).__name__))
check("check_block is total: a wrong type in any field is a ValueError, never a crash", escaped[:5], [])
check("catalog: logsync is a service on both OSes", (catalog.by_name()["logsync"]["kind"], catalog.by_name()["logsync"]["platforms"]),
      ("service", ["macos", "linux"]))
check("manifest: its block and template", (manifest.MANIFESTS["logsync"]["block"], manifest.template("logsync", "macos")["udp"]),
      (("logsync",), {"group": "224.0.0.1", "port": 2237}))

dtmp = Path(tempfile.mkdtemp(prefix="logsync-doctor-"))
tq = dtmp / "tqsl"
tq.write_text(f"#!/bin/sh\nexec {sys.executable} {ROOT / 'tests' / 'fake_tqsl.py'} \"$@\"\n")
os.chmod(tq, 0o755)
k1, k2 = dtmp / "wl", dtmp / "pw"
k1.write_text("x\n")
k2.write_text("y\n")
os.chmod(k1, 0o600)
os.chmod(k2, 0o644)
dport = free_udp()
checks = doctor.logsync_checks({"udp": {"bind": "127.0.0.1", "port": dport}, "wavelog": {"key_file": str(k1)},
                                "lotw": {"tqsl": str(tq), "password_file": str(k2)},
                                "qrz": {"key_file": str(dtmp / "missing")}})
got = {(c.status, c.what.split(":")[0].split(" (")[0]) for c in checks}
check("doctor: TQSL -v answers", ("ok", "TQSL -v") in got, True)
check("  a credential file with mode 600 passes, one readable by others doesn't, a missing one is missing",
      [c.status for c in checks if "credential file" in c.what], ["ok", "missing", "missing"])
check("  the certificate-password warn is there", any(c.status == "warn" and "no password" in c.what for c in checks), True)
check("  the UDP intake can bind", any(c.status == "ok" and f":{dport} is free" in c.what for c in checks), True)
check("  doctor never read a credential", any("y" == c.what.strip() for c in checks), False)
cli = subprocess.run([sys.executable, str(ROOT / "ranch-radio"), "logsync", "status", "--ledger", str(ldb)],
                     capture_output=True, text=True, timeout=30)
check("ranch-radio logsync status reads the ledger", (cli.returncode, "wavelog" in cli.stdout or "lotw" in cli.stdout or "qso" in cli.stdout.lower() or cli.stdout != ""),
      (0, True))

shutil.rmtree(dtmp, ignore_errors=True)
print(f"\n{'all ok' if not fails else f'{fails} FAILED'}", flush=True)
os._exit(1 if fails else 0)
