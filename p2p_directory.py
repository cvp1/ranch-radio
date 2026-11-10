"""The Winlink P2P directory, read-only: the stations that asked to be listed
for peer-to-peer Winlink, for the Peer picker (static/winlink.js).

The directory is a public static file (spec: SPEC.md in the p2p-directory
repo; schema 1). This module fetches it at most every refresh_hours, checks
every row against the spec's rules (a bad row is dropped and counted, never
fatal), keeps the last good copy on disk, and serves that copy with its age
when a fetch fails. Nothing here connects, keys a radio, or writes to the
directory: a picked row only fills the Peer fields, and Connect still goes
through check_peer and every lane gate.

Host file: winlink.directory. Absent: on, with the default URL. false: off.
An object: {"url": "...", "refresh_hours": 6}.
"""

import asyncio
import json
import logging
import math
import os
import re
import time

import aiohttp

log = logging.getLogger("p2p_directory")

DEFAULT_URL = "https://cvp1.github.io/p2p-directory/v1/directory.json"
DEFAULT_REFRESH_H = 6
MAX_BYTES = 512 * 1024
FETCH_TIMEOUT_S = 10
RETRY_S = 300            # after a failed fetch, the next try waits this long
CACHE_FILE = "p2p-directory.json"
SCHEMA = 1

# The spec's rules (p2p-directory SPEC.md, v1). Dial offsets follow Pat
# v1.0.0 app/freq.go:68-92: "fm" modes unshifted, PACTOR/ARDOP/VARA HF
# 1500 Hz below centre, packet unshifted; v1 defines no rule for Robust Packet.
MODES = {
    "VARA_HF": ("bw_hz", (500, 2300, 2750), -1500),
    "VARA_FM": ("bw_hz", (12500, 25000), 0),
    "ARDOP": ("bw_hz", (200, 500, 1000, 2000), -1500),
    "PACTOR": ("bw_hz", (500, 2400), -1500),
    "PACKET": ("baud", (300, 1200, 9600), 0),
    "ROBUST_PACKET": ("baud", (300, 600), None),
}
# Which via kind (static/winlink.js) can call a mode from this station; the
# rest are listed but dimmed with the reason.
MODE_KIND = {"VARA_HF": "varahf", "ARDOP": "ardop", "PACKET": "packet"}
CONTROLS = ("attended", "auto", "sked")
BANDS_KHZ = ((1800, 2000), (3500, 4000), (5250, 5450), (7000, 7300), (10100, 10150),
             (14000, 14350), (18068, 18168), (21000, 21450), (24890, 24990), (28000, 29700),
             (50000, 54000), (70000, 71000), (144000, 148000), (219000, 225000),
             (420000, 450000), (902000, 928000), (1240000, 1300000))
CALL_RE = re.compile(r"[A-Z0-9]{1,3}[0-9][A-Z]{1,4}")
GRID_RE = re.compile(r"[A-R]{2}[0-9]{2}([A-Xa-x]{2})?")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
MAX_ENTRIES, MAX_NOTE, MAX_HOURS = 12, 80, 40
MAX_STATIONS = 5000


class DirectoryError(ValueError):
    pass


def config(block):
    """(url, refresh_hours) from the host file's winlink.directory, or None when off."""
    if block is False:
        return None
    if block is None or block is True:
        return DEFAULT_URL, DEFAULT_REFRESH_H
    if not isinstance(block, dict):
        raise DirectoryError("winlink.directory must be false or an object")
    url = block.get("url", DEFAULT_URL)
    hours = block.get("refresh_hours", DEFAULT_REFRESH_H)
    if not isinstance(url, str) or not re.fullmatch(r"https://[^\s]+|http://(127\.0\.0\.1|localhost)(:\d+)?/[^\s]*", url):
        raise DirectoryError(f"winlink.directory.url = {url!r}: https://, or http:// on this machine")
    if not isinstance(hours, (int, float)) or isinstance(hours, bool) or not 1 <= hours <= 168:
        raise DirectoryError(f"winlink.directory.refresh_hours = {hours!r}: 1 to 168")
    return url, float(hours)


