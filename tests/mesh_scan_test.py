"""mesh_scan.py: the radios in reach, without USB, Bluetooth or the meshtastic package.

  - USB rows: every USB serial device by device path; one with a serial
    number is named usb:SERIAL, one without (or with a number the host file
    would refuse) by its device path; a port that is not USB is left out; a
    Mac's /dev/tty.* twin of a /dev/cu.* board is left out; names are cut
    and made printable
  - Bluetooth rows: one a name, its strongest hearing, strongest first; the
    nameless left out; a name the host file refuses is shown with no port
  - the script on a Python without pyserial or bleak: one JSON object, both
    said under "errors", exit 0 (the page is waiting for an answer)

    python3 tests/mesh_scan_test.py
"""
import json
import os
import subprocess
import sys
from types import SimpleNamespace as P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import mesh_scan  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


ports = [P(device="/dev/tty.usbmodem1101", vid=0x239A, serial_number="1A2B3C4D5E6F7A8B", product="HT-n5262", manufacturer="Heltec"),
         P(device="/dev/cu.usbmodem1101", vid=0x239A, serial_number="1A2B3C4D5E6F7A8B", product="HT-n5262", manufacturer="Heltec"),
         P(device="/dev/cu.SLAB_USBtoUART", vid=0x10C4, serial_number=None, product="CP2102 USB to UART Bridge Controller",
           manufacturer="Silicon Labs"),
         P(device="/dev/cu.usbserial-1", vid=0x10C4, serial_number="RIG 0001", product="x" * 200 + "\x07", manufacturer=None),
         P(device="/dev/cu.Bluetooth-Incoming-Port", vid=None, serial_number=None, product=None, manufacturer=None)]
rows = mesh_scan.usb_rows(ports)
check("USB: every USB serial device, by device path; not the one that is not USB",
      [r["device"] for r in rows], ["/dev/cu.SLAB_USBtoUART", "/dev/cu.usbmodem1101", "/dev/cu.usbserial-1"])
check("  a board with a serial number is named by it", rows[1],
      {"port": "usb:1A2B3C4D5E6F7A8B", "device": "/dev/cu.usbmodem1101", "name": "HT-n5262", "maker": "Heltec"})
check("  one without is named by its device path", rows[0]["port"], "/dev/cu.SLAB_USBtoUART")
check("  and so is one whose number the host file would refuse", rows[2]["port"], "/dev/cu.usbserial-1")
check("  a long name is cut, and printable", (len(rows[2]["name"]), rows[2]["name"].isprintable(), rows[2]["maker"]), (80, True, ""))
check("  on Linux, where a board shows once, it stays",
      [r["port"] for r in mesh_scan.usb_rows([P(device="/dev/ttyACM0", vid=1, serial_number="AB12", product="n", manufacturer="m")])],
      ["usb:AB12"])

rows = mesh_scan.ble_rows([("NODEA_fd78", -70), ("ROOF_4b6b", -82), ("NODEA_fd78", -41), (None, -30), ("", -30),
                           ("odd name!", -90), ("QUIET_0001", None)])
check("Bluetooth: one row a name, strongest first, the nameless left out",
      [(r["name"], r["rssi"]) for r in rows],
      [("NODEA_fd78", -41), ("ROOF_4b6b", -82), ("odd name!", -90), ("QUIET_0001", None)])
check("  the port the host file takes", rows[0]["port"], "ble:NODEA_fd78")
check("  a name it would refuse: shown, no port", rows[2]["port"], None)

# The script itself on this Python (no pyserial, no bleak here: the suite's own venv has neither).
have = []
for mod in ("serial.tools.list_ports", "bleak"):
    try:
        __import__(mod)
        have.append(mod)
    except ImportError:
        pass
r = subprocess.run([sys.executable, os.path.join(ROOT, "mesh_scan.py"), "--ble-seconds", "1"],
                   capture_output=True, text=True, timeout=60)
check("the script answers with one JSON object and exit 0", (r.returncode, len(r.stdout.strip().splitlines())), (0, 1))
d = json.loads(r.stdout)
check("  with its three keys", sorted(d), ["ble", "errors", "usb"])
if "serial.tools.list_ports" not in have:
    check("  no pyserial: said, not raised", d["errors"].get("usb"), "pyserial is not installed in this Python")
if "bleak" not in have:
    check("  no bleak: said, not raised", d["errors"].get("ble"), "bleak is not installed in this Python")

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
