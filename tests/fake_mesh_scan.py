#!/usr/bin/env python3
"""A stand-in for mesh_scan.py: the same one JSON object, no USB or Bluetooth.

    fake_mesh_scan.py [--ble-seconds N]

Answers with one node on USB (by serial number), a transceiver's CAT cable
(no serial number: its device path), two nodes on Bluetooth and one whose
name the host file would refuse. $FAKE_MESH_SCAN holds something else to
answer with: "bad" (not JSON), "slow" (no answer), "error" (Bluetooth off),
or "<script>" (names from the air that must not run on the page).
Each run appends a line to $FAKE_MESH_SCAN_RUNS.
"""
import json
import os
import sys
import time

ANSWER = {"usb": [{"port": "usb:1A2B3C4D5E6F7A8B", "device": "/dev/cu.usbmodem1101", "name": "HT-n5262", "maker": "Heltec"},
                  {"port": "/dev/cu.SLAB_USBtoUART", "device": "/dev/cu.SLAB_USBtoUART", "name": "CP2102 USB to UART Bridge Controller",
                   "maker": "Silicon Labs"}],
          "ble": [{"port": "ble:NODEA_fd78", "name": "NODEA_fd78", "rssi": -41},
                  {"port": "ble:ROOF_4b6b", "name": "ROOF_4b6b", "rssi": -82},
                  {"port": None, "name": "odd name!", "rssi": -90}],
          "errors": {}}

if os.environ.get("FAKE_MESH_SCAN_RUNS"):
    with open(os.environ["FAKE_MESH_SCAN_RUNS"], "a") as f:
        f.write("run\n")
mode = ""
try:
    mode = open(os.environ.get("FAKE_MESH_SCAN") or os.devnull).read().strip()
except OSError:
    pass
if mode == "bad":
    print("Traceback (most recent call last): no")
elif mode == "slow":
    time.sleep(120)
elif mode == "error":
    print(json.dumps({"usb": ANSWER["usb"], "ble": [], "errors": {"ble": "Bluetooth is off"}}))
elif mode == "<script>":
    print(json.dumps({"usb": [], "errors": {},
                      "ble": [{"port": None, "name": "<img src=x onerror=window.pwned=1>", "rssi": -50}]}))
else:
    print(json.dumps(ANSWER))
sys.exit(0)
