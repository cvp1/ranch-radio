"""Meshtastic on the Radio Remote page: messages, channels and the node list.

The node (a LoRa radio on its own, not the HF transceiver) is reached through
mesh_bridge.py, a child process under the Python that has the `meshtastic`
package. This side:
  - starts the bridge and starts it again when it exits (node unplugged,
    serial error), backing off while the node stays away
  - keeps the last MESSAGES_KEEP messages, in memory and in --mesh-log
    (JSON lines, mode 600: private channels are stored in clear there), so
    a page opened later or a server restart still shows the conversation
  - relays the bridge's events to the pages as {"t": "mesh", ...}
  - checks a page's mesh_send (text length, a channel the node has, a node
    id) before the bridge sees it, and tracks each sent message's ACK
  - shows the node's settings (the bridge's SETTINGS: owner, role, LoRa,
    position, ...) and checks a page's mesh_config / mesh_favorite against
    that schema before the bridge writes them to the node
  - runs one traceroute at a time (mesh_trace), keeps the last result per
    node for every page, and passes "heard a packet from X" on as activity
  - with auto_trace on (off by default: it transmits), traces a node by
    itself when it is heard over relays and has no trace yet or its hop
    count changed since its last one (auto_trace_candidates); queued behind
    TRACE_GAP_S, capped per hour, one per node per AUTO_TRACE_REPEAT_S
  - replies and emoji reactions (mesh_send reply_to / emoji), checked
    against a message it holds
  - asks its own node for its neighbours now and then (SELF_NEIGHBORS_S;
    answered inside the node, nothing on the air), so who it hears is known
  - asks a node for its position, telemetry, user info or neighbours
    (mesh_request), one request in flight per node and each kind at most every
    REQUEST_GAP_S (neighbours: 180 s, the firmware's own limit)
  - ignores, unignores or removes a node in the node's DB (mesh_node) and
    reboots the node (mesh_reboot)
  - keeps each node's NeighborInfo (who it hears, with SNR) for the map, and
    the finished traceroutes, in --mesh-log's -links.json (a restart and a
    new bridge keep them: nodes send NeighborInfo hours apart)
  - works out who hears this node (hears_us): a node whose NeighborInfo lists
    us, the first hop of a traceroute, a node that ACKed our DM
  - sends a message of ours again by itself when the mesh NAKs it for a
    reason it may get over (RESEND_REASONS: no relay heard it, a timeout, the
    duty-cycle limit), RESEND_S later, at most len(RESEND_S) times; a page
    can cancel that (mesh_cancel). auto_resend off turns it off
  - sends a text too long for one packet as up to PARTS_MAX parts
    ("[n/N] ..."), one at a time: the next once the one before is ACKed (or
    its ACK wait runs out), the rest dropped if one finally fails
  - adds up what the node hears for the Health view (mesh_stats.py: traffic
    per node and per minute, this node's channel load over 24 h, findings),
    in memory, sent to the pages at most every HEALTH_EVERY_S

Channel keys stay with the bridge (--mesh-key NAME=PATH): only "the node's
key matches" (key_ok) comes back here and goes to the pages.
"""

import asyncio
import json
import logging
import os
import re
import time
from collections import deque

import mesh_convs
from mesh_stats import MeshStats

log = logging.getLogger("mesh")

MESSAGES_KEEP = 300
TEXT_MAX_BYTES = 200          # Meshtastic's payload is 233 bytes; apps cap text near 200
RESTART_S = (5.0, 15.0, 60.0)  # wait before restarting the bridge, growing while it can't reach the node
COMPACT_AT = 4 * MESSAGES_KEEP  # log records before the file is rewritten to the kept messages
NODE_ID = re.compile(r"^![0-9a-f]{8}$")
# The bridge's longest line: the node list is one JSON line (~450 bytes a
# node). asyncio's default cap is 64 KiB; a node with 179 in its DB sent 81 KB
# and the read died without a word (seen live 2026-11-15).
LINE_MAX = 4 << 20
TRACE_GAP_S = 30.0            # firmware drops traceroutes sent closer together than this
TRACES_KEEP = 50
# A sent message whose node reports neither an ACK nor a failure in this long
# becomes "unknown" (and the page offers Retry). Seen live 2026-11-14 on
# a station: a reaction lost on the air stayed "sent" for good, no NAK ever came.
# A late ACK still settles it. MESH_ACK_WAIT_S overrides (tests).
ACK_WAIT_S = float(os.environ.get("MESH_ACK_WAIT_S", "120"))
REQUEST_KINDS = {"position": "position", "telemetry": "telemetry", "env": "environment",
                 "nodeinfo": "user info", "neighbors": "neighbours"}
REQUEST_GAP_S = 60.0          # each kind to one node at most this often: every request is airtime
# A node answers a NeighborInfo request at most once every 3 minutes
# (NeighborInfoModule::allocReply); asking sooner only spends airtime.
REQUEST_GAP_BY_KIND = {"neighbors": 180.0}
# This node's own NeighborInfo, asked of the node itself (answered inside it,
# nothing on the air) a little after it connects and then this often, so the
# station always holds who its own node hears. Only with the module on. The
# node's 3-minute answer limit counts these too, hence not more often.
SELF_NEIGHBORS_S = float(os.environ.get("MESH_SELF_NEIGHBORS_S", "1800"))
SELF_NEIGHBORS_FIRST_S = float(os.environ.get("MESH_SELF_NEIGHBORS_FIRST_S", "20"))
EMOJI_MAX_BYTES = 16          # a reaction is one emoji (with modifiers), not a message
NODE_ACTIONS = {"ignore": "ignore", "unignore": "unignore", "remove": "remove"}
# Auto traceroute (idea from MeshSense 1.0.18, 2026-11-16, which traces on every
# hop-count change with no cap; a flapping node there floods the channel).
AUTO_TRACE_PER_HOUR = 6       # default cap; the host file's mesh.auto_trace_per_hour overrides (1..30)
AUTO_TRACE_PER_HOUR_MAX = 30
AUTO_TRACE_REPEAT_S = float(os.environ.get("MESH_AUTO_TRACE_REPEAT_S", "3600"))  # one node, at most this often
AUTO_TRACE_QUEUE = 20         # waiting nodes; more are dropped until the queue drains
# Automatic resend (idea from Mesh-Client 6.x, 2026-11-17: it resends after 15 s,
# 1 min and 3 min). Only for a NAK the mesh may get over; the firmware's own
# retransmissions sit under each try. MESH_RESEND_S overrides (tests).
RESEND_S = tuple(float(x) for x in os.environ.get("MESH_RESEND_S", "15,60,180").split(","))
RESEND_REASONS = {"MAX_RETRANSMIT", "TIMEOUT", "NO_RESPONSE", "NO_ROUTE", "DUTY_CYCLE_LIMIT",
                  "RATE_LIMIT_EXCEEDED"}
