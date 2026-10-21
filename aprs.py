"""APRS: decode what a UI frame says, and build the few frames we send.

Pure: no I/O apart from reading aprs_devices.json once. The hub calls
decode() for every frame it hears; the store, the page, messaging and the
iGate work from the dict it returns. decode() never raises: a packet it can't
read becomes {"type": "unknown", "error": ...} and is still kept and shown raw.

References: APRS Protocol Reference 1.0.1 (chapter numbers below), the 1.1
and 1.2 addenda (reply-acks, base-91 comment telemetry, !DAO!), and real
traffic heard at a test station (tests/fixtures/aprs/rx-sample.jsonl).

    decode("N9ABC-7", "APZRR0", ["WIDE2-1"], "!4053.64N/10506.23W-hello")
    split_tnc2("N9ABC-7>APZRR0,WIDE2-1:!4053.64N/10506.23W-hello")
"""
import json
import math
import re
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Data type identifiers (1.0.1 ch. 5) this module decodes.
POSITION_NOTS = "!="          # no timestamp; "=" = messaging capable
POSITION_TS = "/@"            # timestamp first; "@" = messaging capable
MICE = "`'\x1c\x1d"           # current and old Mic-E
NO_DATA_TYPE = ("BEACON", "ID", "UIDIGI")

WEATHER_FIELDS = {            # 1.0.1 ch. 12: letter, width, name, scale
    "c": (3, "wind_dir", 1), "s": (3, "wind_speed_mph", 1), "g": (3, "gust_mph", 1),
    "t": (3, "temp_f", 1), "r": (3, "rain_1h_in", 0.01), "p": (3, "rain_24h_in", 0.01),
    "P": (3, "rain_midnight_in", 0.01), "h": (2, "humidity", 1), "b": (5, "pressure_mbar", 0.1),
    "L": (3, "luminosity_wm2", 1), "l": (3, "luminosity_wm2", 1, 1000), "#": (3, "rain_raw", 1),
}

MICE_MESSAGES = ["Emergency", "Priority", "Special", "Committed", "Returning", "In Service",
                 "En Route", "Off Duty"]
MICE_CUSTOM = ["Emergency", "Custom-6", "Custom-5", "Custom-4", "Custom-3", "Custom-2",
               "Custom-1", "Custom-0"]


# ------------------------------------------------------------ small helpers

def _finite(v):
    """RF is hostile: '1e999' and 'nan' parse as floats but aren't JSON."""
    if not math.isfinite(v):
        raise ValueError(f"not a finite number: {v!r}")
    return v


def norm_call(c):
    """'n9abc-0*' -> 'N9ABC': upper case, no repeated-mark, SSID 0 dropped
    (inner third-party headers carry '-0' literally; AX.25 headers never do)."""
    c = (c or "").strip().upper().rstrip("*")
    if c.endswith("-0"):
        c = c[:-2]
    return c


def base_call(c):
    return norm_call(c).partition("-")[0]


def b91(s):
    """Base-91 digits (each char - 33), most significant first."""
    n = 0
    for ch in s:
        v = ord(ch) - 33
        if not 0 <= v <= 90:
            raise ValueError(f"not base-91: {s!r}")
        n = n * 91 + v
    return n


def split_tnc2(line):
    """'SRC>DEST,PATH1,PATH2*:info' -> (src, dest, [path...], info), or None."""
    head, sep, info = line.partition(":")
    if not sep or ">" not in head:
        return None
    src, _, rest = head.partition(">")
    parts = rest.split(",")
    if not src or not parts[0]:
        return None
    return src, parts[0], parts[1:], info


def strip_line_end(info):
    """Drop one trailing line ending (2,486 heard packets end in CR)."""
    if info.endswith("\r\n"):
        return info[:-2]
    if info.endswith(("\r", "\n")):
        return info[:-1]
    return info


# ------------------------------------------------------------ device table

_devices = None


