"""Automatic traceroutes (mesh.py auto_trace), in process, no radio.

Checks the pure rules and the engine driving them through a fake bridge pipe:
  - auto_trace_candidates: only nodes heard since the connection, over at
    least one relay, not ours / MQTT / ignored; a node with a finished trace
    at the same hop count is skipped, a changed hop count re-qualifies it; a
    running trace or a recent automatic one holds it back; a trace loaded
    without "hops" counts its route
  - auto_trace_wait: TRACE_GAP_S after any trace, None while one runs, the
    per-hour cap
  - off by default: a node list full of candidates sends nothing
  - on: one trace per gap, marked auto, recorded with its hop count; the
    hour cap holds the rest; a manual trace settles a queued node, which is
    then not traced again; a disconnect empties the queue
  - the add-on refuses a bad auto_trace_per_hour, and an auto_trace that
    is not a boolean ("false" must not switch it on)
  - the Mesh page's switch (set_auto_trace, the add-on's mesh_auto): off
    drops the queue and the armed timer, says so to every page, and sends
    nothing more, even from a timer that fires anyway; on traces again; the
    add-on refuses anything but true/false, keeps the choice per node across
    restarts over the host file's, and an unreadable kept file starts every
    node off

    python3 tests/mesh_auto_trace_test.py
"""
import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import mesh  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


ME = "!0000aaaa"
NOW = 1_000_000.0


def node(nid, hops, heard=NOW - 10, **kw):
    return {"id": nid, "hops": hops, "last_heard": heard, **kw}


# --- the pure rules

C = mesh.auto_trace_candidates
since = NOW - 600
check("relayed node heard since connect qualifies", C([node("!00000001", 2)], {}, ME, since, {}, NOW), ["!00000001"])
check("direct (0 hops) does not", C([node("!00000001", 0)], {}, ME, since, {}, NOW), [])
check("unknown hops does not", C([node("!00000001", None)], {}, ME, since, {}, NOW), [])
check("heard before the connection does not (stale DB)",
      C([node("!00000001", 2, heard=since - 1)], {}, ME, since, {}, NOW), [])
check("never heard does not", C([node("!00000001", 2, heard=None)], {}, ME, since, {}, NOW), [])
check("ours, MQTT, ignored, bad id do not",
      C([node(ME, 2), node("!00000002", 2, me=True), node("!00000003", 2, mqtt=True),
         node("!00000004", 2, ignored=True), node("bogus", 2)], {}, ME, since, {}, NOW), [])
done = {"!00000001": {"state": "done", "hops": 2, "route": ["!0000cafe", "!0000beef"]}}
check("same hop count as its trace: skipped", C([node("!00000001", 2)], done, ME, since, {}, NOW), [])
check("hop count changed: qualifies again", C([node("!00000001", 3)], done, ME, since, {}, NOW), ["!00000001"])
legacy = {"!00000001": {"state": "done", "route": ["!0000cafe"]}}
check("trace from disk without hops counts its route",
      (C([node("!00000001", 1)], legacy, ME, since, {}, NOW), C([node("!00000001", 2)], legacy, ME, since, {}, NOW)),
      ([], ["!00000001"]))
check("running trace holds it", C([node("!00000001", 2)], {"!00000001": {"state": "running"}}, ME, since, {}, NOW), [])
check("failed trace leaves it a candidate", C([node("!00000001", 2)], {"!00000001": {"state": "failed"}}, ME, since, {}, NOW),
      ["!00000001"])
check("auto-traced within REPEAT_S waits",
      C([node("!00000001", 3)], done, ME, since, {"!00000001": NOW - 100}, NOW, repeat_s=3600), [])
check("after REPEAT_S it may go",
      C([node("!00000001", 3)], done, ME, since, {"!00000001": NOW - 3601}, NOW, repeat_s=3600), ["!00000001"])
check("oldest heard first",
      C([node("!00000001", 2, heard=NOW - 5), node("!00000002", 2, heard=NOW - 50)], {}, ME, since, {}, NOW),
      ["!00000002", "!00000001"])

W = mesh.auto_trace_wait
check("wait: running -> None", W(NOW, NOW - 100, True, [], 6), None)
check("wait: the gap after any trace", W(NOW, NOW - 10, False, [], 6), mesh.TRACE_GAP_S - 10)
check("wait: free -> 0", W(NOW, NOW - 100, False, [], 6), 0.0)
sent = [NOW - 3000 + i for i in range(6)]
check("wait: hour cap reached -> until the oldest ages out", W(NOW, NOW - 100, False, sent, 6), 600.0)
check("wait: sends older than an hour don't count", W(NOW, NOW - 100, False, [NOW - 4000] * 6, 6), 0.0)


# --- the engine, against a fake bridge pipe

class Stdin:
    def __init__(self):
        self.lines = []

    def write(self, b):
        self.lines.append(json.loads(b))

    async def drain(self):
        pass