# A long text goes as parts "[n/N] ...", each one packet (the convention
# Meshtastic apps read back). More than PARTS_MAX is refused: nine packets of
# LongFast airtime is already a lot to ask of a shared channel.
PARTS_MAX = 9
PART_PREFIX_BYTES = len("[9/9] ")
PART_GAP_S = float(os.environ.get("MESH_PART_GAP_S", "2"))   # between one part's ACK and the next
HEALTH_EVERY_S = float(os.environ.get("MESH_HEALTH_EVERY_S", "10"))   # the Health view's updates, at most
RX_FIELDS = ("port", "hops", "hop_start", "snr", "rssi", "mqtt", "relay", "ch", "to_me", "size")
LINKS_KEEP_S = 7 * 86400      # NeighborInfo and traceroutes older than this are dropped
NEIGHBORS_KEEP = 300          # nodes' NeighborInfo kept, newest first
# Every name the node has ever told us, kept after the node drops it from its
# NodeDB: messages, NeighborInfo and traceroutes name nodes long after that, and
# showed bare "!id"s for ~60 of them (a live station, 2026-11-19). Newest first, capped.
NAMES_KEEP = int(os.environ.get("MESH_NAMES_KEEP", "3000"))


def remember_names(book, nodes, now=None, keep=NAMES_KEEP):
    """Fold the node list's names into book {id: {"short", "long", "hw", "t"}}
    in place. -> {id: rec} of the entries that changed (empty: nothing to save
    or send). A name only changes what is kept when it does; t is when we last
    saw the node with that name (its last_heard if it has one). The oldest go
    first past keep."""
    now = time.time() if now is None else now
    changed = {}
    for n in nodes or []:
        nid = n.get("id")
        short, long_ = n.get("shortName"), n.get("longName")
        if not nid or not NODE_ID.match(str(nid)) or not (short or long_):
            continue
        rec = {"short": short or "", "long": long_ or "", "hw": n.get("hwModel") or "",
               "t": n.get("last_heard") or now}
        old = book.get(nid)
        if old and all(old.get(k) == rec[k] for k in ("short", "long", "hw")):
            continue
        if old and rec["t"] < (old.get("t") or 0):
            rec["t"] = old["t"]
        book[nid] = changed[nid] = rec
    if len(book) > keep:
        for nid, _ in sorted(book.items(), key=lambda x: x[1].get("t") or 0)[:len(book) - keep]:
            del book[nid]
            changed.pop(nid, None)
    return changed


# Every node the radio has listed, kept after the radio drops it. A node's own
# list is short (80 on an nRF52: the firmware's MAX_NUM_NODES), and the bridge's
# starts again from it at every connect, so the tab's "N known" fell back to
# 80-odd however many the station had heard: one node here had 380 names in the
# book above and 90 nodes listed (2026-11-22). A phone app keeps its own list;
# so does the station now. The longest unheard go first past the cap.
KEPT_NODES = int(os.environ.get("MESH_KEPT_NODES", "1000"))
KEPT_SAVE_S = float(os.environ.get("MESH_KEPT_SAVE_S", "300"))   # the file is written at most this often
KEPT_FIELDS = ("num", "id", "shortName", "longName", "hwModel", "role", "last_heard", "snr", "hops",
               "lat", "lon", "alt", "mqtt", "licensed")


def kept_record(n):
    """A node as the station keeps it: what the list and the card show, marked."""
    return {**{k: n.get(k) for k in KEPT_FIELDS}, "kept": True}


def keep_nodes(kept, last, nodes, forget=(), keep=KEPT_NODES):
    """Fold the radio's node list into kept {id: record}, in place. A node in
    `last` ({id: node} of the list before this one) that is not in `nodes` has
    left the radio's list: it is kept as it last was. One that is in `nodes`
    again leaves kept. Our own node and the ids in `forget` (removed by the
    operator) are never kept. -> (added {id: record}, dropped [id]): what the
    pages need; both empty when nothing changed."""
    now_ids = {n.get("id") for n in nodes or []}
    added, dropped = {}, []
    for nid in [k for k in kept if k in now_ids or k in forget]:
        del kept[nid]
        dropped.append(nid)
    for nid, n in last.items():
        if nid in now_ids or nid in forget or n.get("me") or not NODE_ID.match(str(nid)):
            continue
        kept[nid] = added[nid] = kept_record(n)
    if len(kept) > keep:
        for nid, _ in sorted(kept.items(), key=lambda x: x[1].get("last_heard") or 0)[:len(kept) - keep]:
            del kept[nid]
            if added.pop(nid, None) is None:
                dropped.append(nid)
    return added, dropped


def hears_us(me, neighbors, traces, messages, now=None):
    """Who hears this node, from what the station already holds; no airtime.
    -> [{"id", "t", "snr", "how"}], newest first. how: "neighbor" (its
    NeighborInfo lists us; snr is how it hears us), "trace" (first hop of a
    traceroute; snr_towards[0]), "dm" (it ACKed our DM: it got it, maybe
    through relays; no snr). A node keeps its newest evidence of each kind.
    Nothing here comes from a rebroadcast of ours: the firmware drops that
    as a duplicate and tells the phone only "implicit ACK", never who."""
    if not me:
        return []
    now = time.time() if now is None else now
    best = {}

    def note(nid, how, t, snr=None):
        if not nid or nid == me or nid == "?" or not t or now - t > LINKS_KEEP_S:
            return
        key = (nid, how)
        if key not in best or t > best[key]["t"]:
            best[key] = {"id": nid, "t": t, "snr": snr, "how": how}

    for nid, rec in (neighbors or {}).items():
        for x in rec.get("neighbors") or []:
            if x.get("id") == me:
                note(nid, "neighbor", rec.get("t"), x.get("snr"))
    for tr in (traces or {}).values():
        if tr.get("state") == "done":
            first = (tr.get("route") or [tr.get("to")])[0]
            snr = (tr.get("snr_towards") or [None])[0]
            note(first, "trace", tr.get("done_t"), snr)
    for m in messages or []:
        if m.get("mine") and m.get("dm") and m.get("ack") == "delivered":
            note(m.get("to"), "dm", m.get("t"))
    return sorted(best.values(), key=lambda r: -r["t"])


def split_text(text, limit=TEXT_MAX_BYTES, parts_max=PARTS_MAX):
    """TEXT as the packets to send: [TEXT] when it fits one, else parts of at
    most LIMIT bytes each, "[n/N] " first, split between words (a word longer
    than a part is split between characters, never inside one). None when it
    would take more than PARTS_MAX parts."""
    if len(text.encode()) <= limit:
        return [text]
    room = limit - PART_PREFIX_BYTES
    parts, cur = [], ""
    for w in text.split():
        while len(w.encode()) > room:            # longer than a part on its own
            if cur:
                parts.append(cur)
                cur = ""
            head, size = "", 0
            for ch in w:
                n = len(ch.encode())
                if size + n > room:
                    break
                head, size = head + ch, size + n
            parts.append(head)
            w = w[len(head):]
        joined = f"{cur} {w}" if cur else w
        if len(joined.encode()) <= room:
            cur = joined
        else:
            parts.append(cur)
            cur = w
    if cur:
        parts.append(cur)
    if len(parts) > parts_max:
        return None
    return [f"[{i}/{len(parts)}] {part}" for i, part in enumerate(parts, 1)]


def trace_hops(tr):
    """How many relays a traceroute found towards its node (None if unknown)."""
    if tr.get("hops") is not None:
        return tr["hops"]
    if tr.get("state") == "done" and isinstance(tr.get("route"), list):
        return len(tr["route"])
    return None


