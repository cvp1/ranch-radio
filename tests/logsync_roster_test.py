"""The roster (logsync_roster.py, logsync_udp's Decode/Status/Reply, the
Roster add-on): against fake JTDX traffic only. tests/fake_jtdx.py's decode()
and status() were checked byte for byte against a real JTDX 2.2 capture (83
decodes, 9 status messages); that capture isn't committed (it holds real
calls), the layout it fixed is.

  messages   the sending station and grid from CQ, CQ DX / CQ POTA, replies
             with a grid or a report; RR73 is never a grid; hashed <CALL>;
             junk and free text name no one; our own transmissions are skipped
  datagrams  Decode (2) and Status (1) parse; JTDX's short Status and its
             null strings; Reply (4) carries the decode's fields unchanged
  roster     calls to us first, then by need, then signal; old rows age out;
             a stale decode can't be answered
  dial       each row keeps the dial it arrived on (and exports it) across
             a QSY; none before JTDX's first Status
  rig        JTDX's Status as what it is doing (transmitting, calling whom):
             told on a change only; silence or its Close reads as gone
  engine     a fake JTDX's decodes through the relay intake fill the roster
             (with needs from a ledger and a cty.csv cut from the real one);
             a click reaches the fake JTDX's own socket as a Reply with that
             decode's exact fields; a stale or vanished key is refused, and
             nothing reaches JTDX; multicast mode refuses
  add-on     snapshot without the sync says why; a reply goes through the
             sync and the page hears the outcome

    python3 tests/logsync_roster_test.py
"""
import os as _os
_os.environ["RANCH_RADIO_OFFLINE"] = "1"      # no test reaches the internet (logsync_refdata)

import asyncio  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import struct  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import fake_jtdx as J  # noqa: E402
import testkit  # noqa: E402

import logsync  # noqa: E402
import logsync_roster as R  # noqa: E402
import logsync_udp as U  # noqa: E402
from logsync_ledger import Ledger  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"), flush=True)


# ================================================================ messages
for msg, want, why in [
        ("CQ N9ABC EN50", ("N9ABC", "EN50", True, None, None), "plain CQ"),
        ("CQ DX JA1XYZ PM95", ("JA1XYZ", "PM95", True, "DX", None), "CQ DX"),
        ("CQ POTA K9GTA EM49", ("K9GTA", "EM49", True, "POTA", None), "CQ POTA"),
        ("CQ VE3ARF FN25         ", ("VE3ARF", "FN25", True, None, None), "JTDX's trailing spaces"),
        ("CQ N9ABC", ("N9ABC", "", True, None, None), "CQ without a grid"),
        ("N0CALL N9ABC EN50", ("N9ABC", "EN50", False, None, "N0CALL"), "an answer with a grid"),
        ("N0CALL N9ABC -12", ("N9ABC", "", False, None, "N0CALL"), "a report"),
        ("N0CALL N9ABC R-12", ("N9ABC", "", False, None, "N0CALL"), "R-report"),
        ("N0CALL N9ABC RR73", ("N9ABC", "", False, None, "N0CALL"), "RR73 is not a grid"),
        ("<W1AW/P> N9ABC 73", ("N9ABC", "", False, None, "W1AW/P"), "a hashed call"),
        ("N0CALL <PJ4/K1ABC> -05", ("PJ4/K1ABC", "", False, None, "N0CALL"), "a hashed sender"),
        ("TNX BOB 73 GL", None, "free text names no one"), ("", None, "empty"), ("CQ", None, "a bare CQ")]:
    p = R.parse_message(msg)
    check(f"message: {why}", p and (p["call"], p["grid"], p["cq"], p["cq_word"], p["to"]), want)