class Station:
    def __init__(self):
        self.sent = []

    def _broadcast(self, payload):
        self.sent.append(json.loads(payload))

    async def _send(self, ws, payload):
        self.sent.append(json.loads(payload))

    def notice(self, *a, **kw):
        pass


def engine(auto, per_hour=6):
    args = SimpleNamespace(mesh_python=sys.executable, mesh_bridge="x", mesh_port="/dev/null", mesh_key=[],
                           mesh_log=None, mesh_auto_trace=auto, mesh_auto_trace_per_hour=per_hour)
    e = mesh.MeshEngine(Station(), args)
    e.proc = SimpleNamespace(stdin=Stdin(), returncode=None)
    e._on_bridge({"ev": "info", "me": {"id": ME}, "channels": [], "preset": None, "missing": []})
    e._on_bridge({"ev": "status", "connected": True, "error": None})
    return e


def traces_sent(e):
    return [c["to"] for c in e.proc.stdin.lines if c["cmd"] == "trace"]


async def settle(e, t):
    """Let the engine's timers run for T seconds."""
    await asyncio.sleep(t)


async def main():
    mesh.TRACE_GAP_S = 0.2                    # the firmware's 30 s, shrunk for the run
    now = time.time()
    many = [node(f"!0000000{i}", 2, heard=now + 1) for i in range(1, 6)]

    e = engine(auto=False)
    e._on_bridge({"ev": "nodes", "nodes": many})
    await settle(e, 0.6)
    check("off by default: nothing sent", traces_sent(e), [])

    e = engine(auto=True, per_hour=2)
    e._on_bridge({"ev": "nodes", "nodes": many})
    await settle(e, 0.1)
    check("on: the first goes at once", traces_sent(e), ["!00000001"])
    rec = e.traces["!00000001"]
    check("marked auto, with its hop count", (rec.get("auto"), rec.get("hops")), (True, 2))
    ref = e.proc.stdin.lines[-1]["ref"]
    e._on_bridge({"ev": "trace", "ref": ref, "to": "!00000001", "ok": True, "route": ["!0000cafe", "!0000beef"]})
    await settle(e, 0.05)
    check("answered fast: the gap from its send holds the next", traces_sent(e), ["!00000001"])
    await settle(e, 0.3)
    check("after the gap, the next", traces_sent(e), ["!00000001", "!00000002"])
    await settle(e, 0.5)
    check("one at a time: nothing while it runs, gap or not", traces_sent(e), ["!00000001", "!00000002"])
    ref = e.proc.stdin.lines[-1]["ref"]
    e._on_bridge({"ev": "trace", "ref": ref, "to": "!00000002", "ok": False, "error": "NO_RESPONSE"})
    await settle(e, 0.6)
    check("hour cap (2) holds the rest", traces_sent(e), ["!00000001", "!00000002"])
    check("cap leaves a timer armed for later", e._auto_timer is not None, True)
    e._on_bridge({"ev": "nodes", "nodes": many})
    await settle(e, 0.3)
    check("a repeat node list adds nothing past the cap", traces_sent(e), ["!00000001", "!00000002"])
    e._on_bridge({"ev": "status", "connected": False, "error": "gone"})
    check("disconnect empties the queue", e._auto_queue, {})
    e.proc.returncode = 0
    await e.stop()

    # a manual trace settles a queued node
    e = engine(auto=True)
    e._trace_t = time.time()                  # a trace just went: the auto one must wait the gap
    e._on_bridge({"ev": "nodes", "nodes": [node("!00000009", 1, heard=time.time() + 1)]})
    e.traces["!00000009"] = {"to": "!00000009", "state": "done", "hops": 1, "route": ["!0000cafe"]}
    await settle(e, 0.5)
    check("a node settled meanwhile is not traced", traces_sent(e), [])
    e._on_bridge({"ev": "nodes", "nodes": [node("!00000009", 2, heard=time.time() + 1)]})
    await settle(e, 0.1)
    check("its hop count changes: traced", traces_sent(e), ["!00000009"])
    e.proc.returncode = 0
    await e.stop()

    # the page's switch
    e = engine(auto=True, per_hour=10)
    many = [node(f"!0000000{i}", 2, heard=time.time() + 1) for i in range(1, 6)]
    e._on_bridge({"ev": "nodes", "nodes": many})
    await settle(e, 0.1)
    check("switch: on, the first goes", traces_sent(e), ["!00000001"])
    check("  the snapshot says on", (e.snapshot()["auto_trace"], e.snapshot()["auto_trace_per_hour"]), (True, 10))
    ref = e.proc.stdin.lines[-1]["ref"]
    e._on_bridge({"ev": "trace", "ref": ref, "to": "!00000001", "ok": True, "route": ["!0000cafe", "!0000beef"]})
    check("  the rest queued, a timer armed", (len(e._auto_queue) > 0, e._auto_timer is not None), (True, True))
    e.station.sent.clear()
    check("switch off: changed", e.set_auto_trace(False), True)
    check("  queue and timer gone", (e._auto_queue, e._auto_timer), ({}, None))
    check("  every page hears it", [x for x in e.station.sent if "auto_trace" in x], [{"t": "mesh", "auto_trace": False}])
    check("  the snapshot says off", e.snapshot()["auto_trace"], False)
    e._on_bridge({"ev": "nodes", "nodes": many})
    await settle(e, 0.6)
    check("  off: nothing more goes out", traces_sent(e), ["!00000001"])
    e._auto_queue["!00000003"] = time.time()  # a timer that fires anyway (armed just before the switch)
    e._auto_fire()
    await settle(e, 0.05)
    check("  a stray timer sends nothing either", traces_sent(e), ["!00000001"])
    check("off again: no change", e.set_auto_trace(False), False)
    check("switch on: changed", e.set_auto_trace(True), True)
    await settle(e, 0.4)
    check("  on: tracing again", len(traces_sent(e)), 2)
    e.proc.returncode = 0
    await e.stop()


