#!/usr/bin/env python3
"""The Meshtastic radios this host can reach, as one JSON object on stdout.

Runs as a child of the station (addons/mesh.py, the Mesh tab's "Find
radios"), under the Python that has the `meshtastic` package: that brings
pyserial (USB) and bleak (Bluetooth). It lists and listens. It opens no
port, connects to nothing and sends nothing.

    mesh_scan.py [--ble-seconds 8]

    {"usb": [{"port": "usb:1A2B3C4D5E6F7A8B", "device": "/dev/cu.usbmodem1101",
              "name": "HT-n5262", "maker": "Heltec"}],
     "ble": [{"port": "ble:NODEA_fd78", "name": "NODEA_fd78", "rssi": -61}],
     "errors": {"ble": "Bluetooth is off"}}

"port" is what a node's "port" in the host file takes (mesh_bridge.py), or
null where there is none to give: a USB device without a serial number is
named by its device path, a Bluetooth name with a character the host file
refuses is shown and left at that.

USB lists every USB serial device, not only nodes: nothing but opening one
tells a node from a transceiver's CAT cable, and this opens nothing. The
names say which is which.

Bluetooth lists what advertises the Meshtastic service now. A node already
connected (to this station, or to a phone) does not advertise, and a node
with a USB client attached has its Bluetooth off (the firmware's choice), so
neither is here. A node on Wi-Fi is not looked for: give it as tcp:HOST.

Seen live 2026-11-22: a station showed half the nodes a phone app did, and
the roof node that heard the rest was in Bluetooth reach of the station the
whole time, unlisted. This says what is in reach and how to name it.
"""
import argparse
import asyncio
import json
import re
import sys

SERVICE = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"     # the Meshtastic BLE service
BLE_NAME = re.compile(r"[A-Za-z0-9_-]{1,40}")         # what the host file's port takes after "ble:" (manifest node_link)
USB_SERIAL = re.compile(r"[A-Za-z0-9]{1,40}")
TEXT_MAX = 80                                         # a name from a device is cut to this


def text(s):
    """A device's own string, as one printable line."""
    return "".join(c for c in str(s or "") if c.isprintable())[:TEXT_MAX].strip()


def usb_rows(ports):
    """pyserial's port list -> the USB serial devices, by device path. A port
    that is not USB (a Bluetooth modem, a built-in UART) has no vendor id and
    is left out."""
    rows = []
    for p in ports:
        if getattr(p, "vid", None) is None:
            continue
        serial = text(getattr(p, "serial_number", None))
        rows.append({"port": f"usb:{serial}" if USB_SERIAL.fullmatch(serial) else p.device,
                     "device": p.device, "name": text(getattr(p, "product", None)),
                     "maker": text(getattr(p, "manufacturer", None))})
    twins = {r["port"] for r in rows if r["device"].startswith("/dev/cu.")}
    # A Mac lists one board twice (/dev/cu.* and /dev/tty.*): the cu one, as mesh_bridge.usb_device picks.
    rows = [r for r in rows if not (r["device"].startswith("/dev/tty.") and r["port"] in twins)]
    return sorted(rows, key=lambda r: r["device"])


def ble_rows(found):
    """[(name, rssi)] as heard -> one row a name, the strongest hearing of
    it, strongest first; the nameless are left out (nothing to connect by)."""
    best = {}
    for name, rssi in found:
        name = text(name)
        if name and (name not in best or (rssi or -999) > (best[name] or -999)):
            best[name] = rssi
    return [{"port": f"ble:{n}" if BLE_NAME.fullmatch(n) else None, "name": n, "rssi": r}
            for n, r in sorted(best.items(), key=lambda x: (-(x[1] or -999), x[0]))]


def scan_usb():
    try:
        from serial.tools import list_ports
    except ImportError:
        raise RuntimeError("pyserial is not installed in this Python")
    return usb_rows(list_ports.comports())


async def scan_ble(seconds):
    try:
        from bleak import BleakScanner
    except ImportError:
        raise RuntimeError("bleak is not installed in this Python")
    found = await BleakScanner.discover(timeout=seconds, return_adv=True, service_uuids=[SERVICE])
    return ble_rows((adv.local_name or dev.name, adv.rssi) for dev, adv in found.values())


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--ble-seconds", type=float, default=8.0, help="how long to listen for Bluetooth advertisements")
    args = p.parse_args()
    out = {"usb": [], "ble": [], "errors": {}}
    try:
        out["usb"] = scan_usb()
    except Exception as e:
        out["errors"]["usb"] = text(e) or type(e).__name__
    try:
        out["ble"] = asyncio.run(scan_ble(min(max(args.ble_seconds, 1.0), 30.0)))
    except Exception as e:                    # no adapter, Bluetooth off, no permission: said, never raised
        out["errors"]["ble"] = text(e) or type(e).__name__
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
