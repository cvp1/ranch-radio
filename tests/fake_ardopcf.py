#!/usr/bin/env python3
"""A stand-in for ardopcf: listens on PORT and PORT+1 (127.0.0.1) and records
how it was started, for station_test.py.

    fake_ardopcf.py PORT CAPTURE PLAYBACK --logdir DIR

$FAKE_ARDOP_LOG: file this fake appends one JSON line per start to.
"""
import json
import os
import socket
import sys
import time

port = int(sys.argv[1])
if os.environ.get("FAKE_ARDOP_LOG"):
    with open(os.environ["FAKE_ARDOP_LOG"], "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "t": time.time()}) + "\n")
socks = []
for p in (port, port + 1):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", p))
    s.listen()
    socks.append(s)
while True:
    time.sleep(1)
