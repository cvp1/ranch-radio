"""CHIRP's memory model for the radio add-ons (2026-10-16: "For
programming follow the standards set by CHIRP").

A channel is shown, edited, exported and imported as a CHIRP memory: the
columns of CHIRP's generic CSV (chirp_common.Memory.CSV_FORMAT), CHIRP's value
lists (TONES, DTCS_CODES, TONE_MODES, CROSS_MODES, SKIP_VALUES) and CHIRP's
rules for turning a radio's separate RX/TX frequencies and tones into
Frequency / Duplex / Offset and Tone / rToneFreq / cToneFreq / DtcsCode /
RxDtcsCode / CrossMode (chirp_common.split_to_offset, split_tone_decode and
split_tone_encode). The rules follow CHIRP (github.com/kk7ds/chirp,
chirp/chirp_common.py, read 2026-10-16); this is a reimplementation, not a
copy of its code.

A radio channel here is the bridge's dict (addons/uvpro_bridge.py
channel_out): rx_freq / tx_freq in MHz, rx_sub_audio / tx_sub_audio as
"" | "100.0" | "D023", power, bandwidth, scan, tx_disable, rx_mod/tx_mod,
talk_around, mute.
"""

import csv
import io

CSV_FORMAT = ["Location", "Name", "Frequency", "Duplex", "Offset", "Tone", "rToneFreq", "cToneFreq",
              "DtcsCode", "DtcsPolarity", "RxDtcsCode", "CrossMode", "Mode", "TStep", "Skip", "Power",
              "Comment", "URCALL", "RPT1CALL", "RPT2CALL", "DVCODE"]
TONES = [67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8, 97.4, 100.0, 103.5, 107.2, 110.9,
         114.8, 118.8, 123.0, 127.3, 131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9,
         171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5, 203.5, 206.5, 210.7, 218.1,
         225.7, 229.1, 233.6, 241.8, 250.3, 254.1]
DTCS_CODES = [23, 25, 26, 31, 32, 36, 43, 47, 51, 53, 54, 65, 71, 72, 73, 74, 114, 115, 116, 122, 125, 131,
              132, 134, 143, 145, 152, 155, 156, 162, 165, 172, 174, 205, 212, 223, 225, 226, 243, 244, 245,
              246, 251, 252, 255, 261, 263, 265, 266, 271, 274, 306, 311, 315, 325, 331, 332, 343, 346, 351,
              356, 364, 365, 371, 411, 412, 413, 423, 431, 432, 445, 446, 452, 454, 455, 462, 464, 465, 466,
              503, 506, 516, 523, 526, 532, 546, 565, 606, 612, 624, 627, 631, 632, 654, 662, 664, 703, 712,
              723, 731, 732, 734, 743, 754]
CROSS_MODES = ["Tone->Tone", "DTCS->", "->DTCS", "Tone->DTCS", "DTCS->Tone", "->Tone", "DTCS->DTCS", "Tone->"]
# CHIRP's TONE_MODES the UV-PRO can hold (it has no reverse-tone modes: no
# DTCS-R / TSQL-R), and the Skip values it can hold (no "P", priority).
TONE_MODES = ["", "Tone", "TSQL", "DTCS", "Cross"]
SKIP_VALUES = ["", "S"]
POWERS = {"high": "High", "med": "Medium", "low": "Low"}
MODES = {"FM": "FM", "NFM": "FM", "AM": "AM", "NAM": "AM"}     # CHIRP's Mode -> the radio's modulation
POWER_IN = {"high": "high", "medium": "med", "med": "med", "mid": "med", "low": "low"}
SPLIT_HZ = 70_000_000         # chirp_common.split_to_offset: a TX more than 70 MHz away is "split"

TONE_STRS = ["%.1f" % t for t in TONES]
DTCS_STRS = ["%03i" % d for d in DTCS_CODES]
DUPLEXES = ["", "+", "-", "split", "off"]
# What the UV-PRO can tune, RX and TX together (any frequency outside these is
# refused here; the radio's read-back is the real check: it rewrites a TX it
# can't send, as it did with an airband channel on linux-station, 2026-10-16).
FREQ_HZ = (18_000_000, 1_000_000_000)


