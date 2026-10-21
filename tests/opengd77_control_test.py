"""Changing the MD-UV390 from the page: opengd77_mem.py, the link's write path
(opengd77.py) and the add-on's apply / refresh / clock / restore, against
tests/fake_opengd77.py (a radio with RAM and flash kept apart, as the real one).

  - memory: mac-shack's real settings bytes decode to what its screen showed;
    every setting is range-checked against the radio's own menus; VFOs,
    tones and the last-used-channel table encode byte for byte
  - the allowlist: only reads, save / restart / clock, and flash writes inside
    the three records ever reach the port (never calibration, never C 6 0,
    never a hotspot frame)
  - a change saves first (so a keypad change since boot survives), backs
    up, writes only what changed, restarts the radio and checks it came back
    on what was written; the radio's screen then shows the new channel
  - refused: a value the radio doesn't offer, an unpinned firmware build;
    a radio that comes back holding something else is reported
  - Restore puts the memory from before the change back

    python3 tests/opengd77_control_test.py
"""
import asyncio
import json
import os
import random
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import opengd77 as g  # noqa: E402
import opengd77_mem as mem  # noqa: E402
from fake_opengd77 import REAL_MEMORY, FakeOpenGD77, channel  # noqa: E402

fails = 0
PINNED = "5797626"


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:66} got {got!r}" + ("" if ok else f" want {want!r}"))


def raises(fn, *a, **k):
    try:
        fn(*a, **k)
    except (ValueError, g.OpenGD77Error) as e:
        return str(e) or True
    return False


# ------------------------------------------------------------------ memory
REAL_SETTINGS = REAL_MEMORY[mem.SETTINGS_ADDR - mem.LUCZ_ADDR:][:mem.SETTINGS_LEN]
REAL_LUCZ = REAL_MEMORY[:mem.LUCZ_LEN]
st = mem.settings(REAL_SETTINGS)
check("real settings: All Channels (zone 4), channel mode, VFO A", (st["zone"], st["mode"], st["vfo"]), (4, "channel", 0))
check("real settings: master power level 6 (its screen said 3W)", (st["power"], mem.POWER_NAMES[st["power"]]), (6, "3W"))
check("real settings: squelch 10/10/10, UTC, TX band limits on",
      (st["squelch_vhf"], st["squelch_220"], st["squelch_uhf"], st["tz_minutes"], st["tz_local"], st["band_limits"]),
      (10, 10, 10, 0, False, 1))
check("real LUCZ: All Channels on channel 1", (mem.lucz(REAL_LUCZ) or {}).get("all"), 1)
check("another build's settings are refused", bool(raises(mem.settings, b"\x7b\x47" + REAL_SETTINGS[2:])), True)
p = mem.patch_settings(REAL_SETTINGS, {"power": 8, "squelch_uhf": 13})
check("patch: only the two bytes change", [i for i in range(len(p)) if p[i] != REAL_SETTINGS[i]], [70, 104])
for bad in ({"power": 10}, {"squelch_vhf": 22}, {"squelch_vhf": 0}, {"backlight_timeout": 7}, {"hotspot": 1},
            {"power": True}, {"zone": 69}, {"tz_minutes": 10}):
    check(f"patch refused: {bad}", bool(raises(mem.patch_settings, REAL_SETTINGS, bad)), True)
p = mem.patch_settings(REAL_SETTINGS, {"tz_minutes": -420, "tz_local": True})
check("timezone: UTC-7, local time shown", (p[12], mem.settings(p)["tz_minutes"]), (0x80 | (64 - 28), -420))
lu = mem.patch_lucz(REAL_LUCZ, zone=2, index=5)
check("LUCZ: zone 2 index 5, All Channels kept", (lu[4 + 2], mem.lucz(lu)["all"]), (5, 1))
lu = mem.patch_lucz(b"\xff" * mem.LUCZ_LEN, all_channel=31)
check("LUCZ: an unset table is started (header, zeros)", (lu[:4], mem.lucz(lu)["all"], lu[5]), (b"LUCZ", 31, 0))
check("tones: 100.0 / 88.5 / D023N / D754I / none",
      [mem.tone_code(t) for t in ("100.0", "88.5", "D023N", "D754I", "")], [0x1000, 0x0885, 0x8023, 0xC754, 0xFFFF])
