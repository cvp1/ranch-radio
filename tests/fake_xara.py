#!/usr/bin/env python3
"""A stand-in for xara (VARA HF), enough for the Winlink VARA tests. It keys
the radio itself through its --ptt rigctld lane, as xara does, and writes
xara-shaped --log lines ("<UTC>Z <secs> <text>", crates/xara/src/log.rs).

    fake_xara.py --call N0CALL --hf 500 --ptt rigctld:127.0.0.1:PORT
                 --addr 127.0.0.1 --port CMD --log FILE [other xara flags, ignored]

Like xara v0.1.3 (crates/xara/src/ptt.rs): one persistent rigctld
connection; at start it sends T 0 once; each key is T 1 ... T 0, expecting
RPRT 0; ANY other reply is a keyer fault and it exits non-zero (3), never
redialing. Listens on CMD (commands) and CMD+1 (data), 127.0.0.1.

Commands from Pat (n8jja/Pat-Vara): VERSION -> "VERSION VARA HF v4.9.0";
PUBLIC/CWID/COMPRESSION/MYCALL/LISTEN/BW<n> -> OK; CONNECT <me> <them> runs
a scripted session; DISCONNECT (graceful) or ABORT (dirty) end it with our
QRT and CW ID; a dropped command connection (Pat gone) ends it the same way
(xara's drop_link). Replies "CONNECTED <me> <them> <bw>", "DISCONNECTED".

Environment:
  FAKE_XARA_EVENTS  file: one JSON line per event (start, key, reply, exit)
  FAKE_XARA_SCRIPT  "ours" (default): data bursts, then our QRT frame with
                    the CW ID riding its key ("tx cw" ~1.4 s after its T 1);
                    "theirs": data bursts, then "tx cw" and a fresh key
                    right after it (the other station's QRT)
  FAKE_XARA_BURSTS  data bursts per session (default 2)
  FAKE_XARA_BURST_S seconds each data burst stays keyed (default 0.8)
  FAKE_XARA_CW_S    seconds the CW part stays keyed (default 1.5)
  FAKE_XARA_EARLY_DISC "1": say DISCONNECTED as soon as "tx cw" is logged,
                    before the CW's T 0 (so Pat returns mid-ID)
  FAKE_XARA_DIE_S   seconds into a session: exit at once (3), mid-burst
  FAKE_XARA_LEVEL   speed level reported in BITRATE lines (default 3)
"""
import json
import os
import socket
import sys
import threading
import time

argv = sys.argv[1:]


def flag(name, default=None):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default


CALL = flag("--call", "N0CALL")
BW = flag("--hf", "2300")
PTT = flag("--ptt", "")
ADDR = flag("--addr", "127.0.0.1")
PORT = int(flag("--port", "8300"))
LOG = flag("--log")
EVENTS = os.environ.get("FAKE_XARA_EVENTS")
SCRIPT = os.environ.get("FAKE_XARA_SCRIPT", "ours")
BURSTS = int(os.environ.get("FAKE_XARA_BURSTS", "2"))
BURST_S = float(os.environ.get("FAKE_XARA_BURST_S", "0.8"))
CW_S = float(os.environ.get("FAKE_XARA_CW_S", "1.5"))
EARLY_DISC = os.environ.get("FAKE_XARA_EARLY_DISC") == "1"
DIE_S = float(os.environ.get("FAKE_XARA_DIE_S", "0") or 0)
LEVEL = int(os.environ.get("FAKE_XARA_LEVEL", "3"))
T0 = time.monotonic()
lock = threading.Lock()


def event(**kw):
    if EVENTS:
        with lock, open(EVENTS, "a") as f:
            f.write(json.dumps({**kw, "pid": os.getpid(), "t": time.time()}) + "\n")


def logline(text):
    if not LOG:
        return
    now = time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int(now * 1000) % 1000:03d}Z"
    with lock, open(LOG, "a") as f:
        f.write(f"{stamp} {time.monotonic() - T0:10.3f} {text}\n")


def die(code, why):
    event(route="exit", code=code, why=why)
    logline(f"xara: {why}")
    os._exit(code)


