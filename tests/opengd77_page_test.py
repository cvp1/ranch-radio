"""The OpenGD77 add-on on the page (headless Chromium), against tests/fake_opengd77.py.

  - the add-on gets a tab; its panel says Connected and names the radio
  - Radio: the front panel shows the channel read off the radio's screen
    (zone, number, name, talkgroup, frequencies); the VFO editor holds VFO A
    as the radio does; a change at the keypad shows up; the screen draws
    pixel for pixel
  - clicking a channel puts the radio on it (written, restarted) and the
    page follows; tuning VFO A puts the radio in VFO mode on it
  - Settings: a changed setting is applied after a confirm, and only it
  - Codeplug (fake dmrconf): Read fills the channel table; a renamed channel
    is not written when the confirm is declined, and is written and read
    back when it is confirmed; the YAML view shows the radio's file
  - our link sent nothing outside the allowlist, and nothing but reads
    before the first change

    CHROME=/path/to/chromium python3 tests/opengd77_page_test.py
"""
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from cdp_browser import Browser  # noqa: E402
from fake_opengd77 import FakeOpenGD77, channel  # noqa: E402
sys.path.insert(0, ROOT)
import opengd77 as g  # noqa: E402

MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME = (os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
          or (MAC_CHROME if os.path.exists(MAC_CHROME) else None))
WEB = 22000 + random.randrange(1000)
CDP = WEB + 1000
TMP = tempfile.mkdtemp(prefix="opengd77-page-")
P = ".addon-opengd77"
TEXT = "(s) => (document.querySelector(s)?.textContent || '')"
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")

FD = os.path.join(TMP, "dmrconf")
os.makedirs(FD)
CODEPLUG = """channels:
  - fm:
      id: ch1
      name: 2m Call
      rxFrequency: 146.52 MHz
      txFrequency: 146.52 MHz
      txTone: {ctcss: 100}
      power: High
  - dmr:
      id: ch2
      name: BM 3104
      rxFrequency: 439.4 MHz
      txFrequency: 430.4 MHz
      colorCode: 1
      timeSlot: TS2
      power: High
"""
with open(os.path.join(FD, "radio.yaml"), "w") as f:
    f.write(CODEPLUG)
fake = FakeOpenGD77(revision="5797626").start()
fake.slots, fake.names, fake.zones = [1, 2], {1: "2m Call", 2: "BM 3104"}, []   # what CODEPLUG holds
fake.records = {1: channel("2m Call", 146_520_000, 146_520_000, tx_tone=0x1000, wide=True),
                2: channel("BM 3104", 439_400_000, 430_400_000, dmr=True, cc=1, ts2=True)}
fake.layout_file = os.path.join(FD, "layout.json")
# The radio in channel mode on "BM 3104" (slot 2, All Channels: zone 0 of none), and a known
# pixel: (0,0) pure red in RGB565, big-endian.
import struct  # noqa: E402
with fake.lock:
    st = bytearray(fake.ram["settings"]); struct.pack_into("<h", st, 58, 0); fake.ram["settings"] = bytes(st)
    lu = bytearray(fake.ram["lucz"]); struct.pack_into("<h", lu, 4 + 68, 1); fake.ram["lucz"] = bytes(lu)
fake.flash[0x604B:0x604B + 116] = fake.ram["settings"]
fake.flash[0x6000:0x6000 + 74] = fake.ram["lucz"]
fake.screen = fake.state_screen()
scr = bytearray(fake.screen)
scr[0:2] = b"\xf8\x00"
fake.screen = bytes(scr)
fake.auto_screen = True
fake.restart_s = 0.4
cfg = os.path.join(TMP, "addons.json")
with open(cfg, "w") as f:
    json.dump({"opengd77": {"port": fake.path, "label": "MD-UV390", "poll": 0.5,
                            "dmrconf": os.path.join(ROOT, "tests/fake_dmrconf.py"),
                            "codeplugs": os.path.join(TMP, "codeplugs"), "restart_wait": 8}}, f)
server = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB), "--rigctl-port", "0",
                           "--addons", cfg, "--addons-state", os.path.join(TMP, "state.json")],
                          cwd=ROOT, env=dict(os.environ, FAKE_DMRCONF_DIR=FD), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", WEB), timeout=0.2).close()
        break
    except OSError:
        time.sleep(0.1)
