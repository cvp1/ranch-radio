"""Callsign -> DXCC entity, from country-files.com's cty.csv (AD1C's
country files, with the ADIF DXCC entity number in the third column).
Stdlib only.

A row: primary prefix, name, ADIF DXCC number, continent, CQ zone, ITU zone,
lat, lon (west positive, as in the file), UTC offset, then the prefixes,
space-separated, ending with ";". A prefix may carry overrides — (CQ) [ITU]
<lat/lon> {continent} ~offset~ — and "=CALL" marks an exact callsign that
belongs to this entity whatever its prefix says (=AH0AB is the United States,
not the Mariana Islands). Rows whose primary prefix starts with "*" are WAE /
non-DXCC (Sicily, …) and are skipped: the needs index counts DXCC entities.

lookup(call):
  1. the whole call, as given, in the exact list;
  2. maritime/aeronautical mobile (/MM, /AM) -> None (no entity);
  3. a portable form: drop /P /M /QRP /A /LH /R /B /AG; of two parts,
     the shorter one that isn't a lone digit is the prefix (KH6/W1AW and
     W1AW/KH6 are both Hawaii); a lone digit (W1AW/4) keeps the base call;
  4. the base call in the exact list, then the longest matching prefix.
"""
import csv
import io
import re

STRIP = {"P", "M", "QRP", "QRPP", "A", "LH", "R", "B", "AG", "J", "E"}
NO_ENTITY = {"MM", "AM"}
_OVERRIDE = re.compile(r"\(\d+\)|\[\d+\]|<[^>]*>|\{[^}]*\}|~[^~]*~")


class Entity:
    __slots__ = ("dxcc", "name", "prefix", "cont", "cq", "itu")

    def __init__(self, dxcc, name, prefix, cont, cq, itu):
        self.dxcc, self.name, self.prefix, self.cont, self.cq, self.itu = dxcc, name, prefix, cont, cq, itu

    def __repr__(self):
        return f"Entity({self.dxcc}, {self.name!r})"


class Cty:
    def __init__(self, text):
        self.prefixes, self.exact, self.by_dxcc = {}, {}, {}
        for row in csv.reader(io.StringIO(text)):
            if len(row) < 10 or row[0].startswith("*"):
                continue
            try:
                e = Entity(int(row[2]), row[1].strip(), row[0].strip(), row[3].strip(), int(row[4]), int(row[5]))
            except ValueError:
                continue
            self.by_dxcc.setdefault(e.dxcc, e)
            for tok in row[9].replace(";", " ").split():
                tok = _OVERRIDE.sub("", tok).strip().upper()
                if not tok:
                    continue
                if tok.startswith("="):
                    self.exact[tok[1:]] = e
                else:
                    self.prefixes[tok] = e
        self._maxlen = max((len(p) for p in self.prefixes), default=0)
        if not self.prefixes:
            raise ValueError("cty.csv: no prefixes (not a cty.csv?)")

    def _longest(self, s):
        for n in range(min(len(s), self._maxlen), 0, -1):
            e = self.prefixes.get(s[:n])
            if e is not None:
                return e
        return None

    def lookup(self, call):
        """The Entity for a callsign, or None (maritime mobile, unknown)."""
        c = (call or "").strip().upper()
        if not c or not re.fullmatch(r"[A-Z0-9/]+", c):
            return None
        if c in self.exact:
            return self.exact[c]
        parts = [p for p in c.split("/") if p]
        if any(p in NO_ENTITY for p in parts[1:]):
            return None
        parts = [p for i, p in enumerate(parts) if i == 0 or p not in STRIP]
        base = max(parts, key=len)
        if len(parts) == 2:
            other = parts[0] if parts[1] == base else parts[1]
            if not other.isdigit() and len(other) < len(base):
                return self.exact.get("/".join(parts)) or self._longest(other)
        e = self.exact.get(base) or self._longest(base)
        if e is not None and e.dxcc == 105 and not re.fullmatch(r"KG4[A-Z]{2}", base):
            return self._longest("K")         # KG4 + two letters is Guantanamo Bay; any other KG4 call is the US
        return e


def mode_group(mode, submode=""):
    """CW, PHONE or DATA (LoTW's mode groups)."""
    m = (mode or "").upper()
    if m == "CW":
        return "CW"
    if m in ("SSB", "USB", "LSB", "AM", "FM", "DIGITALVOICE", "PHONE", "C4FM", "DSTAR", "DMR"):
        return "PHONE"
    return "DATA"
