"""The needs index (logsync_cty.py, logsync_needs.py, logsync_refdata.py):
callsign -> DXCC entity from a cty.csv cut from the real file, worked and
confirmed from a ledger, need levels, LoTW users, and the cached reference
data (fresh, stale, failed fetch, oversize, not the right file).

  cty        exact calls beat prefixes (=AH0AB is the US, =K2GT Hawaii);
             longest prefix wins (VK9 over VK, MM Scotland over M England);
             portable forms (KH6/W1AW, W1AW/KH6, W1AW/P, W1AW/4, DL/W1AW);
             maritime mobile has no entity; KG4 + two letters is Guantanamo,
             any other KG4 call the US; WAE (*) rows are skipped
  needs      from a ledger: the DXCC field Wavelog wrote wins over the
             lookup; confirmed only from the ledger's confirmed state; each
             level fires on the case that should and not on the one beside it
  history    one call's latest QSOs for the call card: exact call, newest
             first, at most 20, date/band/mode/confirmed only, read-only;
             on 60,000 QSOs the plan searches qso_call and scans nothing
  lotw       a user active within a year counts; an older one doesn't
  refdata    a young copy isn't fetched; a stale one is; a failed fetch, an
             oversize answer or one that isn't the file keeps the cache and
             says so; with no cache, None

    python3 tests/logsync_needs_test.py
"""
import os as _os
_os.environ["RANCH_RADIO_OFFLINE"] = "1"      # no test reaches the internet (logsync_refdata)

import datetime as dt
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logsync_cty as C  # noqa: E402
import logsync_needs as N  # noqa: E402
import logsync_refdata as R  # noqa: E402
from logsync_ledger import Ledger  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"), flush=True)


CTY_TEXT = (ROOT / "tests/fixtures/cty-sample.csv").read_text()
cty = C.Cty(CTY_TEXT)


def ent(call):
    e = cty.lookup(call)
    return e.dxcc if e else None


# ================================================================ cty
cases = [("W1AW", 291, "a plain US call"), ("AH0AB", 291, "an exact call beats its prefix (AH0 is Marianas)"),
         ("K2GT", 110, "an exact call beats its prefix (K is the US)"), ("KH0XYZ", 166, "Mariana Islands"),
         ("VK9NA", 189, "longest prefix: VK9 over VK"), ("VK2ABC", 150, "VK"),
         ("MM0ABC", 279, "longest prefix: MM (Scotland) over M (England)"), ("M0ABC", 223, "M is England"),
         ("G4X", 279, "an exact call in another entity's row"), ("KH6/W1AW", 110, "prefix/call"),
         ("W1AW/KH6", 110, "call/prefix"), ("W1AW/P", 291, "/P dropped"), ("W1AW/4", 291, "a lone digit keeps the call"),
         ("DL/W1AW", 230, "a two-letter prefix"), ("W1AW/MM", None, "maritime mobile: no entity"),
         ("KG4NE", 105, "an exact KG4"), ("KG4AB", 105, "KG4 + two letters is Guantanamo"),
         ("KG4ABC", 291, "KG4 + three letters is the US"), ("3D2CCC", 489, "an exact call: Conway Reef"),
         ("3D2ABC", 176, "3D2 is Fiji"), ("JD1BPS/1", 339, "an exact call with a slash"), ("4U1VIC", None, "WAE rows are skipped"),
         ("", None, "empty"), ("W1AW;DROP", None, "junk")]
for call, want, why in cases:
    check(f"cty {call or '(empty)'}: {why}", ent(call), want)
try:
    C.Cty("not,a,cty\n")
    check("cty: a file with no prefixes is refused", "parsed", "refused")
except ValueError:
    check("cty: a file with no prefixes is refused", True, True)
check("mode groups", [C.mode_group(m) for m in ("CW", "SSB", "USB", "AM", "FT8", "MFSK", "RTTY")],
      ["CW", "PHONE", "PHONE", "PHONE", "DATA", "DATA", "DATA"])

