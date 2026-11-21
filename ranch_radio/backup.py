"""`ranch-radio backup [OUT]`: one archive of what a host would lose with its
disk. Read-only: nothing is stopped, moved or changed. (Restore is not here:
putting state back under running servers needs writer exclusion and
recovery from a half-done copy first; review plan PR 2, panel 2026-11-21.)

In it:
  - the host file (~/.config/ranch-radio/hosts/<host>.json), which holds the
    saved per-band TX levels too (codec_save)
  - the station's rendered config (~/.config/radio-remote/station.json)
  - state: every *.json and *.db under ~/.local/state/radio-remote (add-on
    switches, the logbook ledger, Mesh conversations, ...)
  - the packet hub's APRS store and its transmit state (Off and holds:
    tx-state.json; losing it would let --tx enable transmit at the next start)

Databases are copied with SQLite's backup API, never as files: they run in
WAL mode, and a file copy misses what is still in the -wal file. Pat's
config is left out (it holds the Winlink password); so are logs. The
archive is created mode 0600 with a manifest.json of SHA-256 sums.
"""
import hashlib
import io
import json
import os
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path

STATE_DIR = "~/.local/state/radio-remote"
STATION_JSON = "~/.config/radio-remote/station.json"
LEFT_OUT = ("Pat's config (it holds the Winlink password: back it up yourself, encrypted)",
            "logs (server.log, packet-logs/*.jsonl, modem logs)")


def _db_bytes(path):
    """A consistent copy of a live SQLite database, as bytes."""
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "copy.db"
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        dst = sqlite3.connect(out)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return out.read_bytes()


def collect(h, host_file, home=None):
    """[(archive name, source path, kind)] that exist on this machine."""
    home = Path(home or os.path.expanduser("~"))
    ex = lambda p: Path(str(p).replace("~", str(home), 1))  # noqa: E731
    out = []
    hf = Path(host_file)
    if hf.exists():
        out.append((f"hosts/{h['host']}.json", hf, "file"))
    st = ex(STATION_JSON)
    if st.exists():
        out.append(("config/station.json", st, "file"))
    sd = ex(STATE_DIR)
    if sd.is_dir():
        for p in sorted(sd.rglob("*")):
            if p.is_file() and p.suffix in (".json", ".db"):
                out.append((f"state/{p.relative_to(sd)}", p, "db" if p.suffix == ".db" else "file"))
    if h.get("packet"):
        repo = ex(h["repo"]) if str(h["repo"]).startswith("~") else Path(h["repo"])
        for name, kind in (("aprs.db", "db"), ("tx-state.json", "file")):
            p = repo / "packet-logs" / name
            if p.exists():
                out.append((f"packet/{name}", p, kind))
    return out


def backup(h, host_file, out_path, home=None):
    """Write the archive; returns the manifest."""
    items = collect(h, host_file, home)
    manifest = {"format": 1, "host": h["host"], "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "files": [], "left_out": list(LEFT_OUT)}
    blobs = []
    for name, src, kind in items:
        data = _db_bytes(src) if kind == "db" else src.read_bytes()
        blobs.append((name, data))
        manifest["files"].append({"name": name, "source": str(src), "kind": kind, "bytes": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest()})
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".partial")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # never readable by others, even briefly
    try:
        with os.fdopen(fd, "wb") as f, tarfile.open(fileobj=f, mode="w:gz") as tar:
            def add(name, data):
                ti = tarfile.TarInfo(name)
                ti.size, ti.mtime, ti.mode = len(data), int(time.time()), 0o600
                tar.addfile(ti, io.BytesIO(data))
            add("manifest.json", json.dumps(manifest, indent=2).encode())
            for name, data in blobs:
                add(name, data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, out_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return manifest


def verify(path):
    """(ok, problems): every file the manifest names is there with its sum."""
    problems = []
    with tarfile.open(path, "r:gz") as tar:
        m = json.load(tar.extractfile("manifest.json"))
        for f in m["files"]:
            try:
                data = tar.extractfile(f["name"]).read()
            except (KeyError, AttributeError):
                problems.append(f"{f['name']}: missing")
                continue
            if hashlib.sha256(data).hexdigest() != f["sha256"]:
                problems.append(f"{f['name']}: checksum differs")
    return not problems, problems
