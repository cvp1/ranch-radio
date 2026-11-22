"""`ranch-radio backup`: one archive, mode 600, with a manifest whose SHA-256
sums verify; the host file, station.json, the state directory's *.json and
*.db, and the hub's APRS store and transmit state (Off and holds). A live
WAL database is copied whole: rows still in its -wal file are in the backup
(a file copy of the .db alone misses them, checked here as the control).
Pat's config (the Winlink password) and logs are left out, and it says so.
A tampered archive does not verify.

    python3 tests/backup_test.py
"""
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ranch_radio import backup  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


home = pathlib.Path(tempfile.mkdtemp(prefix="backup-home-"))
repo = home / "ranch-radio"
(repo / "packet-logs").mkdir(parents=True)
hosts = home / ".config/ranch-radio/hosts"
hosts.mkdir(parents=True)
h = json.loads((ROOT / "examples/hosts/linux-station.json").read_text())
h["repo"] = str(repo)
(hosts / "linux-station.json").write_text(json.dumps(h))
(home / ".config/radio-remote").mkdir(parents=True)
(home / ".config/radio-remote/station.json").write_text('{"name": "Test Station"}')
state = home / ".local/state/radio-remote"
(state / "ardop").mkdir(parents=True)
(state / "addons.json").write_text('{"mesh": true}')
(state / "ardop" / "ARDOPDebug.log").write_text("a log\n" * 1000)
(repo / "packet-logs" / "tx-state.json").write_text('{"inhibit": true, "holds": ["N9ABC"]}')
(repo / "packet-logs" / "rx.jsonl").write_text("{}\n" * 1000)
(home / ".config/pat").mkdir(parents=True)
(home / ".config/pat/config.json").write_text('{"secure_login_password": "hunter2-not-real"}')

# A live WAL database with rows that are only in its -wal file: the writer
# stays open and autocheckpoint is off.
ledger = state / "logsync.db"
w = sqlite3.connect(ledger)
w.execute("PRAGMA journal_mode=WAL")
w.execute("PRAGMA wal_autocheckpoint=0")
w.execute("CREATE TABLE q (id INTEGER PRIMARY KEY, call TEXT)")
w.executemany("INSERT INTO q (call) VALUES (?)", [(f"N{i}CALL",) for i in range(500)])
w.commit()
aprs = repo / "packet-logs" / "aprs.db"
a = sqlite3.connect(aprs)
a.execute("PRAGMA journal_mode=WAL")
a.execute("PRAGMA wal_autocheckpoint=0")
a.execute("CREATE TABLE heard (call TEXT)")
a.executemany("INSERT INTO heard VALUES (?)", [(f"K{i}XYZ",) for i in range(200)])
a.commit()

# The control: a plain copy of the .db file alone loses the WAL's rows.
raw = home / "raw-copy.db"
shutil.copyfile(ledger, raw)
try:
    n_raw = sqlite3.connect(raw).execute("SELECT count(*) FROM q").fetchone()[0]
except sqlite3.DatabaseError:
    n_raw = 0
check("control: a file copy of the live .db misses the WAL rows", n_raw < 500, True)

out = home / "out" / "bk.tar.gz"
env = dict(os.environ, HOME=str(home), RANCH_RADIO_HOSTS=str(hosts))
p = subprocess.run([sys.executable, str(ROOT / "ranch-radio"), "backup", str(out), "--host", "linux-station",
                    "--home", str(home)], capture_output=True, text=True, env=env, timeout=120)
check("backup ran", p.returncode, 0)
check("  the archive is mode 600", oct(out.stat().st_mode & 0o777), "0o600")
check("  no .partial left behind", list((home / "out").glob("*.partial")), [])
check("  it says what it left out", "Winlink password" in p.stdout and "logs" in p.stdout, True)
with tarfile.open(out) as tar:
    names = sorted(tar.getnames())
    m = json.load(tar.extractfile("manifest.json"))
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="backup-x-"))
    tar.extractall(tmp, filter="data")
check("  what is in it", names, ["config/station.json", "hosts/linux-station.json", "manifest.json", "packet/aprs.db",
                                 "packet/tx-state.json", "state/addons.json", "state/logsync.db"])
check("  the hub's Off and holds", json.loads((tmp / "packet/tx-state.json").read_text()), {"inhibit": True, "holds": ["N9ABC"]})
check("  every ledger row, the WAL's too", sqlite3.connect(tmp / "state/logsync.db").execute("SELECT count(*) FROM q").fetchone()[0],
      500)
check("  every APRS row", sqlite3.connect(tmp / "packet/aprs.db").execute("SELECT count(*) FROM heard").fetchone()[0], 200)
check("  no Pat password anywhere", any(b"hunter2" in (tmp / n).read_bytes() for n in names if (tmp / n).is_file()), False)
check("  no logs", any(n.endswith((".log", ".jsonl")) for n in names), False)
check("  the manifest verifies", backup.verify(out), (True, []))
check("  the live databases are untouched (the writer still sees 500)", w.execute("SELECT count(*) FROM q").fetchone()[0], 500)

# Tamper with one member: verify must say so.
bad = home / "bad.tar.gz"
with tarfile.open(out) as src, tarfile.open(bad, "w:gz") as dst:
    for ti in src.getmembers():
        data = src.extractfile(ti).read()
        if ti.name == "state/addons.json":
            data = b'{"mesh": false}'
            ti.size = len(data)
        import io
        dst.addfile(ti, io.BytesIO(data))
ok, problems = backup.verify(bad)
check("a tampered archive does not verify", (ok, problems), (False, ["state/addons.json: checksum differs"]))
w.close()
a.close()

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
