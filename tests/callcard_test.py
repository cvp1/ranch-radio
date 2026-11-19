"""The call card's lookup on the station (station.py call_lookup and
logsync_roster.lookup_call; 2026-11-19, UX plan PR 4b), in-process: the
station's _command, a fake ledger and fake page sockets. No radio, no
network, no port. The ledger's own query (Ledger.history: exact call, the
qso_call index, at most 20) is tested with the ledger, in
tests/logsync_needs_test.py; this test does not open one, so the logbook
sync's config module is not an edge of it.

  answer     the ledger's rows for the call, upper-cased, through the asking
             tab's add-on route with the request id; "more" at the cap
  need       from the sync's in-memory index, only when the page sends the
             decode's grid, band and mode; otherwise unavailable; the index
             is never built for a lookup
  read-only  the answer goes to the asking socket only; nothing is broadcast
  malformed  empty, no digit, no letter, an empty part around a slash, too
             long, not a string, punctuation: "not a callsign", and the
             ledger is never asked; a missing request id or tab is rejected
  no sync    a station without the logbook sync says so; a ledger that
             can't be read says so, and the station runs on

    python3 tests/callcard_test.py
"""
import asyncio
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import logsync_cty as C  # noqa: E402
import logsync_needs as N  # noqa: E402
import logsync_roster  # noqa: E402,F401  (the call pattern station.py loads by name)
import station as S  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"), flush=True)


class Page:
    """A page's socket: what the station sent it."""
    closed = False

    def __init__(self):
        self.got = []

    async def send_str(self, s):
        self.got.append(json.loads(s))


class FakeLedger:
    """Ledger.history's shape; records each call it was asked for."""
    HISTORY_MAX = 20

    def __init__(self, rows):
        self.rows, self.asked = rows, []

    def history(self, call):
        self.asked.append(call)
        return list(self.rows.get(call, []))[:self.HISTORY_MAX]


W1AW = [{"date": "2026-03-01", "band": "20m", "mode": "FT8", "confirmed": False},
        {"date": "2025-06-01", "band": "20m", "mode": "FT4", "confirmed": True}]
ledger = FakeLedger({"W1AW": W1AW, "N9XYZ": [{"date": "2025-01-01", "band": "20m", "mode": "FT8", "confirmed": False}] * 20})
needs = N.Needs(C.Cty(open(os.path.join(ROOT, "tests/fixtures/cty-sample.csv")).read()))
needs.add("W1AW", "20m", "FT8", "", "<GRIDSQUARE:4>FN31", True)
st = S.Station({"radios": []}, types.SimpleNamespace(token=None, rigctl_port=0))
st.logsync = types.SimpleNamespace(ledger=ledger, needs=needs)
asker, other = Page(), Page()
st.clients = {asker, other}


def never(*a, **k):
    raise AssertionError("not for a lookup")


N.Needs.from_ledger = classmethod(never)    # never builds a needs index


def look(**m):
    asker.got.clear()
    asyncio.run(st._command(asker, {"t": "logsync", "do": "lookup", "id": "cc1", "addon": "roster", **m}))
    return asker.got[-1] if asker.got else None


a = look(call="w1aw")
check("answer: through the asking tab's route, with the request id",
      (a["t"], a["addon"], a["callcard"]["id"], a["callcard"]["call"]), ("addon", "roster", "cc1", "W1AW"))
c = a["callcard"]
check("  the ledger's rows for the upper-cased call", (ledger.asked[-1], c["qsos"]), ("W1AW", W1AW))
check("  not more than it found", c["more"], False)
check("  no decode sent: the need is unavailable", c["need"], None)
check("  at the cap it says there may be more", look(call="N9XYZ")["callcard"]["more"], True)
check("  a call it never worked: no rows", look(call="N9ABC")["callcard"]["qsos"], [])
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

check("read-only: the other page got nothing", other.got, [])
check("  one answer per lookup", (look(call="W1AW") is not None, len(asker.got)), (True, 1))

asked = len(ledger.asked)
for bad in ("", "  ", "W1AW/", "/W1AW", "W1AW//P", "ABCD", "1234", "W1AW;--", "W1AW P", "N" + "9" * 14, "RR73",
            "W1AW'", "%", 42, None, ["W1AW"]):
    r = look(call=bad)["callcard"]
    check(f"malformed {bad!r}: not a callsign", (r.get("error"), "qsos" in r, "call" in r),
          ("not a callsign", False, False))
check("  no malformed call reached the ledger", ledger.asked[asked:], [])
check("  a portable call is a call", look(call="w1aw/p")["callcard"].get("call"), "W1AW/P")


def raises(m):
    try:
        asyncio.run(st._command(asker, {"t": "logsync", "do": "lookup", **m}))
    except ValueError:
        return True
    return False


check("  no request id: rejected", raises({"call": "W1AW", "addon": "roster"}), True)
check("  an id that isn't one: rejected", raises({"id": {"x": 1}, "call": "W1AW", "addon": "roster"}), True)
check("  a tab that doesn't show cards: rejected", raises({"id": 1, "call": "W1AW", "addon": "winlink"}), True)


def broken(call):
    raise OSError("database is locked")


ledger.history = broken
r = look(call="W1AW")["callcard"]
check("a ledger that can't be read: said, no rows", (r.get("error"), "qsos" in r), ("the logbook couldn't be read (OSError)", False))
st.logsync = None
r = look(call="W1AW")["callcard"]
check("no sync: it says so", (r.get("error"), "qsos" in r), ("the logbook sync isn't running on this station", False))

print(f"\n{'all ok' if not fails else f'{fails} FAILED'}", flush=True)
sys.exit(1 if fails else 0)
