"""What the station still needs: worked and confirmed, from the logbook
sync's ledger, so a decode can be marked new DXCC, new on this band, new
grid, and so on (the roster and map). Stdlib only.

Built from every QSO in the ledger (logsync_ledger): its entity (the DXCC
number Wavelog recorded when there is one, else logsync_cty's lookup), band,
mode group (CW / PHONE / DATA, as LoTW groups them) and 4-character grid.
Confirmed means LoTW or QRZ confirmed it (the ledger's "confirmed" state).
The index is rebuilt when the ledger changes, never per decode.

need(call, grid, band, mode) -> (level, reasons), level the highest of:
    dxcc            an entity never worked
    dxcc_band       the entity, never on this band
    dxcc_mode       the entity, never in this mode group
    grid            a grid never worked
    grid_band       the grid, never on this band
    grid_mode       the grid, never in this mode (op_mode: FT8 and FT4 apart, 2026-11-20)
    dxcc_unconfirmed the entity is worked but not confirmed anywhere
    call_band_mode  this call never worked on this band in this mode group
    None            worked before, on this band and mode

grid_cells() is the Grids tab's view (GridTracker2's tracking, by grid, band
and operating mode): {grid4: [[band, mode, confirmed 0|1], ...]}, mode the
one an operator names (op_mode: FT8, FT4, SSB, CW, JS8...), not the group.

LoTW users come from ARRL's lotw-user-activity.csv: a call that uploaded to
LoTW within LOTW_ACTIVE_DAYS counts as a LoTW user.
"""
import csv
import datetime as dt
import io
import re
import sqlite3

from logsync_cty import mode_group

LEVELS = ("dxcc", "dxcc_band", "dxcc_mode", "grid", "grid_band", "grid_mode", "dxcc_unconfirmed", "call_band_mode")
LABELS = {"dxcc": "new DXCC", "dxcc_band": "new DXCC on this band", "dxcc_mode": "new DXCC in this mode",
          "grid": "new grid", "grid_band": "new grid on this band", "grid_mode": "new grid in this mode",
          "dxcc_unconfirmed": "DXCC not yet confirmed",
          "call_band_mode": "new call on this band and mode"}
LOTW_ACTIVE_DAYS = 365
_F = re.compile(r"<([A-Za-z0-9_]+):(\d+)(?::[A-Za-z])?>")
_GRID = re.compile(r"[A-R]{2}[0-9]{2}")


def _fields(adif, want):
    """The few ADIF fields want names, from one record (by length, not regex on values)."""
    out, i = {}, 0
    while True:
        m = _F.search(adif, i)
        if not m:
            return out
        name, n = m.group(1).upper(), int(m.group(2))
        if name in want:
            out[name] = adif[m.end():m.end() + n]
        i = m.end() + n


def op_mode(mode, submode=""):
    """The mode an operator names: FT4 for MFSK/FT4, SSB for USB or LSB,
    else the ADIF mode (FT8, CW, AM, FM, RTTY...)."""
    m, sub = (mode or "").strip().upper(), (submode or "").strip().upper()
    if m in ("SSB", "USB", "LSB") or sub in ("USB", "LSB"):
        return "SSB"
    if m == "MFSK" and sub:
        return sub[:8]
    return m[:8] or "?"


def grid4(g):
    g = (g or "").strip().upper()[:4]
    return g if _GRID.fullmatch(g) else ""