for bad in ("101.0", "D024N", "D999", "abc"):
    check(f"tone refused: {bad}", bool(raises(mem.tone_code, bad)), True)
v = channel("VFO A", 146_520_000, 146_520_000, wide=True)
nv = mem.patch_vfo(v, {"rx_hz": 145_500_000, "tx_hz": 145_500_000, "rx_tone": "88.5", "tx_tone": "D023N",
                       "mode": "FM", "power": 0, "step": 4, "sql": 0, "wide": False})
check("VFO: written fields read back", {k: mem.vfo(nv)[k] for k in ("rx_hz", "rx_tone", "tx_tone", "wide", "step")},
      {"rx_hz": 145_500_000, "rx_tone": "88.5", "tx_tone": "D023N", "wide": False, "step": 4})
check("VFO: and the link's own decoder agrees", g.channel(nv)["rx_tone"], "88.5")
check("VFO: the name is untouched", nv[:16], v[:16])
nv = mem.patch_vfo(v, {"mode": "DMR", "colour_code": 7, "timeslot": 2})
check("VFO: DMR CC 7 TS 2", (g.channel(nv)["mode"], g.channel(nv)["colour_code"], g.channel(nv)["timeslot"]), ("DMR", 7, 2))
for bad in ({"rx_hz": 300_000_000}, {"rx_hz": 146_520_005}, {"colour_code": 16}, {"timeslot": 3}, {"bogus": 1}):
    check(f"VFO refused: {bad}", bool(raises(mem.patch_vfo, v, bad)), True)
check("spans: the changed runs only", mem.spans(0x100, b"abcdef", b"aXcdYZ"), [(0x101, b"X"), (0x104, b"YZ")])

# ------------------------------------------------------------------ the allowlist
ok_reqs = [b"R\x01\x00\x00\x75\x90\x00\x78", b"C\x06\x01", b"C\x06\x02", b"C\x06\x07\x00\x00\x00\x00",
           b"X\x01\x00\x00\x06", b"X\x01\x00\x00\x07", b"X\x02\x00\x00\x60\x4b\x00\x01\x06", b"X\x03"]
for r_ in ok_reqs:
    check(f"allowed: {r_[:3]!r}…", g.refusal(r_), None)
bad_reqs = {"calibration sector": b"X\x01\x00\x00\x10", "channel bank sector": b"X\x01\x00\x00\x03",
            "write past the settings": b"X\x02\x00\x00\x60\xc0\x00\x01\x00",
            "write straddling the settings end": b"X\x02\x00\x00\x60\xbe\x00\x02\x00\x00",
            "oversized chunk": b"X\x02\x00\x00\x60\x4b\x00\x31" + b"\x00" * 49,
            "length mismatch": b"X\x02\x00\x00\x60\x4b\x00\x02\x00",
            "save-and-reboot C 6 0": b"C\x06\x00", "CPS screen C 0": b"C\x00\x00", "LED C 6 3": b"C\x06\x03",
            "EEPROM X 4 (a no-op here)": b"X\x04\x00\x00\x60\x4b\x00\x01\x00", "fake write W": b"W\x00",
            "debug D": b"DR", "hotspot frame": b"\xe0\x03\x01", "clock without time": b"C\x06\x07"}
for name, r_ in bad_reqs.items():
    check(f"refused: {name}", bool(g.refusal(r_)), True)

