"""The Modules tab's server side, end to end (modules plan M1/M2).

A simulated radio server with --host-file, Mesh on a fake bridge, and a host
file whose python takes 3 s per call (a slow venv):
  - the hello says the Modules tab is on
  - {"t":"modules"} answers at once, Mesh "on" from the live registry
  - while doctor runs (seconds), pings still answer fast: the loop isn't blocked
  - when doctor is done every page gets the report, with the time it ran
  - Refresh during a run joins it rather than starting a second
A server without --host-file refuses (a Linux radio server behind a station),
and a station serves Modules from its config's "host_file".

    python3 tests/modules_server_test.py
"""
import asyncio
import json
import os
import random
import socket
import stat
import subprocess
import sys
import os as _os
# Host files come from examples/hosts/ (or a scratch copy) only, never a
# real ~/.config/ranch-radio/hosts on the machine running the tests.
_os.environ["RANCH_RADIO_HOSTS"] = ""
import tempfile
import time

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="modsrv-")
REPO, HOME = os.path.join(TMP, "repo"), os.path.join(TMP, "home")
os.makedirs(os.path.join(REPO, "hosts"))
os.makedirs(HOME)
MARK = os.path.join(TMP, "mesh-marker")
CALLS = os.path.join(TMP, "slowpy-calls")
SLOWPY = os.path.join(TMP, "slowpy")
with open(SLOWPY, "w") as f:
    f.write(f"#!/bin/sh\necho x >> {CALLS}\nsleep 3\nexec {sys.executable} \"$@\"\n")
os.chmod(SLOWPY, os.stat(SLOWPY).st_mode | stat.S_IXUSR)
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:66} got {got!r}" + ("" if ok else f" want {want!r}"))


def write_host(name, h):
    with open(os.path.join(REPO, "hosts", f"{name}.json"), "w") as f:
        json.dump(h, f, indent=2)


write_host("testmac", {"host": "testmac", "os": "macos", "repo": "~/ranch-radio", "python": SLOWPY,
                       "radios": [{"id": "ic7300", "name": "IC-7300", "label": "t.radio", "device": "/dev/null"}],
                       "mesh": {"port": MARK, "keys": []}})
write_host("testlinux", {"host": "testlinux", "os": "linux", "repo": "~/ranch-radio", "python": sys.executable,
                         "radios": [], "station": {"https": False}})


def wait_port(p, port):
    for _ in range(600):                  # 60 s ceiling; returns once the port answers
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return p
        except OSError:
            time.sleep(0.1)
    p.kill()
    print(p.stdout.read())
    raise SystemExit("server did not start")


def server(port, host_file=True):
    env = dict(os.environ, RANCH_RADIO_REPO=REPO, RANCH_RADIO_HOME=HOME)
    argv = [sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(port), "--rigctl-port", "0",
            "--addons-state", os.path.join(TMP, f"state-{port}.json"),
            "--mesh-port", MARK, "--mesh-python", sys.executable,
            "--mesh-bridge", "tests/fake_mesh_bridge.py", "--mesh-log", ""]
    if host_file:
        argv += ["--host-file", "testmac"]
    return wait_port(subprocess.Popen(argv, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True), port)


def station(port):
    cfg = os.path.join(TMP, "station.json")
    with open(cfg, "w") as f:
        json.dump({"name": "Test", "radios": [], "host_file": "testlinux",
                   "addons_state": os.path.join(TMP, "st-state.json")}, f)
    env = dict(os.environ, RANCH_RADIO_REPO=REPO, RANCH_RADIO_HOME=HOME)
    return wait_port(subprocess.Popen([sys.executable, "station.py", "--config", cfg, "--listen", str(port),
                                       "--rigctl-port", "0"], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True), port)


def stop(p):
    p.terminate()
    try:
        p.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()