b = Browser(CHROME, CDP)
Q = lambda sel: f"document.querySelector('{P} {sel}')"  # noqa: E731
CLICK = lambda sel: b.js(f"{Q(sel)}.click()")  # noqa: E731
try:
    b.goto(f"http://127.0.0.1:{WEB}/#/opengd77")
    b.js("window.__errs = []; window.addEventListener('error', (e) => window.__errs.push(e.message))")
    check("tab bar has MD-UV390", bool(b.wait("[...document.querySelectorAll('#rr-nav .st-tab')]"
                                              ".some((t) => t.textContent.includes('MD-UV390'))")), True)
    check("panel: Connected", bool(b.wait(f"({TEXT})('{P} .gd-conn') === 'Connected'")), True)
    check("panel: names the radio", "MD-UV380/390" in (b.wait(f"({TEXT})('{P} .gd-info')") or ""), True)
    check("front panel: the channel read off the radio's screen",
          b.wait(f"({TEXT})('{P} .gd-big') === 'BM 3104' && [({TEXT})('{P} .gd-zone-line'), ({TEXT})('{P} .gd-big')]"),
          ["All Channels · CH 2", "BM 3104"])
    check("front panel: RX / TX and the DMR badges",
          b.wait(f"({TEXT})('{P} .gd-front').includes('439.40000') && ({TEXT})('{P} .gd-front').includes('430.40000')"
                 f" && [...document.querySelectorAll('{P} .gd-front .badge')].map((x) => x.textContent)"),
          ["DMR", "CC 1", "TS 2", "3W"])
    check("the tab names the channel", bool(b.wait("[...document.querySelectorAll('#rr-nav .st-tab')]"
                                                   ".some((t) => (t.title || '').includes('BM 3104'))")), True)
    check("channel grid: both channels, BM 3104 lit",
          b.wait(f"document.querySelectorAll('{P} .gd-ch').length === 2 && "
                 f"[...document.querySelectorAll('{P} .gd-ch')].map((x) => [x.querySelector('b').textContent, x.classList.contains('cur')])"),
          [["2m Call", False], ["BM 3104", True]])
    check("VFO editor: VFO A as the radio holds it",
          b.wait(f"{Q('.gd-rx')}?.value === '146.52000' && [{Q('.gd-rx')}.value, {Q('.gd-rxt')}.value]"),
          ["146.52000", "100.0"])
    fake.vfo[0] = channel("VFO A", 145_230_000, 144_630_000, rx_tone=0x1000, tx_tone=0x1000, wide=True)
    check("a change at the keypad shows up", b.wait(f"{Q('.gd-tx')}.value === '144.63000' && {Q('.gd-rx')}.value"),
          "145.23000")
    CLICK(".gd-grab")
    b.wait(f"({TEXT})('{P} .gd-scr-at') !== ''")
    px = b.js(f"Array.from({Q('.gd-screen')}.getContext('2d').getImageData(0, 0, 1, 1).data)")
    check("Screen: pixel (0,0) is the radio's red", px, [248, 0, 0, 255])
    with fake.lock:
        watched = list(fake.requests)
    check("watching sent nothing but reads", all(r[:1] == b"R" and len(r) == 8 for r in watched), True)

    # go to a channel
    b.wait(f"!{Q('.gd-ch')}.disabled")
    b.js(f"[...document.querySelectorAll('{P} .gd-ch')][0].click()")
    check("click 2m Call: the page shows the radio busy restarting",
          bool(b.wait(f"document.querySelector('{P} .gd').classList.contains('busy')", timeout=5)), True)
    check("then on 2m Call (the radio restarted on it)",
          b.wait(f"({TEXT})('{P} .gd-big') === '2m Call' && ({TEXT})('{P} .gd-zone-line')", timeout=15), "All Channels · CH 1")
    check("the result line says done", "done" in (b.wait(f"({TEXT})('{P} .gd-last')") or ""), True)
    check("the radio runs on channel 1", struct.unpack_from("<h", fake.ram["lucz"], 4 + 68)[0] + 1, 1)

    # tune VFO A
    b.wait(f"!{Q('.gd-v-tune')}.disabled", timeout=10)
    b.js(f"(() => {{ const i = {Q('.gd-rx')}; i.value = '145.500'; i.dispatchEvent(new Event('change')); }})()")
    b.js(f"(() => {{ const s = {Q('.gd-txt')}; s.value = '88.5'; s.dispatchEvent(new Event('change')); }})()")
    check("VFO edits are marked (TX keeps its split: it wasn't simplex)",
          b.js(f"[...document.querySelectorAll('{P} .gd-vfo label.changed')].map((x) => x.textContent)"), ["RX MHz", "TX tone"])
    CLICK(".gd-v-tune")
    check("Tune VFO A: the front panel shows VFO A on 145.50000",
          b.wait(f"({TEXT})('{P} .gd-zone-line') === 'VFO A' && ({TEXT})('{P} .gd-big')", timeout=15), "145.50000MHz")
    check("the radio runs VFO A 145.500 with TX tone 88.5, in VFO mode",
          (g.channel(fake.vfo[0])["rx_hz"], g.channel(fake.vfo[0])["tx_tone"], fake.ram["settings"][81]), (145_500_000, "88.5", 33))

    # settings
    CLICK("[data-tab=settings]")
    b.wait(f"{Q('[data-set=power]')}")
    b.wait(f"!document.querySelector('{P} .gd').classList.contains('busy')", timeout=10)
    b.js(f"(() => {{ const s = {Q('[data-set=power]')}; s.value = '8'; s.dispatchEvent(new Event('change')); }})()")
    check("Settings: one change counted", b.wait(f"({TEXT})('{P} .gd-sticky b')"), "1 change")
    b.js("window.confirm = (q) => { window.asked = q; return true; }")
    CLICK(".gd-s-apply")
    check("Settings: Apply asks, naming the setting", "Master power" in (b.wait("window.asked") or ""), True)
    check("Settings: the radio runs at power level 8 (5W)",
          bool(b.wait(f"({TEXT})('{P} .gd-last').includes('Master power') && ({TEXT})('{P} .gd-last').includes('done')", timeout=15)
               and fake.ram["settings"][70] == 8), True)

    # codeplug
    cp = f"[...document.querySelectorAll('{P} .gd-tbl tbody tr')].map((r) => [...r.children].slice(1, 7).map((c) => c.textContent.trim()))"
    CLICK("[data-tab=codeplug]")
    b.wait(f"!{Q('.gd-cp-read')}.disabled", timeout=10)
    CLICK(".gd-cp-read")
    check("Codeplug: Read fills the table", b.wait(f"{cp}.length === 2 && {cp}", timeout=20),
          [["2m Call", "146.5200", "simplex", "FM", "T 100.0", "High"],
           ["BM 3104", "439.4000", "−9.000", "DMR", "CC1 TS2", "High"]])
    b.js(f"(() => {{ const i = {Q('.gd-edit [data-f=name]')}; i.value = '2m Simplex'; i.dispatchEvent(new Event('change')); }})()")
    check("an edit marks the codeplug edited", "edited, not written" in (b.wait(f"({TEXT})('{P} .gd-cp-sum')") or ""), True)
    b.js("window.confirm = () => false")
    CLICK(".gd-cp-write")
    time.sleep(1.5)
    with open(os.path.join(FD, "radio.yaml")) as f:
        check("Write, declined: the radio keeps its codeplug", f.read(), CODEPLUG)
    b.js("window.confirm = (q) => { window.asked = q; return true; }")
    CLICK(".gd-cp-write")
    check("Write asks first, saying it changes what transmits",
          "changes what the radio transmits" in (b.wait("window.asked") or ""), True)
    check("Write: the table shows the read-back",
          b.wait(f"({TEXT})('{P} .gd-cp-sum').includes('read back') && {cp}[0][0]", timeout=25), "2m Simplex")
    with open(os.path.join(FD, "radio.yaml")) as f:
        held = f.read()
    check("Write: the radio holds the renamed channel, the rest as it was",
          ("name: 2m Simplex" in held, "BM 3104" in held, "439.4 MHz" in held), (True, True, True))
    CLICK(".gd-cp-yaml")
    check("YAML: the radio's file in the editor", "2m Simplex" in (b.wait(f"{Q('.gd-yaml')}.value", timeout=10) or ""), True)
    check("no page errors", b.js("window.__errs"), [])
    with fake.lock:
        reqs = list(fake.requests)
    check("our own link sent nothing outside the allowlist", [r[:3] for r in reqs if g.refusal(r)], [])
finally:
    b.close()
    server.terminate()
    out = server.communicate(timeout=10)[0]
    if fails:
        print(out[-3000:])
    fake.stop()

print(f"\n{'PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
