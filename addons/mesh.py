"""Mesh: a Meshtastic node on the page, as an add-on (mesh.py does the work).

The first add-on with its own tab rather than an add-on panel: its page is
the Mesh tab (static/mesh.js), so it keeps that tab's wire protocol. The page
sends {"t": "mesh_send" | "mesh_config" | "mesh_trace" | "mesh_favorite" |
"mesh_request" | "mesh_node" | "mesh_reboot"}
(this add-on's ROUTES) and gets {"t": "mesh", ...} back. What the add-on
system adds is the on/off switch: off stops the bridge and frees the node's
serial port (the meshtastic CLI needs it to itself), and the choice is kept
across restarts like any add-on's.

Config (station.json "mesh", or server.py --mesh-*, both become this):
    {"port": "/dev/meshtastic", "keys": ["HomeNet=~/.config/ranch-radio/keys/..."],
     "python": "...", "bridge": "...", "log": "...",
     "feed": "mqtt://BROKER/PREFIX", "feed_cred": "FILE", "feed_tx": false}
"feed" also publishes the node's traffic to an MQTT broker (mesh_feed.py).

Delete this file and the Mesh tab goes away; the radio page runs on.
"""

import os
from pathlib import Path
from types import SimpleNamespace

from addons import Addon

HERE = Path(__file__).resolve().parent.parent
OFF_WHY = "Switched off: the node's port is free for other apps"


class MeshAddon(Addon):
    name = "mesh"
    group = "mesh"
    label = "Mesh"
    title = "Meshtastic node"
    page = False                 # its page is the Mesh tab (mesh.js), not an add-on panel
    routes = ("mesh_send", "mesh_config", "mesh_trace", "mesh_favorite",
              "mesh_request", "mesh_node", "mesh_reboot")

    def __init__(self, host, opts):
        super().__init__(host, opts)
        if not opts.get("port"):
            raise ValueError("mesh needs a \"port\" (the node's serial device)")
        if not isinstance(opts.get("auto_trace", False), bool):
            raise ValueError("mesh \"auto_trace\" must be true or false")   # "false" must not switch TX on
        per_hour = opts.get("auto_trace_per_hour", 6)
        if isinstance(per_hour, bool) or not isinstance(per_hour, int) or not 1 <= per_hour <= 30:
            raise ValueError("mesh \"auto_trace_per_hour\" must be a whole number 1..30")
        self.args = SimpleNamespace(
            mesh_port=opts["port"],
            mesh_python=opts.get("python", "~/.local/share/venvs/meshtastic/bin/python"),
            mesh_bridge=os.path.expanduser(opts.get("bridge", str(HERE / "mesh_bridge.py"))),
            mesh_key=list(opts.get("keys", [])),          # the bridge expands each FILE
            mesh_log=opts.get("log", "~/.local/state/radio-remote/mesh.jsonl"),
            mesh_feed=opts.get("feed"), mesh_feed_cred=opts.get("feed_cred"),
            mesh_feed_tx=bool(opts.get("feed_tx", False)),
            # Off by default: it transmits traceroutes on its own (mesh.py auto_trace_candidates).
            mesh_auto_trace=opts.get("auto_trace", False), mesh_auto_trace_per_hour=per_hour)
        self.engine = None
        self._last = None             # the engine before the last switch-off

    async def start(self):
        from mesh import MeshEngine
        # A fresh engine per start: a stopped one is closing for good. With a
        # log its messages come back from it; without one, carry them over.
        self.engine = MeshEngine(self.host, self.args)
        if self._last is not None and self.engine.log_path is None:
            self.engine.messages = self._last.messages
        await self.engine.start()

    async def stop(self):
        if self.engine is not None:
            await self.engine.stop()
            self._last, self.engine = self.engine, None

    def snapshot(self):
        if self.engine is not None:
            return self.engine.snapshot()
        return {"off": True, "status": {"connected": False, "error": OFF_WHY}}

    async def command(self, ws, verb, m):
        e = self.engine
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
        else:
            await super().command(ws, verb, m)


ADDON = MeshAddon