check("is_grid: FN31 yes, RR73 no, AA00 yes", (R.is_grid("FN31"), R.is_grid("RR73"), R.is_grid("AA00")), (True, False, True))
check("latlon: a 4-char grid's centre", R.latlon("FN31"), (41.5, -73.0))
check("distance/bearing: due north one square", R.distance_bearing("FN31", "FN32")[1], 0)
check("  due east one square", R.distance_bearing("FN31", "FN41")[1], 89)
check("  same grid: zero", R.distance_bearing("FN31", "FN31")[0], 0)
check("  no grid: none", R.distance_bearing("FN31", ""), (None, None))

# ================================================================ datagrams
d = U.parse_decode(J.decode(msg="CQ VE3ARF FN25         ", snr=1, df=2518, t_ms=67_440_000, dt_s=0.2))
check("decode: every field", (d["id"], d["new"], d["t_ms"], d["snr"], round(d["dt"], 1), d["df"], d["mode"], d["message"], d["low"], d["off_air"]),
      ("JTDX", True, 67_440_000, 1, 0.2, 2518, "~", "CQ VE3ARF FN25         ", False, False))
s = U.parse_status(J.status(dial=18_100_000, mode="FT8", de="N0CALL", de_grid="FN31", dx_call=None, report="-11", special=0))
check("status: JTDX's short form, null strings read as empty",
      (s["dial"], s["mode"], s["de_call"], s["de_grid"], s["dx_call"], s["dx_grid"]), (18_100_000, "FT8", "N0CALL", "FN31", "", ""))
rep = U.build_reply(d)
check("reply: type 4, schema 2, from JTDX's id", U.header(rep), (2, 4, "JTDX"))
check("  carries the decode's fields unchanged, modifiers 0", rep,
      U.MAGIC.to_bytes(4, "big") + struct.pack(">II", 2, 4) + J._s("JTDX") + struct.pack(">I", 67_440_000)
      + struct.pack(">i", 1) + struct.pack(">d", d["dt"]) + struct.pack(">I", 2518) + J._s("~")
      + J._s("CQ VE3ARF FN25         ") + b"\x00" + b"\x00")
for bad in (b"", b"\x00" * 20, J.decode()[:-3]):
    try:
        U.parse_decode(bad)
        check("decode: a short or foreign datagram is refused", "parsed", "refused")
    except ValueError:
        check("decode: a short or foreign datagram is refused", True, True)

# ================================================================ roster ordering, ageing, freshness
now = [1000.0]
ro = R.Roster(clock=lambda: now[0])
ro.status(U.parse_status(J.status(dial=14_074_000, de="N0CALL", de_grid="FN31")))
check("roster: band from the dial", ro.st["band"], "20m")
for i, (m, snr) in enumerate([("CQ K1AAA FN42", -5), ("CQ JA1XYZ PM95", -18), ("N0CALL W9ZZZ EN50", -20), ("N9ABC N0CALL R-10", 0)]):
    ro.add(U.parse_decode(J.decode(msg=m, snr=snr, df=1000 + i, t_ms=50_000_000)))


class FakeNeeds:
    cty = None

    def need(self, call, grid, band, mode):
        return {"JA1XYZ": ("dxcc", ["dxcc"]), "K1AAA": ("call_band_mode", ["call_band_mode"])}.get(call, (None, []))

    def lotw_user(self, call):
        return "2026-11-01" if call == "JA1XYZ" else None

    def summary(self):
        return {"dxcc_worked": 1}


ro.needs = FakeNeeds()
v = ro.view()
check("  our own transmission isn't a row", [r["call"] for r in v["rows"]].count("N0CALL"), 0)
check("  calls to us first, then by need, then signal", [r["call"] for r in v["rows"]], ["W9ZZZ", "JA1XYZ", "K1AAA"])
check("  rows carry need, LoTW, distance", (v["rows"][1]["need"], v["rows"][1]["lotw"], v["rows"][1]["km"] > 10000), ("dxcc", True, True))
check("  no raw decode reaches the page", any(k in v["rows"][0] for k in ("dec", "t_ms", "id", "low")), False)
k = v["rows"][1]["key"]
check("  a fresh decode can be answered", (ro.for_reply(k)[0] or {}).get("message"), "CQ JA1XYZ PM95")
now[0] += R.REPLY_MAX_AGE_S + 1
check("  a stale one can't", ro.for_reply(k)[0], None)
now[0] += R.KEEP_S
check("  and ages off the roster", ro.view()["rows"], [])

