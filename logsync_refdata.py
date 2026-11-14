"""Reference data the needs index uses, fetched and cached beside the ledger.
Stdlib only.

    cty         country-files.com cty.csv (callsign prefix -> DXCC entity),
                refreshed weekly
    lotw_users  ARRL's lotw-user-activity.csv (who uploads to LoTW, and
                when), refreshed daily

ensure(name, state_dir) returns (path or None, note). A copy younger than its
max age is used as is. An older or missing one is fetched (bounded in time
and size), checked (cty must parse; the user list must have rows with dates)
and swapped in atomically. If the fetch or the check fails, the cached copy
is used and the note says so, loudly; with no copy at all it returns None and
the caller runs without that data (the roster shows needs as unknown).

RANCH_RADIO_OFFLINE=1 in the environment: never fetch, the cache only (the
test suites set it, so no test reaches the internet).
"""
import os
import time
import urllib.request

SOURCES = {
    "cty": {"url": "https://www.country-files.com/cty/cty.csv", "file": "cty.csv",
            "max_age_s": 7 * 86400, "max_bytes": 4 << 20},
    "lotw_users": {"url": "https://lotw.arrl.org/lotw-user-activity.csv", "file": "lotw-user-activity.csv",
                   "max_age_s": 86400, "max_bytes": 64 << 20},
}
TIMEOUT_S = 60
USER_AGENT = "RanchRadio-logsync/1 (+https://github.com/cvp1/ranch-radio)"


def _check(name, data):
    text = data.decode("utf-8", "replace")
    if name == "cty":
        from logsync_cty import Cty
        Cty(text)                             # raises on anything that isn't a cty.csv
    else:
        rows = [ln for ln in text.splitlines()[:2000] if ln.count(",") >= 2 and ln.split(",")[1][:2] in ("19", "20")]
        if len(rows) < 100:
            raise ValueError("not a LoTW user list")
    return text


def _fetch(url, max_bytes):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        data = r.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"larger than {max_bytes} bytes")
    return data


def ensure(name, state_dir, fetch=_fetch, now=time.time, sources=SOURCES):
    s = sources[name]
    path = os.path.join(os.path.expanduser(state_dir), s["file"])
    try:
        age = now() - os.stat(path).st_mtime
    except OSError:
        age = None
    if age is not None and age < s["max_age_s"]:
        return path, ""
    if os.environ.get("RANCH_RADIO_OFFLINE") == "1":
        return (path if age is not None else None), f"{name}: offline (RANCH_RADIO_OFFLINE), no fetch"
    try:
        data = fetch(s["url"], s["max_bytes"])
        _check(name, data)
    except Exception as e:                    # any failure: keep what we have
        why = f"{name}: refresh failed ({type(e).__name__})"
        if age is None:
            return None, why + "; no cached copy"
        return path, why + f"; using the copy from {int(age // 86400)} day(s) ago"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path, ""


def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()
