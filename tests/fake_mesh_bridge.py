#!/usr/bin/env python3
"""A stand-in for mesh_bridge.py: the same JSON-lines protocol, no node.

    fake_mesh_bridge.py PORT [--key NAME=PATH ...]

On start: connected, info (LongFast + HomeNet, whose key_ok compares --key's
file with FAKE_KEY), two nodes, then one message from !0000beef on HomeNet.
Sending: "fail" -> send_failed, "nak" -> ack with MAX_RETRANSMIT, "die" ->
the node goes away (exit 3), anything else -> sent, then ack from !0000beef.
Settings: a cut-down copy of the real schema (owner, device, lora). "set"
updates the values and sends config again, except hop_limit 6, which the
"node" refuses; "favorite" flips the node's star in the node list. "trace" to !0000beef
answers via !0000cafe; to !0000dead, NO_RESPONSE. Every message heard is
also an "rx" (activity).
Each start appends a line to $FAKE_MESH_STARTS (restart counting) and every
command received goes to $FAKE_MESH_LOG.
"""
import argparse
import base64
import json
import os
import sys
import threading
import time

FAKE_KEY = base64.b64encode(bytes(range(32))).decode()
ME = {"num": 0xb2a6fd78, "id": "!b2a6fd78", "long": "Test Node", "short": "TEST", "hw": "HELTEC_V4",
      "fw": "2.7.15"}

SECTIONS = [
    {"name": "owner", "label": "Owner", "fields": [
        {"name": "long_name", "label": "Long name", "type": "str", "max_bytes": 39},
        {"name": "short_name", "label": "Short name", "type": "str", "max_bytes": 4}]},
    {"name": "device", "label": "Device", "fields": [
        {"name": "role", "label": "Role", "type": "enum", "risky": "Changes what this node relays.",
         "options": ["CLIENT", "CLIENT_MUTE", "ROUTER", "CLIENT_BASE"]},
        {"name": "node_info_broadcast_secs", "label": "Node info every", "type": "int", "unit": "s",
         "min": 0, "max": 4294967295}]},
    {"name": "lora", "label": "LoRa", "fields": [
        {"name": "region", "label": "Region", "type": "enum", "options": ["UNSET", "US", "EU_868"],
         "risky": "Wrong region."},
        {"name": "hop_limit", "label": "Hop limit", "type": "int", "min": 0, "max": 7},
        {"name": "tx_enabled", "label": "Transmit", "type": "bool", "risky": "Off sends nothing."}]},
]


def emit(**d):
    sys.stdout.write(json.dumps(d) + "\n")
    sys.stdout.flush()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("port")
    p.add_argument("--key", action="append", default=[])
    args = p.parse_args()
    if os.environ.get("FAKE_MESH_STARTS"):
        with open(os.environ["FAKE_MESH_STARTS"], "a") as f:
            f.write(f"{time.time()}\n")
    key_ok = None
    for spec in args.key:
        name, _, path = spec.partition("=")
        if name == "HomeNet":
            try:
                key_ok = open(path).read().strip() == FAKE_KEY
            except OSError:
                key_ok = None
    emit(ev="status", connected=True, error=None)
    emit(ev="info", me=ME, preset="LongFast", missing=[],
         channels=[{"index": 0, "name": "LongFast", "role": "PRIMARY", "key_ok": None},
                   {"index": 1, "name": "HomeNet", "role": "SECONDARY", "key_ok": key_ok}])
    values = {"owner": {"long_name": ME["long"], "short_name": ME["short"]},
              "device": {"role": "CLIENT", "node_info_broadcast_secs": 10800},
              "lora": {"region": "US", "hop_limit": 3, "tx_enabled": True}}
    emit(ev="config", sections=SECTIONS, values=values)
    now = int(time.time())
    nodes = [
        {"num": ME["num"], "id": ME["id"], "longName": ME["long"], "shortName": ME["short"],
         "hwModel": "HELTEC_V4", "role": None, "last_heard": now, "snr": None, "hops": None,
         "battery": 101, "lat": 40.91, "lon": -105.12, "fav": True, "me": True},
        {"num": 0xcafe, "id": "!0000cafe", "longName": "Ridge Relay", "shortName": "RDG",
         "hwModel": "RAK4631", "role": "ROUTER", "last_heard": now - 600, "snr": 3.25, "hops": 0,
         "battery": None, "lat": 40.97, "lon": -105.02, "fav": False, "me": False},
        {"num": 0xdead, "id": "!0000dead", "longName": "Old Truck", "shortName": "TRK",
         "hwModel": "T_ECHO", "role": "CLIENT", "last_heard": now - 5 * 86400, "snr": None, "hops": 2,
         "battery": 40, "lat": None, "lon": None, "fav": False, "me": False},
        {"num": 0xbeef, "id": "!0000beef", "longName": "Ranch Base", "shortName": "BASE",
         "hwModel": "RAK4631", "role": "ROUTER", "last_heard": now - 30, "snr": 7.5, "hops": 0,
         "battery": 88, "lat": 40.9, "lon": -105.1, "fav": False, "me": False}]
    emit(ev="nodes", nodes=nodes)
    time.sleep(0.5)
    emit(ev="rx", **{"from": "!0000beef"}, port="TEXT_MESSAGE_APP")
    emit(ev="msg", id=1001, **{"from": "!0000beef"}, to="^all", ch=1, dm=False,
         text="gate is closed", snr=7.5, rssi=-90, hops=0)
    next_id = [5000]

    def ack_later(pid, ok, reason):
        time.sleep(0.3)
        emit(ev="ack", id=pid, ok=ok, reason=reason, by="!0000beef", relayed=False)

    for line in sys.stdin:
        m = json.loads(line)
        if os.environ.get("FAKE_MESH_LOG"):
            with open(os.environ["FAKE_MESH_LOG"], "a") as f:
                f.write(line)
        if m.get("cmd") == "set":
            if m["values"].get("hop_limit") == 6:
                emit(ev="done", ref=m["ref"], ok=False, error="node refused")
                continue
            values[m["section"]].update(m["values"])
            emit(ev="done", ref=m["ref"], ok=True, error=None)
            emit(ev="config", sections=SECTIONS, values=values)
            continue
        if m.get("cmd") == "favorite":
            for n in nodes:
                if n["id"] == m["to"]:
                    n["fav"] = m["fav"]
            emit(ev="done", ref=m["ref"], ok=True, error=None)
            emit(ev="nodes", nodes=nodes)
            continue
        if m.get("cmd") == "trace":
            def answer(m=m):
                time.sleep(0.4)
                if m["to"] == "!0000beef":
                    emit(ev="trace", ref=m["ref"], to=m["to"], ok=True, error=None, route=["!0000cafe"],
                         snr_towards=[6.0, 4.5], route_back=["!0000cafe"], snr_back=[5.0, None])
                else:
                    emit(ev="trace", ref=m["ref"], to=m["to"], ok=False, error="NO_RESPONSE")
            threading.Thread(target=answer).start()
            continue
        if m.get("cmd") != "send":
            continue
        if m["text"] == "die":
            emit(ev="status", connected=False, error="connection to the node lost")
            return 3
        if m["text"] == "fail":
            emit(ev="send_failed", ref=m["ref"], error="serial write failed")
            continue
        next_id[0] += 1
        emit(ev="sent", ref=m["ref"], id=next_id[0])
        nak = m["text"] == "nak"
        threading.Thread(target=ack_later, args=(next_id[0], not nak,
                                                 "MAX_RETRANSMIT" if nak else "NONE")).start()
    return 1


if __name__ == "__main__":
    sys.exit(main())
