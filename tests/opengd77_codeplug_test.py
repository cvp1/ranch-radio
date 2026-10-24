"""OpenGD77 codeplug through qdmr's dmrconf (qdmr.py, addons/opengd77.py),
against tests/fake_dmrconf.py and tests/fake_opengd77.py.

  - qdmr YAML channels: FM and DMR, MHz with or without the unit, CTCSS/DCS
  - read: a dated copy, its channels on the page, the YAML on request; the
    add-on lets go of the port while dmrconf runs (no read of ours inside
    a dmrconf call) and takes it back after
  - write refused: without confirm; when dmrconf has no openuv380 driver
    (0.11.3 would write with the command this radio ignores); when the radio
    is not an MD-UV380/390; when dmrconf verify rejects the file. None of
    these call dmrconf write.
  - write: verify, a backup read, write, a read-back; the radio holds the
    new codeplug, the backup holds the old one, and the page is told the
    channels match
  - a read-back that differs is reported, naming the backup

    python3 tests/opengd77_codeplug_test.py
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import qdmr  # noqa: E402
from fake_opengd77 import FakeOpenGD77  # noqa: E402
from testkit import free_port  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


CODEPLUG = """version: 0.12.0
settings:
  introLine1: KK1ABC
radioIDs:
  - dmr: {id: id1, name: KK1ABC, number: 1234567}
zones:
  - id: zone1
    name: Home
    A: [ch1, ch2, ch3]
channels:
  - fm:
      id: ch1
      name: 2m Call
      rxFrequency: 146.52 MHz
      txFrequency: 146.52 MHz
      rxTone: ~
      txTone: {ctcss: 100}
      bandwidth: Wide
      power: High
  - fm:
      id: ch2
      name: "Rpt 147.000"
      rxFrequency: 147.000
      txFrequency: 147.600
      rxTone: {dcs: n023}
      txTone: {ctcss: 88.5 Hz}
      power: Low
  - dmr:
      id: ch3
      name: BM 3104
      rxFrequency: 439.4 MHz
      txFrequency: 430.4 MHz
      colorCode: 1
      timeSlot: TS2
      power: High
"""

# --- parsing
ch = qdmr.channels(CODEPLUG)
check("three channels", [c["name"] for c in ch], ["2m Call", "Rpt 147.000", "BM 3104"])
check("FM, MHz with the unit, CTCSS", (ch[0]["kind"], ch[0]["rx_hz"], ch[0]["tx_tone"], ch[0]["rx_tone"]),
      ("fm", 146_520_000, "100.0", ""))
check("FM, MHz without the unit, DCS, CTCSS in Hz", (ch[1]["tx_hz"], ch[1]["rx_tone"], ch[1]["tx_tone"]),
      (147_600_000, "D023N", "88.5"))
check("DMR: colour code, timeslot", (ch[2]["kind"], ch[2]["colour_code"], ch[2]["timeslot"]), ("dmr", "1", "2"))
check("same channels: equal", qdmr.same_channels(ch, qdmr.channels(CODEPLUG)), (True, None))
check("same channels: one fewer is a difference", qdmr.same_channels(ch, ch[:2])[0], False)
check("other lists (zones) are not channels", len(qdmr.channels(CODEPLUG.replace("channels:", "other:"))), 0)

# --- zones against the radio's own memory
L = {"slots": [1, 2, 5], "names": {1: "2m Call", 2: "Rpt 147.000", 5: "BM 3104"},
     "zones": [{"name": "Home", "slots": [1, 5, 2]}]}
check("zones parsed", [(z["id"], z["name"], z["A"]) for z in qdmr.zones(CODEPLUG)], [("zone1", "Home", ["ch1", "ch2", "ch3"])])
check("zones_match: a file pointing elsewhere is named", bool(qdmr.zones_match(CODEPLUG, L)), True)
fixed = qdmr.rebuild_zones(CODEPLUG, L)
check("rebuild: the zone by position", qdmr.zones(fixed)[0]["A"], ["ch1", "ch3", "ch2"])
check("rebuild: then it matches", qdmr.zones_match(fixed, L), None)
check("rebuild: only the zone line changed",
      [a for a, b in zip(CODEPLUG.splitlines(), fixed.splitlines()) if a != b], ["    A: [ch1, ch2, ch3]"])
try:
    qdmr.rebuild_zones(CODEPLUG, dict(L, names={1: "2m Call", 2: "Other", 5: "BM 3104"}))
    check("rebuild: a slot holding another name is refused", "no error", "QdmrError")
except qdmr.QdmrError as e:
    check("rebuild: a slot holding another name is refused", "slot 2" in str(e), True)
try:
    qdmr.drop_aprs_phantom(CODEPLUG, 2)
    check("phantom: a real extra channel is not dropped", "no error", "QdmrError")
except qdmr.QdmrError as e:
    check("phantom: a real extra channel is not dropped", "BM 3104" in str(e), True)
check("phantom: nothing past the slots, nothing changed", qdmr.drop_aprs_phantom(CODEPLUG, 3), (CODEPLUG, []))
check("compacted: still sparse is named", "slots" in (qdmr.compacted(fixed, L) or ""), True)
C = {"slots": [1, 2, 3], "names": {1: "2m Call", 2: "Rpt 147.000", 3: "BM 3104"},
     "zones": [{"name": "Home", "slots": [1, 3, 2]}]}
check("compacted: slots 1..N, names, zones by the new numbers", qdmr.compacted(fixed, C), None)
check("compacted: a zone the radio holds differently is named",
      bool(qdmr.compacted(fixed, dict(C, zones=[{"name": "Home", "slots": [1, 2, 3]}]))), True)

# --- what qdmr loses: power levels, the DMR ID override
check("power: levels qdmr can't write are named",
      qdmr.power_losses({"names": {1: "SV DMR", 2: "B"}, "power": {1: 7, 2: 9}}), [("SV DMR", 7)])
DMRFILE = """settings:
  defaultID: id1