# ================================================================ needs, from a real ledger file
tmp = Path(tempfile.mkdtemp(prefix="logsync-needs-"))
led = Ledger(str(tmp / "l.db"), ["lotw", "qrz"])


def qso(call, band, mode, t, grid="", dxcc=None, conf=False):
    f = {"CALL": call, "BAND": band, "MODE": mode, "QSO_DATE": t[:8], "TIME_ON": t[8:], "STATION_CALLSIGN": "N0CALL"}
    if grid:
        f["GRIDSQUARE"] = grid
    if dxcc is not None:
        f["DXCC"] = str(dxcc)
    qid, _ = led.add(f, "test", {"lotw": "accepted", "qrz": "sent"}, own="N0CALL")
    if conf:
        led.set(qid, "lotw", "confirmed")


qso("JA1ABC", "20m", "FT8", "20260101120000", "PM95", conf=True)
qso("VE3XYZ", "40m", "CW", "20260102120000", "FN03")
qso("KH0XYZ", "20m", "FT8", "20260103120000", "QK25", dxcc=291)     # Wavelog says US: its DXCC field wins
qso("DL1ABC", "20m", "SSB", "20260104120000", "JO62", conf=True)
led.db.commit()
n = N.Needs.from_ledger(str(tmp / "l.db"), cty)
check("from a ledger: entities worked (Wavelog's DXCC wins over the lookup)", sorted(n.worked["dxcc"]), [1, 230, 291, 339])
check("  confirmed only from the confirmed state", sorted(n.confirmed["dxcc"]), [230, 339])
check("  grids worked, 4 characters", sorted(n.worked["grid"]), ["FN03", "JO62", "PM95", "QK25"])
for args, want, why in [
        (("VK9NA", "RG30", "20m", "FT8"), ("dxcc", ["dxcc", "grid", "call_band_mode"]), "an entity never worked"),
        (("JA2XYZ", "PM95", "40m", "FT8"), ("dxcc_band", ["dxcc_band", "grid_band", "call_band_mode"]), "worked, not on this band"),
        (("JA2XYZ", "PM95", "20m", "CW"), ("dxcc_mode", ["dxcc_mode", "call_band_mode"]), "worked on the band, not in this mode group"),
        (("JA2XYZ", "PM96", "20m", "FT8"), ("grid", ["grid", "call_band_mode"]), "a new grid"),
        (("VE3ABC", "FN03", "40m", "CW"), ("dxcc_unconfirmed", ["dxcc_unconfirmed", "call_band_mode"]), "worked, never confirmed"),
        (("JA2XYZ", "PM95", "20m", "FT8"), ("call_band_mode", ["call_band_mode"]), "only the call is new"),
        (("JA1ABC", "PM95", "20m", "FT8"), (None, []), "worked before, same band and mode"),
        (("JA1ABC", "PM95", "20m", "FT4"), (None, []), "FT4 is the same mode group as FT8"),
        (("W1AW/MM", "", "20m", "FT8"), ("call_band_mode", ["call_band_mode"]), "no entity: only the call can be new")]:
    check(f"need: {why}", n.need(*args), want)
check("summary", n.summary(), {"qsos": 4, "dxcc_worked": 4, "dxcc_confirmed": 2, "grids_worked": 4, "grids_confirmed": 2,
                               "lotw_users": 0})

# ================================================================ grids by band and operating mode (the Grids tab, 2026-11-20)
# grid_cells: every grid worked, each band and the mode an operator names
# (op_mode, not the LoTW group), confirmed if any QSO there is.
for (mode, sub), want in [(("FT8", ""), "FT8"), (("MFSK", "FT4"), "FT4"), (("MFSK", "JS8"), "JS8"), (("SSB", "USB"), "SSB"),
                          (("USB", ""), "SSB"), (("SSB", ""), "SSB"), (("CW", ""), "CW"), (("AM", ""), "AM"), (("", ""), "?")]:
    check(f"op_mode {mode}/{sub or '-'} -> {want}", N.op_mode(mode, sub), want)
