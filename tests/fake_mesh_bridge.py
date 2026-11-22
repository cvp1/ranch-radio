#!/usr/bin/env python3
"""A stand-in for mesh_bridge.py: the same JSON-lines protocol, no node.

    fake_mesh_bridge.py PORT [--key NAME=PATH ...]

On start: connected, info (LongFast + HomeNet, whose key_ok compares --key's
file with FAKE_KEY), two nodes, then one message from !0000beef on HomeNet.
Sending: "fail" -> send_failed, "nak" -> ack with MAX_RETRANSMIT, "die" ->
the node goes away (exit 3), "silent" -> sent and nothing more, anything
else -> sent, then ack from !0000beef.
Settings: a cut-down copy of the real schema (owner, device, lora). "set"
updates the values and sends config again, except hop_limit 6, which the
"node" refuses; "favorite" flips the node's star in the node list. "trace" to !0000beef
answers via !0000cafe; to !0000dead, NO_RESPONSE. Every message heard is
also an "rx" (activity). !0000cafe replies to the first message (reply_id
1001); "react to me" gets a 👍 reaction from !0000beef. NeighborInfo: BASE
hears RDG; on the first start only, RDG hears us (7.25 dB) too. "request" to !0000beef answers (a position request moves it, a
telemetry one fills its air_tx, a neighbours one says it hears us at 4.0 dB); to anyone else, "no reply". "node" ignore /
unignore / remove change the node list; "reboot" is done.
$FAKE_MESH_PORT_NAMES=1 names the node after its port's file name.
$FAKE_MESH_NODES_PAD adds that many far nodes to the node list (a long line).
$FAKE_MESH_BURST=1 also "hears" 30 position packets from !0000dead (hop limit
7, 2 hops, relayed by a node ending in fe: RDG) and 5 texts from !0000beef
heard directly (the Health view's chatty node, hop limit and main relay).
$FAKE_MESH_ABSENT names a file: while it holds a port's file name, the bridge
for that port finds no node, as the real one does for a cable that is out
(not connected, "none of PORT is present", exit 2).
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
    first_start = True
    if os.environ.get("FAKE_MESH_STARTS"):
        try:
            first_start = not open(os.environ["FAKE_MESH_STARTS"]).read().strip()
        except OSError:
            pass
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
    try:
        absent = open(os.environ.get("FAKE_MESH_ABSENT") or os.devnull).read().split()
    except OSError:
        absent = []
    if os.path.basename(args.port) in absent:
        emit(ev="status", connected=False, error=f"none of {args.port} is present")
        return 2
    emit(ev="status", connected=True, error=None)
    me = dict(ME)
    if os.environ.get("FAKE_MESH_PORT_NAMES"):     # two fake nodes told apart on a page (mesh_radios_page_test)
        me["long"] = os.path.basename(args.port)
    emit(ev="info", me=me, preset="LongFast", missing=[],
         channels=[{"index": 0, "name": "LongFast", "role": "PRIMARY", "key_ok": None},
                   {"index": 1, "name": "HomeNet", "role": "SECONDARY", "key_ok": key_ok}])
    values = {"owner": {"long_name": ME["long"], "short_name": ME["short"]},
              "device": {"role": "CLIENT", "node_info_broadcast_secs": 10800},
              "lora": {"region": "US", "hop_limit": 3, "tx_enabled": True},
              "neighbor_info": {"enabled": True}}     # not a section here; the server reads it
    emit(ev="config", sections=SECTIONS, values=values)
    now = int(time.time())
    nodes = [
        {"num": ME["num"], "id": ME["id"], "longName": ME["long"], "shortName": ME["short"],
         "hwModel": "HELTEC_V4", "role": None, "last_heard": now, "snr": None, "hops": None,
         "battery": 101, "lat": 40.91, "lon": -105.12, "fav": True, "me": True, "util": 12.5,
         "air_tx": 1.25, "uptime": 93600,
         "stats": {"tx": 40, "rx": 900, "rx_bad": 3, "online": 3, "total": 4, "util": 12.5, "air_tx": 1.25}},
        {"num": 0xcafe, "id": "!0000cafe", "longName": "Ridge Relay", "shortName": "RDG",
         "hwModel": "RAK4631", "role": "ROUTER", "last_heard": now - 600, "snr": 3.25, "hops": 0,
         "battery": None, "lat": 40.97, "lon": -105.02, "fav": False, "me": False},
        {"num": 0xdead, "id": "!0000dead", "longName": "Old Truck", "shortName": "TRK",
         "hwModel": "T_ECHO", "role": "CLIENT", "last_heard": now - 5 * 86400, "snr": None, "hops": 2,
         "battery": 40, "lat": None, "lon": None, "fav": False, "me": False},
        {"num": 0xbeef, "id": "!0000beef", "longName": "Ranch Base", "shortName": "BASE",
         "hwModel": "RAK4631", "role": "ROUTER", "last_heard": now - 30, "snr": 7.5, "hops": 0,
         "battery": 88, "lat": 40.9, "lon": -105.1, "fav": False, "me": False, "pkc": True,
         "env": {"temp": 21.5, "humidity": 30.0, "pressure": 850.2}}]
    pad = int(os.environ.get("FAKE_MESH_NODES_PAD") or 0)     # a big node DB: one long line
    nodes += [{"num": 0x10000 + i, "id": f"!{0x10000 + i:08x}", "longName": f"Far Node {i:04d} " + "x" * 300,
               "shortName": f"F{i:03d}", "hwModel": "RAK4631", "last_heard": None, "snr": None, "hops": 3,
               "battery": None, "lat": None, "lon": None, "fav": False, "me": False} for i in range(pad)]
    emit(ev="nodes", nodes=nodes)
    time.sleep(0.5)
    rx_direct = {"hops": 0, "hop_start": 3, "snr": 7.5, "rssi": -90, "mqtt": False, "relay": 0xef, "ch": 1,
                 "to_me": False, "size": 40}
    emit(ev="rx", **{"from": "!0000beef"}, port="TEXT_MESSAGE_APP", **rx_direct)
    if os.environ.get("FAKE_MESH_BURST"):
        for _ in range(30):
            emit(ev="rx", **{"from": "!0000dead"}, port="POSITION_APP", hops=2, hop_start=7, snr=-2.0, rssi=-118,
                 mqtt=False, relay=0xfe, ch=0, to_me=False, size=60)
        for _ in range(5):
            emit(ev="rx", **{"from": "!0000beef"}, port="TEXT_MESSAGE_APP", **rx_direct)
    emit(ev="msg", id=1001, **{"from": "!0000beef"}, to="^all", ch=1, dm=False,
         text="gate is closed", snr=7.5, rssi=-90, hops=0)
    emit(ev="msg", id=1002, **{"from": "!0000cafe"}, to="^all", ch=1, dm=False,
         text="thanks, I saw it", snr=3.0, rssi=-110, hops=0, reply_id=1001, emoji=False)
    nb = {"!0000beef": {"t": now, "neighbors": [{"id": "!0000cafe", "snr": 6.5}]}}
    if first_start:                     # only once: a restart must keep it from the links file
        nb["!0000cafe"] = {"t": now, "neighbors": [{"id": ME["id"], "snr": 7.25}, {"id": "!0000beef", "snr": 2.0}]}
    emit(ev="neighbors", neighbors=nb)
    next_id = [5000]

    def ack_later(pid, ok, reason):
        time.sleep(0.3)
        emit(ev="ack", id=pid, ok=ok, reason=reason, by="!0000beef", relayed=False)

    for line in sys.stdin:
        m = json.loads(line)
        if os.environ.get("FAKE_MESH_LOG"):
            with open(os.environ["FAKE_MESH_LOG"], "a") as f:
                f.write(json.dumps({**m, "_port": args.port}) + "\n")     # which node got it (mesh_radios_test)
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
        if m.get("cmd") == "request":
            def answer(m=m):
                time.sleep(0.3)
                if m["to"] == ME["id"] and m["kind"] == "neighbors":   # our own node, asked of itself
                    emit(ev="neighbors", neighbors={ME["id"]: {"t": int(time.time()), "neighbors": [
                        {"id": "!0000cafe", "snr": 7.0}, {"id": "!0000beef", "snr": 3.0}]}})
                    emit(ev="req", ref=m["ref"], to=m["to"], kind=m["kind"], ok=True, error=None)
                    return
                if m["to"] != "!0000beef":
                    nak = m["kind"] == "neighbors" and m["to"] == "!0000dead"   # its NeighborInfo is off
                    emit(ev="req", ref=m["ref"], to=m["to"], kind=m["kind"], ok=False,
                         error="NO_RESPONSE" if nak else "no reply")
                    return
                for n in nodes:
                    if n["id"] == "!0000beef":
                        if m["kind"] == "position":
                            n["lat"], n["pos_t"] = 40.92, int(time.time())
                        elif m["kind"] == "telemetry":
                            n["air_tx"] = 2.5
                if m["kind"] == "neighbors":    # BASE: it hears RDG, and us at 4.0 dB
                    emit(ev="neighbors", neighbors={"!0000beef": {"t": int(time.time()), "neighbors": [
                        {"id": "!0000cafe", "snr": 6.5}, {"id": ME["id"], "snr": 4.0}]}})
                emit(ev="nodes", nodes=nodes)
                emit(ev="req", ref=m["ref"], to=m["to"], kind=m["kind"], ok=True, error=None)
            threading.Thread(target=answer).start()
            continue
        if m.get("cmd") == "node":
            if m["do"] == "remove":
                nodes[:] = [n for n in nodes if n["id"] != m["to"]]
            for n in nodes:
                if n["id"] == m["to"]:
                    n["ignored"] = m["do"] == "ignore"
            emit(ev="done", ref=m["ref"], ok=True, error=None)
            emit(ev="nodes", nodes=nodes)
            continue
        if m.get("cmd") == "reboot":
            emit(ev="done", ref=m["ref"], ok=True, error=None)
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
        if m["text"] == "silent":     # sent, and then the node never says
            continue
        nak = m["text"] == "nak"
        threading.Thread(target=ack_later, args=(next_id[0], not nak,
                                                 "MAX_RETRANSMIT" if nak else "NONE")).start()
        if m["text"] == "react to me":
            def react(pid=next_id[0], ch=m["ch"]):
                time.sleep(0.5)
                emit(ev="msg", id=9001, **{"from": "!0000beef"}, to="^all", ch=ch, dm=False, text="👍",
                     snr=7.5, rssi=-90, hops=0, reply_id=pid, emoji=True)
            threading.Thread(target=react).start()
    return 1


if __name__ == "__main__":
    sys.exit(main())
