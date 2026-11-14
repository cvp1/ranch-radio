"""ADIF for the logbook sync (logsync.py): parse, write, and the one identity
a QSO has wherever it is logged.

    parse(text, strict=True) -> (header, [record])   record: {FIELD: value}
    record(fields) -> "<CALL:5>N9ABC ... <EOR>"
    qso(fields, own=None) -> {"own", "call", "band", "mode", "submode", "ts", "secs"}
    key(q) -> "N0CALL|N9ABC|20m|MFSK|FT4|20261113120315"

Identity (PLAN v3 §5): own call + their call + band + canonical mode/submode +
TIME_ON to the second, in UTC. JTDX's QSO Logged (type 5) carries MODE=FT4;
its Logged ADIF (type 12) carries MODE=MFSK SUBMODE=FT4 (logqso.cpp,
2026-11-13). Both must land on one key, so every mode goes through MODES
before it is compared. Nothing here merges by a time window.

strict parsing is for downloads (LoTW, QRZ, Wavelog): no <EOH>, a record
without <EOR>, a field running past the end, or anything but blanks after
the last <EOR> is an error, never "an empty log" (LoTW's failure is an HTML
page with no end-of-header tag: lotw-help/developer-query-qsos-qsls).
"""

import re

# ADIF 3.1 modes JTDX/WSJT-X/JS8Call send under their own name but that ADIF
# files under MFSK (or another parent): name -> (MODE, SUBMODE).
MODES = {
    "FT4": ("MFSK", "FT4"),
    "JS8": ("MFSK", "JS8"),
    "FST4": ("MFSK", "FST4"),
    "FST4W": ("MFSK", "FST4W"),
    "Q65": ("MFSK", "Q65"),
    "JT65A": ("JT65", "JT65A"), "JT65B": ("JT65", "JT65B"), "JT65C": ("JT65", "JT65C"),
    "JT9A": ("JT9", "JT9A"),
    "USB": ("SSB", "USB"), "LSB": ("SSB", "LSB"),
}

# ADIF band edges, MHz (ADIF 3.1 Band enumeration, HF to 70 cm).
BANDS = [
    ("2190m", 0.1357, 0.1378), ("630m", 0.472, 0.479), ("560m", 0.501, 0.504),
    ("160m", 1.8, 2.0), ("80m", 3.5, 4.0), ("60m", 5.06, 5.45), ("40m", 7.0, 7.3),
    ("30m", 10.1, 10.15), ("20m", 14.0, 14.35), ("17m", 18.068, 18.168), ("15m", 21.0, 21.45),
    ("12m", 24.89, 24.99), ("10m", 28.0, 29.7), ("8m", 40.0, 45.0), ("6m", 50.0, 54.0),
    ("5m", 54.000001, 69.9), ("4m", 70.0, 71.0), ("2m", 144.0, 148.0), ("1.25m", 222.0, 225.0),
    ("70cm", 420.0, 450.0),
]

_TAG = re.compile(r"<([A-Za-z0-9_]+)(?::(\d+)(?::[A-Za-z])?)?>")


class AdifError(ValueError):
    pass


def canon_mode(mode, submode=""):
    """(MODE, SUBMODE), upper-cased, with MODES applied to either."""
    mode, submode = (mode or "").strip().upper(), (submode or "").strip().upper()
    if mode in MODES and not submode:
        return MODES[mode]
    if not mode and submode in MODES:
        return MODES[submode]
    if submode in MODES and MODES[submode][0] == mode:
        return MODES[submode]
    return mode, submode


def band_of(mhz):
    try:
        f = float(mhz)
    except (TypeError, ValueError):
        return ""
    for name, lo, hi in BANDS:
        if lo <= f <= hi:
            return name
    return ""


def parse(text, strict=True):
    """(header, records). strict: see the module docstring."""
    text = text or ""
    pos, header, records, cur = 0, {}, [], {}
    saw_eoh = False
    while True:
        m = _TAG.search(text, pos)
        if not m:
            break
        name = m.group(1).upper()
        if name == "EOH":
            header, cur, saw_eoh = cur, {}, True
            pos = m.end()
            continue
        if name == "EOR":
            if strict and not saw_eoh:
                raise AdifError("a record before the end of the header (no <EOH>)")
            records.append(cur)
            cur = {}
            pos = m.end()
            continue
        if m.group(2) is None:
            pos = m.end()                 # a bare tag that isn't EOH/EOR: skip it
            continue
        n = int(m.group(2))
        val = text[m.end():m.end() + n]
        if len(val) < n:
            raise AdifError(f"field {name} runs past the end of the data (truncated)")
        cur[name] = val
        pos = m.end() + n
    if strict:
        if not saw_eoh:
            raise AdifError("no <EOH>: not an ADIF file (a login or error page?)")
        if cur:
            raise AdifError("the last record has no <EOR> (truncated)")
    elif cur:
        records.append(cur)
    return header, records


def record(fields):
    """One ADIF record (fields in the given order), ending <EOR>."""
    out = []
    for k, v in fields.items():
        if v is None or v == "":
            continue
        v = str(v)
        out.append(f"<{k.upper()}:{len(v)}>{v}")
    return " ".join(out) + " <EOR>"


def document(records, program="RanchRadio"):
    head = f"Ranch Radio logbook sync\n<ADIF_VER:5>3.1.4 <PROGRAMID:{len(program)}>{program} <EOH>\n"
    return head + "\n".join(records) + "\n"


def qso(f, own=None):
    """The identity fields of an ADIF record: {"own", "call", "band", "mode",
    "submode", "ts" (YYYYMMDDHHMMSS UTC), "secs" (TIME_ON had seconds)}.
    Raises AdifError if call, date or time is missing or malformed."""
    call = (f.get("CALL") or "").strip().upper()
    date = (f.get("QSO_DATE") or "").strip()
    t = (f.get("TIME_ON") or "").strip()
    if not call:
        raise AdifError("no CALL")
    if not re.fullmatch(r"\d{8}", date):
        raise AdifError(f"bad QSO_DATE {date!r}")
    if not re.fullmatch(r"\d{4}(\d{2})?", t):
        raise AdifError(f"bad TIME_ON {t!r}")
    secs = len(t) == 6
    band = (f.get("BAND") or "").strip().lower() or band_of(f.get("FREQ"))
    mode, submode = canon_mode(f.get("MODE"), f.get("SUBMODE"))
    me = (f.get("STATION_CALLSIGN") or f.get("OPERATOR") or own or "").strip().upper()
    return {"own": me, "call": call, "band": band, "mode": mode, "submode": submode,
            "ts": date + (t if secs else t + "00"), "secs": secs}


def key(q):
    return "|".join((q["own"], q["call"], q["band"], q["mode"], q["submode"], q["ts"]))


# The fields whose change makes a re-log a revision (PLAN §5): call, time, band
# and mode are the key itself; report and grid are what can change under it.
CONTENT = ("RST_SENT", "RST_RCVD", "GRIDSQUARE")


def content(f):
    return tuple((f.get(k) or "").strip().upper() for k in CONTENT)