cells = n.grid_cells()
check("grid_cells from a ledger: band, mode, confirmed",
      (cells["PM95"], cells["FN03"], cells["JO62"]), ([["20m", "FT8", 1]], [["40m", "CW", 0]], [["20m", "SSB", 1]]))
m = N.Needs()
m.add("K1AA", "20m", "FT8", "", "<GRIDSQUARE:6>EM10ab", False)
m.add("K1AB", "20m", "FT8", "", "<GRIDSQUARE:4>EM10", True)          # same cell, now confirmed
m.add("K1AC", "20m", "FT8", "", "<GRIDSQUARE:4>EM10", False)         # a later worked QSO doesn't unconfirm it
m.add("K1AD", "40m", "MFSK", "FT4", "<GRIDSQUARE:4>EM10", False)
m.add("K1AE", "20m", "SSB", "USB", "<GRIDSQUARE:4>em10", False)      # lower case is the same square
m.add("K1AF", "20m", "FT8", "", "", True)                            # no grid: no cell
check("grid_cells: one cell per band and mode, confirmed sticks, case folded, no grid no cell",
      m.grid_cells(), {"EM10": [["20m", "FT8", 1], ["20m", "SSB", 0], ["40m", "FT4", 0]]})

# ================================================================ one call's history (the call card, 2026-11-19, UX plan PR 4b)
# Ledger.history: the latest QSOs with one exact call, read the way the needs
# are (confirmed = the ledger's confirmed state), indexed and capped.
hl = Ledger(str(tmp / "h.db"), ["lotw", "qrz"])
st_all = {"lotw": "accepted", "qrz": "sent"}


def hq(call, t, band="20m", mode="FT8", sub=None):
    f = {"CALL": call, "BAND": band, "MODE": mode, "QSO_DATE": t[:8], "TIME_ON": t[8:], "STATION_CALLSIGN": "N0CALL"}
    if sub:
        f["SUBMODE"] = sub
    return hl.add(f, "test", st_all, own="N0CALL")[0]


hq("W1AW", "20240101120000", "40m", "CW")
hl.set(hq("W1AW", "20250601130000", "20m", "MFSK", "FT4"), "lotw", "confirmed")
hq("w1aw", "20260301140000")
hq("W1AW/P", "20260302150000")
for i in range(25):
    hq("N9XYZ", f"2025010110{i:02d}00")
changes = hl.db.total_changes
check("history: the call's QSOs, newest first: date, band, mode (submode first), confirmed", hl.history("W1AW"),
      [{"date": "2026-03-01", "band": "20m", "mode": "FT8", "confirmed": False},
       {"date": "2025-06-01", "band": "20m", "mode": "FT4", "confirmed": True},
       {"date": "2024-01-01", "band": "40m", "mode": "CW", "confirmed": False}])
check("  a portable call is its own; never a prefix match",
      ([q["date"] for q in hl.history("W1AW/P")], hl.history("W1A")), (["2026-03-02"], []))
check("  at most 20, the newest", (len(hl.history("N9XYZ")), len(hl.history("N9XYZ", 500))), (20, 20))
check("  read-only", hl.db.total_changes, changes)
db = hl.db
db.execute("BEGIN")
db.executemany("INSERT INTO qso(key, own, call, band, mode, submode, ts, adif) VALUES(?,?,?,?,?,?,?,?)",
               ((f"bulk{i}", "N0CALL", f"N{i % 10}{chr(65 + i % 26)}{chr(65 + (i // 26) % 26)}{chr(65 + (i // 676) % 26)}",
                 "20m", "FT8", "", f"2020{1 + i % 12:02d}01{i % 24:02d}0000", "") for i in range(60000)))
