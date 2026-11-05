#!/usr/bin/env python3
"""A stand-in for meshcore_bridge.py: the same JSON-lines protocol, no node.

    fake_meshcore_bridge.py PORT

On start: connected, info, channels (Public), two contacts, a battery
reading, then one Public message from "Alpha". Sending: "fail" ->
send_failed, "die" -> the node goes away (exit 3), anything else -> sent;
a direct message is then acked. "advert" -> done ok. Settings: config
(node, radio, wifi, bluetooth, as the real bridge sends them); "set"
updates the values and sends config again (the Wi-Fi password only goes
to $FAKE_MC_LOG, like the real node keeps it), except the name "refuse",
which the "node" refuses; wifi and bluetooth writes say reboot. "reboot"
-> done, then the node goes away (exit 0), as a real reboot drops the link.
Each start appends a line to $FAKE_MC_STARTS and every command received
goes to $FAKE_MC_LOG.
"""
import json
import os
import sys
import time

ALPHA = "a1" * 32
BETA = "b2" * 32


SECTIONS = [
    {"name": "node", "label": "Node", "fields": [
        {"name": "name", "label": "Name", "type": "str", "max_bytes": 31},
        {"name": "lat", "label": "Latitude", "type": "float", "min": -90, "max": 90},
        {"name": "lon", "label": "Longitude", "type": "float", "min": -180, "max": 180},
        {"name": "share_location", "label": "Share location in adverts", "type": "bool"}]},
    {"name": "radio", "label": "Radio", "fields": [
        {"name": "freq", "label": "Frequency", "type": "float", "min": 150, "max": 960, "risky": "off mesh"},
        {"name": "bw", "label": "Bandwidth", "type": "enum", "options": [62.5, 125.0, 250.0], "risky": "off mesh"},
        {"name": "sf", "label": "Spreading factor", "type": "int", "min": 5, "max": 12, "risky": "off mesh"},
        {"name": "cr", "label": "Coding rate", "type": "int", "min": 5, "max": 8, "risky": "off mesh"},
        {"name": "tx_power", "label": "TX power", "type": "int", "min": 1, "max": 22}]},
    {"name": "wifi", "label": "Wi-Fi", "fields": [
        {"name": "enabled", "label": "Wi-Fi", "type": "bool"},
        {"name": "ssid", "label": "Network", "type": "str", "max_bytes": 32},
        {"name": "password", "label": "Password", "type": "str", "max_bytes": 63, "write_only": True}]},
    {"name": "bluetooth", "label": "Bluetooth", "fields": [
        {"name": "pin", "label": "PIN", "type": "int", "min": 0, "max": 999999, "write_only": True}]},
]
VALUES = {"node": {"name": "TestNode", "lat": 40.0, "lon": -105.0, "share_location": True},
          "radio": {"freq": 910.525, "bw": 62.5, "sf": 7, "cr": 5, "tx_power": 22},
          "wifi": {"enabled": False, "ssid": "", "password": ""},
          "bluetooth": {"pin": ""}}


def config():
    emit(ev="config", link="tcp:127.0.0.1:5000", sections=SECTIONS, values=VALUES,
         wifi={"status": "connected" if VALUES["wifi"]["enabled"] else "disconnected", "ip": None})


def emit(**d):
    sys.stdout.write(json.dumps(d) + "\n")
    sys.stdout.flush()


def main():
    if os.environ.get("FAKE_MC_STARTS"):
        with open(os.environ["FAKE_MC_STARTS"], "a") as f:
            f.write(f"{time.time()}\n")
    emit(ev="status", connected=True, error=None)
    emit(ev="info", me={"key": "c3" * 32, "name": "TestNode", "lat": 40.0, "lon": -105.0, "share_loc": True},
         device={"model": "Heltec V3", "fw": "v1.18.0-test", "build": "", "max_contacts": 350},
         radio={"freq": 910.525, "bw": 62.5, "sf": 7, "cr": 5, "tx_power": 22})
    emit(ev="channels", channels=[{"index": 0, "name": "Public"}])
    config()
    emit(ev="contacts", contacts=[
        {"key": ALPHA, "name": "Alpha", "type": "chat", "hops": 0, "last_advert": int(time.time()) - 60,
         "lat": 40.1, "lon": -105.0},
        {"key": BETA, "name": "Beta Rpt", "type": "repeater", "hops": 2, "last_advert": int(time.time()) - 7200,
         "lat": None, "lon": None}])
    emit(ev="battery", mv=4100)
    emit(ev="msg", **{"from": None}, name="Alpha", ch=0, dm=False, text="hello mesh", snr=7.5, hops=0,
         sent_t=int(time.time()))
    n = 0
    for line in sys.stdin:
        m = json.loads(line)
        if os.environ.get("FAKE_MC_LOG"):
            with open(os.environ["FAKE_MC_LOG"], "a") as f:
                f.write(json.dumps(m) + "\n")
        if m.get("cmd") == "send":
            if m["text"] == "fail":
                emit(ev="send_failed", ref=m["ref"], error="radio busy")
            elif m["text"] == "die":
                return 3
            else:
                n += 1
                ack = f"{n:08x}"
                emit(ev="sent", ref=m["ref"], ack=ack)
                if m.get("to"):
                    emit(ev="ack", ack=ack)
        elif m.get("cmd") == "advert":
            emit(ev="done", ref=m["ref"], ok=True, error=None)
        elif m.get("cmd") == "set":
            vals = dict(m["values"])
            if vals.get("name") == "refuse":
                emit(ev="done", ref=m["ref"], ok=False, error="RuntimeError: the node refused the name")
                continue
            vals.pop("password", None); vals.pop("pin", None)   # write-only, as on the real node
            VALUES[m["section"]].update(vals)
            emit(ev="done", ref=m["ref"], ok=True, error=None, reboot=m["section"] in ("wifi", "bluetooth"))
            config()
        elif m.get("cmd") == "reboot":
            emit(ev="done", ref=m["ref"], ok=True, error=None, rebooting=True)
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
