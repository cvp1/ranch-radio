"""Mesh as an add-on (addons/mesh.py): the on/off switch, against tests/fake_mesh_bridge.py.

The Mesh tab's protocol is in tests/mesh_test.py; this checks what the add-on
system adds: on by default with no add-on panel; Switch off stops the bridge
(the port is free) and refuses sends; off survives a restart without starting
the bridge; Switch on starts a fresh one; without addons/mesh.py there is no Mesh.

    python3 tests/mesh_addon_test.py
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
from testkit import wait_for  # noqa: E402
import testkit  # noqa: E402

WEB = testkit.free_port()
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
    # the server's argv names MARK too (--mesh-port): match the bridge's argv order
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
    check("  the bridge is stopped (the port is free)", wait_for(lambda: not bridge_running(), 5), True)
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
    wait_for(lambda: sends("back on"), 5)
    check("  sending works again", len(sends("back on")), 1)
    page.close()
finally:
    stop(server)

# --- no addons/mesh.py: the registry runs on without Mesh
import addons  # noqa: E402


class Host:
    def notice(self, *a, **k): pass
    def _broadcast(self, *a): pass
    async def _send(self, *a): pass


sys.modules["addons.mesh"] = None                 # as if the file were deleted
reg = addons.Addons(Host(), {"mesh": {"port": "/dev/null"}}, None)
check("without addons/mesh.py: Mesh is not loaded", reg.get("mesh"), None)
check("  the reason is kept", (reg.items["mesh"]["error"] or "").startswith("not loaded"), True)
check("  and a Mesh message routes nowhere", reg.route("mesh_send"), None)

print(f"\n{'PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