# ------------------------------------------------------------------ the link against the fake radio
fake = FakeOpenGD77(revision=PINNED).start()
fake.restart_s = 0.3
r = g.OpenGD77(fake.path)
r.open()
fake.keypad(settings={102: 15})                     # squelch VHF changed at the keypad (RAM only)
m0 = r.memory()
check("flash still holds the old squelch before a save", mem.settings(m0["settings"])["squelch_vhf"], 10)
r.save()
check("after C 6 2 flash holds the keypad's squelch", mem.settings(r.memory()["settings"])["squelch_vhf"], 15)
new = mem.patch_settings(r.memory()["settings"], {"power": 8})
sp = mem.spans(mem.SETTINGS_ADDR, r.memory()["settings"], new)
r.write_flash(sp)
check("write_flash: flash holds the change, one sector written", (fake.flash_settings()[70], fake.flash_writes),
      (8, [0x6000]))
check("write_flash: nothing else in the sector moved",
      bytes(fake.flash[0x6000 + 0xC0:0x7000]) == bytes(b"\xff" * (0x1000 - 0xC0)), True)
check("write_flash refuses a span outside the records",
      bool(raises(r.write_flash, [(0x10000, b"\x00")])), True)
fake.refuse_x = True
check("write_flash: a refusing radio raises", bool(raises(r.write_flash, sp)), True)
fake.refuse_x = False
secs = r.sectors()
fake.clobber = 0x6800                             # the firmware's own write lands in the shared sector buffer
why = raises(r.write_flash, mem.spans(mem.SETTINGS_ADDR, r.memory()["settings"],
                                      mem.patch_settings(r.memory()["settings"], {"eco": 2})), before=secs)
check("write_flash: a sector changed outside the write is caught", "changed outside the write" in str(why), True)
fake.clobber = None
fake.flash[0x6800] = secs[0x6000][0x800]
r.set_clock(1_792_500_000)
check("clock: C 6 7 with UTC seconds", fake.clock, 1_792_500_000)
r.restart()
r.close()
time.sleep(1.2)
r.open()
check("restart: the radio runs on the written flash", fake.ram["settings"][70], 8)
reqs = list(fake.requests)
check("link: only allowlisted requests went out", [x[:3] for x in reqs if g.refusal(x)], [])
r.close()
fake.stop()

# ------------------------------------------------------------------ the add-on
WEB = 19000 + random.randrange(1000)
TMP = tempfile.mkdtemp(prefix="opengd77-control-")
STATE = os.path.join(TMP, "addons.json")
fake = FakeOpenGD77(revision=PINNED).start()
fake.slots = [1, 2, 3, 4]
fake.names = {1: "2m Call", 2: "Rpt 147.000", 3: "BM 3104", 4: "GMRS 1"}
fake.zones = [{"name": "Home", "slots": [1, 2, 3]}, {"name": "GMRS", "slots": [4]}]
fake.auto_screen = True
fake.screen = fake.state_screen()
CFG = os.path.join(TMP, "addons-cfg.json")
CODEPLUGS = os.path.join(TMP, "codeplugs")
with open(CFG, "w") as f:
    json.dump({"opengd77": {"port": fake.path, "label": "MD-UV390", "poll": 0.5, "codeplugs": CODEPLUGS,
                            "restart_wait": 6}}, f)


class Page:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.events = []
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._open(), self.loop).result(10)

    async def _open(self):
        self.sess = aiohttp.ClientSession()
        self.ws = await self.sess.ws_connect(f"http://127.0.0.1:{WEB}/ws", max_msg_size=0)
        self.hello = json.loads((await self.ws.receive()).data)
        asyncio.ensure_future(self._read())

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                self.events.append(json.loads(msg.data))

    def do(self, verb, **kw):
        n = len(self.events)
        asyncio.run_coroutine_threadsafe(
            self.ws.send_str(json.dumps({"t": "addon", "addon": "opengd77", "do": verb, **kw})), self.loop).result(5)
        return n

    def wait(self, pred, timeout=15, since=0):
        end = time.time() + timeout
        while time.time() < end:
            for e in self.events[since:]:
                if pred(e):
                    return e
            time.sleep(0.05)
        return None

    def notice(self, since, timeout=20):
        e = self.wait(lambda e: e.get("t") == "notice" and ("MD-UV390:" in e.get("msg", "") or
                                                             e.get("msg", "").startswith(("Rejected", "The add-on", "MD-UV390 ")))
                      and "busy" not in e.get("msg", ""), timeout, since)
        return e and e["msg"]


