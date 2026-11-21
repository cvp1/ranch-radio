"""Mesh: a Meshtastic node on the page, as an add-on (mesh.py does the work).

The first add-on with its own tab rather than an add-on panel: its page is
the Mesh tab (static/mesh.js), so it keeps that tab's wire protocol. The page
sends {"t": "mesh_send" | "mesh_config" | "mesh_trace" | "mesh_favorite" |
"mesh_request" | "mesh_node" | "mesh_reboot" | "mesh_cancel" | "mesh_conv"}
(this add-on's ROUTES) and gets {"t": "mesh", ...} back. What the add-on
system adds is the on/off switch: off stops the bridge and frees the node's
serial port (the meshtastic CLI needs it to itself), and the choice is kept
across restarts like any add-on's.

Config (station.json "mesh", or server.py --mesh-*, both become this):
    {"port": "/dev/meshtastic", "keys": ["HomeNet=~/.config/ranch-radio/keys/..."],
     (port: a serial device, ble:NAME, or tcp:HOST[:PORT] for a node on Wi-Fi;
      comma-separate several to try in order)
     "python": "...", "bridge": "...", "log": "...",
     "feed": "mqtt://BROKER/PREFIX", "feed_cred": "FILE", "feed_tx": false}
"feed" also publishes the node's traffic to an MQTT broker (mesh_feed.py).
"auto_resend" (default true) sends a message of ours again when the mesh
NAKs it for a reason it may get over, up to three more times (mesh.py);
false leaves that to the page's Retry.

More than one node (mesh-radios, 2026-11-21): "radios" lists the others,
each with its own bridge, log and engine, e.g. a LongFast monitor beside a
MediumFast node:
    {"port": "ble:NODE_A", "label": "MF18",
     "radios": [{"label": "LF", "port": "/dev/cu.usbmodem1"}]}
Each takes "port" (required), and optionally "label", "keys" (default: the
first node's), "log" (default: the first node's log name plus -<id>),
"feed"/"feed_cred"/"feed_tx" (default: none; one MQTT topic per node),
"auto_trace" (default false) and "auto_resend". The first node's events go
to the pages as before; another node's carry "node": its id (its label,
lower-cased), and a page sends "node" to pick which one a command is for
(none: the first). "mesh_select" answers one page with that node's snapshot.

Delete this file and the Mesh tab goes away; the radio page runs on.
"""

import json
import re

import os
from pathlib import Path
from types import SimpleNamespace

from addons import Addon

HERE = Path(__file__).resolve().parent.parent
OFF_WHY = "Switched off: the node's port is free for other apps"
RADIOS_MAX = 4
_ID = re.compile(r"[^a-z0-9]+")


def _flags(opts, where, trace_default):
    """auto_trace, auto_resend and auto_trace_per_hour, checked: a string
    "false" must never switch a transmitting feature on."""
    if not isinstance(opts.get("auto_trace", trace_default), bool):
        raise ValueError(f"{where} \"auto_trace\" must be true or false")
    if not isinstance(opts.get("auto_resend", True), bool):
        raise ValueError(f"{where} \"auto_resend\" must be true or false")
    per_hour = opts.get("auto_trace_per_hour", 6)
    if isinstance(per_hour, bool) or not isinstance(per_hour, int) or not 1 <= per_hour <= 30:
        raise ValueError(f"{where} \"auto_trace_per_hour\" must be a whole number 1..30")
    return opts.get("auto_trace", trace_default), opts.get("auto_resend", True), per_hour


class _Tagged:
    """The host as one extra node's engine sees it: every event it sends a
    page carries "node", and every notice names the node. Anything else the
    engine reads comes from the host itself."""

    def __init__(self, host, node, label):
        self._host, self._node, self._label = host, node, label

    def _tag(self, payload):
        d = json.loads(payload)
        d["node"] = self._node
        return json.dumps(d)

    def _send(self, ws, payload):
        return self._host._send(ws, self._tag(payload))

    def _broadcast(self, payload):
        self._host._broadcast(self._tag(payload))

    def notice(self, msg, level="info", ws=None):
        self._host.notice(f"{self._label}: {msg}", level, ws)

    def __getattr__(self, k):
        return getattr(self._host, k)