# ================================================================ a click answers the station's CQ
now = [5000.0]
ro = R.Roster(clock=lambda: now[0])
ro.status(U.parse_status(J.status(dial=14_074_000, de="N0CALL", de_grid="FN31")))
ro.add(U.parse_decode(J.decode(msg="CQ JA1XYZ PM95", t_ms=1000, df=900)))
now[0] += 15
ro.add(U.parse_decode(J.decode(msg="K9ABC JA1XYZ -05", t_ms=16000, df=900)))
ro.add(U.parse_decode(J.decode(msg="JA2XYZ K9ABC R-07", t_ms=16000, df=1300)))
rows = {r["call"]: r for r in ro.view()["rows"]}
check("cq: the row shows the latest line, keeps the CQ's grid", (rows["JA1XYZ"]["message"], rows["JA1XYZ"]["grid"]),
      ("K9ABC JA1XYZ -05", "PM95"))
check("  and answers the CQ", (rows["JA1XYZ"]["answer"], (ro.for_reply(rows["JA1XYZ"]["key"])[0] or {}).get("message")),
      ("cq", "CQ JA1XYZ PM95"))
check("  a station never heard calling CQ: its latest line, marked", (rows["K9ABC"]["answer"],
      (ro.for_reply(rows["K9ABC"]["key"])[0] or {}).get("message")), ("line", "JA2XYZ K9ABC R-07"))
check("  the latest line's key still answers it", (ro.for_reply(R.Roster.dkey(ro.rows["JA1XYZ"]["dec"]))[0] or {}).get("message"),
      "K9ABC JA1XYZ -05")
now[0] += R.REPLY_MAX_AGE_S - 10
check("  once the CQ is stale, the latest line", ro.view()["rows"][0]["answer"] if ro.view()["rows"][0]["call"] == "JA1XYZ"
      else {r["call"]: r for r in ro.view()["rows"]}["JA1XYZ"]["answer"], "line")
now[0] += 20
check("  nothing fresh: nothing to answer", {r["call"]: r for r in ro.view()["rows"]}["JA1XYZ"]["answer"], None)
ro.add(U.parse_decode(J.decode(msg="CQ JA1XYZ PM95", t_ms=91000, df=900)))
check("  a new CQ: answerable again", {r["call"]: r for r in ro.view()["rows"]}["JA1XYZ"]["answer"], "cq")

# ================================================================ each row's own dial (UX plan PR 4a, 2026-11-19)
# The scope places a decode at the dial it was heard on plus its df; a row
# kept across a QSY must not take the snapshot's newer dial.
now = [9000.0]
ro = R.Roster(clock=lambda: now[0])
ro.status(U.parse_status(J.status(dial=14_074_000, de="N0CALL", de_grid="FN31")))
ro.add(U.parse_decode(J.decode(msg="CQ N9ABC EN52", df=1200)))
ro.status(U.parse_status(J.status(dial=7_074_000, de="N0CALL", de_grid="FN31")))      # JTDX QSYs; the row stays
ro.add(U.parse_decode(J.decode(msg="CQ K6ABC CM87", df=900)))
v = ro.view()
check("dial: each row keeps the dial and band it arrived on, and the view exports them",
      {r["call"]: (r["band"], r["dial"], r["df"]) for r in v["rows"]},
      {"N9ABC": ("20m", 14_074_000, 1200), "K6ABC": ("40m", 7_074_000, 900)})
check("  the snapshot's dial is JTDX's now, not the kept row's", v["dial"], 7_074_000)
ro2 = R.Roster(clock=lambda: now[0])
ro2.add(U.parse_decode(J.decode(msg="CQ N9ABD EN52", df=1200)))
check("  a decode before any Status: no dial (the scope will not place it)", ro2.view()["rows"][0]["dial"], None)

