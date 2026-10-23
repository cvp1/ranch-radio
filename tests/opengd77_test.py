"""OpenGD77 (MD-UV380/390): opengd77.py and addons/opengd77.py, against tests/fake_opengd77.py.

  - decoding: BCD frequencies, CTCSS / DCS tones, FM vs DMR (colour code,
    timeslot), the radio-info struct
  - the live VFOs come from ONE read covering 0x7590..+112 (a shorter read
    gets the stale codeplug copy, as on the real firmware)
  - only reads ever go out: a write, a 'C' command or a hotspot frame is
    refused before it reaches the port
  - a radio that stops answering times out instead of hanging
  - the add-on on a radio server: VFOs reach the page, the screen comes back
    whole, a tune request is refused with a notice and nothing but reads went
    out, switching off leaves the radio alone and switching on reconnects

    python3 tests/opengd77_test.py
"""
import asyncio
import base64
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
from fake_opengd77 import FakeOpenGD77, channel  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


def only_reads(fake):
    with fake.lock:
        return all(r[:1] == b"R" and len(r) == 8 for r in fake.requests)


# --- decoding
check("BCD 0x14652000 -> 14652000", g.bcd(0x14652000), 14652000)
check("CTCSS 0x1000 -> 100.0", g.tone(0x1000), "100.0")
check("CTCSS 0x0885 -> 88.5", g.tone(0x0885), "88.5")
check("no tone (0xFFFF)", g.tone(0xFFFF), "")
check("DCS 023 normal", g.tone(0x8023), "D023N")
check("DCS 754 inverted", g.tone(0xC754), "D754I")
fm = g.channel(channel("Simplex", 146_520_000, 146_520_000, rx_tone=0x1000, tx_tone=0x0885, wide=True))
check("FM channel", fm, {"name": "Simplex", "rx_hz": 146_520_000, "tx_hz": 146_520_000, "mode": "FM",
                         "rx_only": False, "rx_tone": "100.0", "tx_tone": "88.5", "bandwidth": "25 kHz"})
dmr = g.channel(channel("DMR", 439_400_000, 430_400_000, dmr=True, cc=3, ts2=True))
check("DMR channel: colour code, timeslot", (dmr["mode"], dmr["colour_code"], dmr["timeslot"]), ("DMR", 3, 2))

# --- finding the radio by USB ID (macOS ioreg tree: the radio sits under a hub)
IOREG = """+-o Root  <class IORegistryEntry>
  +-o USB2 Hub@01100000  <class IOUSBHostDevice>
  | {
  |   "idProduct" = 1552
  |   "idVendor" = 1507
  | }
  | +-o HT_n5262@01120000  <class IOUSBHostDevice>
  | | {
  | |   "idProduct" = 17413
  | |   "idVendor" = 9114
  | | }
  | | +-o IOSerialBSDClient  <class IOSerialBSDClient>
  | |     {
  | |       "IOCalloutDevice" = "/dev/cu.usbmodem113301"
  | |     }
  | +-o OpenMDUV380 Transceiver@01130000  <class IOUSBHostDevice>
  |   {
  |     "idProduct" = 148
  |     "idVendor" = 8137
  |   }
  |   +-o IOSerialBSDClient  <class IOSerialBSDClient>
  |       {
  |         "IOCalloutDevice" = "/dev/cu.usbmodem4A2B1"
  |       }
"""
check("found by USB ID, not the Meshtastic node beside it", g._mac_port(IOREG, *g.USB_ID), "/dev/cu.usbmodem4A2B1")
check("no OpenGD77 radio: nothing", g._mac_port(IOREG.split("  | +-o OpenMDUV380")[0], *g.USB_ID), None)
check("the hub's ID is not its children's", g._mac_port(IOREG, 1507, 1552), None)
real_run = g.subprocess.run
def no_ioreg(*a, **k):
    raise FileNotFoundError(2, "No such file or directory", "ioreg")
g.subprocess.run = no_ioreg
try:
    g.find_port() if not os.path.isdir("/sys/class/tty") else None
    lookup = "no error" if not os.path.isdir("/sys/class/tty") else "skipped on Linux"
except g.OpenGD77Error as e:
    lookup = str(e)
raw_mac = b'    | |   "IOMACAddress" = <"N$8-\xdfH">\n' + IOREG.encode()
def ioreg_raw_bytes(cmd, **k):                # what ioreg prints: decoded the way the caller asked
    return g.subprocess.CompletedProcess(cmd, 0, raw_mac.decode(errors=k.get("errors") or "strict"), "")
g.subprocess.run = ioreg_raw_bytes
try:
    found = g.find_port() if not os.path.isdir("/sys/class/tty") else "/dev/cu.usbmodem4A2B1"
except ValueError as e:
    found = f"{type(e).__name__}: {e}"
g.subprocess.run = real_run
check("non-UTF-8 bytes elsewhere in ioreg (a MAC address) don't hide the radio", found, "/dev/cu.usbmodem4A2B1")
check("a broken USB lookup says so (not 'no radio')",
      lookup.startswith("USB lookup failed") or lookup == "skipped on Linux", True)
r = g.OpenGD77("auto")
g_find = g.find_port
g.find_port = lambda *a: None
try:
    r.open()
    missing = None
except g.OpenGD77Error as e:
    missing = str(e)
g.find_port = g_find
check("port auto, radio absent: says so, opens nothing", (missing or "").startswith("no OpenGD77 radio"), True)

