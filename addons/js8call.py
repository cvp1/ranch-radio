"""JS8 add-on: the station's JS8 tab, from its JS8 engines (receive only).

The tab shows, per radio with a JS8 engine:

- the engine's state: connected, the rig's frequency and mode, and TX
  disarmed, or an UNSAFE banner if the engine ever reports TX, auto-reply
  or heartbeat on (js8_engine.py disarms it at every start);
- band activity: the latest text heard on each audio offset;
- messages: complete decodes (from, to, text);
- stations heard: call, SNR, grid, when and where.

Everything comes from the station's JS8 bridge (js8_bridge.py, the "js8"
block station.json gets from the js8 module); this add-on only relays it.
Read-only: nothing is sent to an engine or put on the air. Without the
bridge the tab says why.

Options (the host file's addons block):

    "js8call": {"enabled": true}
"""
import logging

from . import Addon

log = logging.getLogger("addons.js8call")
NO_BRIDGE = "JS8 isn't set up on this station: add the js8 module (ranch-radio add js8) and a js8_port on a radio"


class Js8call(Addon):
    name = "js8call"
    label = "JS8"
    title = "JS8 band activity, directed messages and stations heard, from the station's JS8 engines (receive only)"
    group = "messaging"
    css = True

    def __init__(self, host, opts):
        super().__init__(host, opts)
        self._bridge = None

    def _js8(self):
        return getattr(self.host, "js8", None)

    async def start(self):
        b = self._js8()
        if b is not None and self._relay not in b.listeners:
            b.listeners.append(self._relay)
            self._bridge = b

    async def stop(self):
        if self._bridge is not None and self._relay in self._bridge.listeners:
            self._bridge.listeners.remove(self._relay)
        self._bridge = None

    def _relay(self, msg):
        if msg.get("t") == "js8":
            self.emit({"event": "frame", "radio": msg["radio"], "frame": msg["frame"]})
        elif msg.get("t") == "js8_status":
            self.emit({"event": "status", "radio": msg["radio"], "status": msg["status"]})

    def snapshot(self):
        b = self._js8()
        if b is None:
            why = getattr(self.host, "js8_error", None) or NO_BRIDGE
            return {"ready": False, "why": why}
        names = {r.id: getattr(r, "name", r.id) for r in getattr(self.host, "radios", [])}
        return {"ready": True, "radios": {rid: dict(e.snapshot(), name=names.get(rid, rid))
                                          for rid, e in b.engines.items()}}


ADDON = Js8call
