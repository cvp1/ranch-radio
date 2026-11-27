"""What the QSO add-on (addons/qso.py) needs that is not the station's own:
QRZ.com's XML callsign lookup, POTA activator spots and the match between a
spot and the call being logged, and the ADIF record a typed-in QSO becomes.
Stdlib only, apart from the aiohttp session the add-on hands in.

QRZ XML (xmldata.qrz.com, a QRZ subscription): a login with the QRZ username
and password answers a session key; each lookup sends the key and a callsign.
The key lasts until QRZ says "Session Timeout" or "Invalid session key"; then
one fresh login and one retry. The password is read from its file at each
login and is never logged, returned, or put in an error: a failed request is
reported by its class only, because aiohttp's errors carry the URL and the
URL carries the password.

    Qrz(user, password_file, get).lookup(call) -> (state, data)
        state: ok | not_found | no_login | bad_login | no_subscription
               | offline | error
        data:  {"name", "qth", "state", "county", "country", "grid", "dxcc",
                "lat", "lon"} for ok, else None

POTA spots (api.pota.app/spot/activator): parsed to the few fields the card
uses, including when each was spotted, so an old list read back from the
cache after the station lost the internet is shown as old.

    pota_spots(items, now) -> [spot]
    match(spots, call, khz, now) -> [spot + {"age_s", "on_freq"}], best first
    suggest(matches) -> the POTA reference to fill in, or "" when it is not clear

SOTA: no spots. The SOTA API's terms (api-db2.sota.org.uk/docs, read
2026-11-27) do not allow AI-written software to connect without the SOTA
Management Team's prior approval, so the card takes SOTA references typed in
only, until that approval exists.

    adif_fields(form, own, now) -> {ADIF field: value}   (raises ValueError, naming the field)
"""
import datetime as dt
import re
import time
import xml.etree.ElementTree as ET

import logsync_adif as adif

QRZ_URL = "https://xmldata.qrz.com/xml/current/"
QRZ_AGENT = "RanchRadio-qso/1"
QRZ_MAX_BYTES = 64 << 10
QRZ_OFFLINE_S = 60              # after a failure to reach QRZ, don't try again for this long
CACHE_MAX = 200                 # QRZ answers kept in memory (never on disk), the oldest go first
CACHE_S = 3600

SPOT_MAX_AGE_S = 1800           # a spot older than this suggests nothing
ON_FREQ_KHZ = 3.0               # a spot this close to the dial is the station being worked

POTA_REF = re.compile(r"^[A-Z0-9]{1,4}-\d{4,5}$")
SOTA_REF = re.compile(r"^[A-Z0-9]{1,4}/[A-Z0-9]{2}-\d{3}$")
RST = re.compile(r"^[0-9+\-]{1,6}$")
GRID = re.compile(r"^[A-R]{2}[0-9]{2}([A-X]{2}([0-9]{2})?)?$")
MODES = ("SSB", "CW", "AM", "FM", "FT8", "FT4", "RTTY", "PSK31", "JS8", "OLIVIA", "SSTV", "DIGITALVOICE")
TEXT_MAX = 60


# -- QRZ XML

def _tag(e):
    return e.tag.rsplit("}", 1)[-1]


def parse_qrz(text):
    """QRZ's XML -> {"key", "error", "sub", "call": {field: text}}. Raises
    ValueError for anything that is not QRZ's document."""
    root = ET.fromstring(text)
    if _tag(root) != "QRZDatabase":
        raise ValueError("not a QRZDatabase document")
    out = {"key": None, "error": None, "sub": None, "call": None}
    for child in root:
        t = _tag(child)
        if t == "Session":
            for s in child:
                v = (s.text or "").strip()
                if _tag(s) == "Key":
                    out["key"] = v or None
                elif _tag(s) == "Error":
                    out["error"] = v
                elif _tag(s) == "SubExp":
                    out["sub"] = v
        elif t == "Callsign":
            out["call"] = {_tag(f): (f.text or "").strip() for f in child}
    return out


def qrz_view(c):
    """A QRZ Callsign record -> the fields the card fills, each capped."""
    def g(k):
        return clean(c.get(k, ""))
    name = " ".join(x for x in (g("nickname") or g("fname"), g("name")) if x)
    grid = g("grid").upper()
    out = {"name": name[:TEXT_MAX], "qth": g("addr2"), "state": g("state"), "county": g("county"),
           "country": g("country"), "grid": grid if GRID.match(grid) else "", "dxcc": g("dxcc")}
    for k in ("lat", "lon"):
        try:
            out[k] = round(float(c.get(k, "")), 4)
        except ValueError:
            out[k] = None
    return out