channels:
  - dmr:
      id: ch1
      name: SV DMR
      radioId: !default ""
  - fm:
      id: ch2
      name: FM
"""
pinned, k = qdmr.pin_default_radio_id(DMRFILE)
check("radioId !default pinned to the default ID", (k, "radioId: id1" in pinned, "!default" in pinned), (1, True, False))
check("compacted: an override of 0 in the radio is named",
      "DMR ID with 0" in (qdmr.compacted(fixed, dict(C, dmr_id={1: 0, 2: None, 3: None})) or ""), True)

# --- the add-on on a radio server
TMP = tempfile.mkdtemp(prefix="opengd77-cp-")
FD = os.path.join(TMP, "dmrconf")
os.makedirs(FD)
with open(os.path.join(FD, "radio.yaml"), "w") as f:
    f.write(CODEPLUG)
CPS = os.path.join(TMP, "codeplugs")
WEB = free_port()
fake = FakeOpenGD77().start()
fake.layout_file = os.path.join(FD, "layout.json")
CFG = os.path.join(TMP, "addons.json")
with open(CFG, "w") as f:
    json.dump({"opengd77": {"port": fake.path, "poll": 0.3, "dmrconf": os.path.join(ROOT, "tests/fake_dmrconf.py"),
                            "codeplugs": CPS}}, f)


def calls():
    try:
        with open(os.path.join(FD, "calls.log")) as f:
            return [l.split(" ", 2) for l in f if l.strip()]
    except OSError:
        return []


def verbs():
    return [c[2].split()[0] for c in calls() if not c[2].startswith("--")]


class Page:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.events = []
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.run(self._open())

    def run(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _open(self):
        self.sess = aiohttp.ClientSession()
        self.ws = await self.sess.ws_connect(f"http://127.0.0.1:{WEB}/ws", max_msg_size=0)
        self.hello = json.loads((await self.ws.receive()).data)
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                self.events.append(json.loads(msg.data))

    def send(self, **m):
        self.run(self.ws.send_str(json.dumps({"t": "addon", "addon": "opengd77", **m})))

    def wait(self, pred, timeout=15, since=0):
        end = time.time() + timeout
        while time.time() < end:
            for e in self.events[since:]:
                if pred(e):
                    return e
            time.sleep(0.05)
        return None

    def notice(self, text, since, timeout=15):
        e = self.wait(lambda e: e.get("t") == "notice" and text in json.dumps(e), timeout, since)
        if not e:
            self.dump(f"no notice with {text!r} within {timeout} s", since)
        return e and json.dumps(e)

    def status(self):
        """The add-on's status as the page last heard it (a status event, or
        the snapshot sent when it was switched on)."""
        for e in reversed(list(self.events)):
            if gd(e) and (e.get("status") or (e.get("snapshot") or {}).get("status")):
                return e.get("status") or e["snapshot"]["status"]
        return ((self.hello.get("addons") or {}).get("opengd77") or {}).get("status") or {}

    def ready(self, what, timeout=20):
        """What the page waits for before it enables Read and Write: the radio
        connected and no job running (static/addons/opengd77.js). The LAST
        status counts: a job's own status (connected: false) comes before its
        notice, so a "connected" heard before the job started is never taken
        for the watch being back after it."""
        end = time.time() + timeout
        while time.time() < end:
            st = self.status()
            if st.get("connected") is True and not st.get("busy"):
                return True
            time.sleep(0.05)
        check(f"(precondition) {what}: the radio connected, not busy", self.status(), "connected, not busy")
        self.dump(f"not ready for {what}", max(0, len(self.events) - 30))
        return False

    def dump(self, why, since):
        """What a failing wait saw: every notice and status since, and the server's log."""
        print(f"      -- {why}; since event {since}:")
        for e in list(self.events)[since:]:
            if e.get("t") == "notice":
                print(f"      notice: {e.get('msg')}")
            elif gd(e) and e.get("status"):
                print(f"      status: {e['status']}")
        print("      server log (last 40 lines):\n" + "\n".join("      | " + l for l in server_log(40)))

    def close(self):
        async def _c():
            await self.ws.close()
            await self.sess.close()
        self.run(_c())


