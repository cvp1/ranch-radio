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

log = logging.getLogger("mesh")

MESSAGES_KEEP = 300
TEXT_MAX_BYTES = 200          # Meshtastic's payload is 233 bytes; apps cap text near 200
RESTART_S = (5.0, 15.0, 60.0)  # wait before restarting the bridge, growing while it can't reach the node
COMPACT_AT = 4 * MESSAGES_KEEP  # log records before the file is rewritten to the kept messages
NODE_ID = re.compile(r"^![0-9a-f]{8}$")
TRACE_GAP_S = 30.0            # firmware drops traceroutes sent closer together than this
TRACES_KEEP = 50


class MeshEngine:
    def __init__(self, station, args):
        self.station = station
        self.cmd = [os.path.expanduser(args.mesh_python), args.mesh_bridge, args.mesh_port,
                    *(f"--key={k}" for k in args.mesh_key)]
        self.log_path = os.path.expanduser(args.mesh_log) if args.mesh_log else None
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
        self._closing = False
        self._reached = False         # this run of the bridge got through to the node
        self._task = None

    # --- lifecycle

    async def start(self):
        self._load()
        self._task = asyncio.create_task(self._supervise())

    async def stop(self):
        self._closing = True
        if self._task:
            self._task.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.close()   # the bridge closes the node cleanly on EOF
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()

    async def _supervise(self):
        fails = 0
        while not self._closing:
            self._reached = False
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *self.cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE)
            except OSError as e:
                log.error("cannot start the mesh bridge (%s): %s", self.cmd[0], e)
                self._status(False, f"bridge not started: {e}")
                await asyncio.sleep(RESTART_S[-1])
                continue
            log.info("mesh bridge started (pid %d) on %s", self.proc.pid, self.cmd[2])
            err = asyncio.create_task(self._stderr(self.proc.stderr))
            async for raw in self.proc.stdout:
                try:
                    self._on_bridge(json.loads(raw))
                except (ValueError, KeyError, TypeError) as e:
                    log.warning("mesh bridge: bad line %r (%s)", raw[:200], e)
            code = await self.proc.wait()
            await err
            if self._closing:
                return
            self._fail_pending("the mesh node went away before sending")
            for ws, what in self._writes.values():
                self.station.notice(f"Mesh: the node went away; {what} may not be saved.", "warn", ws)
            self._writes.clear()
            if self._trace_ref is not None:
                self._end_trace("the mesh node went away")
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
            self.info = {k: m.get(k) for k in ("me", "channels", "preset", "missing")}
            for c in self.info["channels"]:
                if c["key_ok"] is False:
                    log.warning("mesh: channel %s on the node does not use the expected key", c["name"])
                    self.station.notice(f"Mesh channel {c['name']}: the node's key is not the "
                                        "expected one.", "warn")
            for name in self.info["missing"] or []:
                log.warning("mesh: channel %s is not configured on the node", name)
            self._send({"info": self.info})
        elif ev == "config":
            self.config = {"sections": m["sections"], "values": m["values"]}
            self._send({"config": self.config})
        elif ev == "done":
            ws, what = self._writes.pop(m["ref"], (None, "?"))
            if m["ok"]:
                log.info("mesh: %s written to the node", what)
                self.station.notice(f"Mesh: {what} saved to the node.", "info", ws)
            else:
                log.warning("mesh: %s not written: %s", what, m.get("error"))
                self.station.notice(f"Mesh: {what} not saved: {m.get('error')}", "warn", ws)
        elif ev == "rx":
            self._send({"rx": {"from": m["from"], "port": m.get("port"), "t": time.time()}})
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
        elif ev == "nodes":
            self.nodes = m["nodes"]
            self._send({"nodes": self.nodes})
        elif ev == "msg":
            rec = {"k": "msg", "id": m["id"], "t": time.time(), "from": m["from"], "to": m["to"],
                   "ch": m["ch"], "dm": bool(m["dm"]), "text": m["text"], "snr": m.get("snr"),
                   "rssi": m.get("rssi"), "hops": m.get("hops")}
            self._add(rec)
        elif ev == "sent":
            rec = self._by_ref.pop(m["ref"], None)
            if rec is not None:
                rec["id"] = m["id"]
                self._settle(rec, "sent")
        elif ev == "send_failed":
            rec = self._by_ref.pop(m["ref"], None)
            if rec is not None:
                self._settle(rec, "failed", m.get("error"))
        elif ev == "ack":
            rec = next((r for r in reversed(self.messages)
                        if r.get("mine") and r.get("id") == m["id"]), None)
            if rec is not None:
                state = ("relayed" if m.get("relayed") else "delivered") if m["ok"] else "failed"
                self._settle(rec, state, None if m["ok"] else m.get("reason"))

    def _status(self, connected, error):
        self.connected, self.error = bool(connected), error
        self._reached = self._reached or self.connected
        log.info("mesh: %s%s", "connected" if connected else "disconnected",
                 f" ({error})" if error else "")
        self._send({"status": {"connected": self.connected, "error": error}})

    def _settle(self, rec, state, why=None):
        rec["ack"] = state
        if why:
            rec["why"] = why
        self._write({"k": "ack", "id": rec.get("id"), "ref_t": rec["t"], "ack": state, "why": why})
        self._send({"update": rec})

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
        self._send({"msg": rec})

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
                "text_max": TEXT_MAX_BYTES}

    # --- pages -> node

    async def send_text(self, ws, text, ch, to=None):
        text = str(text).strip()
        why = None
        if not text:
            why = "Nothing to send."
        elif len(text.encode()) > TEXT_MAX_BYTES:
            why = f"Too long for one mesh message ({len(text.encode())} of {TEXT_MAX_BYTES} bytes)."
        elif not self.connected or self.proc is None or self.proc.returncode is not None:
            why = "The mesh node is not connected."
        elif not any(c["index"] == ch for c in (self.info or {}).get("channels", [])):
            why = f"The node has no channel {ch}."
        elif to is not None and not NODE_ID.match(str(to)):
            why = f"Not a node id: {to!r}."
        if why:
            self.station.notice(why, "warn", ws)
            return
        self._ref += 1
        rec = {"k": "msg", "id": None, "t": time.time(), "from": (self.info["me"] or {}).get("id"),
               "to": to, "ch": ch, "dm": to is not None, "text": text, "mine": True, "ack": "sending"}
        self._by_ref[self._ref] = rec
        self._add(rec)
        self.proc.stdin.write((json.dumps({"cmd": "send", "ref": self._ref, "text": text,
                                           "ch": ch, "to": to}) + "\n").encode())
        await self.proc.stdin.drain()
        log.info("mesh: sending %d bytes on channel %d%s", len(text.encode()), ch,
                 f" to {to}" if to else "")

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
        self._ref += 1
        self._trace_ref, self._trace_t = self._ref, time.time()
        self.traces.pop(to, None)
        self.traces[to] = {"to": to, "state": "running", "t": self._trace_t}
        while len(self.traces) > TRACES_KEEP:
            self.traces.pop(next(iter(self.traces)))
        self._send({"trace": self.traces[to]})
        self.proc.stdin.write((json.dumps({"cmd": "trace", "ref": self._ref, "to": to}) + "\n").encode())
        await self.proc.stdin.drain()
        log.info("mesh: traceroute to %s", to)

    def _end_trace(self, why):
        rec = next((r for r in self.traces.values() if r["state"] == "running"), None)
        self._trace_ref = None
        if rec:
            rec.update(state="failed", error=why, done_t=time.time())
            self._send({"trace": rec})

    # --- history on disk

    def _load(self):
        if not self.log_path:
            return
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
            if m.get("ack") in ("sending", "sent"):
                m["ack"] = "unknown"          # the server restarted before its ACK
            self.messages.append(m)
        log.info("mesh: %d messages from %s", len(self.messages), self.log_path)
        if len(recs) > COMPACT_AT:
            # Keep the file to what is shown: the kept messages, ACKs folded in.
            tmp = self.log_path + ".tmp"
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.writelines(json.dumps(m) + "\n" for m in self.messages)
                os.replace(tmp, self.log_path)
            except OSError as e:
                log.warning("mesh log %s not compacted: %s", self.log_path, e)

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