def auto_trace_candidates(nodes, traces, me, since, auto_last, now, repeat_s=None):
    """Nodes worth a traceroute of our own accord, oldest-heard first. A node
    qualifies when it was heard since SINCE (this connection: the node DB's
    stale entries never trigger), over at least one relay, not over MQTT, not
    ignored, and either has no finished trace or its hop count now differs
    from the trace's. A node traced automatically in the last REPEAT_S waits."""
    repeat_s = AUTO_TRACE_REPEAT_S if repeat_s is None else repeat_s
    out = []
    for n in nodes or []:
        nid, hops, heard = n.get("id"), n.get("hops"), n.get("last_heard") or 0
        if (not nid or nid == me or n.get("me") or n.get("mqtt") or n.get("ignored")
                or not NODE_ID.match(str(nid)) or not isinstance(hops, int) or hops < 1
                or heard < since):
            continue
        if now - auto_last.get(nid, -1e18) < repeat_s:
            continue
        tr = (traces or {}).get(nid)
        if tr and tr.get("state") == "running":
            continue
        if tr and tr.get("state") == "done" and trace_hops(tr) == hops:
            continue
        out.append((heard, nid))
    return [nid for _, nid in sorted(out)]


def auto_trace_wait(now, trace_t, running, sent, per_hour):
    """Seconds until an automatic trace may go (0: now), or None while one
    is running. SENT is the times of automatic traces in the last hour."""
    if running:
        return None
    wait = TRACE_GAP_S - (now - trace_t)
    recent = sorted(t for t in sent if now - t < 3600)
    if len(recent) >= per_hour:
        wait = max(wait, recent[-per_hour] + 3600 - now)
    return max(0.0, wait)


