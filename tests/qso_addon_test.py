"""The QSO add-on (addons/qso.py) and what it uses (qso_lookup.py).

  - a good block loads, on, in the radios group, with no error, and starts
    and stops; a bad block is refused naming the field
  - it is in the module catalog and the manifest
  - QRZ XML: login answers a key; a lookup fills name, QTH, grid; an expired
    session logs in once more and retries; a refused password is bad_login;
    not found; a non-subscriber; offline backs off and never says the
    password; no password file is no_login and asks QRZ nothing
  - POTA spots: parsed, matched by base call (W1AW/P is W1AW), on the dial
    first, stale ones dropped; a two-fer on the dial fills both parks; two
    parks off the dial fill nothing
  - the record: band from the frequency, SSB's sideband (from the radio, else
    by band), CW, the refs checked, a bad call/frequency/grid/ref refused
    naming the field, a time just before midnight UTC lands on the right day
  - the add-on: a lookup carries DXCC, history, the POTA suggestion and QRZ;
    Log goes to the logbook sync's record() as "manual" with the own call;
    with no logbook sync nothing is logged and the page is told why; the
    sticky own refs are kept across a restart; the QRZ login writes the
    password at mode 600 and no answer carries it; the dial push is throttled

    python3 tests/qso_addon_test.py
"""
import asyncio
import datetime as dt
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["RANCH_RADIO_OFFLINE"] = "1"
from addons import Addons  # noqa: E402
from addons import qso as A  # noqa: E402
import qso_lookup as Q  # noqa: E402
from ranch_radio import catalog, manifest  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


def run(c):
    return asyncio.run(c)


class Host:
    def __init__(self, logsync=None, rigs=None):
        self.logsync, self.sent, self.rigs = logsync, [], rigs or []

    def notice(self, *a):
        pass

    def _broadcast(self, payload):
        self.sent.append(json.loads(payload))

    async def _send(self, ws, payload):
        self.sent.append(json.loads(payload))

    def rigs_view(self):
        return self.rigs


# -- registry, catalog, manifest

reg = Addons(Host(), {"qso": {"enabled": True}}, state_path=None)
d = {x["name"]: x for x in reg.describe()}["qso"]
check("loads, on, in its group, no error", (d["label"], d["group"], d["on"], d["error"]), ("QSO", "radios", True, None))
run(reg.start())
check("starts", reg.items["qso"]["running"], True)
run(reg.stop())
check("stops", reg.items["qso"]["running"], False)
bad = Addons(Host(), {"qso": {"pota": "yes"}}, state_path=None)
check("a bad block is refused, naming the field", "pota" in (bad.items["qso"]["error"] or ""), True)
check("in the catalog", "qso" in catalog.by_name(), True)
check("in the manifest, the addons block", (manifest.MANIFESTS.get("qso") or {}).get("block"), ("addons", "qso"))

# -- QRZ XML

NS = 'xmlns="http://xmldata.qrz.com"'
LOGIN_OK = f'<?xml version="1.0"?><QRZDatabase version="1.34" {NS}><Session><Key>K1</Key><SubExp>Wed Jan 1 2027</SubExp></Session></QRZDatabase>'
LOGIN_NONSUB = f'<QRZDatabase {NS}><Session><Key>K1</Key><SubExp>non-subscriber</SubExp></Session></QRZDatabase>'
LOGIN_BAD = f'<QRZDatabase {NS}><Session><Error>Username/password incorrect</Error></Session></QRZDatabase>'
CALL_OK = (f'<QRZDatabase {NS}><Callsign><call>N9ABC</call><fname>Pat</fname><name>Smith</name><addr2>Springfield</addr2>'
           '<state>IL</state><county>Sangamon</county><country>United States</country><grid>EN59ax</grid>'
           '<dxcc>291</dxcc><lat>39.78</lat><lon>-89.65</lon></Callsign><Session><Key>K1</Key></Session></QRZDatabase>')
EXPIRED = f'<QRZDatabase {NS}><Session><Error>Session Timeout</Error></Session></QRZDatabase>'
NOTFOUND = f'<QRZDatabase {NS}><Session><Key>K1</Key><Error>Not found: N0NE</Error></Session></QRZDatabase>'

tmp = tempfile.mkdtemp()
pwfile = os.path.join(tmp, "creds", "qrz-xml-password")
SECRET = "s3cr3t pass"


class FakeQrz:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    async def get(self, url, params):
        self.calls.append(dict(params))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a if isinstance(a, tuple) else (200, a)


f = FakeQrz([])
q = Q.Qrz("N0CALL", pwfile, f.get)
check("no password file: no_login, QRZ never asked", (run(q.lookup("N9ABC")), f.calls), (("no_login", None), []))

