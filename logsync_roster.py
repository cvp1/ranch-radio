"""The roster: who JTDX is hearing now, marked by what the station still
needs (logsync_needs), with the click that calls one of them back. Stdlib only.

Fed by logsync's intake: Status (1) says the dial, mode and our call; each
Decode (2) is read for the sending station and its grid:

    CQ CALL GRID              CQ DX CALL GRID   CQ POTA CALL GRID
    THEIRCALL CALL GRID       THEIRCALL CALL -12   ... R-12 / RR73 / RRR / 73
    <CALL> forms (hashed calls) lose their brackets; RR73 is never a grid.

A row is the latest decode from a station within KEEP_S. The page gets rows
sorted by need (new DXCC first), then signal.

A click answers the station's last CQ or QRZ if one is fresh (REPLY_MAX_AGE_S),
else its latest line: JTDX, by default, takes a Reply only for a line that
starts CQ, CQDX or QRZ ("UDP reply messages" in Settings > Reporting), and the
latest line is often the station working someone else. Each row says which it
would answer ("answer": "cq", "line", or None when nothing is fresh), and the
row's key is that decode's.
"""
import math
import re
import time

KEEP_S = 120
REPLY_MAX_AGE_S = 60              # two FT8 CQ periods
MAX_ROWS = 150
_CALL = re.compile(r"^(?=.*[0-9])(?=.*[A-Z])[A-Z0-9/]{3,13}$")
_GRID = re.compile(r"^[A-R]{2}[0-9]{2}$")
_CQ_WORD = re.compile(r"^(?:[A-Z]{1,4}|[0-9]{3})$")
# What the Map tab gets of a roster row (map_view).
MAP_KEYS = ("key", "answer", "call", "grid", "need", "reasons", "snr", "cq", "to_me", "entity", "km", "bearing", "age",
            "band", "mode", "message", "lotw")
NEED_RANK = {"dxcc": 0, "dxcc_band": 1, "dxcc_mode": 2, "grid": 3, "grid_band": 4, "grid_mode": 5,
             "dxcc_unconfirmed": 6, "call_band_mode": 7, None: 8}


def is_grid(t):
    return bool(_GRID.match(t)) and t != "RR73"


def _call(t):
    t = t.strip("<>").upper()
    return t if _CALL.match(t) and t != "RR73" else None


def lookup_call(t):
    """The call a call card may look up, or None (2026-11-19, UX plan PR 4b):
    a string, upper-cased, the roster's own pattern (a letter and a digit),
    and no empty part around a slash ("W1AW/", "/P", "W1AW//P" are not calls)."""
    if not isinstance(t, str):
        return None
    c = t.strip().upper()
    if not _CALL.match(c) or c == "RR73" or not all(c.split("/")):
        return None
    return c


def parse_message(msg):
    """{"call", "grid", "cq", "cq_word", "to"} for the station sending msg, or None."""
    t = (msg or "").upper().split()
    if not t:
        return None
    if t[0] in ("CQ", "QRZ"):
        word = None
        rest = t[1:]
        if len(rest) >= 2 and _CQ_WORD.match(rest[0]) and _call(rest[1]) and not _call(rest[0]):
            word, rest = rest[0], rest[1:]
        if not rest or not _call(rest[0]):
            return None
        grid = rest[1] if len(rest) > 1 and is_grid(rest[1]) else ""
        return {"call": _call(rest[0]), "grid": grid, "cq": True, "cq_word": word, "to": None}
    if len(t) >= 2 and _call(t[1]):
        grid = t[2] if len(t) > 2 and is_grid(t[2]) else ""
        return {"call": _call(t[1]), "grid": grid, "cq": False, "cq_word": None, "to": _call(t[0])}
    return None


def latlon(grid):
    """The centre of a 4- or 6-character Maidenhead grid, (lat, lon), or None."""
    g = (grid or "").strip()
    if not re.fullmatch(r"[A-Ra-r]{2}[0-9]{2}([A-Xa-x]{2})?", g):
        return None
    g = g[:2].upper() + g[2:4] + g[4:].lower()
    lon = (ord(g[0]) - 65) * 20 - 180 + int(g[2]) * 2
    lat = (ord(g[1]) - 65) * 10 - 90 + int(g[3])
    if len(g) == 6:
        lon += (ord(g[4]) - 97) * (2 / 24) + (1 / 24)
        lat += (ord(g[5]) - 97) * (1 / 24) + (1 / 48)
    else:
        lon, lat = lon + 1, lat + 0.5
    return lat, lon