class MeshEngine:
    def __init__(self, station, args):
        self.station = station
        self.cmd = [os.path.expanduser(args.mesh_python), args.mesh_bridge, args.mesh_port,
                    *(f"--key={k}" for k in args.mesh_key)]
        feed = getattr(args, "mesh_feed", None)
        if feed:                      # the node's traffic on an MQTT broker too (mesh_feed.py)
            self.cmd += [f"--feed={feed}"]
            if getattr(args, "mesh_feed_cred", None):
                self.cmd += [f"--feed-cred={args.mesh_feed_cred}"]
            if getattr(args, "mesh_feed_tx", False):
                self.cmd += ["--feed-tx"]
        self.log_path = os.path.expanduser(args.mesh_log) if args.mesh_log else None
        self.links_path = (os.path.splitext(self.log_path)[0] + "-links.json") if self.log_path else None
        self.names_path = (os.path.splitext(self.log_path)[0] + "-names.json") if self.log_path else None
        self.names = {}               # node id -> {"short", "long", "hw", "t"}: remember_names()
        base = os.path.splitext(self.log_path)[0] if self.log_path else None
        self.kept_path = base and base + "-nodes.json"
        self.kept = {}                # node id -> record: nodes the radio no longer lists (keep_nodes)
        self._listed = {}             # node id -> node: the radio's list as last sent
        self._forget = set()          # ids the operator removed: not kept when they leave the list
        self._kept_timer = None       # the pending write of kept_path
        self.convs = mesh_convs.ConvStore(base and base + "-convs.json")   # archived, muted, pinned, read
        self.trash = mesh_convs.Trash(base and base + "-trash.jsonl")      # deleted, for Undo
        self.proc = None
        self.connected = False
        self.error = None
        self.info = None
        self.config = None            # {"sections": schema, "values": the node's settings}
        self.nodes = []
        self.messages = deque(maxlen=MESSAGES_KEEP)
        self._ref = 0
        self._by_ref = {}             # send ref -> our message record (until the node gives it an id)
        self._writes = {}             # set/favorite ref -> (page, what), until the bridge says done
        self.traces = {}              # node id -> the last traceroute to it (running or finished)
        self._trace_ref = None        # ref of the traceroute in flight
        self._trace_t = 0.0
        self.requests = {}            # node id -> {kind: {"state", "t", "error"}}
        self._req_refs = {}           # request ref -> (node id, kind)
        self.neighbors = {}           # node id -> {"t", "neighbors": [{"id", "snr"}]}
        self._self_nb = None          # the timer for asking our own node for its neighbours
        self.auto_trace = bool(getattr(args, "mesh_auto_trace", False))
        self.auto_per_hour = int(getattr(args, "mesh_auto_trace_per_hour", None) or AUTO_TRACE_PER_HOUR)
        self._auto_queue = {}         # node id -> time queued (insertion order is the order sent)
        self._auto_last = {}          # node id -> when we last traced it automatically
        self._auto_sent = deque(maxlen=AUTO_TRACE_PER_HOUR_MAX)  # times of automatic traces
        self._auto_since = None       # when the bridge last connected: only nodes heard since count
        self._auto_timer = None
        self.auto_resend = bool(getattr(args, "mesh_auto_resend", True))
        self._resends = {}            # message t -> the timer that sends it again
        self._parts = {}              # group (first part's t) -> {"cur": part in flight, "rest": [...], "timer"}
        self.stats = MeshStats()
        self._health_timer = None
        self._closing = False
        self._reached = False         # this run of the bridge got through to the node
        self._task = None

    # --- lifecycle

    async def start(self):
        self._load()
        self._task = asyncio.create_task(self._supervise())

    async def stop(self):
        self._closing = True
        if self._self_nb is not None:
            self._self_nb.cancel()
        if self._auto_timer is not None:
            self._auto_timer.cancel()
        for h in self._resends.values():
            h.cancel()
        for g in self._parts.values():
            if g.get("timer"):
                g["timer"].cancel()
        if self._health_timer is not None:
            self._health_timer.cancel()
        if self._kept_timer is not None:
            self._kept_timer.cancel()
            self._save_kept()
        if self._task:
            self._task.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.close()   # the bridge closes the node cleanly on EOF
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                self.proc.kill()

    async def _supervise(self):
        fails = 0
        while not self._closing:
            self._reached = False
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *self.cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=LINE_MAX)
            except OSError as e:
                log.error("cannot start the mesh bridge (%s): %s", self.cmd[0], e)
                self._status(False, f"bridge not started: {e}")
                await asyncio.sleep(RESTART_S[-1])
                continue
            log.info("mesh bridge started (pid %d) on %s", self.proc.pid, self.cmd[2])
            err = asyncio.create_task(self._stderr(self.proc.stderr))
            while True:
                try:
                    raw = await self.proc.stdout.readline()
                except ValueError:            # longer than LINE_MAX: start over, loudly
                    log.error("mesh bridge sent a line over %d bytes; restarting it", LINE_MAX)
                    self.error = f"the bridge sent a line over {LINE_MAX} bytes"
                    self.proc.kill()
                    break
                if not raw:
                    break
                try:
                    self._on_bridge(json.loads(raw))
                except (ValueError, KeyError, TypeError) as e:
                    log.warning("mesh bridge: bad line %r (%s)", raw[:200], e)
                except Exception:             # never let one event stop the reading
                    log.exception("mesh bridge: line %r not handled", raw[:200])
            code = await self.proc.wait()
            await err
            if self._closing:
                return
            self._fail_pending("the mesh node went away before sending")
            self._stop_resends("the mesh node went away")
            for ws, what in self._writes.values():
                self.station.notice(f"Mesh: the node went away; {what} may not be saved.", "warn", ws)
            self._writes.clear()
            if self._trace_ref is not None:
                self._end_trace("the mesh node went away")
            self._end_requests("the mesh node went away")
            if self.connected:
                self._status(False, self.error or "mesh bridge stopped")
            fails = 0 if self._reached else fails + 1
            wait = RESTART_S[min(fails, len(RESTART_S) - 1)]
            # A node left unplugged fails every restart: say so once, then quietly.
            (log.info if fails > 1 else log.warning)(
                "mesh bridge exited (code %s: %s); restarting in %gs", code, self.error, wait)
            await asyncio.sleep(wait)

    async def _stderr(self, stream):
        async for raw in stream:
            log.info("bridge: %s", raw.decode(errors="replace").rstrip())

    # --- bridge -> pages

    def _on_bridge(self, m):
        ev = m["ev"]
        if ev == "status":
            self._status(m["connected"], m.get("error"))
        elif ev == "info":
            self.info = {k: m.get(k) for k in ("me", "channels", "preset", "missing", "link")}
            for c in self.info["channels"]:
                if c["key_ok"] is False:
                    log.warning("mesh: channel %s on the node does not use the expected key", c["name"])
                    self.station.notice(f"Mesh channel {c['name']}: the node's key is not the "
                                        "expected one.", "warn")
            for name in self.info["missing"] or []:
                log.warning("mesh: channel %s is not configured on the node", name)
            self._send({"info": self.info})
            self._send_hears()
        elif ev == "config":
            self.config = {"sections": m["sections"], "values": m["values"]}
            self._send({"config": self.config})
            if self._self_nb is None:
                self._self_nb = asyncio.get_running_loop().call_later(SELF_NEIGHBORS_FIRST_S, self._self_neighbors)
        elif ev == "done":
            ws, what = self._writes.pop(m["ref"], (None, "?"))
            if m["ok"]:
                log.info("mesh: %s written to the node", what)
                self.station.notice(f"Mesh: {what} saved to the node.", "info", ws)
            else:
                log.warning("mesh: %s not written: %s", what, m.get("error"))
                self.station.notice(f"Mesh: {what} not saved: {m.get('error')}", "warn", ws)
        elif ev == "rx":
            rx = {"from": m["from"], "t": time.time(), **{k: m.get(k) for k in RX_FIELDS}}
            self.stats.packet(rx)
            self._send({"rx": rx})
            self._health_soon()
            rec = self.kept.get(m["from"])
            if rec is not None:       # heard again, not yet back in the radio's list (that takes its NodeInfo)
                rec["last_heard"] = int(rx["t"])
                if rx.get("hops") is not None:
                    rec["hops"] = rx["hops"]
                if rx.get("hops") == 0 and rx.get("snr") is not None:
                    rec["snr"] = rx["snr"]
                self._send({"kept": {"set": [rec], "drop": []}})
                self._kept_soon()
        elif ev == "trace":
            rec = self.traces.get(m["to"])
            if rec is None or m["ref"] != self._trace_ref:
                return
            self._trace_ref = None
            rec.update(state="done" if m["ok"] else "failed", error=m.get("error"), done_t=time.time(),
                       **{k: m.get(k) for k in ("route", "snr_towards", "route_back", "snr_back")})
            log.info("mesh: traceroute to %s %s%s", m["to"], rec["state"],
                     f" ({rec['error']})" if rec["error"] else f" via {rec['route']}")
            self._send({"trace": rec})
            if rec["state"] == "done":
                self._save_links()
                self._send_hears()
            self._auto_kick()
        elif ev == "nodes":
            self.nodes = m["nodes"]
            added, dropped = keep_nodes(self.kept, self._listed, self.nodes, self._forget)
            listed = {n["id"]: n for n in self.nodes if n.get("id")}
            if added or dropped or listed.keys() != self._listed.keys():
                self._kept_soon()
            self._listed = listed
            self._forget.intersection_update(listed)   # gone from the list: nothing left to hold back
            if added or dropped:
                self._send({"kept": {"set": list(added.values()), "drop": dropped}})
            changed = remember_names(self.names, self.nodes)
            if changed:
                self._save_names()
                self._send({"names": changed})
            self.stats.sample(self.nodes)
            self._send({"nodes": self.nodes})
            self._health_soon()
            self._auto_enqueue()
        elif ev == "neighbors":
            # The bridge holds only what it heard since it started: merge, the
            # newer report of a node wins, so a restart doesn't forget the rest.
            for nid, rec in (m["neighbors"] or {}).items():
                if (rec.get("t") or 0) >= (self.neighbors.get(nid) or {}).get("t", 0):
                    self.neighbors[nid] = rec
            self._prune_links()
            self._save_links()
            self._send({"neighbors": self.neighbors})
            self._send_hears()
        elif ev == "req":
            key = self._req_refs.pop(m["ref"], None)
            if key is None:
                return
            to, kind = key
            rec = self.requests.get(to, {}).get(kind)
            if rec is None:
                return
            error = m.get("error")
            if kind == "neighbors" and error == "NO_RESPONSE":
                error = "NO_RESPONSE: its NeighborInfo module is off, or it answered in the last 3 minutes"
            rec.update(state="done" if m["ok"] else "failed", error=error, done_t=time.time())
            log.info("mesh: %s request to %s %s%s", kind, to, rec["state"],
                     f" ({rec['error']})" if rec["error"] else "")
            self._send({"request": {"to": to, "kind": kind, **rec}})
        elif ev == "msg":
            rec = {"k": "msg", "id": m["id"], "t": time.time(), "from": m["from"], "to": m["to"],
                   "ch": m["ch"], "dm": bool(m["dm"]), "text": m["text"], "snr": m.get("snr"),
                   "rssi": m.get("rssi"), "hops": m.get("hops")}
            if m.get("reply_id"):
                rec["reply_id"] = m["reply_id"]
            if m.get("emoji"):
                rec["emoji"] = True
            self._add(rec)
        elif ev == "sent":
            rec = self._by_ref.pop(m["ref"], None)
            if rec is not None:
                rec["id"] = m["id"]
                self._settle(rec, "sent")
                asyncio.get_running_loop().call_later(ACK_WAIT_S, self._ack_timeout, rec, m["id"])
        elif ev == "send_failed":
            rec = self._by_ref.pop(m["ref"], None)
            if rec is not None:
                self._settle(rec, "failed", m.get("error"))
                self._advance(rec)
        elif ev == "ack":
            rec = next((r for r in reversed(self.messages)
                        if r.get("mine") and r.get("id") == m["id"]), None)
            if rec is not None and rec.get("ack") in ("sent", "unknown"):
                state = ("relayed" if m.get("relayed") else "delivered") if m["ok"] else "failed"
                self._settle(rec, state, None if m["ok"] else m.get("reason"))
                if state == "failed":
                    self._schedule_resend(rec, m.get("reason"))
                self._advance(rec)
                if state == "delivered" and rec.get("dm"):
                    self._send_hears()

    def _status(self, connected, error):
        self.connected, self.error = bool(connected), error
        self._reached = self._reached or self.connected
        if self.connected and self._auto_since is None:
            self._auto_since = time.time()
        elif not self.connected:
            self._auto_since = None
            self._auto_queue.clear()
        log.info("mesh: %s%s", "connected" if connected else "disconnected",
                 f" ({error})" if error else "")
        self._send({"status": {"connected": self.connected, "error": error}})

    def _settle(self, rec, state, why=None):
        rec["ack"] = state
        if why:
            rec["why"] = why
        self._write({"k": "ack", "id": rec.get("id"), "ref_t": rec["t"], "ack": state, "why": why})
        self._send({"update": rec})

    def _ack_timeout(self, rec, pid=None):
        # pid: the try this timer is for (a resend is a new packet with its own wait)
        if rec.get("ack") == "sent" and not self._closing and (pid is None or rec.get("id") == pid):
            self._settle(rec, "unknown", f"no ACK or failure from the node in {ACK_WAIT_S:g} s")
            self._advance(rec)

    # --- automatic resend and long texts in parts

    def _schedule_resend(self, rec, reason):
        """Send REC again RESEND_S[tries] from now, if REASON is one the mesh
        may get over and it has tries left. Never a reaction (a lost one is not
        worth the airtime twice), never after auto_resend is switched off."""
        tries = rec.get("tries", 0)
        if (not self.auto_resend or self._closing or rec.get("emoji") or reason not in RESEND_REASONS
                or tries >= len(RESEND_S) or rec["t"] in self._resends):
            return
        delay = RESEND_S[tries]
        rec["retry_at"] = time.time() + delay
        self._resends[rec["t"]] = asyncio.get_running_loop().call_later(delay, self._resend_due, rec)
        log.info("mesh: %s; sending it again in %gs (try %d of %d)", reason, delay, tries + 2, len(RESEND_S) + 1)
        self._send({"update": rec})

    def _resend_due(self, rec):
        self._resends.pop(rec["t"], None)
        rec.pop("retry_at", None)
        if self._closing:
            return
        if not self._live():
            rec["why"] = f"{rec.get('why') or 'not delivered'}; not sent again: the node is not connected"
            self._send({"update": rec})
            self._advance(rec)
            return
        rec["tries"] = rec.get("tries", 0) + 1
        rec.pop("why", None)
        asyncio.ensure_future(self._transmit(rec))

    def _advance(self, rec):
        """REC settled: if it is the part of a long text in flight, send the next
        part (delivered, relayed or unknown) or drop the rest (failed for good)."""
        group = rec.get("group")
        g = self._parts.get(group)
        if g is None or g["cur"] is not rec:
            return
        if rec.get("ack") in ("delivered", "relayed", "unknown"):
            if not g["rest"]:
                del self._parts[group]
                return
            g["cur"] = g["rest"].pop(0)
            g["timer"] = asyncio.get_running_loop().call_later(PART_GAP_S, self._part_due, group)
        elif rec.get("ack") == "failed" and rec["t"] not in self._resends:
            self._drop_parts(group, "an earlier part was not delivered")

    def _part_due(self, group):
        g = self._parts.get(group)
        if g is None or self._closing:
            return
        g["timer"] = None
        if not self._live():
            g["rest"].insert(0, g["cur"])
            self._drop_parts(group, "not sent: the node is not connected")
            return
        asyncio.ensure_future(self._transmit(g["cur"]))

    def _drop_parts(self, group, why):
        g = self._parts.pop(group, None)
        if g is None:
            return
        if g.get("timer"):
            g["timer"].cancel()
        for r in g["rest"]:
            self._settle(r, "failed", why)

    def _stop_resends(self, why):
        """The node went away: nothing waits to be sent again or in parts."""
        for t, h in list(self._resends.items()):
            h.cancel()
            rec = next((r for r in self.messages if r.get("t") == t), None)
            if rec is not None:
                rec.pop("retry_at", None)
                rec["why"] = f"{rec.get('why') or 'not delivered'}; not sent again: {why}"
                self._send({"update": rec})
        self._resends.clear()
        for group, g in list(self._parts.items()):
            if g["cur"].get("ack") == "queued":
                g["rest"].insert(0, g["cur"])
            self._drop_parts(group, f"not sent: {why}")

    async def cancel(self, ws, t):
        """A page's Cancel on a message waiting to be sent again, or on a part
        of a long text not sent yet: it stays failed, and the parts not sent
        yet go with it."""
        rec = next((r for r in self.messages if r.get("mine") and r.get("t") == t), None)
        h = self._resends.pop(t, None) if rec else None
        if rec is None or (h is None and rec.get("ack") != "queued"):
            self.station.notice("Nothing to cancel: that message is not waiting to be sent.", "warn", ws)
            return
        if h is not None:
            h.cancel()
            rec.pop("retry_at", None)
            self._settle(rec, "failed", f"{rec.get('why') or 'not delivered'}; not sent again (cancelled)")
        g = self._parts.get(rec.get("group"))
        if g is not None:
            if g["cur"].get("ack") == "queued":
                g["rest"].insert(0, g["cur"])
            self._drop_parts(rec["group"], "cancelled")
        log.info("mesh: cancelled a message waiting to be sent")

    def _fail_pending(self, why):
        for rec in list(self._by_ref.values()):
            self._settle(rec, "failed", why)
        self._by_ref.clear()
        for rec in self.messages:            # sent, but its ACK can no longer arrive
            if rec.get("mine") and rec.get("ack") == "sent":
                self._settle(rec, "unknown")

    def _add(self, rec):
        self.messages.append(rec)
        self._write(rec)
        if self.convs.heard(rec):
            self._send({"convs": self.convs.view()})
        self._send({"msg": rec})

    # --- conversations: archive, mute, pin, read, delete (mesh_convs.py)

    async def conv(self, ws, m):
        """A page's mesh_conv: {"action", "conv", "msg_t"} for a conversation's
        state; "delete" with "conv" (all of it) or "ts" (those messages);
        "undo" with the "ref" a delete gave. Never anything on the air."""
        action = m.get("action")
        try:
            if action == "delete":
                return self._delete(ws, m)
            if action == "undo":
                return self._undo(ws, m.get("ref"))
            if self.convs.act(m.get("conv"), action, m.get("msg_t")):
                self._send({"convs": self.convs.view()})
        except ValueError as e:
            self.station.notice(f"Mesh: {e}.", "warn", ws)

    def _delete(self, ws, m):
        conv, ts = m.get("conv"), m.get("ts")
        if conv is not None and not mesh_convs.CONV.match(str(conv)):
            raise ValueError("not a conversation")
        if ts is not None and (not isinstance(ts, list) or len(ts) > MESSAGES_KEEP
                               or not all(isinstance(t, (int, float)) and not isinstance(t, bool) for t in ts)):
            raise ValueError("not a list of message times")
        if conv is None and not ts:
            raise ValueError("nothing to delete")
        gone, kept = mesh_convs.pick(self.messages, conv, ts, self._resends)
        if not gone:
            self.station.notice("Mesh: nothing deleted" + (": those are still being sent." if kept else "."), "warn", ws)
            return
        what = "conversation" if conv else "message" if len(gone) == 1 else "messages"
        ref = self.trash.put(gone, what)
        drop = {id(x) for x in gone}
        kept_all = [x for x in self.messages if id(x) not in drop]
        self.messages.clear()
        self.messages.extend(kept_all)
        self._rewrite_log()
        if conv and not kept:
            self.convs.forget(conv)
        log.info("mesh: deleted %d message(s) from the station's history%s", len(gone),
                 f" ({len(kept)} still being sent kept)" if kept else "")
        self._send({"history": list(self.messages), "convs": self.convs.view()})
        self._send({"deleted": {"ref": ref, "n": len(gone), "what": what, "kept": len(kept)}}, ws)

    def _undo(self, ws, ref):
        msgs = self.trash.take(ref) if isinstance(ref, str) else None
        if msgs is None:
            raise ValueError("that delete can no longer be undone")
        have = {x.get("t") for x in self.messages}
        merged = sorted([*self.messages, *(x for x in msgs if x.get("t") not in have)], key=lambda x: x.get("t") or 0)
        self.messages.clear()
        self.messages.extend(merged[-MESSAGES_KEEP:])
        self._rewrite_log()
        log.info("mesh: %d deleted message(s) brought back", len(msgs))
        self._send({"history": list(self.messages), "convs": self.convs.view()})
        self.station.notice(f"Mesh: {len(msgs)} message{'' if len(msgs) == 1 else 's'} brought back.", "info", ws)

    def _rewrite_log(self):
        """The history file as the kept messages, ACKs folded in (also the compaction)."""
        if not self.log_path:
            return
        tmp = self.log_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.writelines(json.dumps(m) + "\n" for m in self.messages)
            os.replace(tmp, self.log_path)
        except OSError as e:
            log.warning("mesh log %s not rewritten: %s", self.log_path, e)

    def _send(self, d, ws=None):
        payload = json.dumps({"t": "mesh", **d})
        if ws is not None:
            asyncio.ensure_future(self.station._send(ws, payload))
        else:
            self.station._broadcast(payload)

    def snapshot(self):
        return {"status": {"connected": self.connected, "error": self.error},
                "info": self.info, "config": self.config, "nodes": self.nodes,
                "traces": list(self.traces.values()), "messages": list(self.messages),
                "requests": self.requests, "neighbors": self.neighbors,
                "hears": self.hears(), "text_max": TEXT_MAX_BYTES, "parts_max": PARTS_MAX,
                "part_bytes": TEXT_MAX_BYTES - PART_PREFIX_BYTES, "auto_resend": self.auto_resend,
                "health": self.health(), "packets": list(self.stats.log), "names": self.names,
                "kept": list(self.kept.values()), "convs": self.convs.view()}

    def health(self):
        return self.stats.summary(self.nodes)

    def _health_soon(self):
        if self._health_timer is None and not self._closing:
            self._health_timer = asyncio.get_running_loop().call_later(HEALTH_EVERY_S, self._health_send)

    def _health_send(self):
        self._health_timer = None
        if not self._closing:
            self._send({"health": self.health()})

    def hears(self):
        me = ((self.info or {}).get("me") or {}).get("id")
        return hears_us(me, self.neighbors, self.traces, self.messages)

    def _send_hears(self):
        self._send({"hears": self.hears()})

    # --- pages -> node

    async def send_text(self, ws, text, ch, to=None, reply_to=None, emoji=False):
        text = str(text).strip()
        why = None
        if reply_to is not None and (isinstance(reply_to, bool) or not isinstance(reply_to, int)
                                     or not any(r.get("id") == reply_to for r in self.messages)):
            why = "That message is no longer here to reply to."
        elif emoji and reply_to is None:
            why = "A reaction needs a message to react to."
        elif emoji and len(text.encode()) > EMOJI_MAX_BYTES:
            why = "A reaction is one emoji."
        elif not text:
            why = "Nothing to send."
        elif len(text.encode()) > TEXT_MAX_BYTES and reply_to is not None:
            why = f"Too long for one mesh message ({len(text.encode())} of {TEXT_MAX_BYTES} bytes); a reply is one."
        elif len(text.encode()) > TEXT_MAX_BYTES and split_text(text) is None:
            why = f"Too long: {len(text.encode())} bytes is more than {PARTS_MAX} mesh messages."
        elif not self.connected or self.proc is None or self.proc.returncode is not None:
            why = "The mesh node is not connected."
        elif not any(c["index"] == ch for c in (self.info or {}).get("channels", [])):
            why = f"The node has no channel {ch}."
        elif to is not None and not NODE_ID.match(str(to)):
            why = f"Not a node id: {to!r}."
        if why:
            self.station.notice(why, "warn", ws)
            return
        texts = [text] if emoji else split_text(text)
        base = time.time()
        recs = []
        for i, part in enumerate(texts):
            rec = {"k": "msg", "id": None, "t": base + i * 0.001, "from": (self.info["me"] or {}).get("id"),
                   "to": to, "ch": ch, "dm": to is not None, "text": part, "mine": True,
                   "ack": "sending" if i == 0 else "queued"}
            if reply_to is not None:
                rec["reply_id"] = reply_to
            if emoji:
                rec["emoji"] = True
            if len(texts) > 1:
                rec["part"], rec["group"] = [i + 1, len(texts)], base
            self._add(rec)
            recs.append(rec)
        if len(recs) > 1:
            self._parts[base] = {"cur": recs[0], "rest": recs[1:], "timer": None}
        await self._transmit(recs[0])

    def _live(self):
        return self.connected and self.proc is not None and self.proc.returncode is None

    async def _transmit(self, rec):
        """Hand REC to the bridge (a first send, a resend, the next part)."""
        self._ref += 1
        self._by_ref[self._ref] = rec
        if rec.get("ack") != "sending":
            rec["ack"] = "sending"
            self._send({"update": rec})
        self.proc.stdin.write((json.dumps({"cmd": "send", "ref": self._ref, "text": rec["text"], "ch": rec["ch"],
                                           "to": rec["to"], "reply_to": rec.get("reply_id"),
                                           "emoji": bool(rec.get("emoji"))}) + "\n").encode())
        await self.proc.stdin.drain()
        log.info("mesh: sending %d bytes on channel %d%s%s", len(rec["text"].encode()), rec["ch"],
                 f" to {rec['to']}" if rec["to"] else "",
                 f" (part {rec['part'][0]} of {rec['part'][1]})" if rec.get("part") else "")

    def _usable(self, ws):
        if not self.connected or self.proc is None or self.proc.returncode is not None:
            self.station.notice("The mesh node is not connected.", "warn", ws)
            return False
        return True

    async def _write_cmd(self, ws, what, cmd):
        self._ref += 1
        self._writes[self._ref] = (ws, what)
        self.proc.stdin.write((json.dumps({**cmd, "ref": self._ref}) + "\n").encode())
        await self.proc.stdin.drain()
        log.info("mesh: writing %s", what)

    def check_settings(self, section, values):
        """Why the page's values for SECTION can't be written, or None."""
        spec = next((s for s in (self.config or {}).get("sections", []) if s["name"] == section), None)
        if spec is None:
            return f"The node has no settings section {section!r}."
        if not isinstance(values, dict) or not values:
            return "Nothing to save."
        fields = {f["name"]: f for f in spec["fields"]}
        for name, v in values.items():
            f = fields.get(name)
            if f is None:
                return f"{spec['label']} has no setting {name!r}."
            t, label = f["type"], f["label"]
            if t == "bool" and not isinstance(v, bool):
                return f"{label}: not on/off."
            if t in ("int", "float"):
                if isinstance(v, bool) or not isinstance(v, (int, float)) or (t == "int" and v != int(v)):
                    return f"{label}: not a {'whole ' if t == 'int' else ''}number."
                if not f.get("min", v) <= v <= f.get("max", v):
                    return f"{label}: {v} is outside {f.get('min', '')}..{f.get('max', '')}."
            if t == "enum" and v not in f["options"]:
                return f"{label}: {v!r} is not one of the choices."
            if t == "str":
                if not isinstance(v, str):
                    return f"{label}: not text."
                if section == "owner" and not v.strip():
                    return f"{label}: can't be empty."
                if len(v.encode()) > f.get("max_bytes", 200):
                    return f"{label}: longer than {f['max_bytes']} bytes."
        return None

    async def set_config(self, ws, section, values):
        if not self._usable(ws):
            return
        why = self.check_settings(section, values)
        if why:
            self.station.notice(why, "warn", ws)
            return
        spec = next(s for s in self.config["sections"] if s["name"] == section)
        types = {f["name"]: f["type"] for f in spec["fields"]}
        values = {k: int(v) if types[k] == "int" else v for k, v in values.items()}
        await self._write_cmd(ws, f"{spec['label']} settings", {"cmd": "set", "section": section, "values": values})

    async def set_favorite(self, ws, to, fav):
        if not self._usable(ws):
            return
        if not NODE_ID.match(str(to)):
            self.station.notice(f"Not a node id: {to!r}.", "warn", ws)
            return
        if to == ((self.info or {}).get("me") or {}).get("id"):
            self.station.notice("That is this node.", "warn", ws)
            return
        await self._write_cmd(ws, f"{'favorite' if fav else 'no favorite'} {to}",
                              {"cmd": "favorite", "to": to, "fav": bool(fav)})

    async def traceroute(self, ws, to):
        if not self._usable(ws):
            return
        why = None
        wait = TRACE_GAP_S - (time.time() - self._trace_t)
        if not NODE_ID.match(str(to)):
            why = f"Not a node id: {to!r}."
        elif to == ((self.info or {}).get("me") or {}).get("id"):
            why = "That is this node."
        elif self._trace_ref is not None:
            why = "A traceroute is already running; wait for it to finish."
        elif wait > 0:
            why = f"The node allows one traceroute every {TRACE_GAP_S:g} s; try again in {wait:.0f} s."
        if why:
            self.station.notice(why, "warn", ws)
            return
        await self._start_trace(to)

    async def _start_trace(self, to, auto=False):
        self._ref += 1
        self._trace_ref, self._trace_t = self._ref, time.time()
        hops = next((n.get("hops") for n in self.nodes if n.get("id") == to), None)
        self.traces.pop(to, None)
        self.traces[to] = {"to": to, "state": "running", "t": self._trace_t, "hops": hops}
        if auto:
            self.traces[to]["auto"] = True
        while len(self.traces) > TRACES_KEEP:
            self.traces.pop(next(iter(self.traces)))
        self._send({"trace": self.traces[to]})
        self.proc.stdin.write((json.dumps({"cmd": "trace", "ref": self._ref, "to": to}) + "\n").encode())
        await self.proc.stdin.drain()
        log.info("mesh: %straceroute to %s", "automatic " if auto else "", to)

    # --- automatic traceroutes (auto_trace)

    def _auto_enqueue(self):
        if not self.auto_trace or self._auto_since is None:
            return
        for nid in auto_trace_candidates(self.nodes, self.traces, self._me(), self._auto_since,
                                         self._auto_last, time.time()):
            if nid not in self._auto_queue and len(self._auto_queue) < AUTO_TRACE_QUEUE:
                self._auto_queue[nid] = time.time()
        self._auto_kick()

    def _auto_kick(self):
        """Arm the timer for the next automatic trace, if one is waiting."""
        if not self.auto_trace or not self._auto_queue or self._auto_timer is not None or self._closing:
            return
        wait = auto_trace_wait(time.time(), self._trace_t, self._trace_ref is not None,
                               self._auto_sent, self.auto_per_hour)
        if wait is None:              # a trace is running: its end kicks again
            return
        self._auto_timer = asyncio.get_running_loop().call_later(wait, self._auto_fire)

    def _auto_fire(self):
        self._auto_timer = None
        if (self._closing or not self.connected or self.proc is None or self.proc.returncode is not None
                or self._auto_since is None):
            return
        now = time.time()
        if auto_trace_wait(now, self._trace_t, self._trace_ref is not None,
                           self._auto_sent, self.auto_per_hour) != 0:
            self._auto_kick()
            return
        # Still worth it? A manual trace or a new hop count may have settled it.
        still = set(auto_trace_candidates(self.nodes, self.traces, self._me(), self._auto_since,
                                          self._auto_last, now))
        while self._auto_queue:
            nid = next(iter(self._auto_queue))
            del self._auto_queue[nid]
            if nid in still:
                self._auto_last[nid] = now
                self._auto_sent.append(now)
                asyncio.ensure_future(self._start_trace(nid, auto=True))
                return

    def _peer_why(self, to, kind=None):
        if not NODE_ID.match(str(to)):
            return f"Not a node id: {to!r}."
        if to == self._me() and kind != "neighbors":     # its own neighbours: asked of itself, off the air
            return "That is this node."
        return None

    def _me(self):
        return ((self.info or {}).get("me") or {}).get("id")

    def _self_neighbors(self):
        """Ask our own node for its NeighborInfo (see SELF_NEIGHBORS_S); quietly
        skipped when it can't be asked now, then again later."""
        if self._closing:
            return
        self._self_nb = asyncio.get_running_loop().call_later(SELF_NEIGHBORS_S, self._self_neighbors)
        me = self._me()
        on = (((self.config or {}).get("values") or {}).get("neighbor_info") or {}).get("enabled")
        mine = self.requests.get(me, {}) if me else {}
        last = mine.get("neighbors")
        if (not me or not on or not self.connected or self.proc is None or self.proc.returncode is not None
                or any(r["state"] == "running" for r in mine.values())
                or (last and time.time() - last["t"] < REQUEST_GAP_BY_KIND["neighbors"])):
            return
        asyncio.ensure_future(self._issue(me, "neighbors"))

    async def request(self, ws, to, kind):
        """Ask node TO for its position, telemetry, environment or user info."""
        if not self._usable(ws):
            return
        why = self._peer_why(to, kind)
        if why is None and kind not in REQUEST_KINDS:
            why = f"Not something to ask a node for: {kind!r}."
        mine = self.requests.get(to, {})
        if why is None and any(r["state"] == "running" for r in mine.values()):
            why = "A request to that node is already waiting for its answer."
        last = mine.get(kind)
        gap = REQUEST_GAP_BY_KIND.get(kind, REQUEST_GAP_S)
        wait = gap - (time.time() - last["t"]) if last else 0
        if why is None and wait > 0:
            why = f"Asked for its {REQUEST_KINDS[kind]} {gap - wait:.0f} s ago; try again in {wait:.0f} s."
        if why:
            self.station.notice(why, "warn", ws)
            return
        await self._issue(to, kind)

    async def _issue(self, to, kind):
        self._ref += 1
        rec = {"state": "running", "t": time.time(), "error": None}
        self.requests.setdefault(to, {})[kind] = rec
        self._req_refs[self._ref] = (to, kind)
        self._send({"request": {"to": to, "kind": kind, **rec}})
        self.proc.stdin.write((json.dumps({"cmd": "request", "ref": self._ref, "to": to, "kind": kind})
                               + "\n").encode())
        await self.proc.stdin.drain()
        log.info("mesh: asking %s for its %s", to, REQUEST_KINDS[kind])

    def _end_requests(self, why):
        for to, kind in self._req_refs.values():
            rec = self.requests.get(to, {}).get(kind)
            if rec and rec["state"] == "running":
                rec.update(state="failed", error=why, done_t=time.time())
                self._send({"request": {"to": to, "kind": kind, **rec}})
        self._req_refs.clear()

    async def node_action(self, ws, to, do):
        if do == "remove" and to in self.kept:    # the station's own record: no radio needed
            del self.kept[to]
            self._send({"kept": {"set": [], "drop": [to]}})
            self._kept_soon()
            log.info("mesh: %s forgotten (the radio no longer listed it)", to)
            self.station.notice(f"Mesh: {to} forgotten.", "info", ws)
            return
        if not self._usable(ws):
            return
        why = self._peer_why(to) or (None if do in NODE_ACTIONS else f"Not a node action: {do!r}.")
        if why:
            self.station.notice(why, "warn", ws)
            return
        if do == "remove":
            self._forget.add(to)      # removed by hand: it must not come back as a kept node
        await self._write_cmd(ws, f"{NODE_ACTIONS[do]} {to}", {"cmd": "node", "to": to, "do": do})

    async def reboot(self, ws):
        if not self._usable(ws):
            return
        await self._write_cmd(ws, "reboot", {"cmd": "reboot"})

    def _end_trace(self, why):
        rec = next((r for r in self.traces.values() if r["state"] == "running"), None)
        self._trace_ref = None
        if rec:
            rec.update(state="failed", error=why, done_t=time.time())
            self._send({"trace": rec})
        self._auto_kick()

    # --- history on disk

    def _prune_links(self, now=None):
        now = time.time() if now is None else now
        keep = sorted(((nid, r) for nid, r in self.neighbors.items()
                       if isinstance(r, dict) and now - (r.get("t") or 0) <= LINKS_KEEP_S),
                      key=lambda x: -(x[1].get("t") or 0))[:NEIGHBORS_KEEP]
        self.neighbors = dict(keep)

    def _load_links(self):
        if not self.links_path:
            return
        try:
            with open(self.links_path) as f:
                d = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("mesh links %s not read: %s", self.links_path, e)
            return
        now = time.time()
        if isinstance(d.get("neighbors"), dict):
            self.neighbors = d["neighbors"]
            self._prune_links(now)
        for tr in d.get("traces") or []:
            if (isinstance(tr, dict) and tr.get("state") == "done" and NODE_ID.match(str(tr.get("to")))
                    and now - (tr.get("done_t") or 0) <= LINKS_KEEP_S):
                self.traces[tr["to"]] = tr
        while len(self.traces) > TRACES_KEEP:
            self.traces.pop(next(iter(self.traces)))
        log.info("mesh: NeighborInfo of %d nodes and %d traceroutes from %s",
                 len(self.neighbors), len(self.traces), self.links_path)

    def _save_links(self):
        if not self.links_path:
            return
        d = {"neighbors": self.neighbors,
             "traces": [t for t in self.traces.values() if t.get("state") == "done"]}
        tmp = self.links_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.links_path), exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(d, f)
            os.replace(tmp, self.links_path)
        except OSError as e:
            log.warning("mesh links %s not written: %s", self.links_path, e)

    def _load_names(self):
        if not self.names_path:
            return
        try:
            with open(self.names_path) as f:
                d = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("mesh names %s not read: %s", self.names_path, e)
            return
        if isinstance(d, dict):
            self.names = {k: v for k, v in d.items() if NODE_ID.match(str(k)) and isinstance(v, dict)}
        log.info("mesh: names of %d nodes from %s", len(self.names), self.names_path)

    def _save_names(self):
        if not self.names_path:
            return
        tmp = self.names_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.names_path), exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.names, f)
            os.replace(tmp, self.names_path)
        except OSError as e:
            log.warning("mesh names %s not written: %s", self.names_path, e)

    def _load_kept(self):
        """Every node in the file is kept until the radio lists it again: with
        the node away the page still shows who was heard."""
        if not self.kept_path:
            return
        try:
            with open(self.kept_path) as f:
                d = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("mesh nodes %s not read: %s", self.kept_path, e)
            return
        if isinstance(d, dict):
            self.kept = {k: kept_record(v) for k, v in d.items() if NODE_ID.match(str(k)) and isinstance(v, dict)}
        log.info("mesh: %d nodes kept from %s", len(self.kept), self.kept_path)

    def _kept_soon(self):
        if self._kept_timer is None and self.kept_path and not self._closing:
            self._kept_timer = asyncio.get_running_loop().call_later(KEPT_SAVE_S, self._save_kept)

    def _save_kept(self):
        """The kept nodes and the radio's own list, in one file: after a
        restart a node the radio had only in memory is not lost."""
        self._kept_timer = None
        if not self.kept_path:
            return
        d = {nid: kept_record(n) for nid, n in self._listed.items()
             if NODE_ID.match(str(nid)) and not n.get("me") and nid not in self._forget}
        d.update(self.kept)
        tmp = self.kept_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.kept_path), exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(d, f)
            os.replace(tmp, self.kept_path)
        except OSError as e:
            log.warning("mesh nodes %s not written: %s", self.kept_path, e)

    def _load(self):
        if not self.log_path:
            return
        self._load_links()
        self._load_names()
        self._load_kept()
        recs = []
        try:
            with open(self.log_path) as f:
                for line in f:
                    try:
                        recs.append(json.loads(line))
                    except ValueError:
                        pass
        except FileNotFoundError:
            return
        except OSError as e:
            log.warning("mesh log %s not read: %s", self.log_path, e)
            return
        msgs, mine = [], {}
        for r in recs:
            if r.get("k") == "msg":
                msgs.append(r)
                if r.get("mine"):
                    mine[r["t"]] = r          # our message: its ACK records name its time
            elif r.get("k") == "ack" and r.get("ref_t") in mine:
                m = mine[r["ref_t"]]
                m["ack"], m["id"] = r["ack"], r.get("id") or m.get("id")
                if r.get("why"):
                    m["why"] = r["why"]
        for m in msgs[-MESSAGES_KEEP:]:
            m.pop("retry_at", None)
            if m.get("ack") in ("sending", "sent"):
                m["ack"] = "unknown"          # the server restarted before its ACK
            elif m.get("ack") == "queued":
                m["ack"], m["why"] = "failed", "not sent: the server restarted"
            self.messages.append(m)
        log.info("mesh: %d messages from %s", len(self.messages), self.log_path)
        if len(recs) > COMPACT_AT:
            self._rewrite_log()             # keep the file to what is shown

    def _write(self, rec):
        if not self.log_path:
            return
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError as e:
            log.warning("mesh log %s not written: %s", self.log_path, e)