class Qrz:
    """One QRZ XML account. `get(url, params)` is a coroutine returning
    (status, text): the add-on's aiohttp call, or a test's fake."""

    def __init__(self, user, password_file, get, clock=time.time):
        self.user, self.password_file, self.get, self.clock = user, password_file, get, clock
        self.key = None
        self.state = "no_login" if not self._password() else "unknown"
        self.down_until = 0.0
        self.refused = None              # the password file's (mtime, size) when QRZ refused it
        self.cache = {}                  # call -> (at, state, data)

    def _password(self):
        import os
        try:
            with open(os.path.expanduser(self.password_file)) as f:
                return f.read(257).strip() or None
        except (OSError, TypeError):
            return None

    async def _ask(self, params):
        try:
            status, text = await self.get(QRZ_URL, {**params, "agent": QRZ_AGENT})
        except Exception as e:                   # never str(e): it carries the URL, and the URL the password
            self.down_until = self.clock() + QRZ_OFFLINE_S
            return None, f"offline ({type(e).__name__})"
        if len(text) > QRZ_MAX_BYTES:
            return None, "answer too large"
        # QRZ's own errors come as its XML whatever the status: a refused login
        # is HTTP 403 with <Error>Username / password required</Error> (read
        # live 2026-11-27), so the body is read first and the status only
        # stands when it isn't QRZ's document.
        try:
            return parse_qrz(text), None
        except (ET.ParseError, ValueError):
            return None, (f"HTTP {status}" if status != 200 else "not QRZ's answer")

    def _pw_mark(self):
        import os
        try:
            st = os.stat(os.path.expanduser(self.password_file))
            return (st.st_mtime_ns, st.st_size)
        except (OSError, TypeError):
            return None

    async def login(self, force=False):
        """Returns the account state; sets self.key on success. A refused
        login is not tried again until the password file changes (or force,
        the tab's Save): a lookup per call typed would otherwise send QRZ a
        stream of bad logins, which can lock the account."""
        if not force and self.state == "bad_login" and self._pw_mark() == self.refused:
            return self.state
        pw = self._password()
        if not self.user or not pw:
            self.key, self.state = None, "no_login"
            return self.state
        doc, err = await self._ask({"username": self.user, "password": pw})
        if doc is None:
            self.state = "offline" if err.startswith("offline") else "error"
            return self.state
        if not doc["key"]:
            e = (doc["error"] or "").lower()
            self.key = None
            self.state = "bad_login" if ("password" in e or "username" in e or "incorrect" in e) else "error"
            self.refused = self._pw_mark() if self.state == "bad_login" else None
            return self.state
        self.key = doc["key"]
        self.state = "no_subscription" if (doc["sub"] or "").lower().startswith("non") else "ok"
        return self.state

    async def lookup(self, call):
        now = self.clock()
        hit = self.cache.get(call)
        if hit and now - hit[0] < CACHE_S:
            return hit[1], hit[2]
        if now < self.down_until:
            return "offline", None
        for attempt in (0, 1):
            if self.key is None:
                st = await self.login()
                if self.key is None:
                    return st, None
            doc, err = await self._ask({"s": self.key, "callsign": call})
            if doc is None:
                return ("offline" if err.startswith("offline") else "error"), None
            e = (doc["error"] or "")
            if doc["call"] is not None:
                data = qrz_view(doc["call"])
                self._keep(call, "ok", data)
                return "ok", data
            if e.lower().startswith("not found"):
                self._keep(call, "not_found", None)
                return "not_found", None
            if not doc["key"] or "session" in e.lower():
                self.key = None                      # expired: one fresh login, one retry
                continue
            return "error", None
        return "error", None

    def _keep(self, call, state, data):
        if len(self.cache) >= CACHE_MAX:
            self.cache.pop(min(self.cache, key=lambda k: self.cache[k][0]))
        self.cache[call] = (self.clock(), state, data)


# -- POTA spots

def _spot_time(s):
    try:
        return dt.datetime.fromisoformat(str(s)).replace(tzinfo=dt.UTC).timestamp()
    except (TypeError, ValueError):
        return None


def pota_spots(items):
    out = []
    for s in items if isinstance(items, list) else []:
        if not isinstance(s, dict):
            continue
        try:
            khz = float(s.get("frequency"))
        except (TypeError, ValueError):
            continue
        ref, call, at = str(s.get("reference") or "").upper(), str(s.get("activator") or "").upper(), _spot_time(s.get("spotTime"))
        if not POTA_REF.match(ref) or not call or at is None:
            continue
        out.append({"call": call[:16], "ref": ref, "name": clean(s.get("name"))[:TEXT_MAX],
                    "loc": clean(s.get("locationDesc"))[:20], "khz": khz, "mode": clean(s.get("mode"))[:8],
                    "grid": str(s.get("grid6") or s.get("grid4") or "")[:6], "at": at})
    return out


def base_call(c):
    """W1AW/P, VE3/W1AW, W1AW -> W1AW: the part with a digit, longest."""
    parts = [p for p in (c or "").upper().split("/") if p]
    withdigit = [p for p in parts if any(ch.isdigit() for ch in p)] or parts
    return max(withdigit, key=len) if withdigit else ""


def match(spots, call, khz, now):
    """The fresh spots of this station, on the dial first, then newest."""
    b = base_call(call)
    out = []
    for s in spots:
        if base_call(s["call"]) != b:
            continue
        age = now - s["at"]
        if age > SPOT_MAX_AGE_S:
            continue
        on = khz is not None and abs(s["khz"] - khz) <= ON_FREQ_KHZ
        out.append({**s, "age_s": max(0, int(age)), "on_freq": on})
    out.sort(key=lambda s: (not s["on_freq"], s["age_s"]))
    return out


