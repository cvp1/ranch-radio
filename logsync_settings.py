"""The Logbook tab's settings for the logbook sync (logsync.py): its paths,
URLs and credentials, set from the page.

Two kinds of setting, kept apart on purpose:

  fields    the host file's logsync block (URLs, the Wavelog station profile,
            TQSL's path and station location, the LoTW login, where each
            credential file lives). The host file stays the one place they
            are declared: a change is planned (the page shows the host-file
            diff), then applied under the host-file lock, then station.json is
            rewritten from render's own output (so `ranch-radio drift` stays
            clean) and the station restarts the sync in place. The radios are
            never touched.

  secrets   the three credentials (Wavelog API key, QRZ logbook key, LoTW
            password). Write-only: a value goes from the page into its file
            (mode 600, written atomically) and is never read back out. The
            page sees only whether it is set, its length and when it changed.
            The sync reads each file at the point of use, so a new key takes
            effect on the next send, and a destination held for a bad key
            sends again as soon as its file changes.

  tests     per destination, a live read-only check of what is set:
            Wavelog check_auth + station_info (the profile exists), QRZ
            ACTION=STATUS, a one-day LoTW query, TQSL -v and its station
            location. Results carry the class of a failure, never a URL
            (LoTW's carries the password) or a credential.
"""
import copy
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import secrets as _secrets
import time

FIELDS = (
    # (path in the logsync block, label, kind)
    (("wavelog", "url"), "Wavelog address", "url"),
    (("wavelog", "station_profile_id"), "Wavelog station profile", "int"),
    (("wavelog", "key_file"), "Wavelog API key file", "path"),
    (("qrz", "key_file"), "QRZ API key file", "path"),
    (("lotw", "tqsl"), "TQSL program", "path"),
    (("lotw", "location"), "TQSL station location", "text"),
    (("lotw", "login"), "LoTW login (callsign)", "text"),
    (("lotw", "password_file"), "LoTW password file", "path"),
)
SECRETS = {"wavelog": ("wavelog", "key_file"), "qrz": ("qrz", "key_file"), "lotw": ("lotw", "password_file")}
SECRET_LABEL = {"wavelog": "Wavelog API key", "qrz": "QRZ logbook API key", "lotw": "LoTW password"}
MAX_SECRET = 256
PLAN_TTL_S = 600
TEST_TIMEOUT_S = 20


class SettingsError(ValueError):
    pass


def dotted(path):
    return ".".join(path)


def _get(block, path):
    d = block
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def view(block, tqsl_home=None):
    """What the page may see: every field's value and each credential's
    state. Never a credential's value."""
    out = {"fields": [], "secrets": {}, "locations": tqsl_locations(tqsl_home)}
    for path, label, kind in FIELDS:
        if path[0] in block:
            out["fields"].append({"key": dotted(path), "label": label, "kind": kind, "value": _get(block, path)})
    for dest, path in SECRETS.items():
        f = _get(block, path)
        if f is not None:
            out["secrets"][dest] = dict(secret_state(f), label=SECRET_LABEL[dest])
    return out