db.execute("INSERT INTO dest(qso_id, dest, state) SELECT id, 'lotw', 'accepted' FROM qso WHERE key LIKE 'bulk%'")
db.execute("COMMIT")
db.execute("ANALYZE")
plan = [r[3] for r in db.execute("EXPLAIN QUERY PLAN " + Ledger.HISTORY_SQL, ("N0AAA", 20))]
print("     plan:", plan, flush=True)
check("  60,000 QSOs: the call is searched on qso_call", any("USING INDEX qso_call (call=?)" in p for p in plan), True)
check("    confirmed is a key search on dest", any(p.startswith("SEARCH d USING") and "qso_id=?" in p for p in plan), True)
check("    neither table is scanned", [p for p in plan if p.startswith("SCAN")], [])
check("    and it answers", len(hl.history("N0AAA")) > 0, True)
hl.close()

# ================================================================ LoTW users
today = dt.date(2026, 11, 14)
n.load_lotw_users("JA1ABC,2026-11-01,10:00:00\nVK9NA,2024-01-01,10:00:00\nDL1ABC,2025-11-15,09:00:00\njunk\n", today)
check("lotw: active within a year; an older upload isn't", (n.lotw_user("JA1ABC"), n.lotw_user("VK9NA"), n.lotw_user("DL1ABC")),
      ("2026-11-01", None, "2025-11-15"))
check("  a portable form finds the base call", n.lotw_user("JA1ABC/P"), "2026-11-01")

# ================================================================ refdata
calls = []


def fetch_ok(url, cap):
    calls.append(url)
    return CTY_TEXT.encode()


def fetch_fail(url, cap):
    calls.append(url)
    raise OSError("no route")


state = tmp / "state"
p, note = R.ensure("cty", state, fetch=fetch_ok)
check("refdata offline: no fetch at all, no cache -> None, said", (p, len(calls), "offline" in note), (None, 0, True))
os.environ["RANCH_RADIO_OFFLINE"] = "0"           # the fetches below are injected, never the internet
p, note = R.ensure("cty", state, fetch=fetch_ok)
check("refdata: no copy -> fetched, checked, cached", (Path(p).read_text() == CTY_TEXT, note, len(calls)), (True, "", 1))
p, note = R.ensure("cty", state, fetch=fetch_ok)
check("  a young copy is not fetched again", (note, len(calls)), ("", 1))
later = lambda: time.time() + 8 * 86400                  # noqa: E731
p, note = R.ensure("cty", state, fetch=fetch_fail, now=later)
check("  stale + a failed fetch: the cache, and the note says so", (p is not None, "refresh failed" in note, "using the copy" in note),
      (True, True, True))
p, note = R.ensure("cty", state, fetch=lambda u, c: b"<html>login</html>", now=later)
check("  stale + an answer that isn't a cty.csv: the cache kept", (Path(p).read_text() == CTY_TEXT, "ValueError" in note), (True, True))
p, note = R.ensure("cty", tmp / "empty", fetch=fetch_fail)
check("  no cache and no fetch: None, said", (p, "no cached copy" in note), (None, True))
srv_dir = tmp / "srv"
srv_dir.mkdir()
(srv_dir / "big").write_bytes(b"x" * 5000)
import functools  # noqa: E402
import http.server  # noqa: E402
import threading  # noqa: E402
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler,
                                                                         directory=str(srv_dir)))
srv.RequestHandlerClass.log_message = lambda *a: None
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
try:
    R._fetch(f"http://127.0.0.1:{port}/big", 1000)
    check("  the real fetch refuses an answer over its size cap", "accepted", "refused")
except ValueError:
    check("  the real fetch refuses an answer over its size cap", True, True)
check("  and takes one under it", len(R._fetch(f"http://127.0.0.1:{port}/big", 10000)), 5000)
srv.shutdown()

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{'all ok' if not fails else f'{fails} FAILED'}", flush=True)
os._exit(1 if fails else 0)