class MeshAddon(Addon):
    name = "mesh"
    group = "mesh"
    label = "Mesh"
    title = "Meshtastic node"
    page = False                 # its page is the Mesh tab (mesh.js), not an add-on panel
    routes = ("mesh_send", "mesh_config", "mesh_trace", "mesh_favorite",
              "mesh_request", "mesh_node", "mesh_reboot", "mesh_cancel", "mesh_conv", "mesh_select")

    def __init__(self, host, opts):
        super().__init__(host, opts)
        if not opts.get("port"):
            raise ValueError("mesh needs a \"port\" (the node: a serial device, ble:NAME or tcp:HOST)")
        trace, resend, per_hour = _flags(opts, "mesh", False)
        self.label = str(opts.get("label") or "Mesh")[:24]
        self.args = SimpleNamespace(
            mesh_port=opts["port"],
            mesh_python=opts.get("python", "~/.local/share/venvs/meshtastic/bin/python"),
            mesh_bridge=os.path.expanduser(opts.get("bridge", str(HERE / "mesh_bridge.py"))),
            mesh_key=list(opts.get("keys", [])),          # the bridge expands each FILE
            mesh_log=opts.get("log", "~/.local/state/radio-remote/mesh.jsonl"),
            mesh_feed=opts.get("feed"), mesh_feed_cred=opts.get("feed_cred"),
            mesh_feed_tx=bool(opts.get("feed_tx", False)),
            # Off by default: it transmits traceroutes on its own (mesh.py auto_trace_candidates).
            mesh_auto_trace=trace, mesh_auto_trace_per_hour=per_hour,
            # On by default: only a message the operator sent, only after the mesh NAKed it,
            # at most len(mesh.RESEND_S) more times (mesh.py _schedule_resend).
            mesh_auto_resend=resend)
        self.engine = None
        self._last = None             # the engine before the last switch-off
        self.extra = self._extra(opts.get("radios") or [])

    def _extra(self, radios):
        """The other nodes: [{"id", "label", "args", "engine", "last"}], checked
        up front so a mistake names its field instead of half-starting."""
        if not isinstance(radios, list) or len(radios) > RADIOS_MAX:
            raise ValueError(f"mesh \"radios\" must be a list of at most {RADIOS_MAX} nodes")
        out, ports, logs = [], {self.args.mesh_port}, {os.path.expanduser(self.args.mesh_log or "")}
        base = os.path.splitext(self.args.mesh_log or "~/.local/state/radio-remote/mesh.jsonl")[0]
        ids = {""}
        for i, r in enumerate(radios):
            where = f"mesh radios[{i}]"
            if not isinstance(r, dict) or not r.get("port"):
                raise ValueError(f"{where} needs a \"port\"")
            label = str(r.get("label") or f"Node {i + 2}")[:24]
            rid = _ID.sub("-", label.lower()).strip("-")[:24]
            if rid in ids:
                raise ValueError(f"{where} \"label\" {label!r} is not unique")
            if r["port"] in ports:
                raise ValueError(f"{where} \"port\" {r['port']!r} is already another node's")
            trace, resend, per_hour = _flags(r, where, False)
            log = r.get("log", f"{base}-{rid}.jsonl")
            if log and os.path.expanduser(log) in logs:
                raise ValueError(f"{where} \"log\" is already another node's")
            ids.add(rid); ports.add(r["port"]); logs.add(os.path.expanduser(log or ""))
            args = SimpleNamespace(
                mesh_port=r["port"], mesh_python=self.args.mesh_python, mesh_bridge=self.args.mesh_bridge,
                mesh_key=list(r.get("keys", self.args.mesh_key)), mesh_log=log,
                mesh_feed=r.get("feed"), mesh_feed_cred=r.get("feed_cred"), mesh_feed_tx=bool(r.get("feed_tx", False)),
                mesh_auto_trace=trace, mesh_auto_trace_per_hour=per_hour, mesh_auto_resend=resend)
            out.append({"id": rid, "label": label, "args": args, "engine": None, "last": None})
        return out

    def radios(self):
        """[{id, label, connected, error}]: the switcher's list, first node first (id "")."""
        def one(rid, label, e):
            return {"id": rid, "label": label, "connected": bool(e and e.connected),
                    "error": (e.error if e else OFF_WHY)}
        return [one("", self.label, self.engine)] + [one(x["id"], x["label"], x["engine"]) for x in self.extra]

    async def start(self):
        from mesh import MeshEngine
        # A fresh engine per start: a stopped one is closing for good. With a
        # log its messages come back from it; without one, carry them over.
        self.engine = MeshEngine(self.host, self.args)
        if self._last is not None and self.engine.log_path is None:
            self.engine.messages = self._last.messages
        if self._last is not None:
            self.engine.stats = self._last.stats      # the Health view's counts outlive a switch-off
        await self.engine.start()
        for x in self.extra:
            x["engine"] = MeshEngine(_Tagged(self.host, x["id"], x["label"]), x["args"])
            if x["last"] is not None:
                if x["engine"].log_path is None:
                    x["engine"].messages = x["last"].messages
                x["engine"].stats = x["last"].stats
            await x["engine"].start()

    async def stop(self):
        for x in self.extra:
            if x["engine"] is not None:
                await x["engine"].stop()
                x["last"], x["engine"] = x["engine"], None
        if self.engine is not None:
            await self.engine.stop()
            self._last, self.engine = self.engine, None

    def snapshot(self):
        snap = (self.engine.snapshot() if self.engine is not None
                else {"off": True, "status": {"connected": False, "error": OFF_WHY}})
        if self.extra:
            snap["radios"] = self.radios()
        return snap

    def _pick(self, m, ws):
        """The engine a page's command is for ("node": none or "" is the first), or None (said)."""
        node = m.get("node") or ""
        if node == "":
            return self.engine
        x = next((x for x in self.extra if x["id"] == node), None)
        if x is None:
            self.host.notice(f"Mesh: no node {str(node)[:24]!r} on this station.", "warn", ws)
            return None
        return x["engine"]

    async def command(self, ws, verb, m):
        e = self._pick(m, ws)
        if e is None:
            return
        if verb == "mesh_select":
            # One page's view of one node: its whole snapshot, to that page only.
            payload = {"t": "mesh", "select": e.snapshot()}
            if self.extra:
                payload["select"]["radios"] = self.radios()
            node = m.get("node") or ""
            if node:
                payload["node"] = node
            await self.host._send(ws, json.dumps(payload))
            return
        if verb == "mesh_send":
            to = m.get("to")             # reply_to is type-checked by the engine
            await e.send_text(ws, m["text"], int(m["ch"]), str(to) if to else None,
                              m.get("reply_to"), bool(m.get("emoji")))
        elif verb == "mesh_config":
            await e.set_config(ws, str(m["section"]), m["values"])
        elif verb == "mesh_trace":
            await e.traceroute(ws, str(m["to"]))
        elif verb == "mesh_favorite":
            await e.set_favorite(ws, str(m["to"]), bool(m["fav"]))
        elif verb == "mesh_request":
            await e.request(ws, str(m["to"]), str(m["kind"]))
        elif verb == "mesh_node":
            await e.node_action(ws, str(m["to"]), str(m["action"]))  # "do" is the router's
        elif verb == "mesh_reboot":
            await e.reboot(ws)
        elif verb == "mesh_cancel":
            t = m.get("msg_t")
            if isinstance(t, bool) or not isinstance(t, (int, float)):
                self.host.notice("Not a message to cancel.", "warn", ws)
                return
            await e.cancel(ws, float(t))
        elif verb == "mesh_conv":
            await e.conv(ws, m)          # archive, mute, pin, read, delete, undo (mesh_convs.py)
        else:
            await super().command(ws, verb, m)


ADDON = MeshAddon

