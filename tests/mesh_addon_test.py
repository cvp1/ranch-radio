"""Mesh as an add-on (addons/mesh.py): the on/off switch, against tests/fake_mesh_bridge.py.

The Mesh tab's own protocol is tests/mesh_test.py's (unchanged); this checks
what the add-on system adds:
  - Mesh is on by default and has no add-on panel (its page is the Mesh tab)
  - "Switch off" stops the bridge, so the node's port is free; the page gets
    the off state; a send while off is refused and never reaches the node
  - off is kept across a restart (the state file), and the bridge isn't started
  - "Switch on" starts a fresh bridge, and sending works again
  - without addons/mesh.py the radio server runs on with no Mesh tab

    python3 tests/mesh_addon_test.py
"""
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
sys.path.insert(0, ROOT)
WEB = 19000 + random.randrange(1000)
TMP = tempfile.mkdtemp(prefix="meshaddon-")
STATE = os.path.join(TMP, "addons.json")
CMD_LOG = os.path.join(TMP, "commands.jsonl")
STARTS = os.path.join(TMP, "starts")
MARK = os.path.join(TMP, "port-marker")        # in the bridge's argv: finds OUR bridge process
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


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
                self.events.append(json.loads(msg.data))

    def send(self, m):
        self.run(self.ws.send_str(json.dumps(m)))

    def wait(self, pred, timeout=10, since=0):
        end = time.time() + timeout
        while time.time() < end:
            for e in self.events[since:]:
                if pred(e):
                    return e
            time.sleep(0.05)
        return None

    def close(self):
        async def _c():
            await self.ws.close()
            await self.sess.close()
        self.run(_c())


def start():
    env = dict(os.environ, FAKE_MESH_LOG=CMD_LOG, FAKE_MESH_STARTS=STARTS)
    p = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB),
                          "--rigctl-port", "0", "--addons-state", STATE,
                          "--mesh-port", MARK, "--mesh-python", sys.executable,
                          "--mesh-bridge", "tests/fake_mesh_bridge.py", "--mesh-log", ""],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", WEB), timeout=0.2).close()
            return p
        except OSError:
            time.sleep(0.1)
    p.kill()
    print(p.stdout.read())
    raise SystemExit("server did not start")


def stop(p):
    p.terminate()
    try:
        p.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()


def bridge_running():
    # The bridge runs as "PY tests/fake_mesh_bridge.py MARK"; the server's own argv
    # names MARK too (--mesh-port), so match the bridge's order, not MARK alone.
    r = subprocess.run(["pgrep", "-f", f"fake_mesh_bridge.py {MARK}"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def starts():
    try:
        with open(STARTS) as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def sends(text):
    try:
        with open(CMD_LOG) as f:
            return [c for c in map(json.loads, filter(str.strip, f)) if c.get("text") == text]
    except OSError:
        return []


def connected(e):
    return e.get("t") == "mesh" and (e.get("status") or {}).get("connected") is True


server = start()
try:
    page = Page()
    check("on by default: config.mesh", page.hello["config"].get("mesh"), True)
    check("no add-on panel for Mesh (its page is the Mesh tab)",
          [a["name"] for a in page.hello["config"].get("addons", [])], [])
    check("not off in the snapshot", (page.hello.get("mesh") or {}).get("off"), None)
    check("the node connects", page.wait(connected) is not None or
          bool((page.hello.get("mesh") or {}).get("status", {}).get("connected")), True)
    check("its bridge is running", bridge_running(), True)

    n = len(page.events)
    page.send({"t": "addon", "addon": "mesh", "do": "power", "on": False})
    off = page.wait(lambda e: e.get("t") == "addon" and e.get("addon") == "mesh" and e.get("on") is False, since=n)
    check("switch off: the page is told", off is not None, True)
    check("  with the off snapshot", ((off or {}).get("snapshot") or {}).get("off"), True)
    time.sleep(1)
    check("  the bridge is stopped (the port is free)", bridge_running(), False)
    with open(STATE) as f:
        check("  the state file says off", json.load(f).get("mesh"), False)

    n = len(page.events)
    page.send({"t": "mesh_send", "text": "while off", "ch": 1})
    warn = page.wait(lambda e: e.get("t") == "notice" and e.get("level") == "warn", since=n, timeout=3)
    check("a send while off is refused", "switched off" in (warn or {}).get("msg", ""), True)
    check("  and never reaches the node", sends("while off"), [])
    page.close()

    before = starts()
    stop(server)
    server = start()
    page = Page()
    check("after a restart: still off", (page.hello.get("mesh") or {}).get("off"), True)
    check("  the Mesh tab is still there", page.hello["config"].get("mesh"), True)
    time.sleep(1.5)
    check("  and the bridge was not started", (starts() - before, bridge_running()), (0, False))

    n = len(page.events)
    page.send({"t": "addon", "addon": "mesh", "do": "power", "on": True})
    on = page.wait(lambda e: e.get("t") == "addon" and e.get("addon") == "mesh" and e.get("on") is True, since=n)
    check("switch on: the page gets a live snapshot", ((on or {}).get("snapshot") or {}).get("off"), None)
    check("  the node connects again", page.wait(connected, since=n) is not None, True)
    check("  a fresh bridge started", starts() - before, 1)
    page.send({"t": "mesh_send", "text": "back on", "ch": 1})
    deadline = time.time() + 5
    while time.time() < deadline and not sends("back on"):
        time.sleep(0.1)
    check("  sending works again", len(sends("back on")), 1)
    page.close()
finally:
    stop(server)

# --- the falsifier: no addons/mesh.py, and the radio server runs on without Mesh
import addons  # noqa: E402


class Host:
    def notice(self, *a, **k): pass
    def _broadcast(self, *a): pass
    async def _send(self, *a): pass


sys.modules["addons.mesh"] = None                 # as if the file were deleted
reg = addons.Addons(Host(), {"mesh": {"port": "/dev/null"}}, None)
check("without addons/mesh.py: Mesh is not loaded", reg.get("mesh"), None)
check("  the reason is kept", (reg.items["mesh"]["error"] or "").startswith("not loaded"), True)
check("  so config.mesh is false (no Mesh tab)", bool(reg.get("mesh")), False)
check("  and a Mesh message routes nowhere", reg.route("mesh_send"), None)

print(f"\n{'PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
