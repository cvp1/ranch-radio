"""mesh.keep_nodes and the engine around it: nodes the radio no longer lists
stay on the station, without a node or a server.

Seen live 2026-11-22: a node's own list holds 80 (the firmware's cap on an
nRF52) and the bridge starts again from it at every connect, so the tab
showed 90 nodes "known" on a station that had heard 380.

  - a node that leaves the radio's list is kept as it last was, marked "kept";
    one that is back in the list leaves kept; nothing changed: nothing to send
  - our own node, a node the operator removed and a node without an id are
    never kept
  - past the cap the longest unheard go, and a node dropped in the same fold
    is not reported as added
  - the engine: the pages are told what was added and dropped; the snapshot
    carries the kept nodes
  - a kept node heard again (a packet, before the radio lists it) gets its
    last heard, hops and (heard directly) SNR, and the pages are told
  - Remove: a listed node removed by hand is not kept when it goes; a kept
    node is forgotten at once, with the radio connected or not
  - the file: written (kept nodes and the radio's list together, never our own
    node), read back as kept, and the listed ones leave kept at the next list;
    a file that is not JSON is said and ignored; stopping writes what is
    pending

    python3 tests/mesh_kept_nodes_test.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mesh  # noqa: E402

FAILS = 0


def check(name, got, want):
    global FAILS
    ok = got == want
    FAILS += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


def node(nid, short, t=None, **more):
    return {"num": int(nid[1:], 16) if nid else None, "id": nid, "shortName": short, "longName": f"{short} long",
            "hwModel": "HELTEC_V4", "role": "CLIENT", "last_heard": t, "snr": 5.5, "hops": 2, "lat": 32.0,
            "lon": -110.0, "alt": 1300, "mqtt": False, "licensed": False, "battery": 80, "fav": False, **more}


def listed(nodes):
    return {n["id"]: n for n in nodes if n.get("id")}


A, B, C, ME = "!0000000a", "!0000000b", "!0000000c", "!000000ee"

# --- keep_nodes
kept = {}
first = [node(ME, "ME", 100, me=True), node(A, "AAA", 100), node(B, "BBB", 200)]
check("the first list: nothing to keep", mesh.keep_nodes(kept, {}, first), ({}, []))
added, dropped = mesh.keep_nodes(kept, listed(first), [first[0], first[2]])
check("a node that left the list is kept", sorted(kept), [A])
check("  as it last was, marked, without the radio's live fields",
      kept[A], {"num": 10, "id": A, "shortName": "AAA", "longName": "AAA long", "hwModel": "HELTEC_V4", "role": "CLIENT",
                "last_heard": 100, "snr": 5.5, "hops": 2, "lat": 32.0, "lon": -110.0, "alt": 1300, "mqtt": False,
                "licensed": False, "kept": True})
check("  and reported as added", (sorted(added), dropped), ([A], []))
check("the same list again: nothing changed", mesh.keep_nodes(kept, listed([first[0], first[2]]), [first[0], first[2]]), ({}, []))
added, dropped = mesh.keep_nodes(kept, listed([first[0], first[2]]), first)
check("back in the list: it leaves kept", (kept, added, dropped), ({}, {}, [A]))
kept = {}
mesh.keep_nodes(kept, listed(first), [])
check("our own node is never kept", sorted(kept), [A, B])
kept = {}
mesh.keep_nodes(kept, listed(first + [node(None, "NOID", 50), {"id": "junk", "shortName": "J"}]), [first[0]], forget={B})
check("nor a node the operator removed, nor one without a node id", sorted(kept), [A])
kept = {A: mesh.kept_record(node(A, "AAA", 100))}
added, dropped = mesh.keep_nodes(kept, {}, [], forget={A})
check("a kept node the operator removed goes", (kept, dropped), ({}, [A]))

kept = {A: mesh.kept_record(node(A, "AAA", 300)), B: mesh.kept_record(node(B, "BBB", 100))}
added, dropped = mesh.keep_nodes(kept, listed([node(C, "CCC", 200)]), [], keep=2)
check("past the cap the longest unheard goes", sorted(kept), [A, C])
check("  reported as dropped; the new one as added", (sorted(added), dropped), ([C], [B]))
kept = {A: mesh.kept_record(node(A, "AAA", 300)), B: mesh.kept_record(node(B, "BBB", 200))}
added, dropped = mesh.keep_nodes(kept, listed([node(C, "CCC", 100)]), [], keep=2)
check("a node dropped in the same fold is not reported at all", (sorted(kept), added, dropped), ([A, B], {}, []))


# --- the engine
class Station:
    def __init__(self):
        self.notices, self.out = [], []

    def notice(self, msg, level="info", ws=None):
        self.notices.append((level, msg))

    def _broadcast(self, payload):
        self.out.append(json.loads(payload))

    async def _send(self, ws, payload):
        self.out.append(json.loads(payload))


class Stdin:
    def __init__(self):
        self.cmds = []

    def write(self, b):
        self.cmds.append(json.loads(b))

    async def drain(self):
        pass


def engine(log=None):
    args = SimpleNamespace(mesh_python="py", mesh_bridge="b.py", mesh_port="x", mesh_key=[], mesh_log=log)
    e = mesh.MeshEngine(Station(), args)
    e.proc = SimpleNamespace(stdin=Stdin(), returncode=None)
    e.connected = True
    e.info = {"me": {"id": ME}, "channels": [{"index": 0, "name": "LongFast"}]}
    return e


def sent(e, key):
    out = [m[key] for m in e.station.out if key in m]
    e.station.out.clear()
    return out


async def main():
    tmp = tempfile.mkdtemp(prefix="meshkept-")
    log = os.path.join(tmp, "mesh.jsonl")
    path = os.path.join(tmp, "mesh-nodes.json")
    mesh.KEPT_SAVE_S = 0.05
    e = engine(log)
    check("the file sits beside the message log", e.kept_path, path)
    e._on_bridge({"ev": "nodes", "nodes": first})
    check("engine, the first list: the pages are told nothing about kept nodes", sent(e, "kept"), [])
    e._on_bridge({"ev": "nodes", "nodes": [first[0], first[2]]})
    check("a node leaves the list: the pages get it as kept", sent(e, "kept"), [{"set": [mesh.kept_record(first[1])], "drop": []}])
    check("  the snapshot carries it", e.snapshot()["kept"], [mesh.kept_record(first[1])])
    check("  the radio's own list is as the radio sent it", [n["id"] for n in e.snapshot()["nodes"]], [ME, B])

    e._on_bridge({"ev": "rx", "from": A, "port": "POSITION_APP", "hops": 0, "snr": 9.25})
    got = sent(e, "kept")
    check("a kept node heard again: its hops and SNR are now", [(k["set"][0]["hops"], k["set"][0]["snr"]) for k in got], [(0, 9.25)])
    check("  and its last heard", e.kept[A]["last_heard"] > 100, True)
    e._on_bridge({"ev": "rx", "from": A, "port": "POSITION_APP", "hops": 3, "snr": -12.0})
    check("  heard through others: the hops, not their SNR", (e.kept[A]["hops"], e.kept[A]["snr"]), (3, 9.25))
    sent(e, "kept")
    e._on_bridge({"ev": "rx", "from": B, "port": "POSITION_APP", "hops": 1, "snr": 1.0})
    check("a packet from a listed node says nothing about kept nodes", sent(e, "kept"), [])

    await asyncio.sleep(0.2)
    d = json.load(open(path))
    check("the file: the kept node and the radio's list, never our own node", sorted(d), [A, B])
    check("  written for its owner only", oct(os.stat(path).st_mode & 0o777), "0o600")

    await e.node_action(None, B, "remove")
    check("Remove on a listed node goes to the radio", [c["do"] for c in e.proc.stdin.cmds], ["remove"])
    e._on_bridge({"ev": "nodes", "nodes": [first[0]]})
    check("  and it is not kept when the radio drops it", sorted(e.kept), [A])
    e._on_bridge({"ev": "nodes", "nodes": [first[0], first[2]]})
    e._on_bridge({"ev": "nodes", "nodes": [first[0]]})
    check("  heard again and gone by itself later: kept like any other", sorted(e.kept), [A, B])
    sent(e, "kept")
    e.connected = False
    await e.node_action(None, A, "remove")
    check("Remove on a kept node forgets it, the radio connected or not", (sorted(e.kept), sent(e, "kept")),
          ([B], [{"set": [], "drop": [A]}]))
    check("  nothing more went to the radio", len(e.proc.stdin.cmds), 1)
    await asyncio.sleep(0.2)
    check("  the file follows", sorted(json.load(open(path))), [B])

    # A restart: everything in the file is kept until the radio lists it again.
    with open(path, "w") as f:
        json.dump({A: mesh.kept_record(first[1]), B: mesh.kept_record(first[2]), "junk": {}, C: "not a record"}, f)
    e2 = engine(log)
    e2._load()
    check("after a restart the file's nodes are kept", sorted(e2.kept), [A, B])
    check("  and shown before the radio answers", [n["id"] for n in e2.snapshot()["kept"]], [A, B])
    e2._on_bridge({"ev": "nodes", "nodes": [first[0], first[2]]})
    check("  the ones the radio lists leave kept", (sorted(e2.kept), sent(e2, "kept")), ([A], [{"set": [], "drop": [B]}]))
    e2.proc = None                # stop() with no bridge to close: only the pending write matters here
    await e2.stop()

    with open(path, "w") as f:
        f.write("{not json")
    said = []
    h = logging.Handler()
    h.emit = lambda r: said.append(r.getMessage())
    mesh.log.addHandler(h)
    e3 = engine(log)
    e3._load()
    mesh.log.removeHandler(h)
    check("a file that is not JSON: nothing kept", e3.kept, {})
    check("  and said", any("not read" in x for x in said), True)

    e4 = engine(None)
    e4._on_bridge({"ev": "nodes", "nodes": first})
    e4._on_bridge({"ev": "nodes", "nodes": [first[0]]})
    check("no log: kept in memory, no file", (sorted(e4.kept), e4.kept_path), ([A, B], None))
    # stop() writes what is pending: nothing waits KEPT_SAVE_S for a restart
    mesh.KEPT_SAVE_S = 60
    e.proc = None
    e._on_bridge({"ev": "nodes", "nodes": [first[0], first[2]]})
    e._on_bridge({"ev": "nodes", "nodes": [first[0], first[1]]})
    await e.stop()
    check("stop writes what was pending", sorted(json.load(open(path))), [A, B])


asyncio.run(main())
print("\nall passed" if not FAILS else f"\n{FAILS} FAILED")
sys.exit(1 if FAILS else 0)