# ================================================================ the engine, end to end through the relay


class Bg:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coro, timeout=30):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def call(self, fn, *a):
        async def f():
            return fn(*a)
        return self.run(f())


BG = Bg()
tmp = Path(tempfile.mkdtemp(prefix="logsync-roster-"))
shutil.copy(ROOT / "tests/fixtures/cty-sample.csv", tmp / "cty.csv")     # the cached copy (offline: never fetched)
led = Ledger(str(tmp / "l.db"), ["wavelog"])
led.add({"CALL": "JA1ABC", "BAND": "20m", "MODE": "FT8", "QSO_DATE": "20260101", "TIME_ON": "120000",
         "STATION_CALLSIGN": "N0CALL", "DXCC": "339", "GRIDSQUARE": "PM95"}, "test", {"wavelog": "sent"}, own="N0CALL")
led.db.commit()
led.close()
port = testkit.free_port()
pushes, rigs = [], []
eng = logsync.LogSync({"call": "N0CALL", "udp": {"bind": "127.0.0.1", "port": port}, "ledger": str(tmp / "l.db"),
                       "timing": {"pull_at_start": False}}, roster=lambda: pushes.append(time.time()),
                      rig=lambda: rigs.append(eng.rig_view()))
BG.run(eng.start())
testkit.wait_for(lambda: eng.needs is not None, 10)
check("engine: the needs index is built from the ledger and the cached cty.csv",
      (eng.needs.summary()["dxcc_worked"], eng.needs.cty is not None), (1, True))
jt = J.FakeJtdx()
jt.send(J.status(dial=14_074_000, de="N0CALL", de_grid="FN31"), ("127.0.0.1", port))
for m, df in (("CQ JA2XYZ PM96", 1200), ("CQ VK9NA RG30", 1400), ("CQ JA1ABC PM95", 1600)):
    jt.send(J.decode(msg=m, df=df), ("127.0.0.1", port))
check("  a burst of decodes: one push to the page", testkit.wait_for(lambda: len(pushes) >= 1, 5) and len(pushes), 1)
rows = {r["call"]: r for r in BG.call(eng.roster_view)["rows"]}
check("  needs on each row (new DXCC, new grid, worked but never confirmed)",
      (rows["VK9NA"]["need"], rows["JA2XYZ"]["need"], rows["JA1ABC"]["need"]), ("dxcc", "grid", "dxcc_unconfirmed"))
check("  entities from cty.csv", (rows["VK9NA"]["entity"], rows["JA2XYZ"]["entity"]), ("Norfolk Island", "Japan"))
jt.poll()
jt.got.clear()
why = BG.call(eng.reply, rows["VK9NA"]["key"])
got = []
testkit.wait_for(lambda: bool(got.extend(jt.poll()) or got), 3)
check("  a click: sent (no reason given)", why, None)
r4 = [d for d, _ in got if J.type_of(d) == 4]
check("  JTDX's own socket gets one Reply", len(r4), 1)
if r4:
    rr = U.Reader(r4[0]); rr.u32(); rr.u32(); rr.u32(); cid = rr.utf8()
    fields = (cid, rr.u32(), rr.i32(), round(struct.unpack(">d", rr.take(8))[0], 1), rr.u32(), rr.utf8(), rr.utf8(), rr.u8(), rr.u8())
    check("  with that decode's exact fields, modifiers 0", fields, ("JTDX", 12 * 3600 * 1000, -10, 0.1, 1400, "~", "CQ VK9NA RG30", 0, 0))
outcomes = []
logsync.REPLY_CONFIRM_S = 0.6
jt.got.clear()
BG.call(eng.reply, rows["VK9NA"]["key"], lambda ok, msg: outcomes.append((ok, msg)))
jt.send(J.status(dial=14_074_000, de="N0CALL", de_grid="FN31", dx_call="VK9NA"), ("127.0.0.1", port))
check("  JTDX took it (its Status names the station): the page is told", testkit.wait_for(lambda: outcomes, 3) and outcomes,
      [(True, "JTDX is calling VK9NA.")])