def gd(e):
    return e.get("t") == "addon" and e.get("addon") == "opengd77"


server = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB),
                           "--rigctl-port", "0", "--addons", CFG, "--addons-state", STATE],
                          cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", WEB), timeout=0.2).close()
        break
    except OSError:
        time.sleep(0.1)
try:
    page = Page()
    c = page.wait(lambda e: gd(e) and (e.get("control") or {}).get("settings"))
    check("connect: the radio's stored settings reach the page",
          c and (c["control"]["settings"]["zone"], c["control"]["saved"], c["control"]["why_not"]), (0, False, None))
    check("connect: and its own zones for the channel picker",
          [z["name"] for z in ((page.wait(lambda e: gd(e) and e.get("layout")) or {}).get("layout") or {}).get("zones", [])],
          ["Home", "GMRS"])
    check("connect: the watch wrote nothing (no save, no flash write)", (fake.saves, fake.flash_writes), (0, []))
    page.wait(lambda e: gd(e) and (e.get("now") or {}).get("channel") == "2m Call")

    fake.keypad(settings={104: 14})                   # UHF squelch changed at the keypad since boot
    n = page.do("apply", goto={"zone": 0, "index": 2})
    msg = page.notice(n)
    check("go to Home Ch 3: done, the radio restarted", bool(msg and "done" in msg and "BM 3104" in msg), True)
    check("go to: the radio runs on zone 0, channel index 2, channel mode",
          (struct.unpack_from("<h", fake.ram["settings"], 58)[0], fake.ram["lucz"][4], fake.ram["settings"][81]),
          (0, 2, 34))
    check("go to: the keypad's squelch survived (saved before patching)", fake.ram["settings"][104], 14)
    now = page.wait(lambda e: gd(e) and (e.get("now") or {}).get("channel") == "BM 3104", timeout=10)
    check("go to: the radio's screen then shows BM 3104", bool(now), True)
    backups = os.listdir(os.path.join(CODEPLUGS, "memory"))
    check("a memory backup was kept before the change", len(backups), 1)

    n = page.do("apply", goto={"zone": "all", "index": 4})
    msg = page.notice(n)
    check("go to All Channels channel 4", bool(msg and "done" in msg), True)
    check("All Channels: zone 2 (= number of zones), LUCZ 4, Home kept on index 2",
          (struct.unpack_from("<h", fake.ram["settings"], 58)[0], mem.lucz(fake.ram["lucz"])["all"], fake.ram["lucz"][4]),
          (2, 4, 2))

    n = page.do("apply", vfos={"0": {"rx_hz": 145_500_000, "tx_hz": 145_500_000, "rx_tone": "88.5",
                                    "tx_tone": "88.5", "mode": "FM"}}, settings={"mode": "vfo", "vfo": 0, "power": 8})
    msg = page.notice(n)
    check("VFO A 145.500 FM 88.5, VFO mode, master 5W: done", bool(msg and "done" in msg), True)
    check("VFO A as the radio runs it", (g.channel(fake.vfo[0])["rx_hz"], g.channel(fake.vfo[0])["tx_tone"]),
          (145_500_000, "88.5"))
    check("VFO mode, power level 8", (fake.ram["settings"][81], fake.ram["settings"][70]), (33, 8))
    v = page.wait(lambda e: gd(e) and e.get("vfos") and e["vfos"][0]["rx_hz"] == 145_500_000, timeout=8)
    check("the page's live VFO A follows", bool(v), True)

    writes, saves = len(fake.flash_writes), fake.saves
    n = page.do("apply", settings={"power": 12})
    msg = page.notice(n)
    check("refused: a power level the radio doesn't have", bool(msg and "not a value the radio offers" in msg), True)
    check("refused: nothing was written", len(fake.flash_writes), writes)
    n = page.do("apply", goto={"zone": 0, "index": 9})
    check("refused: a channel the zone doesn't have", "has no channel" in (page.notice(n) or ""), True)
    n = page.do("apply", settings={"power": 8})
    check("no change needed: says so, no restart", "nothing to change" in (page.notice(n) or ""), True)
    check("no change needed: no flash write", len(fake.flash_writes), writes)

    fake.keypad(settings={105: 3})
    n = page.do("memory_refresh")
    page.notice(n)
    c = page.wait(lambda e: gd(e) and (e.get("control") or {}).get("saved") and
                  e["control"]["settings"]["eco"] == 3, since=n)
    check("Refresh: saves first, so the keypad's eco level shows", bool(c), True)

    n = page.do("clock_sync")
    msg = page.notice(n)
    check("Set clock: C 6 7 with now (UTC)", bool(fake.clock and abs(fake.clock - time.time()) < 5), True)

    def reject(f):                                   # the radio resets VFO B on boot (sanity check)
        f.flash[0x7590 + 56 + 16:0x7590 + 56 + 24] = b"\x00\x00\x60\x13\x00\x00\x60\x13"
        f.on_restart = None
    fake.on_restart = reject
    n = page.do("apply", vfos={"1": {"rx_hz": 446_100_000, "tx_hz": 446_100_000}})
    msg = page.notice(n)
    check("a radio that comes back on something else is reported", bool(msg and "holds something else" in msg), True)

    before = sorted(os.listdir(os.path.join(CODEPLUGS, "memory")))
    n = page.do("memory_restore", confirm=True, file=before[0])
    msg = page.notice(n)
    check("Restore the first backup: done", bool(msg and "restored" in msg), True)
    check("Restore: Home Ch 1 again, channel mode, power 6, squelch as then",
          (struct.unpack_from("<h", fake.ram["settings"], 58)[0], fake.ram["settings"][81], fake.ram["settings"][70],
           fake.ram["settings"][104]), (0, 34, 6, 14))
    n = page.do("memory_restore")
    check("Restore needs confirm", "confirm" in (page.notice(n) or ""), True)
    with fake.lock:
        reqs = list(fake.requests)
    check("add-on: only allowlisted requests ever went out", [x[:3] for x in reqs if g.refusal(x)], [])