def suggest(matches):
    """The reference(s) to fill in: every park spotted on the dial (a two-fer
    is two spots), else the one park if there is only one; "" when unclear."""
    on = sorted({m["ref"] for m in matches if m["on_freq"]})
    if on:
        return ",".join(on)
    refs = {m["ref"] for m in matches}
    return refs.pop() if len(refs) == 1 else ""


# -- the record

def clean(v):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(v or "")).strip()


def _refs(v, pat, name):
    v = clean(v).upper().replace(" ", "")
    if not v:
        return ""
    refs = v.split(",")
    if not all(pat.match(r) for r in refs):
        raise ValueError(f"{name}: not a reference ({v})")
    return ",".join(refs)


def adif_fields(form, own, now=None):
    """The page's form -> ADIF fields, checked. Code computes everything it
    can (band from frequency, SSB's sideband, the date and time in UTC)."""
    import logsync_roster as R
    f = form if isinstance(form, dict) else {}
    call = R.lookup_call(f.get("call"))
    if call is None:
        raise ValueError("call: not a callsign")
    try:
        mhz = float(f.get("mhz"))
    except (TypeError, ValueError):
        raise ValueError("frequency: a number in MHz") from None
    band = adif.band_of(f"{mhz:.6f}")
    if not band:
        raise ValueError(f"frequency: {mhz} MHz is in no amateur band")
    mode = clean(f.get("mode")).upper()
    side = clean(f.get("sideband")).upper()
    if mode not in MODES:
        raise ValueError(f"mode: one of {', '.join(MODES)}")
    if mode == "SSB":
        sub = side if side in ("USB", "LSB") else ("LSB" if mhz < 10 else "USB")
        mode_f, sub_f = "SSB", sub
    elif mode == "PSK31":
        mode_f, sub_f = "PSK", "PSK31"
    else:
        mode_f, sub_f = adif.canon_mode(mode)
    now = now if now is not None else time.time()
    t_on = _utc(f.get("time_on"), now, "time on")
    t_off = max(_utc(f.get("time_off"), now, "time off"), t_on)
    out = {"CALL": call, "QSO_DATE": t_on.strftime("%Y%m%d"), "TIME_ON": t_on.strftime("%H%M%S"),
           "QSO_DATE_OFF": t_off.strftime("%Y%m%d"), "TIME_OFF": t_off.strftime("%H%M%S"),
           "BAND": band, "FREQ": f"{mhz:.6f}", "MODE": mode_f, "SUBMODE": sub_f,
           "STATION_CALLSIGN": own or ""}
    for k, name in (("rst_sent", "RST_SENT"), ("rst_rcvd", "RST_RCVD")):
        v = clean(f.get(k))
        if v and not RST.match(v):
            raise ValueError(f"{k.replace('_', ' ')}: digits only (59, 599, -12)")
        out[name] = v
    for k, name in (("name", "NAME"), ("qth", "QTH"), ("state", "STATE"), ("county", "CNTY"),
                    ("country", "COUNTRY"), ("comment", "COMMENT")):
        out[name] = clean(f.get(k))[:TEXT_MAX if k != "comment" else 200]
    for k, name in (("grid", "GRIDSQUARE"), ("my_grid", "MY_GRIDSQUARE")):
        g = clean(f.get(k)).upper()
        if g and not GRID.match(g):
            raise ValueError(f"{k.replace('_', ' ')}: a Maidenhead grid (FN31, FN31pr)")
        out[name] = g
    out["POTA_REF"] = _refs(f.get("pota"), POTA_REF, "POTA")
    out["MY_POTA_REF"] = _refs(f.get("my_pota"), POTA_REF, "my POTA")
    out["SOTA_REF"] = _refs(f.get("sota"), SOTA_REF, "SOTA")
    out["MY_SOTA_REF"] = _refs(f.get("my_sota"), SOTA_REF, "my SOTA")
    if "," in out["MY_SOTA_REF"] or "," in out["SOTA_REF"]:
        raise ValueError("SOTA: one summit per QSO")
    dx = clean(f.get("dxcc"))
    out["DXCC"] = dx if dx.isdigit() else ""
    return out


def _utc(v, now, name):
    """"" -> now; "HH:MM", "HHMM", "HH:MM:SS" -> today (UTC) at that time,
    yesterday if that is in the future (a QSO logged just after midnight)."""
    t = dt.datetime.fromtimestamp(now, dt.UTC).replace(microsecond=0)
    s = clean(v).replace(":", "")
    if not s:
        return t
    if not re.fullmatch(r"\d{4}(\d{2})?", s) or int(s[:2]) > 23 or int(s[2:4]) > 59 or (len(s) == 6 and int(s[4:]) > 59):
        raise ValueError(f"{name}: HH:MM in UTC")
    at = t.replace(hour=int(s[:2]), minute=int(s[2:4]), second=int(s[4:6]) if len(s) == 6 else 0)
    if at > t + dt.timedelta(minutes=5):
        at -= dt.timedelta(days=1)
    return at