def gd(e):
    return e.get("t") == "addon" and e.get("addon") == "opengd77"


def connected(e):
    return gd(e) and (e.get("status") or {}).get("connected") is True


NEW = CODEPLUG.replace("2m Call", "2m Simplex").replace("146.52 MHz", "146.55 MHz")

server = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB), "--rigctl-port", "0",
                           "--addons", CFG, "--addons-state", os.path.join(TMP, "state.json")],
                          cwd=ROOT, env=dict(os.environ, FAKE_DMRCONF_DIR=FD),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
LOG = []                          # the server's log, read as it comes (a full pipe would stall the server)


def _drain_log():
    for line in server.stdout:
        LOG.append(line.rstrip("\n"))


threading.Thread(target=_drain_log, daemon=True).start()


def server_log(n):
    return [l for l in list(LOG) if " civ INFO " not in l][-n:]


for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", WEB), timeout=0.2).close()
        break
    except OSError:
        time.sleep(0.1)
try:
    page = Page()
    page.ready("the first read")

    # read
    n = len(page.events)
    page.send(do="codeplug_read")
    check("read: done, with its channel count", bool(page.notice("codeplug read: 3 channels", n)), True)
    e = page.wait(lambda e: gd(e) and (e.get("codeplug") or {}).get("channels"), since=n)
    check("read: the channels reach the page", e and [c["name"] for c in e["codeplug"]["channels"]],
          ["2m Call", "Rpt 147.000", "BM 3104"])
    saved = sorted(os.listdir(CPS))
    check("read: a dated copy is kept", len(saved) == 1 and saved[0].endswith("-read.yaml"), True)
    check("read: the radio was asked for openuv380", any("--radio openuv380" in c[2] for c in calls()), True)
    check("after the read: the VFO watch is back", page.wait(connected, since=n) is not None, True)
    t0, t1 = map(float, [c for c in calls() if c[2].startswith("read")][-1][:2])
    with fake.lock:
        inside = [t for t in fake.times if t0 < t < t1]
    check("no read of ours while dmrconf held the radio", inside, [])
    n = len(page.events)
    page.send(do="codeplug_get")
    e = page.wait(lambda e: gd(e) and e.get("codeplug_yaml"), since=n)
    check("the YAML on request", e and e["codeplug_yaml"], CODEPLUG)

    open(os.path.join(FD, "old"), "w").close()
    page.ready("a read with dmrconf 0.11.3")
    n = len(page.events)
    before = [v for v in verbs() if v == "read"]
    page.send(do="codeplug_read")
    check("read with dmrconf 0.11.3: refused, says why", bool(page.notice("codeplug layout", n)), True)
    check("  and dmrconf read was not run", [v for v in verbs() if v == "read"], before)
    os.remove(os.path.join(FD, "old"))

    # a read dmrconf can't decode cleanly: shown, and a write is stopped before dmrconf write
    open(os.path.join(FD, "warn"), "w").close()
    page.ready("a read with linking problems")
    n = len(page.events)
    page.send(do="codeplug_read")
    msg = page.notice("could not decode it cleanly", n)
    check("read with linking problems: says writing is blocked", bool(msg and "writing is blocked" in msg), True)
    e = page.wait(lambda e: gd(e) and (e.get("codeplug") or {}).get("problems"), since=n)
    check("  the problems reach the page", bool(e and "out of bounds" in e["codeplug"]["problems"][0]), True)
    page.ready("a write after a read with problems")
    n = len(page.events)
    before = len(calls())
    page.send(do="codeplug_write", yaml=NEW, confirm=True)
    check("write when the backup read has problems: stopped, nothing sent",
          bool(page.notice("Nothing was sent", n, timeout=20)), True)
    check("  dmrconf write never ran", [c[2].split()[0] for c in calls()[before:] if not c[2].startswith("--")],
          ["verify", "encode", "read"])
    os.remove(os.path.join(FD, "warn"))

    # refusals: none of them reaches dmrconf write
    page.ready("the refusals")
    n = len(page.events)
    page.send(do="codeplug_write", yaml=NEW)
    check("write without confirm: refused", bool(page.notice("confirm first", n)), True)
    open(os.path.join(FD, "old"), "w").close()
    n = len(page.events)
    page.send(do="codeplug_write", yaml=NEW, confirm=True)
    check("write with dmrconf 0.11.3: refused, says why", bool(page.notice("no 'openuv380' driver", n)), True)
    os.remove(os.path.join(FD, "old"))
    page.ready("a write verify rejects")
    n = len(page.events)
    page.send(do="codeplug_write", yaml=NEW.replace("2m Simplex", "BAD"), confirm=True)
    check("write a file verify rejects: refused with dmrconf's reason",
          bool(page.notice("outside the radio's bands", n)), True)
    check("no refusal reached dmrconf write", "write" in verbs(), False)
    with open(os.path.join(FD, "radio.yaml")) as f:
        check("the radio still holds its codeplug", f.read(), CODEPLUG)

    # sparse channel slots (the real radio on mac-shack, 2026-10-17): slots 1, 2, 5;
    # zone Home holds slots 1, 5, 2. qdmr reads the channels as ch1..ch3, takes
    # the zone's slot numbers as channel numbers (5 is out of bounds, dropped,
    # with its warning) and makes up an 'APRS Channel' for an unset APRS frequency.
    fake.slots, fake.names = [1, 2, 5], {1: "2m Call", 2: "Rpt 147.000", 5: "BM 3104"}
    fake.zones = [{"name": "Home", "slots": [1, 5, 2]}]
    QDMR_SPARSE = CODEPLUG.replace("A: [ch1, ch2, ch3]", "A: [ch1, ch2]") + """  - fm:
      id: ch4
      name: APRS Channel
      rxFrequency: 127 MHz
      txFrequency: 127 MHz
      bandwidth: Narrow
positioning:
  - aprs:
      id: aprs1
      name: APRS1
      revert: ch4
      source: KK1ABC-7
"""
    with open(os.path.join(FD, "radio.yaml"), "w") as f:
        f.write(QDMR_SPARSE)
    open(os.path.join(FD, "warn"), "w").close()
    page.ready("a write onto slots with gaps")
    n = len(page.events)
    before = len(calls())
    page.send(do="codeplug_write", yaml=NEW, confirm=True)
    msg = page.notice("slots with gaps", n)
    check("gaps, a file whose zones aren't the radio's: refused", bool(msg), True)
    check("  naming the empty slots", bool(msg and "empty: 3-4" in msg), True)
    check("  and only dmrconf's driver check ran", [c[2].split()[0] for c in calls()[before:]], ["--list-radios"])

    page.ready("a read of slots with gaps")
    n = len(page.events)
    page.send(do="codeplug_read")
    msg = page.notice("codeplug read: 3 channels", n)
    check("gaps, read: zones rebuilt from the radio", bool(msg and "zones rebuilt" in msg), True)
    check("  the made-up APRS channel dropped", bool(msg and "APRS Channel" in msg), True)
    check("  and writing is not blocked", bool(msg and "blocked" not in msg), True)
    n = len(page.events)
    page.send(do="codeplug_get")
    e = page.wait(lambda e: gd(e) and e.get("codeplug_yaml"), since=n)
    got = e and e["codeplug_yaml"] or ""
    check("  the zone points where the radio's does", qdmr.zones(got)[0]["A"] if got else None, ["ch1", "ch3", "ch2"])
    check("  no APRS channel, no revert to it", ("APRS Channel" in got, "revert:" in got), (False, False))
    check("  qdmr's own file kept beside it", any(f.endswith("-read-qdmr.yaml") for f in os.listdir(CPS)), True)

    page.ready("a write from that read")
    n = len(page.events)
    edited = got.replace("2m Call", "2m Simplex")
    page.send(do="codeplug_write", yaml=edited, confirm=True)
    msg = page.notice("codeplug written", n, timeout=20)
    check("gaps, a write from that read: done, in slots 1-3", bool(msg and "slots 1-3" in msg), True)
    check("  the radio's memory: slots closed, the zone by the new numbers",
          (fake.slots, fake.zones), ([1, 2, 3], [{"name": "Home", "slots": [1, 3, 2]}]))
    os.remove(os.path.join(FD, "warn"))
    with open(os.path.join(FD, "radio.yaml"), "w") as f:
        f.write(CODEPLUG)
    fake.names = {1: "2m Call", 2: "Rpt 147.000", 3: "BM 3104"}
    fake.zones = [{"name": "Home", "slots": [1, 2, 3]}]

    # write
    page.ready("the write")
    n = len(page.events)
    before = len(calls())
    page.send(do="codeplug_write", yaml=NEW, confirm=True)
    check("write: done, channels match, backup named", bool(page.notice("3 channels match", n, timeout=20)), True)
    check("write: verify, encode, backup read, write, read-back", [c[2].split()[0] for c in calls()[before:]
                                                                  if not c[2].startswith("--")],
          ["verify", "encode", "read", "write", "read"])
    with open(os.path.join(FD, "radio.yaml")) as f:
        check("write: the radio holds the new codeplug", f.read(), NEW)
    backup = [f for f in sorted(os.listdir(CPS)) if f.endswith("-before-write.yaml")]
    with open(os.path.join(CPS, backup[-1])) as f:
        check("write: the backup holds the old one", f.read(), CODEPLUG)
    e = page.wait(lambda e: gd(e) and (e.get("codeplug") or {}).get("kind") == "read back after write", since=n)
    check("write: the page shows the read-back", e and e["codeplug"]["channels"][0]["name"], "2m Simplex")

    # verify crashes (dmrconf 0.15.1, openuv380): write's own check decides
    open(os.path.join(FD, "crash"), "w").close()
    page.ready("a write when verify crashes")
    n = len(page.events)
    before = len(calls())                  # before the send: verify may have run by the next line
    page.send(do="codeplug_write", yaml=NEW.replace("2m Simplex", "BAD"), confirm=True)
    check("verify crashes, the offline encode refuses a bad file",
          bool(page.notice("encode refused the codeplug", n, timeout=20)), True)
    check("  before anything was read from or sent to the radio",
          [c[2].split()[0] for c in calls()[before:] if not c[2].startswith("--")], ["verify", "encode"])
    with open(os.path.join(FD, "radio.yaml")) as f:
        check("  and the radio keeps its codeplug", f.read(), NEW)
    page.ready("a good write when verify crashes")
    n = len(page.events)
    page.send(do="codeplug_write", yaml=NEW.replace("2m Simplex", "2m Crash"), confirm=True)
    check("verify crashes, a good file is still written and read back",
          bool(page.notice("3 channels match", n, timeout=20)), True)
    with open(os.path.join(FD, "radio.yaml")) as f:
        check("  the radio holds it", "2m Crash" in f.read(), True)
    os.remove(os.path.join(FD, "crash"))
    with open(os.path.join(FD, "radio.yaml"), "w") as f:
        f.write(NEW)

    # a channel at a power level qdmr can't write back: the read says so, writing is blocked
    fake.power = {2: 7}
    page.ready("a read with a power level qdmr can't keep")
    n = len(page.events)
    page.send(do="codeplug_read")
    msg = page.notice("codeplug read", n)
    check("power level qdmr can't keep: read says writing is blocked",
          bool(msg and "power level" in msg and "blocked" in msg), True)
    page.ready("a write with a power level qdmr can't keep")
    n = len(page.events)
    page.send(do="codeplug_write", yaml=NEW, confirm=True)
    check("  and a write stops before dmrconf write", bool(page.notice("Nothing was sent", n, timeout=20)), True)
    fake.power = {}

    # the radio restarts after a write (OpenGD77 does): the read-back waits for it
    with open(os.path.join(FD, "reboot"), "w") as f:
        f.write("2")
    page.ready("a write the radio restarts after")
    n = len(page.events)
    page.send(do="codeplug_write", yaml=NEW.replace("2m Simplex", "2m Reboot"), confirm=True)
    check("radio restarts after the write: read back once it's back",
          bool(page.notice("3 channels match", n, timeout=40)), True)
    with open(os.path.join(FD, "reboot"), "w") as f:
        f.write("9")
    page.ready("a write the radio never comes back from")
    n = len(page.events)
    page.send(do="codeplug_write", yaml=NEW, confirm=True)
    msg = page.notice("failed", n, timeout=60)
    check("radio never comes back: says it was written, not 'nothing sent'",
          bool(msg and "written, but not read back" in msg and "Nothing was sent" not in msg), True)
    os.remove(os.path.join(FD, "reboot"))
    os.remove(os.path.join(FD, "rebooting"))
    with open(os.path.join(FD, "radio.yaml"), "w") as f:
        f.write(NEW)

    # a read-back that differs
    open(os.path.join(FD, "drop"), "w").close()
    page.ready("a write whose read-back differs")
    n = len(page.events)
    page.send(do="codeplug_write", yaml=CODEPLUG, confirm=True)
    msg = page.notice("read-back differs", n, timeout=20)
    check("a differing read-back is reported", bool(msg), True)
    check("  naming the previous codeplug", bool(msg and "-before-write.yaml" in msg), True)
    os.remove(os.path.join(FD, "drop"))

    # not an MD-UV380/390
    fake.radio_type = 7
    n = len(page.events)
    page.send(do="power", on=False)
    check("(precondition) the add-on switched off", bool(page.wait(lambda e: gd(e) and e.get("on") is False, since=n)),
          True)
    n = len(page.events)
    page.send(do="power", on=True)
    page.ready("a write on an MD-380")
    check("  the radio now says it is an MD-380", (page.wait(lambda e: gd(e) and e.get("info"), since=n) or {})
          .get("info", {}).get("type"), 7)
    n = len(page.events)
    before = len(calls())
    page.send(do="codeplug_write", yaml=NEW, confirm=True)
    check("write on an MD-380: refused", bool(page.notice("not reported itself as an MD-UV380/390", n)), True)
    check("  and dmrconf was not run", calls()[before:], [])
    with fake.lock:
        reads = all(r[:1] == b"R" and len(r) == 8 for r in fake.requests)
    check("our own link sent nothing but reads", reads, True)
    page.close()
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()
    if fails:
        print("server log (last 40 lines):\n" + "\n".join(server_log(40)))
    fake.stop()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