A.write_secret(pwfile, SECRET)
check("the password file is mode 600", stat.S_IMODE(os.stat(pwfile).st_mode), 0o600)
f = FakeQrz([LOGIN_OK, CALL_OK])
q = Q.Qrz("N0CALL", pwfile, f.get)
st, data = run(q.lookup("N9ABC"))
check("login then lookup: ok", st, "ok")
check("the fields", (data["name"], data["qth"], data["state"], data["grid"], data["dxcc"], data["country"]),
      ("Pat Smith", "Springfield", "IL", "EN59AX", "291", "United States"))
check("login sent the user, password and agent", (f.calls[0]["username"], f.calls[0]["password"], f.calls[0]["agent"]),
      ("N0CALL", SECRET, Q.QRZ_AGENT))
check("the lookup sent the key, not the password", (f.calls[1].get("s"), "password" in f.calls[1]), ("K1", False))
check("a second lookup of the same call is from memory", (run(q.lookup("N9ABC"))[0], len(f.calls)), ("ok", 2))

f = FakeQrz([EXPIRED, LOGIN_OK, CALL_OK])
q = Q.Qrz("N0CALL", pwfile, f.get)
q.key = "OLD"
check("an expired session: one login, one retry", (run(q.lookup("N9ABC"))[0], len(f.calls)), ("ok", 3))

f = FakeQrz([LOGIN_BAD])
q = Q.Qrz("N0CALL", pwfile, f.get)
check("a refused password: bad_login", (run(q.lookup("N9ABC")), q.state), (("bad_login", None), "bad_login"))
check("refused: the next lookups don't log in again (an account lockout)",
      (run(q.lookup("K1ABC")), run(q.lookup("W1AW")), len(f.calls)), (("bad_login", None), ("bad_login", None), 1))
A.write_secret(pwfile, SECRET + "x")
f.answers = [LOGIN_OK, CALL_OK]
check("refused: a changed password is tried", run(q.lookup("N9ABC"))[0], "ok")
A.write_secret(pwfile, SECRET)

# QRZ's live answer to a refused login (2026-11-27): HTTP 403, its XML in the body
LOGIN_403 = ('<QRZDatabase xmlns="http://xml.qrz.com" version="1.36">\n<Session>\n'
             '<Error>Username / password required</Error>\n<Remark></Remark>\n</Session>\n</QRZDatabase>')
f = FakeQrz([(403, LOGIN_403)])
q = Q.Qrz("N0CALL", pwfile, f.get)
check("a refused login as QRZ sends it (403 + XML): bad_login", run(q.login()), "bad_login")
check("refused: QRZ's own words are kept for the page", q.says, "Username / password required")
f = FakeQrz([(503, "<html>down</html>")])
q = Q.Qrz("N0CALL", pwfile, f.get)
check("a non-QRZ error page: error", run(q.login()), "error")

f = FakeQrz([LOGIN_NONSUB])
q = Q.Qrz("N0CALL", pwfile, f.get)
check("a non-subscriber login", run(q.login()), "no_subscription")

f = FakeQrz([LOGIN_OK, NOTFOUND])
q = Q.Qrz("N0CALL", pwfile, f.get)
check("not found", run(q.lookup("N0NE")), ("not_found", None))


class Boom(Exception):
    def __str__(self):
        return f"Cannot connect to https://xmldata.qrz.com/xml/current/?username=N0CALL&password={SECRET}"


clock = [1000.0]
f = FakeQrz([Boom()])
q = Q.Qrz("N0CALL", pwfile, f.get, clock=lambda: clock[0])
check("offline: offline", run(q.lookup("N9ABC")), ("offline", None))
check("offline: nothing kept carries the password", SECRET in repr(vars(q)), False)
check("offline: no second try within the back-off", (run(q.lookup("K1ABC")), len(f.calls)), (("offline", None), 1))
clock[0] += Q.QRZ_OFFLINE_S + 1
f.answers = [LOGIN_OK, CALL_OK]
check("after the back-off it tries again", run(q.lookup("N9ABC"))[0], "ok")

# -- POTA spots

NOW = dt.datetime(2026, 11, 27, 15, 0, tzinfo=dt.UTC).timestamp()


