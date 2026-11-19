"""The call card's lookup (station.py call_lookup, logsync_ledger history,
logsync_roster lookup_call; 2026-11-19, UX plan PR 4b), in-process: a real
ledger in a scratch directory, the station's _command, fake page sockets. No
radio, no network, no port.

  answer     the latest QSOs with that exact call, newest first: date, band,
             mode (the submode when there is one), confirmed; nothing else of
             the QSO; a lower-case call is upper-cased; a portable call is
             its own call; at most 20, and "more" says there were
  need       from the sync's in-memory index, only when the page sends the
             decode's grid, band and mode; otherwise unavailable; the index
             is never built for a lookup
  read-only  the ledger is unchanged; recent() is not used; the answer goes
             to the asking socket only, through its tab's add-on route
  malformed  empty, no digit, no letter, an empty part around a slash, too
             long, not a string, punctuation: "not a callsign", no query;
             a missing request id or tab is rejected
  no sync    a station without the logbook sync says so
  index      EXPLAIN QUERY PLAN on a 60,000-QSO ledger searches qso_call
             (call=?) and the dest primary key, and scans neither table

    python3 tests/callcard_test.py
"""
import asyncio
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import logsync_cty as C  # noqa: E402
import logsync_ledger as L  # noqa: E402
import logsync_needs as N  # noqa: E402
import station as S  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"), flush=True)


def qso(call, date, time_on, band="20m", mode="FT8", sub=None):
    f = {"CALL": call, "BAND": band, "MODE": mode, "QSO_DATE": date, "TIME_ON": time_on,
         "STATION_CALLSIGN": "N0CALL", "GRIDSQUARE": "FN31"}
    if sub:
        f["SUBMODE"] = sub
    return f


class Page:
    """A page's socket: what the station sent it."""
    closed = False

    def __init__(self):
        self.got = []

    async def send_str(self, s):
        self.got.append(json.loads(s))


TMP = tempfile.TemporaryDirectory(prefix="callcard-test-")
ledger = L.Ledger(os.path.join(TMP.name, "ledger.db"))
ids = {}
for k, f in {"old": qso("W1AW", "20240101", "120000", "40m", "CW"),
             "mid": qso("W1AW", "20250601", "130000", "20m", "MFSK", "FT4"),
             "new": qso("w1aw", "20260301", "140000", "20m", "FT8"),
             "port": qso("W1AW/P", "20260302", "150000"),
             "other": qso("N9ABC", "20260303", "160000")}.items():
    ids[k], _ = ledger.add(f, "jtdx", {d: "pending" for d in ledger.dests})
ledger.set(ids["mid"], "lotw", "confirmed")
for i in range(25):                         # more than a card shows
    ledger.add(qso("N9XYZ", "20250101", f"10{i:02d}00"), "jtdx", {d: "pending" for d in ledger.dests})

needs = N.Needs(C.Cty(open(os.path.join(ROOT, "tests/fixtures/cty-sample.csv")).read()))
needs.add("W1AW", "20m", "FT8", "", "<GRIDSQUARE:4>FN31", True)
ARGS = types.SimpleNamespace(token=None, rigctl_port=0)
st = S.Station({"radios": []}, ARGS)
st.logsync = types.SimpleNamespace(ledger=ledger, needs=needs)
asker, other = Page(), Page()
st.clients = {asker, other}


def never(*a, **k):
    raise AssertionError("not for a lookup")


L.Ledger.recent = never                     # the lookup never sorts the whole table
N.Needs.from_ledger = classmethod(never)    # and never builds a needs index


def look(**m):
    asker.got.clear()
    asyncio.run(st._command(asker, {"t": "logsync", "do": "lookup", "id": "cc1", "addon": "roster", **m}))
    return asker.got[-1] if asker.got else None


changes = ledger.db.total_changes
a = look(call="w1aw")
check("answer: through the asking tab's route, with the request id",
      (a["t"], a["addon"], a["callcard"]["id"], a["callcard"]["call"]), ("addon", "roster", "cc1", "W1AW"))
c = a["callcard"]
check("  the call's QSOs, newest first: date, band, mode (submode first), confirmed",
      c["qsos"], [{"date": "2026-03-01", "band": "20m", "mode": "FT8", "confirmed": False},
                  {"date": "2025-06-01", "band": "20m", "mode": "FT4", "confirmed": True},
                  {"date": "2024-01-01", "band": "40m", "mode": "CW", "confirmed": False}])