def distance_bearing(a, b):
    """(km, degrees) from grid a to grid b, or (None, None)."""
    p, q = latlon(a), latlon(b)
    if not p or not q:
        return None, None
    la1, lo1, la2, lo2 = map(math.radians, (*p, *q))
    d = 2 * 6371 * math.asin(math.sqrt(math.sin((la2 - la1) / 2) ** 2
                                       + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return round(d), round((math.degrees(math.atan2(y, x)) + 360) % 360)


class Roster:
    def __init__(self, needs=None, clock=time.monotonic):
        self.needs, self.clock = needs, clock
        self.st = {}                       # the last Status: dial, mode, de_call, de_grid, band
        self.rows = {}                     # call -> {"dec", "at", "parsed", "band", "mode", "dial", "cq": {"dec", "at"} | None}

    def status(self, d):
        from logsync_adif import band_of
        self.st = {"dial": d.get("dial"), "mode": d.get("mode") or "", "de_call": (d.get("de_call") or "").upper(),
                   "de_grid": d.get("de_grid") or "", "id": d.get("id"),
                   "dx_call": (d.get("dx_call") or "").upper(),
                   "band": band_of(f"{(d.get('dial') or 0) / 1e6:.6f}") if d.get("dial") else ""}

    def add(self, dec):
        """A Decode (2): kept if it names a station. -> the call, or None."""
        p = parse_message(dec.get("message"))
        if not p or p["call"] == self.st.get("de_call"):
            return None
        now, old = self.clock(), self.rows.get(p["call"])
        cq = {"dec": dec, "at": now} if p["cq"] else (old or {}).get("cq")
        if not p["grid"] and old and old["parsed"]["grid"]:
            p = dict(p, grid=old["parsed"]["grid"])         # "N0CALL K1ABC -10" keeps the grid its CQ gave
        # The dial is stamped at arrival, like the band (2026-11-19, UX plan
        # PR 4a): a row kept across a QSY must place its signal where it was
        # heard (dial + df), never at the snapshot's newer dial.
        self.rows[p["call"]] = {"dec": dec, "at": now, "parsed": p, "band": self.st.get("band", ""),
                                "mode": self.st.get("mode", ""), "dial": self.st.get("dial"), "cq": cq}
        return p["call"]

    def prune(self):
        now = self.clock()
        for c in [c for c, r in self.rows.items() if now - r["at"] > KEEP_S]:
            del self.rows[c]

    @staticmethod
    def dkey(d):
        return f"{d['id']}|{d['t_ms']}|{d['df']}|{d['message'].strip()}"

    def target(self, r):
        """(decode, "cq" | "line") a click on row r answers, or (None, None)."""
        now = self.clock()
        if r.get("cq") and now - r["cq"]["at"] <= REPLY_MAX_AGE_S:
            return r["cq"]["dec"], "cq"
        if now - r["at"] <= REPLY_MAX_AGE_S:
            return r["dec"], "line"
        return None, None

    def key(self, r):
        d, _ = self.target(r)
        return self.dkey(d or r["dec"])

    def view(self):
        """The page's rows: newest data, sorted by need then signal; no raw bytes."""
        self.prune()
        now, me, out = self.clock(), self.st.get("de_call"), []
        for call, r in self.rows.items():
            p, d = r["parsed"], r["dec"]
            level, reasons = (self.needs.need(call, p["grid"], r["band"], r["mode"]) if self.needs else (None, []))
            e = self.needs.cty.lookup(call) if self.needs and self.needs.cty else None
            km, brg = distance_bearing(self.st.get("de_grid"), p["grid"])
            out.append({"key": self.key(r), "answer": self.target(r)[1], "call": call, "grid": p["grid"], "cq": p["cq"], "cq_word": p["cq_word"],
                        "to_me": bool(me) and p["to"] == me, "to": p["to"], "snr": d["snr"], "dt": round(d["dt"], 1),
                        "df": d["df"], "message": d["message"].strip(), "age": round(now - r["at"]),
                        "need": level, "reasons": reasons, "entity": e.name if e else "", "cont": e.cont if e else "",
                        "lotw": bool(self.needs and self.needs.lotw_user(call)), "km": km, "bearing": brg,
                        "band": r["band"], "mode": r["mode"], "dial": r.get("dial")})
        out.sort(key=lambda x: (not x["to_me"], NEED_RANK.get(x["need"], 8), -x["snr"]))
        return {"band": self.st.get("band", ""), "mode": self.st.get("mode", ""), "dial": self.st.get("dial"),
                "rows": out[:MAX_ROWS], "needs": self.needs.summary() if self.needs else None}

    def map_view(self, band=None):
        """The Map tab: our grid, worked/confirmed grids (the current band by
        default, one band, or every band with "all"), and the decodes that
        carry a grid (MAP_KEYS)."""
        rv = self.view()
        b = rv["band"] if band in (None, "") else (None if band == "all" else band)
        return {"me": self.st.get("de_grid", ""), "band": rv["band"], "showing": b or "all",
                "grids": self.needs.grid_map(b) if self.needs else {},
                "decodes": [{k: r[k] for k in MAP_KEYS} for r in rv["rows"] if r["grid"]]}

    def for_reply(self, key):
        """The decode to answer (the one key names, the station's CQ or its
        latest line), or (None, why)."""
        now = self.clock()
        for r in self.rows.values():
            for d, at in ((r["dec"], r["at"]), *(((r["cq"]["dec"], r["cq"]["at"]),) if r.get("cq") else ())):
                if self.dkey(d) == key:
                    if now - at > REPLY_MAX_AGE_S:
                        return None, f"{r['parsed']['call']}'s decode is {round(now - at)} s old: answer a fresh one"
                    return d, None
        return None, "that decode is no longer on the roster"
