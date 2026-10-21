"""Helpers for tests that run server.py --sim with tests/fake_pat.py as Pat
(the same harness as winlink_test.py, importable)."""
import asyncio
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPS = 15000 + random.randrange(1000)
LANE = 16000 + random.randrange(1000)
PATP = 17000 + random.randrange(1000)
WEB = 18000 + random.randrange(1000)
URL = "ardop:///N0DAJ?bw=2000MAX&freq=3588.5"


def ptt():
    with socket.create_connection(("127.0.0.1", APPS), timeout=3) as s:
        f = s.makefile("r")
        s.sendall(b"t\n")
        v = f.readline().strip()
        f.close()
        return v


def lane_ptt_refused():
    with socket.create_connection(("127.0.0.1", LANE), timeout=3) as s:
        f = s.makefile("r")
        s.sendall(b"T 1\n")
        v = f.readline().strip()
        if v == "RPRT 0":
            s.sendall(b"T 0\n")
            f.readline()
        f.close()
        return v == "RPRT -11"


class Page:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.events = []
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.run(self._open())

    def run(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _open(self):
        self.sess = aiohttp.ClientSession()
        self.ws = await self.sess.ws_connect(f"http://127.0.0.1:{WEB}/ws")
        self.hello = json.loads((await self.ws.receive()).data)
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                m = json.loads(msg.data)
                if m.get("t") in ("winlink", "notice"):
                    self.events.append(m)

    def send(self, m):
        self.run(self.ws.send_str(json.dumps(m)))

    def wait(self, pred, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            for e in list(self.events):
                if pred(e):
                    return e
            time.sleep(0.1)
        return None

    def close(self):
        async def _c():
            await self.ws.close()
            await self.sess.close()
        self.run(_c())
        time.sleep(0.5)


def ended(e):
    return e.get("t") == "winlink" and e.get("session") == "ended"


def fake_log(path):
    try:
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]
    except OSError:
        return []


def start(env_extra=None, *extra):
    logf = tempfile.NamedTemporaryFile(prefix="fakepat-", suffix=".jsonl", delete=False).name
    env = dict(os.environ, FAKE_PAT_RIGCTL=str(LANE), FAKE_PAT_LOG=logf, **(env_extra or {}))
    p = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB),
         "--rigctl-port", str(APPS), "--winlink-rigctl-port", str(LANE),
         "--winlink-pat", f"{sys.executable} tests/fake_pat.py", "--winlink-pat-port", str(PATP),
         *extra],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            for port in (WEB, LANE, PATP):
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            time.sleep(0.5)          # Radio Remote's websocket to Pat
            return p, logf
        except OSError:
            time.sleep(0.1)
    p.kill()
    print(p.stdout.read())
    raise SystemExit("server did not start")


def stop(p):
    p.terminate()
    try:
        return p.communicate(timeout=40)[0]
    except subprocess.TimeoutExpired:
        p.kill()
        return p.communicate()[0]