check("  nothing else of a QSO", sorted({k for q in c["qsos"] for k in q}), ["band", "confirmed", "date", "mode"])
check("  not more than it found", c["more"], False)
check("  no decode sent: the need is unavailable", c["need"], None)
check("  a portable call is its own", [q["date"] for q in look(call="W1AW/P")["callcard"]["qsos"]], ["2026-03-02"])
check("  never a prefix match", look(call="W1A")["callcard"]["qsos"], [])
big = look(call="N9XYZ")["callcard"]
check("  at most 20, newest first, and it says there are more",
      (len(big["qsos"]), big["more"]), (20, True))
check("  the map asks through its own route", look(call="N9ABC", addon="map")["addon"], "map")

want = needs.need("N9ABC", "EN52", "20m", "FT8")
got = look(call="N9ABC", grid="en52", band="20m", mode="FT8")["callcard"]["need"]
check("need: from the in-memory index with the decode's grid, band and mode",
      (got["level"], got["reasons"]), (want[0], want[1]))
check("  a worked call on its band and mode: nothing new",
      look(call="W1AW", grid="FN31", band="20m", mode="FT8")["callcard"]["need"]["level"], None)
check("  without a band: unavailable", look(call="N9ABC", grid="EN52", mode="FT8")["callcard"]["need"], None)
check("  a grid that isn't one: unavailable", look(call="N9ABC", grid="RR73", band="20m", mode="FT8")["callcard"]["need"], None)
st.logsync.needs = None
check("  the index not built yet: unavailable",
      look(call="N9ABC", grid="EN52", band="20m", mode="FT8")["callcard"]["need"], None)
st.logsync.needs = needs

check("read-only: the ledger is unchanged after every lookup", ledger.db.total_changes, changes)
check("  the other page got nothing", other.got, [])
check("  one answer per lookup", (look(call="W1AW") is not None, len(asker.got)), (True, 1))

queries = []
real_history = ledger.history
ledger.history = lambda call, *a: queries.append(call) or real_history(call, *a)
for bad in ("", "  ", "W1AW/", "/W1AW", "W1AW//P", "ABCD", "1234", "W1AW;--", "W1AW P", "N" + "9" * 14, "RR73",
            "W1AW'", "%", 42, None, ["W1AW"]):
    r = look(call=bad)["callcard"]
    check(f"malformed {bad!r}: not a callsign, no query", (r.get("error"), "qsos" in r, "call" in r),
          ("not a callsign", False, False))
check("  no malformed call reached the ledger", queries, [])
ledger.history = real_history


def raises(m):
    try:
        asyncio.run(st._command(asker, {"t": "logsync", "do": "lookup", **m}))
    except ValueError:
        return True
    return False


check("  no request id: rejected", raises({"call": "W1AW", "addon": "roster"}), True)
check("  an id that isn't one: rejected", raises({"id": {"x": 1}, "call": "W1AW", "addon": "roster"}), True)
check("  a tab that doesn't show cards: rejected", raises({"id": 1, "call": "W1AW", "addon": "winlink"}), True)

st.logsync = None
r = look(call="W1AW")["callcard"]
check("no sync: it says so", (r.get("error"), "qsos" in r), ("the logbook sync isn't running on this station", False))

# ---- the index, on a ledger the size of a busy decade
db = ledger.db
db.execute("BEGIN")
db.executemany("INSERT INTO qso(key, own, call, band, mode, submode, ts, adif) VALUES(?,?,?,?,?,?,?,?)",
               ((f"bulk{i}", "N0CALL", f"N{i % 10}{chr(65 + i % 26)}{chr(65 + (i // 26) % 26)}{chr(65 + (i // 676) % 26)}",
                 "20m", "FT8", "", f"2020{1 + i % 12:02d}01{i % 24:02d}0000", "") for i in range(60000)))
db.executemany("INSERT INTO dest(qso_id, dest, state) SELECT id, ?, 'pending' FROM qso WHERE key LIKE 'bulk%'",
               [("wavelog",)])
db.execute("COMMIT")
db.execute("ANALYZE")
plan = [r[3] for r in db.execute("EXPLAIN QUERY PLAN " + L.Ledger.HISTORY_SQL, ("N0AAA", 20))]
print("     plan:", plan, flush=True)
check("index: the call is searched on qso_call", any("USING INDEX qso_call (call=?)" in p for p in plan), True)
check("  the confirmed flag is a primary-key search on dest",
      any(p.startswith("SEARCH d USING") and "qso_id=?" in p for p in plan), True)
check("  neither table is scanned", [p for p in plan if p.startswith("SCAN")], [])
check("  the bulk ledger still answers one call", len(ledger.history("N0AAA")) > 0, True)

ledger.close()
TMP.cleanup()
print(f"\n{'all ok' if not fails else f'{fails} FAILED'}", flush=True)
sys.exit(1 if fails else 0)
