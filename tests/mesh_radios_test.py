"""More than one Meshtastic node on a station (addons/mesh.py "radios",
mesh-radios 2026-11-21), against two tests/fake_mesh_bridge.py processes.

  - config: each extra node needs a port; labels and ports are unique; a log
    may not be another node's; auto_trace is off unless said (and a string is
    refused); keys default to the first node's, a feed never carries over;
    the log defaults to the first node's name plus -<id>; at most 4
  - the snapshot lists the nodes (first is id ""), each with its state
  - the first node's events go out as before (no "node"); another node's
    carry "node": its id
  - a command goes to the node it names ("node"), the first when it names none
  - mesh_select answers the asking page only, with that node's snapshot
  - a notice from another node names it; an unknown node is refused, said
  - Switch off stops both bridges; Switch on starts both again

    python3 tests/mesh_radios_test.py
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
from addons.mesh import MeshAddon  # noqa: E402

WEB = testkit.free_port()
TMP = tempfile.mkdtemp(prefix="meshradios-")
STATE = os.path.join(TMP, "addons.json")
CMD_LOG = os.path.join(TMP, "commands.jsonl")
STARTS = os.path.join(TMP, "starts")
MARK_A = os.path.join(TMP, "port-mf18")
MARK_B = os.path.join(TMP, "port-lf")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


def refused(opts):
    try:
        MeshAddon(None, opts)
    except ValueError as e:
        return str(e)
    return None


# ---------------------------------------------------------------- config
P = {"port": "/dev/a", "label": "MF18", "keys": ["Ranch=/k"], "feed": "mqtt://b/p", "log": "~/m/mesh.jsonl"}
a = MeshAddon(None, dict(P, radios=[{"label": "LF", "port": "/dev/b"}]))
x = a.extra[0]
check("an extra node: its id is its label, lower-cased", (x["id"], x["label"]), ("lf", "LF"))
check("  keys default to the first node's", x["args"].mesh_key, ["Ranch=/k"])
check("  a feed never carries over (one topic per node)", x["args"].mesh_feed, None)
check("  auto_trace is off unless said", x["args"].mesh_auto_trace, False)
check("  its log: the first node's name plus -<id>", x["args"].mesh_log, "~/m/mesh-lf.jsonl")
check("the switcher's list: first node first, id ''", [(r["id"], r["label"]) for r in a.radios()], [("", "MF18"), ("lf", "LF")])
check("no radios: no extra engines", MeshAddon(None, {"port": "/dev/a"}).extra, [])
check("an extra node with no port is refused, naming it", refused(dict(P, radios=[{"label": "LF"}])),
      'mesh radios[0] needs a "port"')
check("  the same port twice", "already another node's" in (refused(dict(P, radios=[{"port": "/dev/a"}])) or ""), True)
check("  the same label twice", "not unique" in (refused(dict(P, radios=[{"label": "X", "port": "/dev/b"},
                                                                     {"label": "x", "port": "/dev/c"}])) or ""), True)
check("  a log that is the first node's", "log" in (refused(dict(P, radios=[{"port": "/dev/b", "log": "~/m/mesh.jsonl"}])) or ""), True)
check('  "auto_trace": "false" (a string)', "auto_trace" in (refused(dict(P, radios=[{"port": "/dev/b", "auto_trace": "false"}])) or ""), True)
check("  more than 4", "at most 4" in (refused(dict(P, radios=[{"port": f"/dev/x{i}"} for i in range(5)])) or ""), True)


# ---------------------------------------------------------------- two fake nodes
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
    adds = os.path.join(TMP, "addons-cfg.json")
    with open(adds, "w") as f:
        json.dump({"mesh": {"port": MARK_A, "label": "MF18", "python": sys.executable,
                            "bridge": "tests/fake_mesh_bridge.py", "log": "",
                            "radios": [{"label": "LF", "port": MARK_B, "log": ""}]}}, f)
    p = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB),
                          "--rigctl-port", "0", "--addons-state", STATE, "--addons", adds],
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


def running(mark):
    r = subprocess.run(["pgrep", "-f", f"fake_mesh_bridge.py {mark}"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def got(text):
    """The bridge ports a text reached."""
    try:
        with open(CMD_LOG) as f:
            return [c.get("_port") for c in map(json.loads, filter(str.strip, f)) if c.get("text") == text]
    except OSError:
        return []


def connected(node):
    return lambda e: (e.get("t") == "mesh" and e.get("node") == node
                      and (e.get("status") or {}).get("connected") is True)


server = start()
try:
    page = Page()
    snap = page.hello.get("mesh") or {}
    check("the snapshot lists both nodes", [(r["id"], r["label"]) for r in snap.get("radios") or []],
          [("", "MF18"), ("lf", "LF")])
    check("both bridges run", wait_for(lambda: running(MARK_A) and running(MARK_B), 10), True)
    check("  each with its state (both connect)", wait_for(lambda: all(r["connected"] for r in
          (Page().hello.get("mesh") or {}).get("radios") or [{}]), 10), True)

    page.send({"t": "mesh_send", "text": "to lf", "ch": 1, "node": "lf"})
    page.send({"t": "mesh_send", "text": "to first", "ch": 1})
    wait_for(lambda: got("to lf") and got("to first"), 5)
    check("a command naming lf reaches the LF bridge only", got("to lf"), [MARK_B])
    check("  one naming none reaches the first", got("to first"), [MARK_A])

    other = Page()
    n, n2 = len(page.events), len(other.events)
    page.send({"t": "mesh_select", "node": "lf"})
    sel = page.wait(lambda e: e.get("t") == "mesh" and "select" in e, since=n)
    check("mesh_select: that node's snapshot, tagged", ((sel or {}).get("node"), bool((sel or {}).get("select", {}).get("nodes"))),
          ("lf", True))
    check("  with the switcher's list", [r["id"] for r in (sel or {}).get("select", {}).get("radios") or []], ["", "lf"])
    time.sleep(0.5)
    check("  to the asking page only", [e for e in other.events[n2:] if "select" in e], [])
    other.close()

    n = len(page.events)
    page.send({"t": "mesh_trace", "to": "nonsense", "node": "lf"})
    w = page.wait(lambda e: e.get("t") == "notice", since=n, timeout=3)
    check("a notice from the LF node names it", (w or {}).get("msg", "").startswith("LF: "), True)
    n = len(page.events)
    page.send({"t": "mesh_send", "text": "nowhere", "ch": 1, "node": "hf"})
    w = page.wait(lambda e: e.get("t") == "notice", since=n, timeout=3)
    check("an unknown node is refused, said", "no node 'hf'" in (w or {}).get("msg", ""), True)
    time.sleep(0.3)
    check("  and nothing was sent", got("nowhere"), [])

    page.send({"t": "addon", "addon": "mesh", "do": "power", "on": False})
    check("Switch off stops both bridges", wait_for(lambda: not running(MARK_A) and not running(MARK_B), 5), True)
    n = len(page.events)
    page.send({"t": "addon", "addon": "mesh", "do": "power", "on": True})
    check("Switch on starts both again", wait_for(lambda: running(MARK_A) and running(MARK_B), 10), True)
    check("  the LF node's events carry node: lf", page.wait(connected("lf"), since=n) is not None, True)
    check("  the first node's events carry no node", page.wait(connected(None), since=n) is not None, True)
    page.close()
finally:
    stop(server)

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
