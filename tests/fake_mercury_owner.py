#!/usr/bin/env python3
"""A stand-in for Mercury HF's PTT (plan winlink-mercury M0): Hamlib NET
rigctl (model 2) the way headless Mercury v1.9.25 drives it
(-P hamlib -R 2 -A 127.0.0.1:PORT): the netrigctl open handshake, then per
key one "T 1", silence for the whole frame (headless Mercury never talks to
rigctld while keyed), then "T 0". A refused key does not stop it: Mercury's
ptt_on() goes on to play the frame (tcp_interfaces.c:2496-2508), so this
waits out the frame and sends its T 0 just the same, then carries on.

Nothing here transmits: it talks to a server.py --sim lane.

    fake_mercury_owner.py --port PORT [--key-s 2] [--gap-s 1] [--count 1]
                          [--delay-s 0] [--t0-first] [--events FILE] [--hold]

--hold: after the last key stay connected (silent) until killed.
Events (FILE, one JSON line each): {"ev": "open"}, {"ev": "key", "n", "reply"},
{"ev": "unkey", "n", "reply"}, {"ev": "done"}, {"ev": "error", "msg"}.
"""
import argparse
import json
import socket
import sys
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--key-s", type=float, default=2.0)
    p.add_argument("--gap-s", type=float, default=1.0)
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--delay-s", type=float, default=0.0)
    p.add_argument("--t0-first", action="store_true")
    p.add_argument("--events")
    p.add_argument("--hold", action="store_true")
    a = p.parse_args()
    out = open(a.events, "a", buffering=1) if a.events else sys.stdout

    def ev(**d):
        out.write(json.dumps(dict(d, t=round(time.time(), 3))) + "\n")
        out.flush()

    try:
        s = socket.create_connection(("127.0.0.1", a.port), timeout=10)
    except OSError as e:
        ev(ev="error", msg=str(e))
        return 2
    s.settimeout(None)
    f = s.makefile("r")

    def ask(line, multi=False):
        s.sendall((line + "\n").encode())
        if not multi:
            return f.readline().strip()
        lines = []
        while True:
            ln = f.readline()
            if not ln:
                break
            lines.append(ln.strip())
            if ln.strip() == "done":
                break
        return lines

    ask("\\get_powerstat")
    ask("\\chk_vfo")
    ask("\\dump_state", multi=True)
    ev(ev="open")
    if a.t0_first:
        ask("T 0")
    time.sleep(a.delay_s)
    for n in range(a.count):
        if n:
            time.sleep(a.gap_s)
        ev(ev="key", n=n, reply=ask("T 1"))
        time.sleep(a.key_s)              # the frame: not a byte to the lane
        ev(ev="unkey", n=n, reply=ask("T 0"))
    ev(ev="done")
    if a.hold:
        while True:
            time.sleep(60)
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