def hz(mhz):
    return int(round(float(mhz) * 1_000_000))


def format_freq(f_hz):
    """chirp_common.format_freq: Hz -> "146.520000"."""
    return "%i.%06i" % (f_hz // 1_000_000, f_hz % 1_000_000)


# --- sub-audio: the radio's "" / "100.0" / "D023" <-> CHIRP's (mode, value, polarity)

def tone_spec(s):
    s = (s or "").strip().upper()
    if not s:
        return ("", None, None)
    if s.startswith("D"):
        return ("DTCS", int(s[1:]), "N")
    return ("Tone", round(float(s), 1), None)


def sub_audio(spec):
    mode, val, _pol = spec
    if mode == "Tone":
        return "%.1f" % float(val)
    if mode == "DTCS":
        return "D%03i" % int(val)
    return ""


# --- radio channel -> CHIRP memory

def to_memory(ch, location=None):
    """A CHIRP memory (CSV columns, as strings formatted as CHIRP writes them)
    plus "id" and "extra" (talk_around, mute: CHIRP keeps those in a
    driver's extra settings)."""
    rx, tx = hz(ch["rx_freq"]), hz(ch["tx_freq"])
    if ch.get("tx_disable"):
        duplex, offset = "off", 0
    elif abs(tx - rx) > SPLIT_HZ:
        duplex, offset = "split", tx
    else:
        d = tx - rx
        duplex, offset = ("-" if d < 0 else "+" if d > 0 else ""), abs(d)

    # chirp_common.split_tone_decode, with CHIRP's defaults for unused fields
    tmode, rtone, ctone, dtcs, rx_dtcs, cross = "", 88.5, 88.5, 23, 23, "Tone->Tone"
    txm, txv, txp = tone_spec(ch.get("tx_sub_audio"))
    rxm, rxv, rxp = tone_spec(ch.get("rx_sub_audio"))
    pol = (txp or "N") + (rxp or "N")
    if not txm and not rxm:
        pass
    elif txm == "Tone" and not rxm:
        tmode, rtone = "Tone", txv
    elif txm == rxm == "Tone" and txv == rxv:
        tmode, ctone = "TSQL", txv
    elif txm == rxm == "DTCS" and txv == rxv:
        tmode, dtcs = "DTCS", txv
    else:
        tmode, cross = "Cross", f"{txm}->{rxm}"
        if txm == "Tone":
            rtone = txv
        elif txm == "DTCS":
            dtcs = txv
        if rxm == "Tone":
            ctone = rxv
        elif rxm == "DTCS":
            rx_dtcs = rxv

    narrow = ch.get("bandwidth") == "NARROW"
    mod = ch.get("rx_mod", "FM")
    mode = {"FM": "NFM" if narrow else "FM", "AM": "NAM" if narrow else "AM"}.get(mod, mod)
    return {
        "id": ch["id"],
        "Location": "%i" % (ch["id"] + 1 if location is None else location),
        "Name": ch.get("name") or "",
        "Frequency": format_freq(rx),
        "Duplex": duplex,
        "Offset": format_freq(offset),
        "Tone": tmode,
        "rToneFreq": "%.1f" % rtone,
        "cToneFreq": "%.1f" % ctone,
        "DtcsCode": "%03i" % dtcs,
        "DtcsPolarity": pol,
        "RxDtcsCode": "%03i" % rx_dtcs,
        "CrossMode": cross,
        "Mode": mode,
        "TStep": "5.00",              # the UV-PRO keeps no per-channel step; CHIRP's default
        "Skip": "" if ch.get("scan") else "S",
        "Power": POWERS.get(ch.get("power"), str(ch.get("power"))),
        "Comment": "",
        "URCALL": "", "RPT1CALL": "", "RPT2CALL": "", "DVCODE": "",
        "extra": {"talk_around": bool(ch.get("talk_around")), "mute": bool(ch.get("mute"))},
    }


def tone_summary(m):
    t = m["Tone"]
    if t == "Tone":
        return f"Tone {m['rToneFreq']}"
    if t == "TSQL":
        return f"TSQL {m['cToneFreq']}"
    if t == "DTCS":
        return f"DTCS {m['DtcsCode']}"
    if t == "Cross":
        tx, rx = m["CrossMode"].split("->", 1)
        side = lambda mode, tone, code: (f"Tone {tone}" if mode == "Tone" else f"DTCS {code}" if mode == "DTCS" else "none")
        return f"Cross {side(tx, m['rToneFreq'], m['DtcsCode'])} / {side(rx, m['cToneFreq'], m['RxDtcsCode'])}"
    return ""


# --- CHIRP memory edits -> radio channel values

def normalize(field, v):
    """A value as CHIRP writes it ("100" -> "100.0", "23" -> "023")."""
    if isinstance(v, bool):
        return v
    v = "" if v is None else str(v).strip()
    try:
        if field in ("rToneFreq", "cToneFreq") and v:
            return "%.1f" % float(v)
        if field in ("DtcsCode", "RxDtcsCode") and v:
            return "%03i" % int(v)
        if field in ("Frequency", "Offset") and v:
            return format_freq(hz(v))
    except ValueError:
        pass
    if field == "Power":
        return POWERS.get(POWER_IN.get(v.lower(), ""), v)
    if field == "DtcsPolarity":
        return v.upper()
    return v


def _freqs(mem, cur, ch):
    """chirp_common's Frequency / Duplex / Offset -> the radio's rx_freq,
    tx_freq (MHz) and tx_disable: (why, values)."""
    try:
        rx = hz(mem.get("Frequency", cur["Frequency"]))
    except (TypeError, ValueError):
        return f"Frequency {mem.get('Frequency')!r}: not a frequency in MHz.", None
    duplex = normalize("Duplex", mem.get("Duplex", cur["Duplex"]))
    if duplex not in DUPLEXES:
        return f"Duplex {duplex!r}: CHIRP's are {', '.join(d or '(simplex)' for d in DUPLEXES)}.", None
    try:
        off = hz(mem.get("Offset", cur["Offset"]) or 0)
    except (TypeError, ValueError):
        return f"Offset {mem.get('Offset')!r}: not a frequency in MHz.", None
    if duplex == "+":
        tx = rx + off
    elif duplex == "-":
        tx = rx - off
    elif duplex == "split":
        tx = off
    else:
        tx = rx
    lo, hi = FREQ_HZ
    for label, f in (("Frequency", rx),) + ((("TX frequency", tx),) if duplex != "off" else ()):
        if not lo <= f < hi:
            return f"{label} {format_freq(max(f, 0))} MHz is outside what the UV-PRO tunes.", None
    out = {"rx_freq": rx / 1e6, "tx_disable": duplex == "off"}
    if duplex == "off":
        # CHIRP has no TX on an "off" row; the radio keeps one: leave it, or RX if there was none
        out["tx_freq"] = ch["tx_freq"] if ch.get("tx_disable") else rx / 1e6
    else:
        out["tx_freq"] = tx / 1e6
    return None, {k: v for k, v in out.items()
                  if (abs(v - ch.get(k, 0)) > 5e-7 if k != "tx_disable" else v != bool(ch.get(k)))}


def from_memory(mem, ch):
    """Check a whole CHIRP memory MEM against what the UV-PRO can hold and
    what may be changed from here, and turn it into the radio's channel
    values. Returns (why, values): why is None and values the fields that
    differ from CH; or why says what is wrong."""
    cur = to_memory(ch)
    why, freqs = _freqs(mem, cur, ch)
    if why:
        return why, None
    name = mem.get("Name", cur["Name"])
    if not isinstance(name, str):
        return "Name: not text.", None
    if len(name.encode()) > 10:
        return f"Name {name!r}: longer than the radio's 10 characters.", None
    tmode = mem.get("Tone", cur["Tone"])
    if tmode not in TONE_MODES:
        return f"Tone mode {tmode!r}: the UV-PRO can't hold it (it has {', '.join(m or 'none' for m in TONE_MODES)}).", None
    for f in ("rToneFreq", "cToneFreq"):
        if mem.get(f, cur[f]) not in TONE_STRS and mem.get(f, cur[f]) != cur[f]:
            return f"{f} {mem.get(f)!r} is not one of CHIRP's tones.", None
    for f in ("DtcsCode", "RxDtcsCode"):
        if mem.get(f, cur[f]) not in DTCS_STRS and mem.get(f, cur[f]) != cur[f]:
            return f"{f} {mem.get(f)!r} is not one of CHIRP's DTCS codes.", None
    if mem.get("DtcsPolarity", cur["DtcsPolarity"]) != "NN":
        return "DtcsPolarity: the UV-PRO holds normal polarity (NN) only.", None
    cross = mem.get("CrossMode", cur["CrossMode"])
    if cross not in CROSS_MODES:
        return f"CrossMode {cross!r} is not one of CHIRP's cross modes.", None
    mode = mem.get("Mode", cur["Mode"])
    if mode != cur["Mode"] and mode not in MODES:
        return f"Mode {mode!r}: the UV-PRO has {', '.join(MODES)}.", None
    skip = mem.get("Skip", cur["Skip"])
    if skip not in SKIP_VALUES:
        return f"Skip {skip!r}: the UV-PRO has scan on or off only (\"\" or \"S\").", None
    power = mem.get("Power", cur["Power"])
    if power not in POWERS.values():
        return f"Power {power!r}: the UV-PRO has {', '.join(POWERS.values())}.", None
    extra = {**cur["extra"], **(mem.get("extra") or {})}
    if not all(isinstance(v, bool) for v in extra.values()) or set(extra) - {"talk_around", "mute"}:
        return "extra: talk_around and mute are on/off only.", None

    # chirp_common.split_tone_encode
    rtone, ctone = mem.get("rToneFreq", cur["rToneFreq"]), mem.get("cToneFreq", cur["cToneFreq"])
    dtcs, rx_dtcs = mem.get("DtcsCode", cur["DtcsCode"]), mem.get("RxDtcsCode", cur["RxDtcsCode"])
    tx = rx = ("", None, None)
    if tmode == "Tone":
        tx = ("Tone", rtone, None)
    elif tmode == "TSQL":
        tx = rx = ("Tone", ctone, None)
    elif tmode == "DTCS":
        tx = rx = ("DTCS", dtcs, "N")
    elif tmode == "Cross":
        txm, rxm = cross.split("->", 1)
        tx = ("Tone", rtone, None) if txm == "Tone" else ("DTCS", dtcs, "N") if txm == "DTCS" else tx
        rx = ("Tone", ctone, None) if rxm == "Tone" else ("DTCS", rx_dtcs, "N") if rxm == "DTCS" else rx

    want = {**freqs, "name": name, "tx_sub_audio": sub_audio(tx), "rx_sub_audio": sub_audio(rx),
            "power": next(k for k, v in POWERS.items() if v == power),
            "bandwidth": "NARROW" if mode in ("NFM", "NAM") else "WIDE",
            **({"rx_mod": MODES[mode], "tx_mod": MODES[mode]} if mode in MODES and mode != cur["Mode"] else {}),
            "scan": skip == "", "talk_around": extra["talk_around"], "mute": extra["mute"]}
    return None, {k: v for k, v in want.items() if v != ch.get(k)}


# --- CSV

def to_csv(memories):
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(CSV_FORMAT)
    for m in memories:
        w.writerow([m[c] for c in CSV_FORMAT])
    return out.getvalue()


def parse_csv(text):
    """CHIRP CSV -> (why, rows): rows are dicts by column name, values
    normalized as CHIRP writes them. Comment and the D-STAR columns are kept
    but never written to the UV-PRO (it has no place for them)."""
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as e:
        return f"Not a CSV file: {e}", None
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return "The file is empty.", None
    head = [h.strip() for h in rows[0]]
    need = ["Location", "Name", "Frequency", "Duplex", "Offset", "Tone", "rToneFreq", "cToneFreq",
            "DtcsCode", "DtcsPolarity", "RxDtcsCode", "CrossMode", "Mode", "Skip", "Power"]
    missing = [c for c in need if c not in head]
    if missing:
        return f"Not a CHIRP CSV: no {', '.join(missing)} column.", None
    out = []
    for r in rows[1:]:
        d = {h: (r[i].strip() if i < len(r) else "") for i, h in enumerate(head)}
        out.append({k: normalize(k, v) for k, v in d.items()})
    return None, out