def secret_state(path):
    """{"set", "chars", "updated", "mode_ok"} for a credential file; reads its
    length, never prints its value."""
    p = os.path.expanduser(str(path))
    try:
        st = os.stat(p)
        with open(p, "rb") as f:
            n = len(f.read(MAX_SECRET + 2).strip())
    except OSError:
        return {"set": False, "file": str(path)}
    return {"set": n > 0, "chars": n, "file": str(path), "mode_ok": (st.st_mode & 0o077) == 0,
            "updated": dt.datetime.fromtimestamp(st.st_mtime, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}


def write_secret(block, dest, value):
    """Write a credential to the file the block names for dest: atomically,
    mode 600, its directory 700 if this creates it. Returns its state."""
    if dest not in SECRETS:
        raise SettingsError(f"no credential called {dest!r}")
    path = _get(block, SECRETS[dest])
    if not path:
        raise SettingsError(f"logsync.{dotted(SECRETS[dest])} is not set: set the file first")
    if not isinstance(value, str):
        raise SettingsError("a credential is text")
    v = value.strip()
    if not v:
        raise SettingsError("empty: nothing written")
    if len(v) > MAX_SECRET or re.search(r"[\s\x00-\x1f\x7f]", v):
        raise SettingsError(f"{SECRET_LABEL[dest]}: one line, no spaces, at most {MAX_SECRET} characters")
    p = os.path.expanduser(str(path))
    d = os.path.dirname(p) or "."
    if not os.path.isdir(d):
        os.makedirs(d, mode=0o700)
    tmp = os.path.join(d, f".{os.path.basename(p)}.{os.getpid()}.{_secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(v + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return secret_state(path)


def tqsl_locations(home=None):
    """TQSL's station location names (~/.tqsl/station_data), [] if none."""
    p = os.path.join(home or os.path.expanduser("~"), ".tqsl", "station_data")
    try:
        with open(p, errors="replace") as f:
            text = f.read(1 << 20)
    except OSError:
        return []
    return sorted(set(re.findall(r'<StationData\s+name="([^"]*)"', text)))


# ---------------------------------------------------------------- plan / apply

def _coerce(kind, label, v):
    if kind == "int":
        if isinstance(v, bool):
            raise SettingsError(f"{label}: a number")
        try:
            return int(str(v).strip())
        except ValueError:
            raise SettingsError(f"{label}: a number") from None
    if not isinstance(v, str):
        raise SettingsError(f"{label}: text")
    v = v.strip()
    if not v:
        raise SettingsError(f"{label}: can't be empty")
    if re.search(r"[\x00-\x1f\x7f]", v):
        raise SettingsError(f"{label}: one line")
    return v


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Plans:
    """Planned host-file edits, each bound to the host file's text it was made
    from (an apply after another edit is refused) and expiring unapplied."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.plans = {}

    def make(self, host_path, changes, check):
        """changes: {"wavelog.url": value, ...}. check(host_dict) raises
        ValueError on a block the sync or render would refuse. Returns the
        plan's page view: {"id", "diff", "changes"}."""
        from ranch_radio import hostfile
        kinds = {dotted(p): (p, label, kind) for p, label, kind in FIELDS}
        if not isinstance(changes, dict) or not changes:
            raise SettingsError("nothing to change")
        with open(host_path) as f:
            text = f.read()
        h = json.loads(text)
        block = h.get("logsync")
        if not isinstance(block, dict):
            raise SettingsError("this host file has no logsync block (ranch-radio add logsync)")
        new_text, new_h, done = text, copy.deepcopy(h), []
        for k, v in changes.items():
            if k not in kinds:
                raise SettingsError(f"{k!r} is not a setting here")
            path, label, kind = kinds[k]
            v = _coerce(kind, label, v)
            if _get(block, path) == v:
                continue
            try:
                new_text = hostfile.put(new_text, ("logsync",) + path, v)
            except hostfile.EditError as e:
                raise SettingsError(f"{label}: {e}") from None
            new_h = json.loads(new_text)
            done.append({"key": k, "label": label, "from": _get(block, path), "to": v})
        if not done:
            raise SettingsError("those are the settings already")
        try:
            check(new_h)
        except ValueError as e:
            raise SettingsError(str(e)) from None
        pid = _secrets.token_hex(6)
        diff = "".join(difflib.unified_diff(text.splitlines(True), new_text.splitlines(True),
                                            os.path.basename(host_path), os.path.basename(host_path), n=1))
        self._expire()
        self.plans[pid] = {"path": str(host_path), "base": sha(text), "new": new_text, "at": self.clock()}
        return {"id": pid, "diff": diff, "changes": done}

    def _expire(self):
        now = self.clock()
        for k in [k for k, p in self.plans.items() if now - p["at"] > PLAN_TTL_S]:
            del self.plans[k]

    def apply(self, pid):
        """Write the plan's host file under the host-file lock. Returns the new
        host dict. The plan is spent either way."""
        from ranch_radio import hostlock
        self._expire()
        p = self.plans.pop(pid, None)
        if p is None:
            raise SettingsError("that plan expired or was already applied: plan it again")
        path = p["path"]
        with hostlock.locked(os.path.dirname(path)):
            with open(path) as f:
                now = f.read()
            if sha(now) != p["base"]:
                raise SettingsError("the host file changed since this plan was made: plan it again")
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                f.write(p["new"])
            os.replace(tmp, path)
        return json.loads(p["new"])


def write_station_json(h, home=None):
    """Rewrite the installed station.json from render's own artifact for h
    (what `ranch-radio install` would write), so drift stays clean. Returns
    the new logsync block."""
    from ranch_radio import render
    home = home or os.path.expanduser("~")
    art = [a for a in render.artifacts(h, home) if a.path.endswith("station.json")]
    if len(art) != 1:
        raise SettingsError("render gives no station.json for this host")
    a = art[0]
    dest = os.path.join(home, a.path) if not os.path.isabs(a.path) else a.path
    tmp = f"{dest}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(a.content, f, indent=2)
        f.write("\n")
    os.replace(tmp, dest)
    return a.content.get("logsync")


# ---------------------------------------------------------------- live tests

def _read(path):
    with open(os.path.expanduser(str(path))) as f:
        return f.read().strip()


async def test(dest, block, http, scrub, tqsl_home=None):
    """A live, read-only check of one destination's settings. -> {"dest",
    "ok", "msg"}; msg is scrubbed here (no URL, no credential), whoever calls."""
    seen = []
    r = await _test(dest, block, http, tqsl_home, seen)
    r["msg"] = scrub(r["msg"], seen, cap=300)
    return r


async def _test(dest, block, http, tqsl_home, secrets_):
    import aiohttp
    try:
        if dest == "wavelog":
            w = block["wavelog"]
            key = _read(w["key_file"])
            secrets_.append(key)
            base = w["url"].rstrip("/") + "/index.php/api/"
            async with http.get(base + "check_auth/" + key) as r:
                d = await r.json(content_type=None)
            if d.get("status") != "valid":
                return _r(dest, False, "Wavelog refused the API key")
            if "w" not in str(d.get("rights", "")):
                return _r(dest, False, f"the API key is read-only (rights {d.get('rights')}): make a read+write key")
            async with http.get(base + "station_info/" + key) as r:
                st = await r.json(content_type=None)
            ids = {str(s.get("station_id")): s for s in st} if isinstance(st, list) else {}
            s = ids.get(str(w["station_profile_id"]))
            if s is None:
                return _r(dest, False, f"no station profile {w['station_profile_id']} in Wavelog (it has: "
                                       + ", ".join(f"{k} {v.get('station_profile_name')}" for k, v in ids.items()) + ")")
            return _r(dest, True, f"key valid (read+write); profile {w['station_profile_id']} is "
                                  f"{s.get('station_profile_name')} ({s.get('station_callsign')})")
        if dest == "qrz":
            q = block["qrz"]
            key = _read(q["key_file"])
            secrets_.append(key)
            async with http.post(q.get("url", "https://logbook.qrz.com/api"),
                                 data={"KEY": key, "ACTION": "STATUS"},
                                 headers={"User-Agent": f"RanchRadio/settings ({block.get('call', '')})"}) as r:
                text = await r.text()
            res = (re.search(r"(?:^|&)RESULT=([A-Z]+)", text) or [None, ""])[1]
            if res == "OK":
                n = (re.search(r"(?:^|&|;)COUNT=(\d+)", text) or [None, "?"])[1]
                return _r(dest, True, f"key valid; QRZ logbook answered ({n} QSOs)")
            if res == "AUTH":
                return _r(dest, False, "QRZ refused the API key")
            return _r(dest, False, f"QRZ answered {res or 'nothing usable'}")
        if dest == "lotw":
            lw = block["lotw"]
            out = []
            loc_ok = lw.get("location") in tqsl_locations(tqsl_home)
            pw = _read(lw["password_file"])
            secrets_.append(pw)
            params = {"login": lw["login"], "password": pw, "qso_query": "1", "qso_qsl": "yes",
                      "qso_qslsince": (dt.datetime.now(dt.UTC) - dt.timedelta(days=1)).strftime("%Y-%m-%d")}
            async with http.get(lw.get("url", "https://lotw.arrl.org/lotwuser/lotwreport.adi"), params=params) as r:
                text = await r.text()
            if "<eoh>" not in text.lower():
                return _r(dest, False, "LoTW didn't accept the login and password (it answered with a web page)")
            out.append("login and password accepted")
            tq = os.path.expanduser(lw["tqsl"])
            if not (os.path.isfile(tq) and os.access(tq, os.X_OK)):
                return _r(dest, False, "; ".join(out + [f"TQSL not found at {lw['tqsl']}"]))
            out.append(f"station location {lw['location']!r} " + ("found in TQSL" if loc_ok else "NOT in TQSL"))
            return _r(dest, loc_ok, "; ".join(out))
        return _r(dest, False, f"no test for {dest!r}")
    except KeyError as e:
        return _r(dest, False, f"not set: {e.args[0]}")
    except (aiohttp.ClientError, TimeoutError) as e:     # before OSError: a refused connection is both
        return _r(dest, False, f"couldn't reach it ({type(e).__name__})")
    except OSError as e:
        return _r(dest, False, f"credential file: {type(e).__name__}")
    except ValueError as e:
        return _r(dest, False, f"unexpected answer ({type(e).__name__})")


def _r(dest, ok, msg):
    return {"dest": dest, "ok": bool(ok), "msg": msg}