def dial_khz(centre_khz, mode):
    """The frequency to put on the rig (Pat's freq=), or None (no v1 rule)."""
    rule = MODES.get(mode)
    if rule is None or rule[2] is None:
        return None
    return round(centre_khz + rule[2] / 1000.0, 3)


def _text(v, cap):
    return isinstance(v, str) and len(v) <= cap and not CONTROL_CHARS.search(v)


def _entry(e):
    if not isinstance(e, dict):
        return None
    mode = e.get("mode")
    if mode not in MODES:
        return None
    field, allowed, _ = MODES[mode]
    khz = e.get("centre_khz")
    if not isinstance(khz, (int, float)) or isinstance(khz, bool) or not math.isfinite(khz):
        return None
    if not any(lo <= khz <= hi for lo, hi in BANDS_KHZ):
        return None
    width = e.get(field)
    if not isinstance(width, int) or isinstance(width, bool) or width not in allowed:
        return None
    if e.get("control") not in CONTROLS or not _text(e.get("hours"), MAX_HOURS):
        return None
    flags = e.get("flags", [])
    if not isinstance(flags, list) or not all(_text(f, 40) for f in flags) or len(flags) > 8:
        return None
    return {"centre_khz": khz, "mode": mode, field: width, "control": e["control"],
            "hours": e["hours"], "flags": list(flags), "dial_khz": dial_khz(khz, mode)}


def check_row(s):
    """A station row as published, checked; None if it breaks a rule."""
    if not isinstance(s, dict):
        return None
    call, grid, note = s.get("call"), s.get("grid"), s.get("note", "")
    if not isinstance(call, str) or not CALL_RE.fullmatch(call):
        return None
    if not isinstance(grid, str) or not GRID_RE.fullmatch(grid):
        return None
    if not _text(note, MAX_NOTE):
        return None
    confirmed = s.get("confirmed")
    if not isinstance(confirmed, str) or not DATE_RE.fullmatch(confirmed):
        return None
    entries = s.get("entries")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ENTRIES:
        return None
    checked = [_entry(e) for e in entries]
    if any(c is None for c in checked):
        return None
    return {"call": call, "grid": grid, "note": note, "confirmed": confirmed,
            "stale": s.get("stale") is True, "entries": checked}


def parse_doc(body):
    """(rows, dropped, generated) from the file's bytes; DirectoryError if
    the file as a whole can't be used (wrong schema, not JSON, too big)."""
    if len(body) > MAX_BYTES:
        raise DirectoryError(f"the directory is over {MAX_BYTES // 1024} kB")
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError) as e:
        raise DirectoryError(f"the directory is not JSON ({type(e).__name__})") from None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise DirectoryError(f"the directory's schema is {doc.get('schema') if isinstance(doc, dict) else None!r}, "
                             f"not {SCHEMA}")
    stations = doc.get("stations")
    if not isinstance(stations, list):
        raise DirectoryError("the directory has no station list")
    rows, dropped = [], 0
    for s in stations[:MAX_STATIONS]:
        r = check_row(s)
        if r is None:
            dropped += 1
        else:
            rows.append(r)
    dropped += max(0, len(stations) - MAX_STATIONS)
    generated = doc.get("generated") if isinstance(doc.get("generated"), str) else None
    return rows, dropped, generated


def grid_latlon(grid):
    g = grid.upper()
    lon = (ord(g[0]) - 65) * 20 - 180 + int(g[2]) * 2
    lat = (ord(g[1]) - 65) * 10 - 90 + int(g[3])
    if len(g) >= 6:
        lon += (ord(g[4]) - 65) * (2 / 24) + 1 / 24
        lat += (ord(g[5]) - 65) / 24 + 1 / 48
    else:
        lon, lat = lon + 1, lat + 0.5
    return lat, lon