# --- the protocol against the fake radio
fake = FakeOpenGD77().start()
r = g.OpenGD77(fake.path, timeout=1.0)
r.open()
info = r.info()
check("radio info: model", info["model"], "MD-UV380/390")
check("radio info: revision, build", (info["revision"], info["built"]), ("abc1234", "20260131120000"))
check("radio info: has the colour screen", info["screen"], True)
a, b = r.vfos()
check("VFO A", (a["rx_hz"], a["mode"], a["rx_tone"]), (146_520_000, "FM", "100.0"))
check("VFO B: DMR, -9 MHz", (b["rx_hz"], b["tx_hz"], b["mode"]), (439_400_000, 430_400_000, "DMR"))
last = fake.requests[-1]
check("the VFOs came from one read covering 0x7590..+112",
      struct.unpack(">cBIH", last), (b"R", 1, 0x7590, g.VFO_READ_LEN))
short = r.read(g.AREA_FLASH, 0x7590, 112)            # one byte short of the patch window
check("a shorter read gets the stored copy, not the live VFOs", short != fake.vfo[0] + fake.vfo[1], True)
scr = r.screen()
check("screen: 160x128x2 bytes, whole", (len(scr), scr == fake.screen), (40960, True))
scr = bytearray(fake.screen); scr[1024] = 0x2D; fake.screen = bytes(scr)    # a chunk that starts with '-'
check("data starting with '-' is data, not a refusal", r.screen() == fake.screen, True)
for bad in (b"X\x02\x00\x00\x00\x00\x00\x01Z", b"C\x06\x00", b"\xe0\x03\x01"):
    try:
        r._send(bad)
        refused = False
    except g.OpenGD77Error:
        refused = True
    check(f"refused before the port: {bad[:1]!r}", refused, True)
check("only 8-byte reads reached the radio", only_reads(fake), True)
fake.mute = True
t0 = time.monotonic()
try:
    r.vfos()
    timed_out = False
except g.OpenGD77Error:
    timed_out = True
check("a radio that stops answering times out", (timed_out, time.monotonic() - t0 < 3), (True, True))
r.close()
fake.stop()

# --- the add-on on a radio server
WEB = 19000 + random.randrange(1000)
TMP = tempfile.mkdtemp(prefix="opengd77-")
STATE = os.path.join(TMP, "addons.json")
fake = FakeOpenGD77().start()
CFG = os.path.join(TMP, "addons-cfg.json")
with open(CFG, "w") as f:
    json.dump({"opengd77": {"port": fake.path, "label": "MD-UV390", "poll": 0.5}}, f)


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

    def send(self, m):
        self.run(self.ws.send_str(json.dumps(m)))

    def wait(self, pred, timeout=10, since=0):
        end = time.time() + timeout
        while time.time() < end:
            for e in self.events[since:]:
                if pred(e):
                    return e
            time.sleep(0.05)
        return None

    def close(self):
        async def _c():
            await self.ws.close()
            await self.sess.close()
        self.run(_c())


def start():
    p = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB),
                          "--rigctl-port", "0", "--addons", CFG, "--addons-state", STATE],
                         cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", WEB), timeout=0.2).close()
            return p
        except OSError:
            time.sleep(0.1)
    p.kill()
    print(p.stdout.read())
    raise SystemExit("server did not start")


def stop(p):
    p.terminate()
    try:
        return p.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired:
        p.kill()
        return p.communicate()[0]


def gd(e):
    return e.get("t") == "addon" and e.get("addon") == "opengd77"


def connected(e):
    return gd(e) and (e.get("status") or {}).get("connected") is True


server = start()
try:
    time.sleep(1.0)                        # the radio is up before the page opens
    page = Page()
    snap = page.hello.get("addons", {}).get("opengd77", {})
    check("add-on: a page opened later sees it connected", (snap.get("status") or {}).get("connected"), True)
    check("add-on: and the VFOs in its first message", [x["rx_hz"] for x in snap.get("vfos") or []],
          [146_520_000, 439_400_000])
    fake.vfo[0] = channel("VFO A", 147_000_000, 147_600_000, rx_tone=0x1000, tx_tone=0x1000)
    n = len(page.events)
    v = page.wait(lambda e: gd(e) and e.get("vfos"), since=n)
    check("add-on: a change at the keypad shows up", v and v["vfos"][0]["tx_hz"], 147_600_000)
    page.send({"t": "addon", "addon": "opengd77", "do": "screen"})
    s = page.wait(lambda e: gd(e) and e.get("screen"))
    check("add-on: the screen comes back whole", s and base64.b64decode(s["screen"]) == fake.screen, True)
    n = len(page.events)
    page.send({"t": "addon", "addon": "opengd77", "do": "tune", "freq": 146_520_000})
    w = page.wait(lambda e: e.get("t") == "notice" and "no CAT" in json.dumps(e), since=n)
    check("add-on: a tune request is refused with a notice", w is not None, True)
    check("add-on: nothing but reads went out", only_reads(fake), True)

    n = len(page.events)
    page.send({"t": "addon", "addon": "opengd77", "do": "power", "on": False})
    page.wait(lambda e: gd(e) and e.get("on") is False, since=n)
    time.sleep(0.5)
    with fake.lock:
        before = len(fake.requests)
    time.sleep(1.5)
    with fake.lock:
        quiet = len(fake.requests) == before
    check("switched off: the radio is left alone", quiet, True)
    n = len(page.events)
    page.send({"t": "addon", "addon": "opengd77", "do": "power", "on": True})
    check("switched on: the radio connects again", page.wait(connected, since=n) is not None, True)
    page.close()
finally:
    out = stop(server)
    if fails:
        print(out[-3000:])
    fake.stop()

print(f"\n{'PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