class Rig:
    """xara's Rigctld keyer: one connection, T 1 / T 0, RPRT 0 or a fault."""

    def __init__(self, spec):
        if not spec.startswith("rigctld"):
            die(2, f"--ptt {spec}: this fake knows rigctld only")
        parts = spec.split(":")
        host, port = (parts[1], int(parts[2])) if len(parts) == 3 else ("127.0.0.1", 4532)
        try:
            self.s = socket.create_connection((host, port), timeout=2)
        except OSError as e:
            die(3, f"rigctld at {host}:{port}: {e}")
        self.f = self.s.makefile("r")
        self.keyed = False
        self.set(False)                 # open() sends T 0 once

    def set(self, on):
        try:
            self.s.sendall(b"T 1\n" if on else b"T 0\n")
            r = self.f.readline().strip()
        except OSError as e:
            die(3, f"PTT rigctld failed: {e}")
        event(route="key", on=on, reply=r)
        if r != "RPRT 0":
            die(3, f"PTT rigctld at {PTT}: {r}; released")
        self.keyed = on


rig = Rig(PTT)
logline(f"xara: rigctld at {PTT.partition(':')[2] or '127.0.0.1:4532'}")
event(route="start", argv=argv)


class Session:
    def __init__(self, conn, me, them):
        self.conn, self.me, self.them = conn, me, them
        self.stop = None                 # None | "disconnect" | "abort" | "dropped"
        self.t0 = time.monotonic()

    def say(self, line):
        try:
            self.conn.sendall((line + "\r").encode())
        except OSError:
            pass

    def check_die(self):
        if DIE_S and time.monotonic() - self.t0 >= DIE_S:
            die(3, "killed mid-session")

    def burst(self, what, secs):
        rig.set(True)
        logline(f"  {what}")
        end = time.monotonic() + secs
        while time.monotonic() < end:
            self.check_die()
            time.sleep(0.05)
        rig.set(False)

    def close_ours(self):
        """Our QRT frame, the CW ID queued behind it in the same key."""
        rig.set(True)
        logline("  tx c9 f0 Qrt")
        time.sleep(1.4)
        logline("  tx cw Closing")
        if EARLY_DISC:
            self.say("DISCONNECTED")
        time.sleep(CW_S)
        rig.set(False)

    def close_theirs(self):
        """Their QRT heard: the CW ID queued, keyed on the next audio block."""
        logline("  tx cw Closing")
        time.sleep(0.05)
        rig.set(True)
        if EARLY_DISC:
            self.say("DISCONNECTED")
        time.sleep(CW_S)
        rig.set(False)

    def run(self):
        event(route="session", them=self.them)
        self.burst("tx c1 f1 Calling", 0.6)
        self.say(f"CONNECTED {self.me} {self.them} {BW}")
        for i in range(BURSTS):
            if self.stop:
                break
            logline(f"< BITRATE ({LEVEL})  {LEVEL * 100} bps TX")
            self.burst(f"tx L{LEVEL}x2 Sending", BURST_S)
            time.sleep(0.3)
        if SCRIPT == "theirs" and not self.stop:
            self.close_theirs()
        else:
            self.close_ours()
        if not EARLY_DISC:
            self.say("DISCONNECTED")
        event(route="session_end", stop=self.stop)


current = None


def serve_cmd(conn):
    global current
    buf = b""
    while True:
        try:
            data = conn.recv(4096)
        except OSError:
            data = b""
        if not data:
            if current is not None and current.stop is None:
                current.stop = "dropped"     # xara's host_dropped -> drop_link
            event(route="cmd_closed")
            return
        buf += data
        while b"\r" in buf or b"\n" in buf:
            i = min(x for x in (buf.find(b"\r"), buf.find(b"\n")) if x >= 0)
            line, buf = buf[:i].decode().strip(), buf[i + 1:]
            if not line:
                continue
            event(route="cmd", line=line)
            word = line.split()[0].upper()
            if word == "VERSION":
                conn.sendall(b"VERSION VARA HF v4.9.0\r")
            elif word == "CONNECT":
                parts = line.split()
                current = Session(conn, parts[1] if len(parts) > 1 else CALL, parts[2] if len(parts) > 2 else "?")
                conn.sendall(b"OK\r")
                threading.Thread(target=current.run, daemon=True).start()
            elif word in ("DISCONNECT", "ABORT"):
                if current is not None and current.stop is None:
                    current.stop = "abort" if word == "ABORT" else "disconnect"
                conn.sendall(b"OK\r")
            else:
                conn.sendall(b"OK\r")


def listen(port, handler):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((ADDR, port))
    s.listen()
    while True:
        c, _ = s.accept()
        threading.Thread(target=handler, args=(c,), daemon=True).start()


def serve_data(conn):
    while conn.recv(4096):
        pass


threading.Thread(target=listen, args=(PORT + 1, serve_data), daemon=True).start()
listen(PORT, serve_cmd)
