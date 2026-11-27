"""The HaLow add-on's OpenMANET side (halow_mesh.py, addons/halow.py read_mesh)
against fake nodes (tests/fake_halow_ssh.py answering each by name):

  - a node's read is parsed: its mesh MAC, neighbours with the dBm it hears
    each at, paths with next hop and hop count; a driver's 0 dBm is no
    reading; something that isn't an OpenMANET node parses to None
  - the views put together: a neighbour read on both ends is "both", with both
    numbers; one only the gate hears is "out"; one only heard of through a
    path has its hops and next hop and no direction; each link once, with the
    sides that were reported; the gate's own "heard by", strongest first
  - the add-on reads the gate direct and each named node through the gate (a
    ProxyCommand naming the gate and the same key), at once; the mesh's own
    key (mesh.key: OpenMANET's dropbear takes RSA only), not the bridge's
  - a named node that doesn't answer is listed as unread, with why; the mesh
    still shows it from the others' views
  - a node leaving the mesh is noticed once; the seen list keeps it
    (halow-mesh-seen.json) and is read back on start
  - the gate not answering: the error shows, one notice; the last view is kept
  - bad mesh blocks don't load: no gate, nodes not a list, too many nodes

    python3 tests/halow_mesh_test.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import halow_mesh as M  # noqa: E402
from addons import Addons  # noqa: E402
from addons import halow as halow_mod  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


G, N2, N3, N4 = "02:00:00:00:0a:01", "02:00:00:00:0a:02", "02:00:00:00:0a:03", "02:00:00:00:0a:04"


def node(mac, host, neighbors, paths=()):
    topo = {"nodeinfo": {"macaddress": mac.upper(), "hostname": host, "mesh_peer_count": len(neighbors)},
            "neighbors": [{"mac": m, "signal": s} for m, s in neighbors] or None,
            "paths": [{"destination": d, "next_hop": h, "hop_count": c} for d, h, c in paths] or None}
    board = {"hostname": host, "model": "Heltec HT-HD01 V2", "release": {"description": "OpenMANET 24.10 1.8.1"}}
    return f"@@board\n{json.dumps(board, indent=1)}\n@@topo\n{json.dumps(topo, indent=1)}\n@@end\n"


# The gate hears node2 and, faintly, node4 (which nobody reads); node3 is two
# hops out, through node2. node2 hears the gate and node3.
GATE = node(G, "ranch01", [(N2, -41), (N4, -80)], [(N3, N2, 2)])
NODE2 = node(N2, "node2", [(G, -39), (N3, -70)])

# --- parsing
r = M.parse_mesh_read(GATE)
check("parse: the node's mesh MAC (lower case) and name", (r["mac"], r["hostname"], r["model"]), (G, "ranch01", "Heltec HT-HD01 V2"))
check("parse: neighbours with the dBm it hears each at", r["neighbors"], [{"mac": N2, "signal": -41}, {"mac": N4, "signal": -80}])
check("parse: paths with next hop and hop count", r["paths"], [{"dest": N3, "next_hop": N2, "hops": 2}])
check("parse: a driver's 0 dBm is no reading", M.parse_mesh_read(node(G, "x", [(N2, 0)]))["neighbors"], [{"mac": N2, "signal": None}])
check("parse: no mesh11s answer is not a node", M.parse_mesh_read("@@board\n{}\n@@topo\n\n@@end\n"), None)
check("parse: a cut-off read is not a node", M.parse_mesh_read(GATE.replace("@@end\n", "")), None)
check("parse: an empty mesh (no peers: null lists) is a node with none",
      (M.parse_mesh_read(node(G, "ranch01", []))["neighbors"], M.parse_mesh_read(node(G, "ranch01", []))["paths"]), ([], []))

# --- the views put together
v = M.mesh_view(G, {G: M.parse_mesh_read(GATE), N2: M.parse_mesh_read(NODE2)})
by = {n["mac"]: n for n in v["nodes"]}
check("view: every node any view mentions", sorted(by), [G, N2, N3, N4])
check("view: node2, read on both ends, is both ways with both numbers",
      (by[N2]["dir"], by[N2]["gate_hears"], by[N2]["hears_gate"], by[N2]["hops"]), ("both", -41, -39, 1))
check("view: node4, heard only by the gate, is out; it hears the gate is not claimed",
      (by[N4]["dir"], by[N4]["gate_hears"], "hears_gate" in by[N4], by[N4]["read"]), ("out", -80, False, False))
check("view: node3, known through a path, has hops and next hop, no direction to the gate",
      (by[N3]["hops"], by[N3]["via"], by[N3]["dir"]), (2, N2, ""))
links = {(x["a"], x["b"]): {k: x[k] for k in ("ab", "ba") if k in x} for x in v["links"]}
check("view: each link once, with the sides reported",
      links, {(G, N2): {"ab": -41, "ba": -39}, (G, N4): {"ab": -80}, (N2, N3): {"ab": -70}})
check("view: the gate is heard by node2 at -39", v["heard_by"], [{"mac": N2, "signal": -39}])
check("view: no gate, no mesh", M.mesh_view(None, {}), {"gate": None, "nodes": [], "links": []})


# --- the add-on, against fake nodes
class Host:
    def __init__(self):
        self.notices, self.sent = [], []

    def notice(self, msg, level="info", ws=None):
        self.notices.append((level, msg))

    def _broadcast(self, payload):
        self.sent.append(json.loads(payload))

    async def _send(self, ws, payload):
        self.sent.append(json.loads(payload))


tmp = Path(tempfile.mkdtemp(prefix="halow-mesh-test-"))
os.environ["FAKE_HALOW_DIR"] = str(tmp)
(tmp / "read.txt").write_text("")
(tmp / "mesh-192.168.1.197.txt").write_text(GATE)
(tmp / "mesh-node2.txt").write_text(NODE2)
fake = str(ROOT / "tests" / "fake_halow_ssh.py")
os.chmod(fake, 0o755)
BLOCK = {"enabled": True, "host": "192.168.1.65", "key": str(tmp / "key"), "ssh": fake, "poll": 3600,
         "mesh": {"gate": "192.168.1.197", "nodes": ["node2", "node3"], "key": str(tmp / "mesh_rsa")}}

for bad, field in (({"host": "h", "mesh": {"nodes": []}}, "mesh.gate"),
                   ({"host": "h", "mesh": {"gate": "g", "nodes": "node2"}}, "mesh.nodes"),
                   ({"host": "h", "mesh": {"gate": "g", "nodes": [f"n{i}" for i in range(M.MAX_NODES + 1)]}}, "mesh.nodes"),
                   ({"host": "h", "mesh": {"gate": "g", "nodes": ["a;b"]}}, "mesh.nodes")):
    reg = Addons(Host(), {"halow": bad}, state_path=None)
    err = reg.items["halow"]["error"] or ""
    check(f"a bad mesh block doesn't load, naming {field}", (err.startswith("not loaded"), field in err), (True, True))

host = Host()
a = halow_mod.Halow(host, BLOCK)
a.state_dir = str(tmp / "state")
check("no mesh block: no mesh in the snapshot", halow_mod.Halow(Host(), {**BLOCK, "mesh": None}).snapshot()["mesh"], None)


def mesh_log():
    p = tmp / "mesh.log"
    rows = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
    p.unlink(missing_ok=True)
    return rows


async def main():
    await a.read_mesh()
    s = a.snapshot()["mesh"]
    rows = mesh_log()
    log = {r["target"]: r["proxy"] for r in rows}
    check("the mesh's own key on every read (OpenMANET's dropbear takes RSA only)",
          sorted({r["key"] for r in rows}), [str(tmp / "mesh_rsa")])
    check("the gate is read direct", log.get("root@192.168.1.197"), None)
    check("each named node is read through the gate, with the same key and known hosts",
          all(p and "root@192.168.1.197" in p and "-W %h:%p" in p and str(tmp / "mesh_rsa") in p and "halow_known_hosts" in p
              for t, p in log.items() if t != "root@192.168.1.197"), True)
    check("the mesh's own key is used, on both hops, never the bridge's",
          all(str(tmp / "mesh_rsa") in p and str(tmp / "key") + " " not in p for t, p in log.items() if p), True)
    check("  both named nodes were asked", sorted(log), ["root@192.168.1.197", "root@node2", "root@node3"])
    check("the mesh view in the snapshot", (s["gate"], s["error"], sorted(n["mac"] for n in s["nodes"])), (G, None, [G, N2, N3, N4]))
    check("a named node that doesn't answer is listed as unread, with why",
          (list(s["unread"]), "No route to host" in s["unread"]["node3"]), (["node3"], True))
    check("  and still shown from the others' views", [n["hops"] for n in s["nodes"] if n["mac"] == N3], [2])
    check("a node read is named as the host file names it", [n["name"] for n in s["nodes"] if n["mac"] == N2], ["node2"])
    check("no notices on the first read", host.notices, [])
    check("the seen list holds every node in the mesh", sorted(r["mac"] for r in s["seen"]), [G, N2, N3, N4])

    # node2 drops off: the gate no longer hears it, and it doesn't answer
    (tmp / "mesh-192.168.1.197.txt").write_text(node(G, "ranch01", [(N4, -80)]))
    (tmp / "mesh-node2.txt").unlink()
    await a.read_mesh()
    await a.read_mesh()
    left = [m for _, m in host.notices if "left the mesh" in m]
    check("a node leaving is noticed once each, by name", sorted(left),
          ["HaLow mesh: 02:00:00:00:0a:03 left the mesh.", "HaLow mesh: node2 left the mesh."])
    s = a.snapshot()["mesh"]
    check("  it stays in the seen list, not live", [(r["mac"], r["live"]) for r in s["seen"] if r["mac"] == N2], [(N2, False)])
    b = halow_mod.Halow(Host(), BLOCK)
    b.state_dir = a.state_dir
    b._load_mesh_seen()
    check("the seen list is read back on start", sorted(b.mesh_seen), [G, N2, N3, N4])

    # the gate stops answering
    (tmp / "mesh-192.168.1.197.txt").unlink()
    host.notices.clear()
    await a.read_mesh()
    await a.read_mesh()
    s = a.snapshot()["mesh"]
    check("gate down: the error shows, the last view is kept",
          ("can't read the mesh gate" in (s["error"] or ""), sorted(n["mac"] for n in s["nodes"])), (True, [G, N4]))
    check("  one notice, not one per read", len([m for _, m in host.notices if "mesh gate" in m]), 1)
    (tmp / "mesh-192.168.1.197.txt").write_text(GATE)
    await a.read_mesh()
    check("  and one when it answers again", [m for _, m in host.notices if "answers again" in m],
          ["HaLow mesh: the gate answers again."])


asyncio.run(main())
print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