asyncio.run(main())

# --- configuration
from addons.mesh import MeshAddon  # noqa: E402

for bad in (0, 31, "6", True, 2.5):
    try:
        MeshAddon(None, {"port": "/dev/null", "auto_trace_per_hour": bad})
        check(f"add-on refuses auto_trace_per_hour={bad!r}", "accepted", "ValueError")
    except ValueError:
        check(f"add-on refuses auto_trace_per_hour={bad!r}", "ValueError", "ValueError")
a = MeshAddon(None, {"port": "/dev/null"})
check("add-on default: off, 6 an hour", (a.args.mesh_auto_trace, a.args.mesh_auto_trace_per_hour), (False, 6))
a = MeshAddon(None, {"port": "/dev/null", "auto_trace": True, "auto_trace_per_hour": 3})
check("add-on passes them on", (a.args.mesh_auto_trace, a.args.mesh_auto_trace_per_hour), (True, 3))

for bad in ("false", 1, None):
    try:
        MeshAddon(None, {"port": "/dev/null", "auto_trace": bad})
        check(f"add-on refuses auto_trace={bad!r}", "accepted", "ValueError")
    except ValueError:
        check(f"add-on refuses auto_trace={bad!r}", "ValueError", "ValueError")

# --- the add-on's switch: mesh_auto, kept across restarts
import tempfile  # noqa: E402
import addons.mesh as mesh_addon  # noqa: E402


class Host:
    def __init__(self):
        self.notes = []

    def notice(self, msg, level="info", ws=None):
        self.notes.append(msg)


def addon(tmp, **kw):
    a = MeshAddon(Host(), {"port": "/dev/null", "label": "MF18", "auto_trace": True,
                           "radios": [{"label": "LF", "port": "/dev/null2"}], **kw})
    a.state_dir = tmp
    calls = []
    a.engine = SimpleNamespace(set_auto_trace=lambda on: calls.append(("", on)))
    a.extra[0]["engine"] = SimpleNamespace(set_auto_trace=lambda on: calls.append(("lf", on)))
    return a, calls


with tempfile.TemporaryDirectory() as tmp:
    a, calls = addon(tmp)
    for bad in ("false", 0, None):
        asyncio.run(a.command(None, "mesh_auto", {"on": bad}))
    check("mesh_auto refuses anything but true/false, and says so", (calls, len(a.host.notes)), ([], 3))
    check("  nothing kept", os.path.exists(os.path.join(tmp, mesh_addon.AUTO_FILE)), False)
    asyncio.run(a.command(None, "mesh_auto", {"on": False}))
    asyncio.run(a.command(None, "mesh_auto", {"on": True, "node": "lf"}))
    check("mesh_auto reaches the picked node", calls, [("", False), ("lf", True)])
    with open(os.path.join(tmp, mesh_addon.AUTO_FILE)) as f:
        check("  kept per node", json.load(f), {"": False, "lf": True})
    check("  and in the args a switch-off and on starts from", (a.args.mesh_auto_trace, a.extra[0]["args"].mesh_auto_trace),
          (False, True))
    b, _ = addon(tmp)                        # a restart: the host file says on for MF18, off for LF
    check("restart, before: the host file's", (b.args.mesh_auto_trace, b.extra[0]["args"].mesh_auto_trace), (True, False))
    b._auto_apply(b._auto_saved())
    check("  after: the switch's choice wins", (b.args.mesh_auto_trace, b.extra[0]["args"].mesh_auto_trace), (False, True))
    with open(os.path.join(tmp, mesh_addon.AUTO_FILE), "w") as f:
        f.write("{not json")
    c, _ = addon(tmp)
    c.extra[0]["args"].mesh_auto_trace = True
    c._auto_apply(c._auto_saved())
    check("an unreadable kept file: every node starts off", (c.args.mesh_auto_trace, c.extra[0]["args"].mesh_auto_trace),
          (False, False))
    d, _ = addon(None)
    d.state_dir = None
    d._auto_apply(d._auto_saved())
    check("no state directory: the host file's", d.args.mesh_auto_trace, True)

print(f"\n{fails} failed" if fails else "\nall passed")
sys.exit(1 if fails else 0)
