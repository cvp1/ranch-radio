"""Shutdown test (F10): SIGTERM closes rigctl sessions and browser sockets
cleanly and fast, so apps see an orderly EOF and the service restarts quickly.

Runs server.py --sim (never real hardware) with a raw rigctl client connected,
and in a second scenario a browser WebSocket too, then sends SIGTERM and times
when each client sees its connection end and when the process exits.

    .venv/bin/python tests/shutdown_test.py [rig]
"""
import asyncio
import os
import random
import signal
import socket
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RIG = sys.argv[1] if len(sys.argv) > 1 else "ic7300"
EOF_MAX_S = 1.0
EXIT_MAX_S = 2.0
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:52} got {got!r}" + ("" if ok else f" want {want!r}"))


def check_true(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'ok  ' if cond else 'FAIL'}  {name:52} {detail}")


def start_server():
    rport, web = 15000 + random.randrange(2000), 18000 + random.randrange(2000)
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--rig", RIG, "--no-audio",
         "--listen", str(web), "--host", "127.0.0.1", "--rigctl-port", str(rport)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(80):
        try:
            socket.create_connection(("127.0.0.1", rport), timeout=0.2).close()
            socket.create_connection(("127.0.0.1", web), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    time.sleep(0.5)
    return proc, rport, web


class RigctlWatch:
    """A connected rigctl client; records how its connection ended."""

    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.s.sendall(b"f\n")
        self.first = self.s.recv(100)
        self.s.settimeout(None)
        self.end = None          # (monotonic time, "eof" | error text)
        threading.Thread(target=self._wait, daemon=True).start()

    def _wait(self):
        try:
            data = self.s.recv(100)
            self.end = (time.monotonic(), "eof" if not data else f"data {data!r}")
        except OSError as e:
            self.end = (time.monotonic(), type(e).__name__)


class BrowserWatch:
    """A connected browser control WebSocket; records when it was closed."""

    def __init__(self, web):
        self.end = None
        self.ready = threading.Event()
        threading.Thread(target=lambda: asyncio.run(self._run(web)), daemon=True).start()
        self.ready.wait(5)

    async def _run(self, web):
        import aiohttp
        try:
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(f"http://127.0.0.1:{web}/ws", heartbeat=None) as ws:
                    await ws.receive(timeout=3)          # hello
                    self.ready.set()
                    while True:
                        m = await ws.receive()
                        if m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                      aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                            self.end = (time.monotonic(), m.type.name)
                            return
        except Exception as e:
            self.end = (time.monotonic(), type(e).__name__)
            self.ready.set()


def scenario(with_browser):
    label = "rigctl + browser" if with_browser else "rigctl only"
    print(f"\n# F10 SIGTERM with {label} connected")
    proc, rport, web = start_server()
    rc = RigctlWatch(rport)
    check(f"{label}: rigctl client got a reply", rc.first.strip().isdigit(), True)
    br = BrowserWatch(web) if with_browser else None
    if br:
        check_true(f"{label}: browser connected", br.ready.is_set() and br.end is None)
    time.sleep(0.3)
    t0 = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    try:
        out = proc.communicate(timeout=15)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        out = proc.communicate()[0]
    t_exit = time.monotonic() - t0
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and (rc.end is None or (br and br.end is None)):
        time.sleep(0.05)
    check_true(f"{label}: server exited within {EXIT_MAX_S:g}s", t_exit <= EXIT_MAX_S, f"{t_exit:.2f}s")
    if rc.end:
        dt, how = rc.end[0] - t0, rc.end[1]
        check(f"{label}: rigctl client sees a clean EOF", how, "eof")
        check_true(f"{label}: rigctl EOF within {EOF_MAX_S:g}s", dt <= EOF_MAX_S, f"{dt:.2f}s")
    else:
        check_true(f"{label}: rigctl connection ended", False)
    if br:
        if br.end:
            dt = br.end[0] - t0
            check_true(f"{label}: browser socket closed within {EOF_MAX_S:g}s", dt <= EOF_MAX_S,
                       f"{dt:.2f}s ({br.end[1]})")
        else:
            check_true(f"{label}: browser socket closed", False)
    check_true(f"{label}: no Traceback", "Traceback" not in out, out[-600:] if "Traceback" in out else "")


if __name__ == "__main__":
    scenario(False)
    scenario(True)
    print("\nPASS" if not fails else f"\n{fails} FAILED")
    sys.exit(1 if fails else 0)
