"""The OpenMANET side of the HaLow add-on (addons/halow.py): a Wi-Fi HaLow
mesh (802.11s with batman-adv, OpenMANET firmware) read node by node.

Each OpenMANET node answers `ubus call mesh11s get_topology` with its own
view: its mesh MAC, its direct neighbours with the signal it hears each at
(dBm), and the paths to nodes further out (destination, next hop, hop
count). That is how OpenMANET's own topology page draws the mesh. One node's
view says only what IT hears; how a neighbour hears it is in the neighbour's
view. So the add-on reads the gate (on the LAN) and every node named in the
host file (through the gate: they live on the mesh's own subnet behind it),
and mesh_view() puts the views together: each link with both directions
where both ends were read, one where only one was. Read-only: nothing here
changes a node.
"""
import json
import re

import halow_ap

# One read per node: who it is, and what it hears. ubus answers as root on the
# box itself (the web API's ACL, which refuses batctl and iw, isn't in the way).
MESH_READ = "; ".join([
    "echo @@board", "ubus call system board 2>/dev/null",
    "echo @@topo", "ubus call mesh11s get_topology 2>/dev/null",
    "echo @@end",
])
MAX_NODES = 16          # nodes named in the host file; a mesh larger than this is read from the gate's view only


def _json(lines):
    try:
        v = json.loads("\n".join(lines) or "null")
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def _mac(v):
    m = str(v or "").lower()
    return m if halow_ap.MAC.match(m) else None


def _dbm(v):
    """A signal as reported, or None. 0 is what a driver says when it has no reading."""
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return n if -130 <= n < 0 else None


def parse_mesh_read(text):
    """MESH_READ's output -> {mac, hostname, model, release, peer_count,
    neighbors: [{mac, signal}], paths: [{dest, next_hop, hops}]}, or None
    when it isn't an OpenMANET node answering (no mesh11s, no MAC)."""
    sec = halow_ap.sections(text)
    if "end" not in sec:
        return None
    topo, board = _json(sec.get("topo", [])) or {}, _json(sec.get("board", [])) or {}
    info = topo.get("nodeinfo") or {}
    mac = _mac(info.get("macaddress"))
    if not mac:
        return None
    neighbors = []
    for n in topo.get("neighbors") or []:
        m = _mac((n or {}).get("mac"))
        if m and m != mac:
            neighbors.append({"mac": m, "signal": _dbm(n.get("signal"))})
    paths = []
    for p in topo.get("paths") or []:
        d, h = _mac((p or {}).get("destination")), _mac((p or {}).get("next_hop"))
        if d and h and d != mac:
            try:
                hops = int(p.get("hop_count"))
            except (TypeError, ValueError):
                hops = None
            paths.append({"dest": d, "next_hop": h, "hops": hops})
    release = board.get("release") if isinstance(board.get("release"), dict) else {}
    try:
        peers = int(info.get("mesh_peer_count"))
    except (TypeError, ValueError):
        peers = len(neighbors)
    return {"mac": mac, "hostname": str(info.get("hostname") or board.get("hostname") or ""),
            "model": str(board.get("model") or ""), "release": str(release.get("description") or ""),
            "peer_count": peers, "neighbors": neighbors, "paths": paths}


def mesh_view(gate, reads):
    """The gate's read and every other node's read (mac -> parse_mesh_read)
    -> {"nodes": [...], "links": [...], "gate": mac}.

    A node: {mac, hostname, read (its own view came back), hops (0 the gate,
    1 a neighbour of the gate, more from the gate's paths, None unknown),
    via (the gate's next hop to it), hears_gate / gate_hears (dBm, None when
    that side gave no number; the key is absent when that side says nothing),
    dir ("both", "in" it hears the gate, "out" the gate hears it, "")}.

    A link: {a, b, ab, ba}: ab is the dBm a hears b at (absent when a's view,
    or a's read, doesn't have b). Every pair any read node reports, once."""
    if not gate:
        return {"gate": None, "nodes": [], "links": []}
    hears = {}                                   # (a, b) -> dBm a hears b at
    for a, r in reads.items():
        for n in r.get("neighbors", []):
            hears[(a, n["mac"])] = n["signal"]
    g = reads.get(gate) or {}
    paths = {p["dest"]: p for p in g.get("paths", [])}
    macs = {gate} | set(reads) | {b for _, b in hears} | set(paths) | {p["next_hop"] for p in paths.values()}
    nodes = []
    for m in sorted(macs):
        r = reads.get(m) or {}
        node = {"mac": m, "hostname": r.get("hostname", ""), "model": r.get("model", ""), "release": r.get("release", ""),
                "read": m in reads, "hops": 0 if m == gate else 1 if (gate, m) in hears or (m, gate) in hears
                else paths[m]["hops"] if m in paths else None,
                "via": paths[m]["next_hop"] if m in paths and paths[m]["next_hop"] != m else None}
        if m != gate:
            if (gate, m) in hears:
                node["gate_hears"] = hears[(gate, m)]
            if (m, gate) in hears:
                node["hears_gate"] = hears[(m, gate)]
            out, inn = "gate_hears" in node, "hears_gate" in node
            node["dir"] = "both" if out and inn else "out" if out else "in" if inn else ""
        nodes.append(node)
    links, done = [], set()
    for (a, b) in hears:
        k = tuple(sorted((a, b)))
        if k in done:
            continue
        done.add(k)
        x, y = k
        link = {"a": x, "b": y}
        if (x, y) in hears:
            link["ab"] = hears[(x, y)]
        if (y, x) in hears:
            link["ba"] = hears[(y, x)]
        links.append(link)
    heard_by = sorted(((n["mac"], n["hears_gate"]) for n in nodes if "hears_gate" in n), key=lambda t: -(t[1] or -999))
    return {"gate": gate, "nodes": nodes, "links": links,
            "heard_by": [{"mac": m, "signal": s} for m, s in heard_by]}


HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