def at(mins_ago):
    return dt.datetime.fromtimestamp(NOW - mins_ago * 60, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S")


RAW = [
    {"activator": "N9ABC/P", "frequency": "14285", "mode": "SSB", "reference": "US-1234", "name": "A Park",
     "locationDesc": "US-IL", "spotTime": at(3), "grid6": "EN59ax"},
    {"activator": "N9ABC", "frequency": "14286.5", "mode": "SSB", "reference": "US-5678", "name": "B Park",
     "locationDesc": "US-IL", "spotTime": at(5), "grid4": "EN59"},
    {"activator": "N9ABC", "frequency": "7200", "mode": "SSB", "reference": "US-9999", "spotTime": at(90)},
    {"activator": "K1XYZ", "frequency": "14285", "mode": "SSB", "reference": "US-0001", "spotTime": at(1)},
    {"activator": "BAD", "frequency": "x", "reference": "US-0002", "spotTime": at(1)},
    "junk",
]
spots = Q.pota_spots(RAW)
check("spots: the malformed ones dropped", len(spots), 4)
m = Q.match(spots, "N9ABC", 14285.0, NOW)
check("match: by base call, stale dropped, on the dial first", [(s["ref"], s["on_freq"]) for s in m],
      [("US-1234", True), ("US-5678", True)])
check("a two-fer on the dial fills both parks", Q.suggest(m), "US-1234,US-5678")
check("two parks, neither on the dial: nothing filled", Q.suggest(Q.match(spots, "N9ABC", 7100.0, NOW)), "")
check("one park off the dial is still filled", Q.suggest(Q.match(spots, "K1XYZ", 7100.0, NOW)), "US-0001")
check("no spot: nothing", Q.suggest(Q.match(spots, "W1AW", 14285.0, NOW)), "")

# -- the record

BASE = {"call": "n9abc", "mhz": "14.28500", "mode": "SSB", "sideband": "USB", "rst_sent": "59", "rst_rcvd": "57",
        "name": "Pat", "pota": "us-1234, US-5678", "my_pota": "US-0001", "grid": "en59ax", "time_on": ""}
r = Q.adif_fields(BASE, "N0CALL", NOW)
check("record: call, band, mode, sideband", (r["CALL"], r["BAND"], r["MODE"], r["SUBMODE"], r["FREQ"]),
      ("N9ABC", "20m", "SSB", "USB", "14.285000"))
check("record: date and time now, UTC", (r["QSO_DATE"], r["TIME_ON"]), ("20261127", "150000"))
check("record: refs, own call, grid", (r["POTA_REF"], r["MY_POTA_REF"], r["STATION_CALLSIGN"], r["GRIDSQUARE"]),
      ("US-1234,US-5678", "US-0001", "N0CALL", "EN59AX"))
check("record: SSB with no sideband from the radio is LSB on 40m",
      Q.adif_fields({**BASE, "mhz": "7.2", "sideband": ""}, "N0CALL", NOW)["SUBMODE"], "LSB")
r = Q.adif_fields({**BASE, "mode": "CW", "rst_sent": "599"}, "N0CALL", NOW)
check("record: CW has no submode", (r["MODE"], r["SUBMODE"]), ("CW", ""))
check("record: PSK31 files as PSK", Q.adif_fields({**BASE, "mode": "PSK31"}, "N0CALL", NOW)["MODE"], "PSK")
check("record: its identity, as the ledger keys it", Q.adif.key(Q.adif.qso(Q.adif_fields(BASE, "N0CALL", NOW), "N0CALL")),
      "N0CALL|N9ABC|20m|SSB||20261127150000")
check("record: 23:58 typed at 00:01 UTC is yesterday",
      Q.adif_fields({**BASE, "time_on": "23:58"}, "N0CALL", NOW - 15 * 3600 + 60)["QSO_DATE"], "20261126")
for label, change, word in (("call", {"call": "PARK"}, "call"), ("frequency", {"mhz": "15.5"}, "frequency"),
                            ("mode", {"mode": "WSPRX"}, "mode"), ("POTA", {"pota": "PARK-1"}, "POTA"),
                            ("SOTA", {"sota": "W1-HA"}, "SOTA"), ("grid", {"grid": "ZZ99"}, "grid"),
                            ("report", {"rst_sent": "five nine"}, "rst"), ("time", {"time_on": "25:00"}, "time")):
    try:
        Q.adif_fields({**BASE, **change}, "N0CALL", NOW)
        got = None
    except ValueError as e:
        got = word.lower() in str(e).lower()
    check(f"record: a bad {label} is refused, naming it", got, True)
check("record: a SOTA ref", Q.adif_fields({**BASE, "sota": "w1/ha-001"}, "N0CALL", NOW)["SOTA_REF"], "W1/HA-001")

# -- the add-on, end to end


class Entity:
    dxcc, name, cont, cq, itu = 291, "United States", "NA", 4, 7


class Cty:
    def lookup(self, call):
        return Entity()


class Needs:
    cty = Cty()

    def need(self, call, grid, band, mode):
        return ("grid", ["EN59 never worked"])


class Ledger:
    def history(self, call):
        return [{"date": "2026-10-01", "band": "40m", "mode": "SSB", "confirmed": True}] if call == "N9ABC" else []


class Cfg:
    call = "N0CALL"


class Sync:
    def __init__(self):
        self.cfg, self.ledger, self.needs, self.recorded = Cfg(), Ledger(), Needs(), []

    def record(self, fields, source):
        self.recorded.append((fields, source))
        return 7, "new"


state = tempfile.mkdtemp()
sync = Sync()
host = Host(sync, [{"id": "g90", "name": "G90", "present": True, "connected": True, "freq": 14_285_000,
                    "mode": "USB", "data": False}])


def make(opts=None):
    a = A.Qso(host, {"qrz_password_file": pwfile, **(opts or {})})
    a.state_dir = state
    a.clock = lambda: NOW
    return a


a = make({"radio": "g90"})
run(a.start())
a.spots, a.spots_at = spots, NOW - 60
a.qrz.post = FakeQrz([LOGIN_OK, CALL_OK]).get
snap = a.snapshot()
check("snapshot: the radio's dial, the logbook, QRZ set",
      (snap["rigs"][0]["freq"], snap["logsync"], snap["qrz"]["set"], snap["qrz"]["user"]), (14_285_000, True, True, "N0CALL"))
out = run(a.lookup({"id": "q1", "call": "n9abc", "khz": 14285, "band": "20m", "mode": "SSB"}))
check("lookup: DXCC, history, POTA, QRZ, need",
      (out["dxcc"]["name"], len(out["qsos"]), out["pota_ref"], out["qrz_state"], out["qrz"]["name"], out["need"]["level"]),
      ("United States", 1, "US-1234,US-5678", "ok", "Pat Smith", "grid"))
check("lookup: no answer carries the password", SECRET in json.dumps(out), False)
check("lookup: not a call", run(a.lookup({"id": "q2", "call": "73"}))["error"], "not a callsign")

out = a.log_qso({"id": "l1", "qso": {**BASE, "my_sota": "W1/HA-001"}})
check("log: into the logbook sync as manual", (len(sync.recorded), sync.recorded[0][1], sync.recorded[0][0]["CALL"]),
      (1, "manual", "N9ABC"))
check("log: the page hears what was logged", (out["qid"], out["what"], out["band"], out["mode"]), (7, "new", "20m", "USB"))
check("log: the own refs are kept", a.my.get("my_sota"), "W1/HA-001")
out = a.log_qso({"id": "l2", "qso": {**BASE, "mhz": "99"}})
check("log: a bad form logs nothing, says why", (len(sync.recorded), "frequency" in out["error"]), (1, True))
run(a.stop())

b = make()
run(b.start())
check("the own refs survive a restart", (b.my.get("my_sota"), b.my.get("my_pota")), ("W1/HA-001", "US-0001"))
run(b.stop())

c = A.Qso(Host(None, []), {"qrz_password_file": pwfile})
run(c.start())
check("no logbook sync: nothing logged, the page told", "isn't running" in c.log_qso({"id": "l3", "qso": BASE}).get("error", ""), True)
check("no logbook sync: the snapshot says so", c.snapshot()["logsync"], False)
run(c.stop())

# the login, from the tab
pw2 = os.path.join(tmp, "fresh", "qrz")
e = make({"qrz_password_file": pw2})
run(e.start())
check("no password yet: the tab is asked for a login", (e.snapshot()["qrz"]["set"], e.snapshot()["qrz"]["state"]), (False, "no_login"))
e.qrz.post = FakeQrz([LOGIN_OK]).get
out = run(e.login({"user": "n0call", "password": "  hunter2  "}))
check("login: written, mode 600, and tried",
      (open(pw2).read(), stat.S_IMODE(os.stat(pw2).st_mode), out["qrz_login"]["state"]), ("hunter2\n", 0o600, "ok"))
check("login: the answer never carries the password", "hunter2" in json.dumps(out), False)
check("login: an empty password writes nothing", "empty" in run(e.login({"password": ""}))["error"], True)
check("login: a control character is refused", "one line" in run(e.login({"password": "a\nb"}))["error"], True)
run(e.stop())


async def push_twice():
    g = make()
    await g.start()
    host.sent.clear()
    g.push_rig()
    g.push_rig()
    await asyncio.sleep(A.RIG_PUSH_S + 0.1)
    await g.stop()
    return [m.get("event") for m in host.sent]


check("two dial changes in a burst: one push", run(push_twice()), ["rigs"])

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