time.sleep(0.8)
check("  once", len(outcomes), 1)
outcomes.clear()
BG.call(eng.reply, rows["JA2XYZ"]["key"], lambda ok, msg: outcomes.append((ok, msg)))
jt.send(J.status(dial=14_074_000, de="N0CALL", de_grid="FN31", dx_call="VK9NA"), ("127.0.0.1", port))
check("  JTDX ignored it (no Status with that call): the page is told which setting",
      testkit.wait_for(lambda: outcomes, 3) and (outcomes[0][0], "Accept UDP requests" in outcomes[0][1], "reply messages" in outcomes[0][1]),
      (False, True, False))
eng.roster.add(U.parse_decode(J.decode(msg="N9XYZ JA3XYZ -03", df=1700)))
k3 = {r["call"]: r for r in BG.call(eng.roster_view)["rows"]}["JA3XYZ"]["key"]
outcomes.clear()
BG.call(eng.reply, k3, lambda ok, msg: outcomes.append((ok, msg)))
check("  a non-CQ line ignored: it names the reply filter too",
      testkit.wait_for(lambda: outcomes, 3) and ("reply messages" in outcomes[0][1] and "wasn't calling CQ" in outcomes[0][1]), True)
first, second = [], []
BG.call(eng.reply, rows["JA2XYZ"]["key"], lambda ok, msg: first.append((ok, msg)))
BG.call(eng.reply, rows["VK9NA"]["key"], lambda ok, msg: second.append((ok, msg)))
check("  a second click before JTDX answers: the first page is told it was replaced",
      testkit.wait_for(lambda: first, 1) and (first[0][0], "replaced" in first[0][1], "JA2XYZ" in first[0][1]), (False, True, True))
jt.send(J.status(dial=14_074_000, de="N0CALL", de_grid="FN31", dx_call="VK9NA"), ("127.0.0.1", port))
check("  and the second still resolves on its own Status", testkit.wait_for(lambda: second, 3) and second,
      [(True, "JTDX is calling VK9NA.")])
time.sleep(0.8)
check("  each told once", (len(first), len(second)), (1, 1))
jt.poll()
jt.got.clear()
check("  an unknown key: refused, nothing sent", (BG.call(eng.reply, "nope") is not None, (time.sleep(0.4), jt.poll())[1]), (True, []))
for row in eng.roster.rows.values():
    row["at"] -= R.REPLY_MAX_AGE_S + 5
why = BG.call(eng.reply, rows["JA2XYZ"]["key"])
check("  a stale decode: refused with its age, nothing sent", ("old" in (why or ""), (time.sleep(0.4), jt.poll())[1]), (True, []))

# ---- what JTDX says it is doing (rig_view), for the Map tab: edge-triggered, and never a stale "transmitting"
dest = ("127.0.0.1", port)
calling = dict(dial=14_074_000, de="N0CALL", de_grid="FN31", dx_call="VK9NA", dx_grid="RG30", tx_enabled=True, decoding=False)
testkit.wait_for(lambda: len(rigs) >= 1, 3)
rigs.clear()
jt.send(J.status(transmitting=True, **calling), dest)
check("rig: JTDX transmitting is told at once, with the station it is calling",
      testkit.wait_for(lambda: len(rigs) == 1, 3) and rigs[-1],
      {"alive": True, "transmitting": True, "tx_enabled": True, "decoding": False, "dx_call": "VK9NA", "dx_grid": "RG30",
       "mode": "FT8", "dial": 14_074_000, "band": "20m"})