def _device_table():
    global _devices
    if _devices is None:
        try:
            d = json.loads((HERE / "aprs_devices.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {"tocalls": {}, "mice": {}, "micelegacy": {}}
        pats = []
        for key, dev in d.get("tocalls", {}).items():
            if "?" in key or "*" in key:
                rx = re.compile("^" + re.escape(key).replace(r"\?", ".").replace(r"\*", ".*") + "$")
                pats.append((-len(key.replace("*", "")), key, rx, dev))
        pats.sort()
        _devices = {"exact": {k: v for k, v in d.get("tocalls", {}).items() if "?" not in k and "*" not in k},
                    "patterns": [(rx, dev) for _, _, rx, dev in pats],
                    "mice": d.get("mice", {}), "micelegacy": d.get("micelegacy", {})}
    return _devices


def _dev(entry):
    if not entry:
        return None
    return " ".join(x for x in (entry.get("vendor"), entry.get("model")) if x and x != "Unknown") or None


def device_from_tocall(dest):
    t = _device_table()
    dest = base_call(dest)
    if dest in t["exact"]:
        return _dev(t["exact"][dest])
    for rx, dev in t["patterns"]:
        if rx.match(dest):
            return _dev(dev)
    return None


# ------------------------------------------------------------ timestamps

TS_RX = re.compile(r"^(\d{6})([zh/])")


def parse_timestamp(s):
    """7-char APRS timestamp (1.0.1 ch. 6) -> dict, or None."""
    m = TS_RX.match(s)
    if not m:
        return None
    a, b, c = int(m.group(1)[0:2]), int(m.group(1)[2:4]), int(m.group(1)[4:6])
    if m.group(2) == "h":
        return {"kind": "hms", "hour": a, "minute": b, "second": c}
    return {"kind": "dhm_z" if m.group(2) == "z" else "dhm_local", "day": a, "hour": b, "minute": c}


def reported_time(ts, received):
    """When a timestamped report was made, as epoch seconds, from the time it
    was received (the timestamp has no month/year, or no date). Local-time
    stamps: None (the sender's time zone is unknown)."""
    if not ts or ts["kind"] == "dhm_local":
        return None
    r = time.gmtime(received)
    try:
        if ts["kind"] == "mdhm":           # positionless weather: month, day, hour, minute (UTC)
            e = _timegm(time.struct_time((r.tm_year, ts["month"], ts["day"], ts["hour"], ts["minute"], 0, 0, 0, 0)))
            if not (1 <= ts["month"] <= 12 and 1 <= ts["day"] <= 31):
                return None
            return _timegm(time.struct_time((r.tm_year - 1, ts["month"], ts["day"], ts["hour"], ts["minute"],
                                             0, 0, 0, 0))) if e > received + 86400 else e
        if ts["kind"] == "hms":
            t = time.struct_time((r.tm_year, r.tm_mon, r.tm_mday, ts["hour"], ts["minute"], ts["second"], 0, 0, 0))
            e = _timegm(t)
            return e - 86400 if e > received + 3600 else e
        t = time.struct_time((r.tm_year, r.tm_mon, ts["day"], ts["hour"], ts["minute"], 0, 0, 0, 0))
        e = _timegm(t)
        if e > received + 86400:           # last month's day
            mon, year = (r.tm_mon - 1, r.tm_year) if r.tm_mon > 1 else (12, r.tm_year - 1)
            e = _timegm(time.struct_time((year, mon, ts["day"], ts["hour"], ts["minute"], 0, 0, 0, 0)))
        return e
    except (ValueError, OverflowError):
        return None


def _timegm(t):
    import calendar
    return calendar.timegm(t)


# ------------------------------------------------------------ positions

UNCOMP_RX = re.compile(r"^(\d{2}[\d ]{2}\.[\d ]{2})([NSns])(.)(\d{3}[\d ]{2}\.[\d ]{2})([EWew])(.)")
COMP_TABLES = set("/\\ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij")
AMBIG_BOX_MIN = {0: 0.0, 1: 0.1, 2: 1.0, 3: 10.0, 4: 60.0}


def _blank(text, amb):
    """Blank the last `amb` digits of a ddmm.mm/dddmm.mm string (ambiguity)."""
    chars = list(text)
    for i in [i for i, ch in enumerate(chars) if ch.isdigit() or ch == " "][::-1][:amb]:
        chars[i] = " "
    return "".join(chars)


def _check_range(lat, lon):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("position out of range")


def _ddmm(text, deg_digits):
    """'3320.  ' -> (degrees, ambiguity). Spaces replace digits from the right."""
    spaces = text.count(" ")
    t = text.replace(" ", "0")
    deg = int(t[:deg_digits])
    minutes = float(t[deg_digits:])
    if minutes >= 60:
        raise ValueError(f"minutes {minutes} in {text!r}")
    return deg + minutes / 60, spaces


def _position_uncompressed(d, s):
    m = UNCOMP_RX.match(s)
    if not m:
        raise ValueError(f"not a position: {s[:20]!r}")
    lat, amb = _ddmm(m.group(1), 2)
    amb = min(amb, 4)
    lon, _ = _ddmm(_blank(m.group(4), amb), 3)   # longitude takes latitude's ambiguity (1.0.1 ch. 6)
    box = AMBIG_BOX_MIN[amb] / 60
    lat, lon = lat + box / 2, lon + box / 2
    if m.group(2).upper() == "S":
        lat = -lat
    if m.group(5).upper() == "W":
        lon = -lon
    _check_range(lat, lon)
    d.update(lat=round(lat, 6), lon=round(lon, 6), ambiguity=amb, box_deg=round(box, 6),
             symbol=m.group(3) + m.group(6), compressed=False)
    return s[19:]


def _position_compressed(d, s):
    if len(s) < 13 or s[0] not in COMP_TABLES:
        raise ValueError("not a compressed position")
    table = s[0]
    if "a" <= table <= "j":                 # overlay digits 0-9 travel as a-j
        table = chr(ord(table) - ord("a") + ord("0"))
    lat = 90 - b91(s[1:5]) / 380926
    lon = -180 + b91(s[5:9]) / 190463
    _check_range(lat, lon)
    code, c, sp, t = s[9], s[10], s[11], s[12]
    d.update(lat=round(lat, 6), lon=round(lon, 6), ambiguity=0, box_deg=0.0,
             symbol=table + code, compressed=True)
    if c != " ":                            # space: no course/speed, range or altitude
        tbits = ord(t) - 33
        if 0 <= tbits <= 90 and (tbits & 0x18) == 0x10:
            d["alt_ft"] = int(1.002 ** b91(c + sp))       # 1.0.1 ch. 9: cs 4610 is 10,004 ft
        elif c == "{":
            d["range_mi"] = round(2 * 1.08 ** (ord(sp) - 33), 1)
        elif "!" <= c <= "z":
            course, kt = (ord(c) - 33) * 4, 1.08 ** (ord(sp) - 33) - 1
            if code == "_":                 # 1.0.1 ch. 12: a weather station's cs is the wind
                d["weather"] = {"wind_dir": course, "wind_speed_mph": round(kt * 1.15078)}
            else:
                d["course"] = course
                d["speed_kt"] = round(kt, 1)
    return s[13:]


def _position(d, s):
    """Uncompressed or compressed position, then the data extension and the
    comment's extras. Returns nothing; fills d."""
    rest = _position_uncompressed(d, s) if s[:1].isdigit() else _position_compressed(d, s)
    _comment(d, rest)


DATA_EXT = [
    (re.compile(r"^PHG(\d)(\d)(\d)(\d)"), "phg"),
    (re.compile(r"^RNG(\d{4})"), "rng"),
    (re.compile(r"^DFS(\d)(\d)(\d)(\d)"), "dfs"),
]
CSE_SPD = re.compile(r"^(\d{3}|\.{3}| {3})/(\d{3}|\.{3}| {3})")
ALT_RX = re.compile(r"/A=(-?\d{5,6})")
DAO_RX = re.compile(r"!([Ww])([\x21-\x7b ])([\x21-\x7b ])!")
B91_TELEM = re.compile(r"\|([!-{]{2})((?:[!-{]{2}){1,6})\|")


def _comment(d, c):
    """Data extension, weather (after a _ symbol), altitude, !DAO!, base-91
    telemetry, then what's left is the comment."""
    weather_symbol = d.get("symbol", "")[1:] == "_"
    m = CSE_SPD.match(c)
    if m:
        a, b = m.group(1), m.group(2)
        a = int(a) if a.isdigit() else None
        b = int(b) if b.isdigit() else None
        if weather_symbol:                 # 1.0.1 ch. 12: wind direction / speed (mph)
            wx = d.setdefault("weather", {})
            if a is not None and 0 <= a <= 360:
                wx["wind_dir"] = a
            if b is not None:
                wx["wind_speed_mph"] = b
        else:
            if a is not None and 1 <= a <= 360:
                d["course"] = a                # 360 is north; 000 means unknown
            if b is not None:
                d["speed_kt"] = b
        c = c[7:]
    elif weather_symbol and c[:1] == "-" and re.match(r"^-\d\d/\d{3}", c):
        # Heard here: "-01/001" (an invalid direction); keep the speed.
        d.setdefault("weather", {})["wind_speed_mph"] = int(c[4:7])
        c = c[7:]
    else:
        for rx, key in DATA_EXT:
            m = rx.match(c)
            if m:
                d[key] = m.group(0)[3:]
                if key == "phg":
                    p, h, g, dr = (int(x) for x in m.groups())
                    d["phg_power_w"] = p * p
                    d["phg_height_ft"] = 10 * 2 ** h
                    d["phg_gain_db"] = g
                    d["phg_dir"] = dr * 45 if dr else None
                c = c[7:]
                break
    if weather_symbol:
        wx, rest = _weather_fields(c, positionless=False)
        if wx or "weather" in d:
            d.setdefault("weather", {}).update(wx)
            d["type"] = "weather"
            c = rest
    m = ALT_RX.search(c)
    if m:
        d["alt_ft"] = int(m.group(1))
        c = c[:m.start()] + c[m.end():]
    m = DAO_RX.search(c)
    if m:
        d["dao"] = m.group(0)
        if d.get("ambiguity", 0) == 0 and not d.get("compressed") and "lat" in d:
            _apply_dao(d, m.group(1), m.group(2), m.group(3))
        c = c[:m.start()] + c[m.end():]
    m = B91_TELEM.search(c)
    if m:
        vals = m.group(2)
        nums = [b91(vals[i:i + 2]) for i in range(0, len(vals), 2)]
        d["telemetry"] = {"seq": b91(m.group(1)), "analog": nums[:5],
                          "bits": format(nums[5], "08b")[::-1] if len(nums) > 5 else None}
        c = c[:m.start()] + c[m.end():]
    d["comment"] = c.strip()


def _apply_dao(d, kind, a, b):
    """!DAO! (APRS 1.2): one more digit of precision. W: human digits
    (thousandths of a minute); w: base-91 (1/91 of a hundredth)."""
    try:
        if kind == "W":
            dl, dn = int(a) / 1000 / 60, int(b) / 1000 / 60
        else:
            dl, dn = (ord(a) - 33) / 91 * 0.01 / 60, (ord(b) - 33) / 91 * 0.01 / 60
    except ValueError:
        return
    d["lat"] = round(d["lat"] + (dl if d["lat"] >= 0 else -dl), 7)
    d["lon"] = round(d["lon"] + (dn if d["lon"] >= 0 else -dn), 7)


def _weather_fields(s, positionless=True):
    """'g007t072r000P000b10150...' -> ({...}, rest). Missing values ('...',
    spaces) are left out; b00000 (no pressure) too. After a position the wind
    came as DDD/SSS, and "s" is snowfall (inches, last 24 h); in a positionless
    report "c"/"s" are the wind (1.0.1 ch. 12)."""
    wx, i = {}, 0
    while i < len(s) and s[i] in WEATHER_FIELDS:
        spec = WEATHER_FIELDS[s[i]]
        if s[i] == "s" and not positionless:
            spec = (3, "snow_24h_in", 0.1)
        width, name, scale = spec[0], spec[1], spec[2]
        raw = s[i + 1:i + 1 + width]
        if len(raw) < width:
            break
        if raw.strip(" .") == "":
            i += 1 + width
            continue
        try:
            v = int(raw)
        except ValueError:
            break
        if name == "humidity" and v == 0:
            v = 100
        if name == "pressure_mbar" and v == 0:
            i += 1 + width
            continue
        v = v * scale + (spec[3] if len(spec) > 3 else 0)
        wx[name] = round(v, 2) if isinstance(v, float) else v
        i += 1 + width
    return wx, s[i:]


# ------------------------------------------------------------ Mic-E (ch. 10)

MICE_DEST = re.compile(r"^[0-9A-LP-Z]{3}[0-9LP-Z]{3}$")   # 1.0.1 ch. 10: A-K only in the first three


def _mice_digit(ch):
    if "0" <= ch <= "9":
        return ch, 0
    if "A" <= ch <= "J":
        return str(ord(ch) - ord("A")), 2          # custom message bit
    if "P" <= ch <= "Y":
        return str(ord(ch) - ord("P")), 1          # standard message bit
    if ch in "KLZ":
        return " ", (2 if ch == "K" else 1 if ch == "Z" else 0)
    raise ValueError(f"not a Mic-E destination character: {ch!r}")


def _mice(d, dest, info):
    dest = base_call(dest)
    if not MICE_DEST.match(dest):
        raise ValueError(f"Mic-E destination {dest!r}")
    digits, bits = [], []
    for ch in dest:
        dg, bit = _mice_digit(ch)
        digits.append(dg)
        bits.append(bit)
    north = "P" <= dest[3] <= "Z"
    lon_offset = 100 if "P" <= dest[4] <= "Z" else 0
    west = "P" <= dest[5] <= "Z"
    lat, amb = _ddmm("".join(digits[:4]) + "." + "".join(digits[4:6]), 2)
    if len(info) < 9:
        raise ValueError("Mic-E info too short")
    deg = ord(info[1]) - 28 + lon_offset
    if 180 <= deg <= 189:
        deg -= 80
    elif 190 <= deg <= 199:
        deg -= 190
    mins = ord(info[2]) - 28
    if mins >= 60:
        mins -= 60
    hund = ord(info[3]) - 28
    if not (0 <= deg <= 179 and 0 <= mins <= 59 and 0 <= hund <= 99):
        raise ValueError("Mic-E longitude out of range")
    amb = min(amb, 4)
    box = AMBIG_BOX_MIN[amb] / 60
    lon_deg, _ = _ddmm(_blank(f"{deg:03d}{mins:02d}.{hund:02d}", amb), 3)
    lon = lon_deg + box / 2
    lat = lat + box / 2
    _check_range(lat, lon)
    sp, dc, se = ord(info[4]) - 28, ord(info[5]) - 28, ord(info[6]) - 28
    speed = sp * 10 + dc // 10
    if speed >= 800:
        speed -= 800
    course = (dc % 10) * 100 + se
    if course >= 400:
        course -= 400
    d.update(type="mice", lat=round(lat if north else -lat, 6), lon=round(-lon if west else lon, 6),
             ambiguity=min(amb, 4), box_deg=round(box, 6), symbol=info[8] + info[7], compressed=False,
             speed_kt=speed, course=course if 0 < course <= 360 else None)
    a, b, c = bits[0], bits[1], bits[2]
    if 2 in (a, b, c):
        n = (a == 2) << 2 | (b == 2) << 1 | (c == 2)
        d["mice_msg"] = MICE_CUSTOM[n]
    else:
        n = (a == 1) << 2 | (b == 1) << 1 | (c == 1)
        d["mice_msg"] = MICE_MESSAGES[n]
    rest = info[9:]
    rest = _mice_device(d, rest)
    m = re.match(r"^([!-{]{3})\}", rest)
    if m:
        d["alt_ft"] = round((b91(m.group(1)) - 10000) * 3.28084)
        rest = rest[4:]
    _comment_extras_only(d, rest)


def _mice_device(d, rest):
    t = _device_table()
    if rest[:1] in ("`", "'") and len(rest) >= 3 and rest[-2:] in t["mice"]:
        d["device"] = _dev(t["mice"][rest[-2:]])
        return rest[1:-2]
    if rest[:1] in (">", "]"):
        # The most specific marker first: "]...=" is a TM-D710, "]" alone a TM-D700.
        for key, dev in sorted(t["micelegacy"].items(), key=lambda kv: -len(kv[1].get("suffix", ""))):
            pre, suf = dev.get("prefix", ""), dev.get("suffix", "")
            if pre and rest.startswith(pre) and (not suf or rest.endswith(suf)):
                d["device"] = _dev(dev)
                rest = rest[len(pre):]
                return rest[:-len(suf)] if suf else rest
    if rest[:1] in ("`", "'"):
        return rest[1:]
    return rest


def _comment_extras_only(d, c):
    """For Mic-E: altitude, !DAO!, telemetry and the comment; no data
    extension or weather (course and speed came in the info field)."""
    m = ALT_RX.search(c)
    if m:
        d["alt_ft"] = int(m.group(1))
        c = c[:m.start()] + c[m.end():]
    m = DAO_RX.search(c)
    if m:
        d["dao"] = m.group(0)
        if d.get("ambiguity", 0) == 0:
            _apply_dao(d, m.group(1), m.group(2), m.group(3))
        c = c[:m.start()] + c[m.end():]
    m = B91_TELEM.search(c)
    if m:
        vals = m.group(2)
        nums = [b91(vals[i:i + 2]) for i in range(0, len(vals), 2)]
        d["telemetry"] = {"seq": b91(m.group(1)), "analog": nums[:5],
                          "bits": format(nums[5], "08b")[::-1] if len(nums) > 5 else None}
        c = c[:m.start()] + c[m.end():]
    d["comment"] = c.strip()


# ------------------------------------------------------------ messages (ch. 14)

ACK_RX = re.compile(r"^(ack|rej)([A-Za-z0-9]{1,5})(?:\}([A-Za-z0-9]{0,5}))?\s*$")
MSGNO_RX = re.compile(r"^([A-Za-z0-9]{1,5})(\}([A-Za-z0-9]{0,5}))?\s*$")
TELEM_META = ("PARM.", "UNIT.", "EQNS.", "BITS.")


def _message(d, info):
    if len(info) < 11 or info[10] != ":":
        raise ValueError("message without its 9-character addressee")
    addressee = info[1:10].strip().upper()
    text = info[11:]
    d["addressee"] = addressee
    m = ACK_RX.match(text)
    if m:
        d["type"] = m.group(1)                      # "ack" | "rej"
        d["msgno"] = m.group(2)
        d["replyack_capable"] = "}" in text
        d["reply_ack"] = m.group(3) or None
        return
    for pre in TELEM_META:
        # 1.0.1 ch. 13: a station's metadata is addressed to itself; anything
        # else that starts "PARM." is an ordinary message.
        if text.startswith(pre) and norm_call(addressee) == d["src"]:
            d["type"] = "telemetry_meta"
            d["meta"] = pre[:4].lower()
            body = text[len(pre):]
            if pre == "EQNS.":
                nums = []
                for x in body.split(",")[:15]:
                    x = x.strip()
                    nums.append(_finite(float(x)) if x else 0.0)     # heard: ",," for 0
                nums += [0.0] * (15 - len(nums))
                d["eqns"] = [nums[i:i + 3] for i in range(0, 15, 3)]
            elif pre == "BITS.":
                bits, _, title = body.partition(",")
                d["bits_sense"], d["project"] = bits.strip(), title.strip()
            else:
                d["names" if pre == "PARM." else "units"] = [x.strip() for x in body.split(",")]
            return
    if addressee.startswith(("BLN", "NWS-", "NWS_")):
        d["type"] = "bulletin"
        d["text"] = text
        return
    i = text.rfind("{")
    if i >= 0:
        m = MSGNO_RX.match(text[i + 1:])
        if m:
            d["msgno"] = m.group(1)
            d["replyack_capable"] = m.group(2) is not None
            d["reply_ack"] = m.group(3) or None
            text = text[:i]
    if text.startswith("?"):                # a directed query (ch. 15), msgno and reply-ack kept
        d["type"] = "query"
        d["query"] = text[1:].split("?")[0].strip()
        return
    d["type"] = "message"
    d["text"] = text.rstrip()


# ------------------------------------------------------------ telemetry (ch. 13)

def _telemetry(d, info):
    body = info[2:]
    parts = body.split(",")
    d["type"] = "telemetry"
    d["seq"] = parts[0].strip()
    analog = []
    for x in parts[1:6]:
        x = x.strip()
        try:
            analog.append(_finite(float(x)) if "." in x or "e" in x.lower() else int(x))
        except ValueError:
            analog.append(None)
    d["analog"] = analog
    if len(parts) > 6:
        bits = parts[6][:8]
        d["bits"] = bits if re.fullmatch(r"[01]{1,8}", bits) else None
        d["comment"] = parts[6][8:].strip()


def scale(eqn, raw):
    """Telemetry EQNS (a, b, c): a*x^2 + b*x + c."""
    if raw is None or eqn is None:
        return None
    a, b, c = eqn
    try:
        v = a * raw * raw + b * raw + c
    except OverflowError:
        return None
    return v if math.isfinite(v) else None


# ------------------------------------------------------------ the rest

def _object(d, info):
    if len(info) < 18:
        raise ValueError("object too short")
    d["type"] = "object"
    d["name"] = info[1:10].rstrip()
    d["live"] = info[10] == "*"
    if info[10] not in "*_":
        raise ValueError("object without live/killed flag")
    ts = parse_timestamp(info[11:18])
    d["timestamp"] = ts
    _position(d, info[18:])
    if d.get("type") == "weather":
        d["type"] = "object"
        d["is_weather"] = True


def _item(d, info):
    m = re.match(r"^\)([\x20-\x7e]{3,9}?)([!_])", info)
    if not m:
        raise ValueError("item without a name")
    d["type"] = "item"
    d["name"] = m.group(1)
    d["live"] = m.group(2) == "!"
    _position(d, info[m.end():])
    if d.get("type") == "weather":
        d["type"] = "item"


def _status(d, dest, info):
    d["type"] = "status"
    s = info[1:]
    ts = parse_timestamp(s[:7])
    if ts and ts["kind"] == "dhm_z":
        d["timestamp"] = ts
        s = s[7:]
    if MICE_DEST.match(base_call(dest)):
        m = re.match(r"^([!-{]{3})\}", s)
        if m:
            d["alt_ft"] = round((b91(m.group(1)) - 10000) * 3.28084)
            s = s[4:]
    d["text"] = s


def _positionless_weather(d, info):
    d["type"] = "weather"
    s = info[1:]
    if re.match(r"^\d{8}", s):
        d["timestamp"] = {"kind": "mdhm", "month": int(s[0:2]), "day": int(s[2:4]),
                          "hour": int(s[4:6]), "minute": int(s[6:8])}
        s = s[8:]
    wx, rest = _weather_fields(s)
    d["weather"] = wx
    d["comment"] = rest.strip()


def _capabilities(d, info):
    d["type"] = "capabilities"
    caps = {}
    for tok in info[1:].split(","):
        k, _, v = tok.strip().partition("=")
        if k:
            caps[k] = v or True
    d["capabilities"] = caps


def _third_party(d, info, depth):
    d["type"] = "third_party"
    d["third_party"] = True
    parts = split_tnc2(info[1:])
    if parts is None:
        raise ValueError("third-party packet without an inner header")
    src, dest, path, inner = parts
    d["inner_tcpip"] = any(p.rstrip("*").upper() in ("TCPIP", "TCPXX") for p in path)
    if depth >= 1:
        d["error"] = "nested third-party"
        return
    d["inner"] = decode(src, dest, path, inner, _depth=depth + 1)


def _printable(s):
    """Text, not binary: no control characters other than tab (a DEL or a
    Latin-1 byte inside digi ID text was heard and is still text)."""
    return all(ch >= " " or ch == "\t" for ch in s)


def decode(src, dest, path=(), info="", _depth=0):
    """What a UI frame says, as a dict (see the module docstring)."""
    if isinstance(info, (bytes, bytearray)):
        info = bytes(info).decode("latin-1")
    d = {"src": norm_call(src), "dest": norm_call(dest), "path": [p for p in path],
         "type": "unknown"}
    info = strip_line_end(info)
    d["info"] = info
    if "\r" in info or "\n" in info:
        d["error"] = "embedded line break"
    try:
        _dispatch(d, dest, info, _depth)
    except (ValueError, IndexError, KeyError, ZeroDivisionError, OverflowError) as e:
        d["type"] = "unknown"
        d["error"] = f"{type(e).__name__}: {e}"
    if "device" not in d:
        dev = device_from_tocall(dest) if d["type"] != "mice" else None
        if dev:
            d["device"] = dev
    return d


def _dispatch(d, dest, info, depth):
    if not info:
        d["error"] = "empty"
        return
    k = info[0]
    if info.startswith("!!"):
        d["error"] = "Peet Bros raw weather (!!) not decoded"
    elif k in POSITION_NOTS:
        d["type"] = "position"
        d["messaging"] = k == "="
        _position(d, info[1:])
    elif k in POSITION_TS:
        d["type"] = "position"
        d["messaging"] = k == "@"
        d["timestamp"] = parse_timestamp(info[1:8])
        if d["timestamp"] is None:
            raise ValueError("position without a valid timestamp")
        _position(d, info[8:])
    elif k in MICE:
        _mice(d, dest, info)
    elif k == ";":
        _object(d, info)
    elif k == ")":
        _item(d, info)
    elif k == ":":
        _message(d, info)
    elif k == "T" and info.startswith("T#"):
        _telemetry(d, info)
    elif k == ">":
        _status(d, dest, info)
    elif k == "<":
        _capabilities(d, info)
    elif k == "?":
        d["type"] = "query"
        d["query"] = info[1:].split("?")[0].strip()
    elif k == "_":
        _positionless_weather(d, info)
    elif k == "}":
        _third_party(d, info, depth)
    elif k in "$#*%&,[{":
        d["error"] = f"data type {k!r} not decoded"
    elif _printable(info):
        # Digi ID text and other plain beacons (BEACON, ID, UIDIGI, ...), and
        # "TC 0"-style TNC text: shown as status-like text.
        bang = info.find("!", 0, 40)
        if bang > 0 and info[bang + 1:bang + 2].isdigit():
            # 1.0.1 ch. 5: a "!" position up to 40 characters in (old digis)
            try:
                d["type"] = "position"
                _position(d, info[bang + 1:])
                d["beacon_text"] = info[:bang].strip()
                return
            except ValueError:
                pass
        d["type"] = "beacon_text"
        d["text"] = info


# ------------------------------------------------------------ building frames

BAD_TEXT = set("|~{}")


def _check_text(s, max_len):
    if len(s) > max_len:
        raise ValueError(f"longer than {max_len} characters")
    if any(not (" " <= ch <= "~") for ch in s):
        raise ValueError("not printable ASCII (no line breaks)")


def addressee(call):
    return norm_call(call).ljust(9)[:9]


def message_info(to, text, msgno=None, reply_ack=None, capable=True):
    """':ADDRESSEE:text{MM}' (reply-ack capable) or '{MM}AA' with a free ack."""
    _check_text(text, 67)
    if BAD_TEXT & set(text):
        raise ValueError("message text may not contain | ~ { }")
    tail = ""
    if msgno is not None:
        if not re.fullmatch(r"[A-Za-z0-9]{1,5}", msgno):
            raise ValueError(f"bad msgno {msgno!r}")
        tail = "{" + msgno
        if reply_ack is not None:
            if not re.fullmatch(r"[A-Za-z0-9]{1,5}", reply_ack):
                raise ValueError(f"bad reply-ack {reply_ack!r}")
            tail += "}" + reply_ack
        elif capable:
            tail += "}"
    return f":{addressee(to)}:{text}{tail}"


def ack_info(to, msgno, rej=False):
    if not re.fullmatch(r"[A-Za-z0-9]{1,5}", msgno or ""):
        raise ValueError(f"bad msgno {msgno!r}")
    return f":{addressee(to)}:{'rej' if rej else 'ack'}{msgno}"


def position_info(lat, lon, symbol="/-", comment="", ambiguity=0, messaging=False):
    """Uncompressed position without timestamp, with ambiguity 0-4."""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("position out of range")
    if not 0 <= ambiguity <= 4:
        raise ValueError("ambiguity 0-4")
    _check_text(comment, 43)
    if len(symbol) != 2:
        raise ValueError("symbol is table + code")

    def fmt(v, deg_digits, hemis):
        h = hemis[0] if v >= 0 else hemis[1]
        v = abs(v)
        deg = int(v)
        mins = (v - deg) * 60
        if ambiguity:
            # The cell the position is in, by truncation: rounding first can
            # carry into the next cell and put the box beside the position.
            step = AMBIG_BOX_MIN[ambiguity]
            mins = math.floor(mins / step + 1e-9) * step
        else:
            mins = round(mins, 2)
        if mins >= 60:
            deg, mins = deg + 1, 0.0
        s = f"{deg:0{deg_digits}d}{mins:05.2f}"
        if ambiguity:
            chars = list(s)
            idx = [i for i, ch in enumerate(chars) if ch.isdigit()][deg_digits:][::-1][:ambiguity]
            for i in idx:
                chars[i] = " "
            s = "".join(chars)
        return s + h
    return (("=" if messaging else "!") + fmt(lat, 2, "NS") + symbol[0] + fmt(lon, 3, "EW")
            + symbol[1] + comment)
