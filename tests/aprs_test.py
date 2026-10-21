"""aprs.py, the APRS decoder and encoder (APRS plan M1). Pure: no ports, no
radio, about a second.

  - worked examples from APRS 1.0.1 and the 1.1/1.2 addenda, and synthetic
    vectors for what the ranch hasn't heard (object kills, compressed
    altitude, !DAO! with ambiguity, reply-acks both ways, wrapped messages)
  - every packet the ranch heard (packet-logs/rx.jsonl, else the committed
    sample tests/fixtures/aprs/rx-sample.jsonl) decodes without raising;
    under 2 % are "unknown", and each data type decodes to its type
  - packets pinned from the log (PHG, Mic-E with device and altitude,
    compressed, weather wind, ambiguity, empty EQNS, ack00}, msgno + CR)
  - round trips for the frames we'll send (messages, acks, positions)
  - the oracle: aprslib (GPL, in its own venv, never imported here) reads the
    same packets; latitude, longitude and altitude must agree, and every other
    difference is one of the documented choices below. Skipped without the venv.

    python3 tests/aprs_test.py
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import aprs  # noqa: E402

ORACLE_PY = Path(os.environ.get("APRS_ORACLE_PY",
                                Path.home() / ".local/share/venvs/aprslib-oracle/bin/python"))
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


def near(a, b, tol=1e-4):
    return a is not None and b is not None and abs(a - b) <= tol


def check_true(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'ok  ' if cond else 'FAIL'}  {name:62} {detail}")


def dec(info, src="N0CALL", dest="APRS", path=()):
    return aprs.decode(src, dest, list(path), info)


def sub(d, keys):
    return {k: d.get(k) for k in keys}


# --- 1.0.1 ch. 8: uncompressed positions ----------------------------------
d = dec("!4903.50N/07201.75W-Test 001234")
check("uncompressed: lat/lon", (near(d["lat"], 49.058333), near(d["lon"], -72.029167)), (True, True))
check("  symbol, comment, type", sub(d, ("symbol", "comment", "type", "messaging")),
      {"symbol": "/-", "comment": "Test 001234", "type": "position", "messaging": False})
check("altitude in the comment", sub(dec("!4903.50N/07201.75W-Test /A=001234"), ("alt_ft", "comment")),
      {"alt_ft": 1234, "comment": "Test"})
d = dec("!49  .  N/072  .  W-")
check("ambiguity 4: the 1-degree box's centre",
      (d["ambiguity"], near(d["lat"], 49.5), near(d["lon"], -72.5)), (4, True, True))
d = dec("!4903.5 N/07201.7 W-")
check("ambiguity 1 (heard form: one space)", (d["ambiguity"], near(d["lat"], 49 + 3.55 / 60)), (1, True))
d = dec("/092345z4903.50N/07201.75W>Test1234")
check("timestamp DHM zulu", d["timestamp"], {"kind": "dhm_z", "day": 9, "hour": 23, "minute": 45})
check("  symbol and comment", sub(d, ("symbol", "comment")), {"symbol": "/>", "comment": "Test1234"})
d = dec("@092345/4903.50N/07201.75W>Test1234")
check("timestamp DHM local, messaging", (d["timestamp"]["kind"], d["messaging"]), ("dhm_local", True))
d = dec("=4903.50N/07201.75W#PHG5132")
check("PHG5132: 25 W, 20 ft, 3 dB, 90 deg",
      sub(d, ("phg_power_w", "phg_height_ft", "phg_gain_db", "phg_dir")),
      {"phg_power_w": 25, "phg_height_ft": 20, "phg_gain_db": 3, "phg_dir": 90})
check("course/speed", sub(dec("!4903.50N/07201.75W>088/036"), ("course", "speed_kt")),
      {"course": 88, "speed_kt": 36})
check("course 360 is north (000 is unknown)", dec("!4903.50N/07201.75W>360/010")["course"], 360)
check("course 000: unknown", dec("!4903.50N/07201.75W>000/010").get("course"), None)

# --- 1.0.1 ch. 9: compressed ------------------------------------------------
d = dec("!/5L!!<*e7>7P[")
check("compressed: 49.5 N, 72.75 W", (near(d["lat"], 49.5), near(d["lon"], -72.75)), (True, True))
check("  course 88, speed 36.2 kt", (d["course"], d["speed_kt"]), (88, 36.2))
check("compressed altitude (T says GGA): 10,004 ft", dec("=/5L!!<*e7OS]S")["alt_ft"], 10004)
check("compressed range: 20.1 mi", dec("!/5L!!<*e7>{?!")["range_mi"], 20.1)
d = dec("!/5L!!<*e7>  C")
check("compressed, space in c: no course/speed/range/altitude",
      [k for k in ("course", "speed_kt", "range_mi", "alt_ft") if k in d], [])
check("compressed overlay table a-j is digit 0-9", dec("!a5L!!<*e7>7P[")["symbol"], "0>")

# --- 1.0.1 ch. 12: weather ----------------------------------------------------
d = dec("_10090556c220s004g005t077r000p000P000h50b09900wRSW")
check("positionless weather", d["weather"],
      {"wind_dir": 220, "wind_speed_mph": 4, "gust_mph": 5, "temp_f": 77, "rain_1h_in": 0.0,
       "rain_24h_in": 0.0, "rain_midnight_in": 0.0, "humidity": 50, "pressure_mbar": 990.0})
check("  its timestamp (MDHM)", d["timestamp"], {"kind": "mdhm", "month": 10, "day": 9, "hour": 5, "minute": 56})
d = dec("!4903.50N/07201.75W_220/004g005t077r000p000P000h50b09900wRSW")
check("weather after a _ symbol: DDD/SSS is wind, not course",
      (d["type"], d["weather"]["wind_dir"], d["weather"]["wind_speed_mph"], "course" in d), ("weather", 220, 4, False))
check("  then the fields", sub(d["weather"], ("gust_mph", "temp_f", "humidity", "pressure_mbar")),
      {"gust_mph": 5, "temp_f": 77, "humidity": 50, "pressure_mbar": 990.0})
check("  after a position, s is snowfall", dec("!4903.50N/07201.75W_220/004s012")["weather"].get("snow_24h_in"), 1.2)
check("_ symbol with a plain comment: a position", dec("!4021.53N/10515.83W_NPS_004_coronado")["type"], "position")
check("negative temperature", dec("!4903.50N/07201.75W_220/004t-05")["weather"]["temp_f"], -5)
check("missing values left out (...)", "gust_mph" in dec("!4903.50N/07201.75W_220/004g...t077")["weather"], False)
check("b00000: no pressure", "pressure_mbar" in dec("!4903.50N/07201.75W_220/004b00000")["weather"], False)

# --- 1.2: !DAO! and base-91 comment telemetry --------------------------------
d = dec("!4903.50N/07201.75W-Test!W25!")
check("!DAO! W: one more digit", (near(d["lat"], 49 + 3.502 / 60, 1e-6), near(d["lon"], -(72 + 1.755 / 60), 1e-6)),
      (True, True))
check("  removed from the comment", d["comment"], "Test")
d = dec("!4903.5 N/07201.7 W-!W25!")
check("!DAO! ignored on an ambiguous position (no false precision)", near(d["lat"], 49 + 3.55 / 60, 1e-6), True)
check("base-91 comment telemetry |ss11|", dec("!4903.50N/07201.75W-Text |ss11|")["telemetry"],
      {"seq": 7544, "analog": [1472], "bits": None})

# --- objects and items (ch. 11) ------------------------------------------------
d = dec(";LEADER   *092345z4903.50N/07201.75W>088/036")
check("object", sub(d, ("type", "name", "live", "course", "speed_kt")),
      {"type": "object", "name": "LEADER", "live": True, "course": 88, "speed_kt": 36})
check("object killed", dec(";LEADER   _092345z4903.50N/07201.75W>")["live"], False)
check("item", sub(dec(")AID #2!4903.50N/07201.75WA"), ("type", "name", "live", "symbol")),
      {"type": "item", "name": "AID #2", "live": True, "symbol": "/A"})
check("item killed", dec(")AID #2_4903.50N/07201.75WA")["live"], False)

# --- messages (ch. 14, 1.1 reply-acks) -------------------------------------------
check("message with msgno", sub(dec(":WU2Z     :Testing{003"), ("type", "addressee", "text", "msgno")),
      {"type": "message", "addressee": "WU2Z", "text": "Testing", "msgno": "003"})
check("ack", sub(dec(":KB2ICI-14:ack003"), ("type", "addressee", "msgno")),
      {"type": "ack", "addressee": "KB2ICI-14", "msgno": "003"})
check("rej", dec(":KB2ICI-14:rej003")["type"], "rej")
check("reply-ack in a message {MM}AA", sub(dec(":N9ABC    :hi{AB}CD"), ("msgno", "reply_ack", "replyack_capable")),
      {"msgno": "AB", "reply_ack": "CD", "replyack_capable": True})
check("reply-ack capable {MM}", sub(dec(":N9ABC    :hi{AB}"), ("msgno", "reply_ack", "replyack_capable")),
      {"msgno": "AB", "reply_ack": None, "replyack_capable": True})
check("ackMM} (heard from ANSRVR)", sub(dec(":KM7AQB-2 :ack00}"), ("type", "msgno", "replyack_capable")),
      {"type": "ack", "msgno": "00", "replyack_capable": True})
check("ackMM}AA", sub(dec(":N9ABC    :ackAB}CD"), ("type", "msgno", "reply_ack")),
      {"type": "ack", "msgno": "AB", "reply_ack": "CD"})
check("ack1 is id 1, not 12", dec(":N9ABC    :ack1")["msgno"], "1")
check("5-char msgno with a trailing CR (heard)", dec(":N9ABC    :hi{97919\r")["msgno"], "97919")
check("message without msgno", sub(dec(":N9ABC    :hello"), ("type", "text", "msgno")),
      {"type": "message", "text": "hello", "msgno": None})
check("a { in the text that isn't a msgno stays text", dec(":N9ABC    :a {b} c")["text"], "a {b} c")
check("bulletin", dec(":BLN1     :Snow on the pass")["type"], "bulletin")
check("directed query", sub(dec(":N9ABC    :?APRSP"), ("type", "query")), {"type": "query", "query": "APRSP"})
check("PARM.", dec(":N0QBF-11 :PARM.Battery,Btemp,ATemp,Pres,Alt,Camra", src="N0QBF-11")["names"][:2], ["Battery", "Btemp"])
check("EQNS. with an empty coefficient (heard)", dec(":N7UV-9   :EQNS.0,0.075,0,0,,10,0", src="N7UV-9")["eqns"][:2],
      [[0.0, 0.075, 0.0], [0.0, 0.0, 10.0]])
check("BITS.", sub(dec(":N0QBF-11 :BITS.10110000,N0QBF's Big Balloon", src="N0QBF-11"), ("bits_sense", "project")),
      {"bits_sense": "10110000", "project": "N0QBF's Big Balloon"})

# --- telemetry, status, capabilities, queries (ch. 13, 16, 15) --------------------
d = dec("T#005,199,000,255,073,123,01101001")
check("telemetry", sub(d, ("type", "seq", "analog", "bits")),
      {"type": "telemetry", "seq": "005", "analog": [199, 0, 255, 73, 123], "bits": "01101001"})
check("T without # is TNC text, not telemetry", dec("TC 0")["type"], "beacon_text")
check("EQNS scale", aprs.scale([0.0, 5.2, 0.0], 10), 52.0)
check("status", sub(dec(">Net Control Center"), ("type", "text")), {"type": "status", "text": "Net Control Center"})
check("status with timestamp", dec(">092345zNet Control")["timestamp"]["kind"], "dhm_z")
check("capabilities", dec("<IGATE,MSG_CNT=3,LOC_CNT=10")["capabilities"],
      {"IGATE": True, "MSG_CNT": "3", "LOC_CNT": "10"})
check("general query", sub(dec("?APRS?"), ("type", "query")), {"type": "query", "query": "APRS"})

# --- third party (ch. 17) ---------------------------------------------------------
d = dec("}W1AW>APRS,TCPIP,W9EN-10*::N9ABC    :hello{7", src="W9EN-10")
check("third party: outer kept, inner decoded",
      (d["type"], d["third_party"], d["inner_tcpip"], d["inner"]["type"], d["inner"]["src"], d["inner"]["msgno"]),
      ("third_party", True, True, "message", "W1AW", "7"))
check("third party without TCPIP", dec("}W1AW>APRS,WIDE1-1:>status")["inner_tcpip"], False)
check("nested third party: an error, not decoded twice",
      dec("}A>B,TCPIP*:}C>D,TCPIP*:>x")["inner"].get("error"), "nested third-party")
check("inner -0 SSID normalised (heard: KJ7ROX-0)", dec("}KJ7ROX-0>APGRWO,TCPIP*::N9ABC    :x")["inner"]["src"], "KJ7ROX")

# --- odd and broken input never raises -------------------------------------------
check("empty info: unknown", (dec("")["type"], dec("")["error"]), ("unknown", "empty"))
check("digi ID text: beacon_text", dec("Pinal Pk. Az. for Info", dest="APN382")["type"], "beacon_text")
check("embedded line break flagged", dec("!4903.50N/07201.75W-a\r\nb")["error"], "embedded line break")
check("trailing CRLF stripped", dec(">hi\r\n")["text"], "hi")
for junk in ("!", "!99", "`", ";x", ")", ":x", "T#", "}", "!!0000", "/1234", "@zzzzzzz", "!/!!!!!!!!!!!",
             "`\x80\x80\x80\x80\x80\x80\x80\x80", ":ABCDEFGHI:" + "{" * 5, "\x00\x01"):
    try:
        t = aprs.decode("N0CALL", "S32U6T", [], junk)["type"]
        ok = isinstance(t, str)
    except Exception as e:  # noqa: BLE001
        ok = f"raised {e!r}"
    check(f"junk {junk!r} decodes without raising", ok, True)
check("norm_call", [aprs.norm_call(x) for x in ("n9abc-0*", "W7MOT-3*", " K7X ")], ["N9ABC", "W7MOT-3", "K7X"])
check("split_tnc2", aprs.split_tnc2("A>B,C*,D:x:y"), ("A", "B", ["C*", "D"], "x:y"))
check("device from tocall", aprs.device_from_tocall("APDW18"), "WB2OSZ DireWolf")

# --- round trips: what we'll send (M3/M4) -----------------------------------------
m = aprs.message_info("w1aw-9", "hi there", msgno="AB")
check("message built", m, ":W1AW-9   :hi there{AB}")
check("  decodes back", sub(dec(m), ("addressee", "text", "msgno", "replyack_capable")),
      {"addressee": "W1AW-9", "text": "hi there", "msgno": "AB", "replyack_capable": True})
check("message with a free ack", aprs.message_info("W1AW", "x", msgno="AB", reply_ack="CD"), ":W1AW     :x{AB}CD")
check("ack built (id verbatim)", aprs.ack_info("w1aw-9", "97919"), ":W1AW-9   :ack97919")
for bad in ("a{b", "a|b", "x" * 68, "line\rbreak"):
    try:
        aprs.message_info("W1AW", bad, msgno="1")
        r = "accepted"
    except ValueError:
        r = "refused"
    check(f"message text {bad[:12]!r}: refused", r, "refused")
p = aprs.position_info(40.8939, -105.1037, "/-", "N9ABC", ambiguity=1)
d = dec(p)
check("position built at ambiguity 1", (p[:21], d["ambiguity"], d["comment"]), ("!4053.6 N/10506.2 W-N", 1, "N9ABC"))
check("  its box holds the true position",
      (abs(d["lat"] - 40.8939) <= d["box_deg"] / 2 + 1e-6, abs(d["lon"] + 105.1037) <= d["box_deg"] / 2 + 1e-6), (True, True))
d = dec(aprs.position_info(-33.5, 151.25, "/>", ""))
check("southern/eastern hemispheres round trip", (near(d["lat"], -33.5), near(d["lon"], 151.25)), (True, True))

# --- traffic heard on the air -------------------------------------------------------
# A real receive log is private (it says where the station is): it lives with
# the station's configs. RANCH_RADIO_APRS_SAMPLE, the checkout's own
# tests/fixtures/aprs/rx-sample.jsonl, or ~/.config/ranch-radio's; without one
# the corpus checks are skipped.
def _corpus_sample():
    for c in (os.environ.get("RANCH_RADIO_APRS_SAMPLE"), ROOT / "tests/fixtures/aprs/rx-sample.jsonl",
              Path(os.path.expanduser("~/.config/ranch-radio/tests/fixtures/aprs/rx-sample.jsonl"))):
        if c and Path(c).exists():
            return Path(c)
    return None


sample = _corpus_sample()
logs = [p for p in (ROOT / "packet-logs/rx.jsonl.1", ROOT / "packet-logs/rx.jsonl") if p.exists()]
HAVE_CORPUS = bool(logs or sample)
rows, rows_sample = [], []
if not HAVE_CORPUS:
    print("\nskip  corpus: no receive log (RANCH_RADIO_APRS_SAMPLE or ~/.config/ranch-radio/tests/fixtures/aprs/)")
else:
    corpus_files = logs or [sample]
    rows = []
    for f in corpus_files:
        for line in open(f, encoding="utf-8", errors="replace"):
            try:
                rows.append(json.loads(line)["text"])
            except (ValueError, KeyError):
                pass
    rows_sample = [json.loads(line)["text"] for line in open(sample)] if sample else rows
    print(f"\n# corpus: {len(rows)} packets from {', '.join(p.name for p in corpus_files)}")
    types, raised, by_kind = {}, [], {}
    EXPECT = {"!": {"position", "weather", "unknown"}, "=": {"position", "weather"}, "/": {"position", "weather"},
              "@": {"position", "weather"}, "`": {"mice"}, "'": {"mice"}, ";": {"object"}, ")": {"item"},
              "T": {"telemetry", "beacon_text"}, ">": {"status"}, "}": {"third_party"},
              ":": {"message", "ack", "rej", "telemetry_meta", "bulletin", "query"}, "_": {"weather"}}
    for t in rows:
        parts = aprs.split_tnc2(t)
        if parts is None:
            raised.append(("not TNC2", t))
            continue
        try:
            d = aprs.decode(*parts)
        except Exception as e:  # noqa: BLE001
            raised.append((repr(e), t))
            continue
        types[d["type"]] = types.get(d["type"], 0) + 1
        k = parts[3][:1]
        if k in EXPECT:
            good, total = by_kind.get(k, (0, 0))
            by_kind[k] = (good + (d["type"] in EXPECT[k]), total + 1)
    check("every packet decodes without raising", raised[:3], [])
    unknown = types.get("unknown", 0) / max(1, len(rows))
    print(f"     types: {dict(sorted(types.items(), key=lambda kv: -kv[1]))}")
    check(f"unknown under 2 % ({100 * unknown:.2f} %)", unknown < 0.02, True)
    for k, (good, total) in sorted(by_kind.items()):
        floor = 0.95 if k == "!" else 0.99         # "!": Peet Bros "!!" raw weather stays unknown
        check(f"data type {k!r}: {good}/{total} decode to their type", good / total >= floor, True)


    def pinned(needle):
        t = next((x for x in rows + rows_sample if needle in x), None)
        if t is None:
            return None
        d = aprs.decode(*aprs.split_tnc2(t))
        return d["inner"] if d["type"] == "third_party" and "inner" in d else d


    d = pinned("HELIO>APNU19")
    check("HELIO: PHG4730 and its digi software", sub(d, ("symbol", "phg", "phg_power_w", "device")),
          {"symbol": "S#", "phg": "4730", "phg_power_w": 16, "device": "IW3FQG UIdigi"})
    d = pinned('N7KMH-9>S3PXRV')
    check("Mic-E: position, course, speed, altitude, radio",
          (near(d["lat"], 33.137667), near(d["lon"], -112.009833), d["course"], d["speed_kt"], d["alt_ft"],
           d["device"], d["mice_msg"]), (True, True, 23, 50, 1129, "Yaesu FTM-510D", "In Service"))
    d = pinned("!/=XO:28$k4  C")
    check("compressed, no cs data, /A= in the comment", sub(d, ("compressed", "alt_ft", "comment", "course")),
          {"compressed": True, "alt_ft": 1215, "comment": "Graywolf/0.14.14", "course": None})
    d = pinned("_332/003g007t072")
    check("weather: wind from DDD/SSS, then temperature and pressure",
          sub(d["weather"], ("wind_dir", "wind_speed_mph", "gust_mph", "temp_f", "pressure_mbar")),
          {"wind_dir": 332, "wind_speed_mph": 3, "gust_mph": 7, "temp_f": 72, "pressure_mbar": 1015.0})
    d = pinned("3320.  N/11136.  W")
    check("ambiguity 2 (heard)", (d["ambiguity"], near(d["lat"], 33 + 20.5 / 60), near(d["lon"], -(111 + 36.5 / 60))),
          (2, True, True))
    d = pinned("EQNS.0,0.075,0,0,0.1953")
    check("EQNS with an empty coefficient (heard)", d["eqns"][4], [0.0, 10.0, 0.0])
    d = pinned(":ack00}")
    check("ack00} wrapped by an iGate (heard)", sub(d, ("type", "msgno", "replyack_capable")),
          {"type": "ack", "msgno": "00", "replyack_capable": True})
    d = pinned("{97919\r")
    check("msgno with a trailing CR (heard)", d and d.get("msgno"), "97919")
    d = pinned(":TC 0")
    check("TC 0 (heard): TNC text", d and d.get("type"), "beacon_text")

# --- code review 2026-10-19 (Codex 6 Astra): regression vectors ------------------------
d = dec(":N0CALL   :EQNS.1e999,0,0", src="N0CALL")
check("EQNS with an infinite coefficient: unknown, not Infinity", d["type"], "unknown")
d = dec("T#005,1.0e999,nan,0,0,0,00000000")
check("telemetry: non-finite readings become None", d["analog"][:2], [None, None])
check("  and the result is strict JSON", bool(json.dumps(d, allow_nan=False)), True)
check("scale() overflowing: None", aprs.scale([1e300, 0, 0], 1e10), None)
d = dec("_09190556c220s004")
check("positionless weather keeps its month", time.gmtime(aprs.reported_time(d["timestamp"], 1792400000)).tm_mon, 9)
d = dec("`&+Dp!l>/", dest="SQZZZZ")
check("Mic-E ambiguity 4: longitude box centred on the degree", (d["ambiguity"], d["lon"]), (4, -110.5))
d = dec("!3320.  N/11136.47W-")
check("uncompressed: longitude takes latitude's ambiguity", d["lon"], round(-(111 + 36.5 / 60), 6))
for la, lo, amb in [(49.9999983, -72, 4), (49.9999983, -72, 1), (32.123456, -105.98765, 2), (-33.99999, 151.99999, 3)]:
    d = aprs.decode("N0CALL", "APRS", [], aprs.position_info(la, lo, ambiguity=amb))
    half = d["box_deg"] / 2 + 1e-6
    check(f"encoder ambiguity {amb}: the box holds the position ({la}, {lo})",
          (abs(d["lat"] - la) <= half, abs(d["lon"] - lo) <= half), (True, True))
d = dec("!/5L!!<*e7_7P[g050t077")
check("compressed weather: cs is the wind, not motion",
      (d["type"], d.get("course"), d["weather"].get("wind_dir"), d["weather"].get("gust_mph")), ("weather", None, 88, 50))
check("compressed position out of range: unknown", dec("!/" + "{" * 8 + ">  !")["type"], "unknown")
check("Mic-E latitude 99: unknown", dec("`&+Dp!l>/", dest="YYPPPP")["type"], "unknown")
check("Mic-E A-K in the last three destination characters: unknown", dec("`&+Dp!l>/", dest="SQSAAA")["type"], "unknown")
d = dec(":N9ABC    :?APRSP{AB}CD")
check("directed query keeps msgno and reply-ack", sub(d, ("type", "query", "msgno", "reply_ack")),
      {"type": "query", "query": "APRSP", "msgno": "AB", "reply_ack": "CD"})
d = dec(":N9ABC    :PARM.hello{AB}", src="W1AW")
check("PARM. to another station is a message", sub(d, ("type", "msgno")), {"type": "message", "msgno": "AB"})
check("tocall APRS: no 'Unknown Unknown' device", dec(">hi").get("device"), None)

# --- the oracle ------------------------------------------------------------------------
# Documented differences from aprslib (not bugs here):
#   - weather after a _ symbol: aprslib calls DDD/SSS course/speed; here it is
#     wind direction/speed (1.0.1 ch. 12)
#   - a speed of 000: kept here (0 kt), dropped by aprslib
#   - third-party: here the inner packet is decoded; aprslib doesn't place it
print()
if not ORACLE_PY.exists() or not HAVE_CORPUS:
    print(f"skip  oracle: no aprslib venv at {ORACLE_PY}, or no corpus")
else:
    src = rows_sample if not logs else rows
    r = subprocess.run([str(ORACLE_PY), "-I", str(ROOT / "tests/aprs_oracle.py")],
                       input="".join(json.dumps(t) + "\n" for t in src), capture_output=True, text=True,
                       timeout=120)
    out = r.stdout.splitlines()
    check("oracle answered every packet", len(out), len(src))
    pos = latlon = alt = 0
    bad = []
    for t, o in zip(src, out):
        o = json.loads(o)
        d = aprs.decode(*aprs.split_tnc2(t))
        if d["type"] == "third_party" or o.get("latitude") is None:
            continue
        pos += 1
        if near(o["latitude"], d.get("lat")) and near(o["longitude"], d.get("lon")):
            latlon += 1
        else:
            bad.append((t, o["latitude"], o["longitude"], d.get("lat"), d.get("lon")))
        oa, da = o.get("altitude"), d.get("alt_ft")
        if (oa is None) == (da is None) and (oa is None or abs(round(oa * 3.28084) - da) <= 2):
            alt += 1
        else:
            bad.append((t, "alt", oa, da))
    print(f"     {pos} positions compared with aprslib")
    # Coverage (review 2026-10-19): an oracle that refuses everything must not PASS.
    ours = sum(1 for t in src if (lambda d: d["type"] != "third_party" and d.get("lat") is not None)(
        aprs.decode(*aprs.split_tnc2(t))))
    refused = sum(1 for o in out if "error" in json.loads(o))
    check_true("oracle: compared at least 95 % of our positions", ours and pos >= 0.95 * ours,
               f"{pos} of {ours} (oracle refused {refused} packets)")
    check("oracle: latitude and longitude agree on every position", (latlon, bad[:2]), (pos, []))
    check("oracle: altitude agrees on every position", alt, pos)

print(f"\n{'PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