jt.send(J.status(transmitting=True, **calling), dest)
jt.send(J.heartbeat(), dest)
jt.send(J.status(transmitting=False, **calling), dest)
check("  an unchanged Status says nothing; its turn ending does",
      (testkit.wait_for(lambda: len(rigs) == 2, 3), len(rigs), rigs[-1]["transmitting"], rigs[-1]["tx_enabled"]), (True, 2, False, True))
jt.send(J.status(transmitting=True, **calling), dest)
testkit.wait_for(lambda: len(rigs) == 3, 3)
silent, logsync.RIG_SILENT_S = logsync.RIG_SILENT_S, 0.6
jt.send(J.heartbeat(), dest)                     # then nothing: it crashed, or the machine slept, mid-transmission
check("  transmitting, then silence: after RIG_SILENT_S it is gone, not transmitting for ever",
      (testkit.wait_for(lambda: len(rigs) == 4, 5), rigs[-1]), (True, {"alive": False}))
logsync.RIG_SILENT_S = silent
jt.send(J.status(transmitting=False, **calling), dest)
testkit.wait_for(lambda: len(rigs) == 5, 3)
jt.send(J.close(), dest)
check("  back, then its Close: gone at once", (testkit.wait_for(lambda: len(rigs) == 6, 3), rigs[-2]["alive"], rigs[-1]), (True, True, {"alive": False}))
BG.run(eng.stop())

mport = testkit.free_port()
eng2 = logsync.LogSync({"call": "N0CALL", "udp": {"group": "239.255.77.77", "port": mport, "iface": "127.0.0.1"},
                        "ledger": str(tmp / "m.db"), "timing": {"pull_at_start": False}})
try:
    BG.run(eng2.start())
    eng2.roster.status(U.parse_status(J.status()))
    eng2.roster.add(U.parse_decode(J.decode(msg="CQ JA2XYZ PM96")))
    k = eng2.roster.view()["rows"][0]["key"]
    check("multicast: the roster can't answer (GridTracker does there)", "multicast" in (BG.call(eng2.reply, k) or ""), True)
    BG.run(eng2.stop())
except OSError as e:
    print(f"SKIP multicast reply check: {type(e).__name__} (no multicast on this machine)")

# ================================================================ the add-on, with a fake host
sys.path.insert(0, str(ROOT))
from addons.roster import Roster as RosterAddon  # noqa: E402


class Host:
    def __init__(self, ls):
        self.logsync, self.notes, self.sent = ls, [], []

    def notice(self, msg, level="info", ws=None):
        self.notes.append((level, msg))

    def _broadcast(self, payload):
        self.sent.append(payload)

    async def _send(self, ws, payload):
        self.sent.append(payload)


class Sync:
    def __init__(self):
        self.asked = []

    def roster_view(self):
        return {"rows": [{"call": "K1AAA"}]}

    def reply(self, key, done=None):
        self.asked.append(key)
        if key == "good":
            done(False, "JTDX didn't take it.")
        return None if key == "good" else "that decode is no longer on the roster"


a = RosterAddon(Host(None), {"enabled": True})
check("add-on: without the sync, the snapshot says why", "isn't running" in a.snapshot().get("error", ""), True)
h = Host(Sync())
a = RosterAddon(h, {"enabled": True})
check("  with it, the roster", a.snapshot(), {"roster": {"rows": [{"call": "K1AAA"}]}})
BG.run(a.command(None, "reply", {"key": "good", "call": "K1AAA"}))
BG.run(a.command(None, "reply", {"key": "gone", "call": "K1AAA"}))
check("  a reply goes to the sync; the page hears each outcome", (h.logsync.asked, [lv for lv, _ in h.notes]), (["good", "gone"], ["warn", "warn"]))
check("  JTDX's outcome is the notice", h.notes[0][1], "Roster: JTDX didn't take it.")
a.push()
check("  a push broadcasts the roster", '"addon": "roster"' in h.sent[-1] and "K1AAA" in h.sent[-1], True)

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{'all ok' if not fails else f'{fails} FAILED'}", flush=True)
_os._exit(1 if fails else 0)
