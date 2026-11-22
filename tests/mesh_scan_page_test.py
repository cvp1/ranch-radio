"""The Mesh tab's "Find radios" (addons/mesh.py "mesh_scan", mesh_scan.py),
driven in headless Chromium against server.py --sim, tests/fake_mesh_bridge.py
and tests/fake_mesh_scan.py in place of the real scan.

  - a "Find radios" button beside Settings; it opens a view in place of the
    conversation, and looks at once the first time
  - Bluetooth: each radio with its signal and the port the host file takes;
    USB: each serial device with its maker and device path, a board with a
    serial number as usb:SERIAL
  - a radio this station already uses says whose it is, matched in a
    comma-separated port list by usb:SERIAL, device path or ble:NAME
  - a name the host file would refuse is shown without a port
  - the notes say what is not listed and how to add one
  - "Look again" runs the scan again; a second request while one runs does
    not start another
  - Health and Settings close the view; the button opens it again without
    a new scan
  - Bluetooth off: said, and USB still listed; an answer that is not JSON,
    or none in time: said
  - a name from the air is text on the page, never markup

    python3 tests/mesh_scan_page_test.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from cdp_browser import Browser  # noqa: E402
import testkit  # noqa: E402

WEB = testkit.free_port()
CDP = testkit.free_port()
CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
TMP = tempfile.mkdtemp(prefix="meshscanpage-")
MODE = os.path.join(TMP, "mode")
RUNS = os.path.join(TMP, "runs")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


def mode(m=""):
    with open(MODE, "w") as f:
        f.write(m)


def runs():
    try:
        return len(open(RUNS).read().split())
    except OSError:
        return 0


mode()
adds = os.path.join(TMP, "addons-cfg.json")
with open(adds, "w") as f:
    json.dump({"mesh": {"port": "/dev/null", "label": "MF18", "python": sys.executable, "log": "",
                        "bridge": "tests/fake_mesh_bridge.py", "scan": "tests/fake_mesh_scan.py",
                        "radios": [{"label": "LF", "port": "usb:1a2b3c4d5e6f7a8b, ble:NODEA_fd78", "log": ""}]}}, f)
server = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB), "--rigctl-port", "0",
                           "--addons-state", os.path.join(TMP, "state.json"), "--addons", adds], cwd=ROOT,
                          env=dict(os.environ, FAKE_MESH_SCAN=MODE, FAKE_MESH_SCAN_RUNS=RUNS, MESH_SCAN_WAIT_S="2"),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

BOX = "document.querySelector('#mesh-scan')"
GO = "document.querySelector('#mesh-scan-go')"
ROWS = f"[...{BOX}.querySelectorAll('.mesh-scan-row')]"
ROW = "(r) => [r.querySelector('b').textContent, r.querySelector('.wl-dim')?.textContent || '', " \
      "r.querySelector('code')?.textContent || null, r.querySelector('.mesh-scan-mine')?.textContent || '']"
DONE = f"{GO} && !{GO}.disabled && {GO}.textContent === 'Look again'"
ERRS = f"[...{BOX}.querySelectorAll('.mesh-scan-err')].map((e) => e.textContent)"
b = Browser(CHROME, CDP)
try:
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", WEB), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    b.goto(f"http://127.0.0.1:{WEB}/?v=mesh#/mesh")
    b.wait("document.querySelector('#mesh-scan-btn') && document.querySelector('#mesh-state').textContent === 'Connected'")
    check("a Find radios button, before Settings",
          b.js("[document.querySelector('#mesh-scan-btn').textContent, document.querySelector('#mesh-scan-btn').nextElementSibling.id]"),
          ["Find radios", "mesh-cfg-btn"])
    check("  its view is shut, and nothing has been scanned", (b.js(f"{BOX}.hidden"), runs()), (True, 0))

    b.js("document.querySelector('#mesh-scan-btn').click()")
    check("it opens in place of the conversation", b.js(f"[{BOX}.hidden, document.querySelector('#mesh-main').hidden]"), [False, True])
    b.wait(DONE)
    check("  and looked at once", runs(), 1)
    got = b.js(f"{ROWS}.map({ROW})")
    check("Bluetooth: name, signal, the host file's port, whose it is", got[:3],
          [["NODEA_fd78", "-41 dBm", "ble:NODEA_fd78", "this station's LF"],
           ["ROOF_4b6b", "-82 dBm", "ble:ROOF_4b6b", ""],
           ["odd name!", "-90 dBm", None, ""]])
    check("  a name the host file would refuse says why it has no port",
          "refuses a character" in b.js(f"{ROWS}[2].textContent"), True)
    check("USB: name, maker and device, the port, whose it is", got[3:],
          [["HT-n5262", "Heltec · /dev/cu.usbmodem1101", "usb:1A2B3C4D5E6F7A8B", "this station's LF"],
           ["CP2102 USB to UART Bridge Controller", "Silicon Labs · /dev/cu.SLAB_USBtoUART", "/dev/cu.SLAB_USBtoUART", ""]])
    notes = b.js(f"{BOX}.querySelector('.mesh-scan-notes').textContent")
    check("the notes: what is not listed, and how to add one",
          ["stops advertising" in notes, "tcp:HOST" in notes, '"radios"' in notes, "ranch-radio install --apply" in notes,
           "bonded" in notes], [True] * 5)

    b.js(f"{GO}.click()")
    check("Look again: the button waits", b.js(f"[{GO}.disabled, {GO}.textContent]"), [True, "Looking…"])
    b.wait(DONE)
    check("  and the scan ran again", runs(), 2)

    b.js("document.querySelector('#mesh-health-btn').click()")
    check("Health closes it", b.js(f"[{BOX}.hidden, document.querySelector('#mesh-health').hidden]"), [True, False])
    b.js("document.querySelector('#mesh-scan-btn').click()")
    check("the button opens it again, with the last answer and no new scan",
          (b.js(f"[{BOX}.hidden, document.querySelector('#mesh-health').hidden, {ROWS}.length]"), runs()), ([False, True, 5], 2))
    b.js("document.querySelector('#mesh-cfg-btn').click()")
    check("Settings closes it", b.js(f"[{BOX}.hidden, document.querySelector('#mesh-cfg').hidden]"), [True, False])
    b.js("document.querySelector('#mesh-scan-btn').click()")

    mode("error")
    b.js(f"{GO}.click()")
    b.wait(f"{DONE} && {ERRS}.length")
    check("Bluetooth off: said", b.js(ERRS), ["ble: Bluetooth is off"])
    check("  USB still listed", b.js(f"{ROWS}.map((r) => r.querySelector('b').textContent)"),
          ["HT-n5262", "CP2102 USB to UART Bridge Controller"])

    mode("bad")
    b.js(f"{GO}.click()")
    b.wait(f"{DONE} && {ERRS}.length && {ROWS}.length === 0")
    check("an answer that is not JSON: said", b.js(f"{ERRS}[0].startsWith('scan: ')"), True)

    mode("slow")
    n = runs()
    b.js(f"{GO}.click()")
    b.js('send({ t: "mesh_scan" })')                       # a second request while the first runs
    b.wait(f"{DONE} && {ERRS}.length")
    check("no answer in time: said", b.js(ERRS), ["scan: no answer in 2 s"])
    check("  and the second request did not start another scan", runs(), n + 1)

    mode("<script>")
    b.js(f"{GO}.click()")
    b.wait(f"{DONE} && {ROWS}.length === 1")
    check("a name from the air is text, not markup",
          b.js(f"[{ROWS}[0].querySelector('b').textContent, {BOX}.querySelectorAll('img').length, typeof window.pwned]"),
          ["<img src=x onerror=window.pwned=1>", 0, "undefined"])
finally:
    b.close()
    server.terminate()
    try:
        server.wait(10)
    except subprocess.TimeoutExpired:
        server.kill()
    shutil.rmtree(TMP, ignore_errors=True)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
