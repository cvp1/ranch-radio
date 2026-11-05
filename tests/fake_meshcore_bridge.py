#!/usr/bin/env python3
"""A stand-in for meshcore_bridge.py: the same JSON-lines protocol, no node.

    fake_meshcore_bridge.py PORT

On start: connected, info, channels (Public), two contacts, a battery
reading, then one Public message from "Alpha". Sending: "fail" ->
send_failed, "die" -> the node goes away (exit 3), anything else -> sent;
a direct message is then acked. "advert" -> done ok.
Each start appends a line to $FAKE_MC_STARTS and every command received
goes to $FAKE_MC_LOG.
"""
import json
import os
import sys
import time

ALPHA = "a1" * 32
BETA = "b2" * 32


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
    return 0


if __name__ == "__main__":
    sys.exit(main())
