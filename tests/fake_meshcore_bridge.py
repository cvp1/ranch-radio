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

The rest of the protocol: a third contact, the repeater "Gamma Rpt" with a
position, on Alpha's route; a pending node "Delta" (heard, not added); the
node's radio stats and one packet heard via Gamma. A direct message is
"sent" then "delivered" (the retrying bridge's lines). Requests answer at
once: status, telemetry, neighbours (Gamma hears Alpha at 6.25 dB), owner,
path (via Gamma both ways), trace, login (passwords "good" and "Sav3d-Pw"
are admin, "guest" is a guest, anything else is wrong), cli (echoed back as the repeater's
reply), clock, fav, reset_path, remove, add (Delta). A request to "Beta Rpt"
times out. channel_join adds the channel, channel_leave drops it.
"""
import json
import os
import sys
import time

ALPHA = "a1" * 32
BETA = "b2" * 32
GAMMA = "d4" * 32
DELTA = "e5" * 32


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
    config()
    emit(ev="channels", channels=[{"index": 0, "name": "Public"}])
    contacts = {
        ALPHA: {"key": ALPHA, "name": "Alpha", "type": "chat", "hops": 1, "path": ["d4"], "fav": False,
                "last_advert": int(time.time()) - 60, "lat": 40.1, "lon": -105.0},
        BETA: {"key": BETA, "name": "Beta Rpt", "type": "repeater", "hops": 2, "path": ["d4", "77"], "fav": False,
               "last_advert": int(time.time()) - 7200, "lat": None, "lon": None},
        GAMMA: {"key": GAMMA, "name": "Gamma Rpt", "type": "repeater", "hops": 0, "path": [], "fav": False,
                "last_advert": int(time.time()) - 9000, "lat": 40.05, "lon": -105.02}}
    pending = {DELTA: {"key": DELTA, "name": "Delta", "type": "chat", "hops": None, "path": [], "fav": False,
                       "last_advert": int(time.time()) - 30, "lat": None, "lon": None}}
    channels = [{"index": 0, "name": "Public"}]

    def send_contacts():
        emit(ev="contacts", contacts=sorted(contacts.values(), key=lambda c: -c["last_advert"]))

    send_contacts()
    emit(ev="pending", contacts=list(pending.values()))
    emit(ev="battery", mv=4100)
    emit(ev="stats", noise_floor=-118, last_rssi=-96, last_snr=7.25, tx_air_secs=42, rx_air_secs=600,
         recv=120, sent=14, flood_tx=9, direct_tx=5, flood_rx=100, direct_rx=20, recv_errors=1,
         uptime_secs=3700, queue_len=0)
    emit(ev="rx", snr=6.5, rssi=-101, type="ADVERT", route="FLOOD", hops=1, last="d4")
    emit(ev="msg", **{"from": None}, name="Alpha", ch=0, dm=False, text="hello mesh", snr=7.5, hops=0,
         sent_t=int(time.time()))
    n = 0
    for line in sys.stdin:
        m = json.loads(line)
        if os.environ.get("FAKE_MC_LOG"):
            with open(os.environ["FAKE_MC_LOG"], "a") as f:
                f.write(json.dumps(m) + "\n")
        cmd, ref, key = m.get("cmd"), m.get("ref"), m.get("key")

        def result(ok=True, data=None, error=None):
            emit(ev="result", ref=ref, kind=cmd, key=key, ok=ok, data=data if ok else None, error=error)

        if cmd == "send":
            if m["text"] == "fail":
                emit(ev="send_failed", ref=m["ref"], error="radio busy")
            elif m["text"] == "die":
                return 3
            elif m.get("to"):
                emit(ev="sent", ref=ref, ack=None, flood=False)
                emit(ev="delivered", ref=ref, attempts=1, flood=False, trip_ms=1800)
            else:
                n += 1
                emit(ev="sent", ref=m["ref"], ack=f"{n:08x}")
        elif key == BETA and cmd not in ("fav", "remove", "reset_path"):
            result(False, error="no answer to the request in 8 s")
        elif cmd == "status":
            result(data={"bat": 4012, "uptime": 90061, "airtime": 3600, "noise_floor": -117, "last_rssi": -88,
                         "last_snr": 9.5, "nb_recv": 5000, "nb_sent": 700, "sent_flood": 600, "sent_direct": 100,
                         "recv_flood": 4500, "recv_direct": 500, "direct_dups": 3, "flood_dups": 250,
                         "tx_queue_len": 0, "full_evts": 0})
        elif cmd == "telemetry":
            result(data={"lpp": [{"channel": 1, "type": "voltage", "value": 4.01},
                                 {"channel": 1, "type": "temperature", "value": 31.5}]})
        elif cmd == "neighbours":
            result(data={"total": 1, "neighbours": [{"key": ALPHA[:8], "snr": 6.25, "secs_ago": 300}]})
        elif cmd == "owner":
            result(data={"name": "Gamma Rpt", "owner": "N0CALL"})
        elif cmd == "path":
            result(data={"out": ["d4"], "back": ["d4"]})
        elif cmd == "trace":
            result(data={"hops": [{"hash": "d4", "snr": 8.0}, {"snr": 7.5}]})
        elif cmd == "login":
            pw = m.get("password")
            if pw in ("good", "Sav3d-Pw", "guest"):
                result(data={"admin": pw != "guest", "permissions": 0 if pw == "guest" else 1})
            else:
                result(False, error="wrong password")
        elif cmd in ("cli", "clock"):
            result(data={"sent": True})
            emit(ev="cli_reply", key=key[:12], text="OK - clock set" if cmd == "clock" else f"echo: {m['text']}")
        elif cmd == "fav":
            contacts[key]["fav"] = bool(m.get("fav"))
            result(data={"fav": contacts[key]["fav"]})
            send_contacts()
        elif cmd == "reset_path":
            contacts[key]["hops"], contacts[key]["path"] = None, []
            result(data={})
            send_contacts()
        elif cmd == "remove":
            contacts.pop(key, None)
            result(data={})
            send_contacts()
        elif cmd == "add":
            contacts[key] = pending.pop(key)
            result(data={})
            emit(ev="pending", contacts=list(pending.values()))
            send_contacts()
        elif cmd == "channel_join":
            channels.append({"index": max(c["index"] for c in channels) + 1, "name": m["name"]})
            emit(ev="channels", channels=channels)
            emit(ev="done", ref=ref, ok=True, error=None)
        elif cmd == "channel_leave":
            channels[:] = [c for c in channels if c["index"] != m["index"]]
            emit(ev="channels", channels=channels)
            emit(ev="done", ref=ref, ok=True, error=None)
        elif cmd == "advert":
            emit(ev="done", ref=m["ref"], ok=True, error=None)
        elif cmd == "set":
            vals = dict(m["values"])
            if vals.get("name") == "refuse":
                emit(ev="done", ref=m["ref"], ok=False, error="RuntimeError: the node refused the name")
                continue
            vals.pop("password", None); vals.pop("pin", None)   # write-only, as on the real node
            VALUES[m["section"]].update(vals)
            emit(ev="done", ref=m["ref"], ok=True, error=None, reboot=m["section"] in ("wifi", "bluetooth"))
            config()
        elif cmd == "reboot":
            emit(ev="done", ref=m["ref"], ok=True, error=None, rebooting=True)
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
