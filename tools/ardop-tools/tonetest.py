"""Key the IC-7300 for ardopcf's TWOTONETEST and log PO watts / ALC / SWR.

ardopcf runs without PTT of its own, so it asks its host for PTT. This script
is that host and keys the rig through Radio Remote's rigctl. Hard cap: unkey
after MAX_S seconds whatever ardopcf says.
"""
import socket
import time

MAX_S = 8.0

ardop = socket.create_connection(("127.0.0.1", 8515), timeout=0.2)
rig = socket.create_connection(("127.0.0.1", 4532), timeout=2)
rigf = rig.makefile("rw")


def rigcmd(c):
    rigf.write(c + "\n")
    rigf.flush()
    return rigf.readline().strip()


def meters():
    return rigcmd("l RFPOWER_METER_WATTS"), rigcmd("l ALC"), rigcmd("l SWR")


keyed = False
keyed_at = None
t0 = time.time()
buf = b""
try:
    ardop.sendall(b"MYCALL N9ABC\r")
    time.sleep(0.3)
    ardop.sendall(b"TWOTONETEST\r")
    while time.time() - t0 < 15:
        try:
            buf += ardop.recv(1024)
        except socket.timeout:
            pass
        while b"\r" in buf:
            line, buf = buf.split(b"\r", 1)
            msg = line.decode(errors="replace").strip()
            if msg:
                print(f"{time.time() - t0:5.1f}s ardop: {msg}", flush=True)
            if msg == "PTT TRUE" and not keyed:
                print("  key:", rigcmd("T 1"), flush=True)
                keyed, keyed_at = True, time.time()
            elif msg == "PTT FALSE" and keyed:
                print("  unkey:", rigcmd("T 0"), flush=True)
                keyed = False
        if keyed:
            w, alc, swr = meters()
            print(f"{time.time() - t0:5.1f}s  W={w}  ALC={alc}  SWR={swr}", flush=True)
            if time.time() - keyed_at > MAX_S:
                print("  hard cap unkey:", rigcmd("T 0"), flush=True)
                keyed = False
        elif keyed_at and time.time() - keyed_at > 1:
            break
        time.sleep(0.3)
finally:
    if keyed:
        print("  final unkey:", rigcmd("T 0"))
    print("PTT now:", rigcmd("t"))