class Needs:
    def __init__(self, cty=None):
        self.cty = cty
        self.worked = {"dxcc": set(), "dxcc_band": set(), "dxcc_mode": set(), "grid": set(), "grid_band": set(), "grid_mode": set(),
                       "call_band_mode": set()}
        self.confirmed = {"dxcc": set(), "grid": set(), "grid_band": set()}
        self.cells = {}                  # grid4 -> {(band, op_mode): confirmed}
        self.qsos = 0
        self.lotw_users = {}

    def entity(self, call):
        e = self.cty.lookup(call) if self.cty else None
        return e.dxcc if e else None

    def add(self, call, band, mode, submode, adif, confirmed):
        f = _fields(adif or "", {"DXCC", "GRIDSQUARE"})
        try:
            ent = int(f["DXCC"]) or None
        except (KeyError, ValueError):
            ent = self.entity(call)
        band, mg, g = (band or "").lower(), mode_group(mode, submode), grid4(f.get("GRIDSQUARE"))
        w = self.worked
        if ent:
            w["dxcc"].add(ent)
            w["dxcc_band"].add((ent, band))
            w["dxcc_mode"].add((ent, mg))
            if confirmed:
                self.confirmed["dxcc"].add(ent)
        if g:
            w["grid"].add(g)
            w["grid_band"].add((g, band))
            w["grid_mode"].add((g, op_mode(mode, submode)))
            if confirmed:
                self.confirmed["grid"].add(g)
                self.confirmed["grid_band"].add((g, band))
            c = self.cells.setdefault(g, {})
            k = (band, op_mode(mode, submode))
            c[k] = c.get(k, False) or bool(confirmed)
        w["call_band_mode"].add(((call or "").upper(), band, mg))
        self.qsos += 1

    @classmethod
    def from_ledger(cls, path, cty=None):
        n = cls(cty)
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            conf = {r[0] for r in db.execute("SELECT qso_id FROM dest WHERE state='confirmed'")}
            for qid, call, band, mode, sub, adif in db.execute("SELECT id, call, band, mode, submode, adif FROM qso"):
                n.add(call, band, mode, sub, adif, qid in conf)
        finally:
            db.close()
        return n

    def load_lotw_users(self, text, today=None):
        """ARRL's lotw-user-activity.csv: call,date,time per line."""
        today = today or dt.datetime.now(dt.UTC).date()
        users = {}
        for row in csv.reader(io.StringIO(text)):
            if len(row) < 2:
                continue
            try:
                d = dt.date.fromisoformat(row[1].strip())
            except ValueError:
                continue
            if (today - d).days <= LOTW_ACTIVE_DAYS:
                users[row[0].strip().upper()] = d.isoformat()
        self.lotw_users = users
        return len(users)

    def lotw_user(self, call):
        c = (call or "").upper()
        return self.lotw_users.get(c) or self.lotw_users.get(c.split("/")[0])

    def need(self, call, grid, band, mode, submode=""):
        """(level or None, [reasons]) for a station heard on band/mode."""
        ent = self.entity(call)
        band, mg, g = (band or "").lower(), mode_group(mode, submode), grid4(grid)
        w, reasons = self.worked, []
        if ent:
            if ent not in w["dxcc"]:
                reasons.append("dxcc")
            else:
                if (ent, band) not in w["dxcc_band"]:
                    reasons.append("dxcc_band")
                if (ent, mg) not in w["dxcc_mode"]:
                    reasons.append("dxcc_mode")
        if g:
            if g not in w["grid"]:
                reasons.append("grid")
            elif (g, band) not in w["grid_band"]:
                reasons.append("grid_band")
            elif (g, op_mode(mode, submode)) not in w["grid_mode"]:
                reasons.append("grid_mode")
        if ent and ent in w["dxcc"] and ent not in self.confirmed["dxcc"]:
            reasons.append("dxcc_unconfirmed")
        if ((call or "").upper(), band, mg) not in w["call_band_mode"]:
            reasons.append("call_band_mode")
        reasons.sort(key=LEVELS.index)
        return (reasons[0] if reasons else None), reasons

    def grid_map(self, band=None):
        """{grid4: "c" (confirmed) | "w" (worked)}, on one band or all."""
        if band:
            b = band.lower()
            out = {g: "w" for g, bb in self.worked["grid_band"] if bb == b}
            out.update({g: "c" for g, bb in self.confirmed["grid_band"] if bb == b})
            return out
        out = {g: "w" for g in self.worked["grid"]}
        out.update({g: "c" for g in self.confirmed["grid"]})
        return out

    def grid_cells(self):
        """{grid4: [[band, op_mode, 1 confirmed | 0 worked], ...]}: every grid
        worked, each band and mode it was worked on (the Grids tab filters)."""
        return {g: sorted([b, m, int(c)] for (b, m), c in cells.items()) for g, cells in self.cells.items()}

    def summary(self):
        w, c = self.worked, self.confirmed
        return {"qsos": self.qsos, "dxcc_worked": len(w["dxcc"]), "dxcc_confirmed": len(c["dxcc"]),
                "grids_worked": len(w["grid"]), "grids_confirmed": len(c["grid"]), "lotw_users": len(self.lotw_users)}