class Page:
    def __init__(self, port):
        self.port, self.events = port, []

    async def open(self):
        self.sess = aiohttp.ClientSession()
        self.ws = await self.sess.ws_connect(f"http://127.0.0.1:{self.port}/ws")
        self.hello = json.loads((await self.ws.receive(timeout=10)).data)
        self.reader = asyncio.ensure_future(self._read())
        return self

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                self.events.append((time.monotonic(), json.loads(msg.data)))

    async def send(self, m):
        await self.ws.send_str(json.dumps(m))

    async def wait(self, pred, timeout=10, since=0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for _, e in self.events[since:]:
                if pred(e):
                    return e
            await asyncio.sleep(0.02)
        return None

    async def close(self):
        await self.ws.close()
        await self.sess.close()


async def radio_server(port):
    a, b = await Page(port).open(), await Page(port).open()
    check("hello: the Modules tab is on", a.hello["config"].get("modules"), True)
    t0 = time.monotonic()
    await a.send({"t": "modules"})
    rep = await a.wait(lambda e: e.get("t") == "modules", 5)
    check("modules answers at once (before doctor's slow python)", rep is not None and time.monotonic() - t0 < 2.5, True)
    items = {i["name"]: i for i in (rep or {}).get("items", [])}
    check("Mesh, running on its fake bridge, reads on", items.get("mesh", {}).get("state"), "on")
    check("the first look starts doctor (checking)", (rep or {}).get("checking"), True)
    check("winlink is available here, with its add command",
          (items.get("winlink", {}).get("state"), "add winlink" in (items.get("winlink", {}).get("command") or "")),
          ("available", True))
    # While doctor waits on a 3 s python, the loop must keep answering.
    worst = 0.0
    for i in range(10):
        n = len(a.events)
        s = time.monotonic()
        await a.send({"t": "ping", "ts": i})
        got = await a.wait(lambda e, i=i: e.get("t") == "pong" and e.get("ts") == i, 5, since=n)
        worst = max(worst, time.monotonic() - s if got else 99)
        await asyncio.sleep(0.3)
    check("pings answer within 0.5 s while doctor runs", worst < 0.5, True)
    n = len(b.events)
    await b.send({"t": "modules_check"})            # Refresh mid-run: joins, doesn't start another
    done = await a.wait(lambda e: e.get("t") == "modules" and e.get("checked_at"), 60)
    check("when doctor is done, every page gets the report", done is not None and
          await b.wait(lambda e: e.get("t") == "modules" and e.get("checked_at"), 5, since=n) is not None, True)
    core = {c["what"].split(" ")[0]: c["status"] for c in next(
        i for i in (done or {}).get("items", []) if i["name"] == "core")["needs"]} if done else {}
    check("doctor ran the host file's python", core.get("python"), "ok")
    calls = sum(1 for _ in open(CALLS)) if os.path.exists(CALLS) else 0
    check("Refresh during a run joined it (one python version + one aiohttp + sounddevice call)", calls <= 3, True)
    await a.close()
    await b.close()


async def no_host_file(port):
    a = await Page(port).open()
    check("no --host-file: hello says no Modules tab", a.hello["config"].get("modules"), False)
    await a.send({"t": "modules"})
    e = await a.wait(lambda e: e.get("t") in ("notice", "modules"), 5)
    check("  and modules is refused with a notice", (e or {}).get("t"), "notice")
    await a.close()


async def station_page(port):
    a = await Page(port).open()
    check("station: hello says the Modules tab is on", a.hello["config"].get("modules"), True)
    await a.send({"t": "modules"})
    rep = await a.wait(lambda e: e.get("t") == "modules", 5)
    items = {i["name"]: i for i in (rep or {}).get("items", [])}
    check("station: reports its host file", (rep or {}).get("host"), "testlinux")
    check("station: uvpro is available on Linux", items.get("uvpro", {}).get("state"), "available")
    await a.close()


p1 = 19000 + random.randrange(1000)
srv = server(p1)
try:
    asyncio.run(radio_server(p1))
finally:
    stop(srv)
p2 = p1 + 1
srv = server(p2, host_file=False)
try:
    asyncio.run(no_host_file(p2))
finally:
    stop(srv)
p3 = p1 + 2
st = station(p3)
try:
    asyncio.run(station_page(p3))
finally:
    stop(st)
print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