def distance_km(grid_a, grid_b):
    if not (isinstance(grid_a, str) and GRID_RE.fullmatch(grid_a) and GRID_RE.fullmatch(grid_b)):
        return None
    (la1, lo1), (la2, lo2) = grid_latlon(grid_a), grid_latlon(grid_b)
    la1, lo1, la2, lo2 = map(math.radians, (la1, lo1, la2, lo2))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return round(2 * 6371 * math.asin(math.sqrt(h)))


class Directory:
    """One per station. view() is what the route returns."""

    def __init__(self, url, refresh_hours, cache_path=None, clock=time.time):
        self.url, self.refresh_s = url, refresh_hours * 3600
        self.cache_path = cache_path
        self.clock = clock
        self.rows, self.dropped, self.generated = [], 0, None
        self.fetched = None          # wall time of the copy we hold
        self.error = None            # why the last fetch failed (None after a good one)
        self._tried = None           # wall time of the last attempt
        self._lock = asyncio.Lock()
        self._load_cache()

    def _load_cache(self):
        if not self.cache_path:
            return
        try:
            with open(self.cache_path, "rb") as f:
                saved = json.loads(f.read())
            rows, dropped, generated = parse_doc(json.dumps(saved["doc"]).encode())
            fetched = float(saved["fetched"])
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError, DirectoryError) as e:
            log.warning("p2p directory: cached copy unusable (%s); fetching afresh", e)
            return
        self.rows, self.dropped, self.generated, self.fetched = rows, dropped, generated, fetched

    def _save_cache(self, doc):
        if not self.cache_path:
            return
        tmp = self.cache_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
            with open(tmp, "w") as f:
                json.dump({"fetched": self.fetched, "url": self.url, "doc": doc}, f)
            os.replace(tmp, self.cache_path)
        except OSError as e:
            log.warning("p2p directory: can't keep a copy at %s (%s)", self.cache_path, e)

    def due(self):
        now = self.clock()
        if self._tried is not None and self.error and now - self._tried < RETRY_S:
            return False
        return self.fetched is None or now - self.fetched >= self.refresh_s

    async def refresh(self, session, force=False):
        """Fetch if due (or forced, still at most once per RETRY_S after a failure)."""
        async with self._lock:
            if not force and not self.due():
                return
            if force and self._tried is not None and self.clock() - self._tried < 10:
                return
            self._tried = self.clock()
            try:
                timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_S)
                async with session.get(self.url, timeout=timeout,
                                       headers={"Accept": "application/json"}) as r:
                    if r.status != 200:
                        raise DirectoryError(f"the directory answered HTTP {r.status}")
                    # read(n) may return less than n: read to EOF or one byte past the cap
                    body = b""
                    while len(body) <= MAX_BYTES:
                        chunk = await r.content.read(MAX_BYTES + 1 - len(body))
                        if not chunk:
                            break
                        body += chunk
                rows, dropped, generated = parse_doc(body)
            except (DirectoryError, aiohttp.ClientError, TimeoutError, OSError) as e:
                self.error = str(e) or type(e).__name__
                log.info("p2p directory: fetch failed: %s", self.error)
                return
            self.rows, self.dropped, self.generated = rows, dropped, generated
            self.fetched, self.error = self.clock(), None
            self._save_cache(json.loads(body))
            if dropped:
                log.info("p2p directory: %d row(s) dropped as malformed", dropped)

    def view(self, here_grid=None, mycall=None):
        now = self.clock()
        base = mycall.split("-")[0].upper() if isinstance(mycall, str) and mycall else None
        rows = []
        for r in self.rows:
            rows.append({**r, "distance_km": distance_km(here_grid, r["grid"]),
                         "self": r["call"] == base})
        age = None if self.fetched is None else max(0, int(now - self.fetched))
        return {"enabled": True, "url": self.url, "rows": rows, "dropped": self.dropped,
                "generated": self.generated, "age_s": age,
                "stale": age is not None and age > self.refresh_s * 2,
                "error": self.error,
                "reason": None if self.fetched is not None else
                          (f"No copy of the directory yet: {self.error}" if self.error
                           else "Fetching the directory…")}