finally:
    server.terminate()
    try:
        out = server.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired:
        server.kill()
        out = server.communicate()[0]
    fake.stop()

# an unpinned build: everything still reads, nothing is changed
fake = FakeOpenGD77(revision="abc1234").start()
with open(CFG, "w") as f:
    json.dump({"opengd77": {"port": fake.path, "label": "MD-UV390", "poll": 0.5, "codeplugs": CODEPLUGS}}, f)
WEB += 1
server = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB),
                           "--rigctl-port", "0", "--addons", CFG, "--addons-state", STATE],
                          cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", WEB), timeout=0.2).close()
        break
    except OSError:
        time.sleep(0.1)
try:
    page = Page()
    c = page.wait(lambda e: gd(e) and (e.get("control") or {}).get("why_not"))
    check("another build: the page is told why changing is off", bool(c and "not the build" in c["control"]["why_not"]),
          True)
    n = page.do("apply", settings={"power": 7})
    check("another build: a change is refused", "not the build" in (page.notice(n) or ""), True)
    check("another build: nothing but reads went out", all(x[:1] == b"R" for x in fake.requests), True)
finally:
    server.terminate()
    try:
        server.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
    fake.stop()

if fails:
    print("\n".join(l for l in out.splitlines() if "civ INFO" not in l)[-6000:])
print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
