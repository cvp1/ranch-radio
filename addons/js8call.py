"""JS8 add-on: the station's JS8 tab, from its JS8 engines.

The tab shows, per radio with a JS8 engine:

- the engine's state: connected, the rig's frequency and mode, and TX
  disarmed, or an UNSAFE banner if the engine ever reports TX, auto-reply
  or heartbeat on (js8_engine.py disarms it at every start);
- band activity: the latest text heard on each audio offset;
- messages: complete decodes (from, to, text);
- stations heard: call, SNR, grid, when and where.

- sending: Arm (per radio, for minutes; js8_bridge.ARM_MAX_S at most);
  while armed HB, CQ, the directed queries to a picked station (SNR?,
  GRID?, INFO?, STATUS?, HEARING?), a message box, offset and speed; Disarm. The arm is the page's: closing
  it, the radio's STOP TX, or the time running out disarms the engine.

Everything comes from the station's JS8 bridge (js8_bridge.py, the "js8"
block station.json gets from the js8 module); this add-on relays it, and
hands the page's arm, send and disarm to the bridge, which holds every
gate. Without the bridge the tab says why.

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
    title = "JS8 band activity, directed messages and stations heard, and sending while armed"
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
        elif msg.get("t") == "js8_spectrum":
            self.emit({"event": "spectrum", "radio": msg["radio"], "hz": msg["hz"], "row": msg["row"]})

    async def command(self, ws, verb, m):
        b = self._js8()
        if b is None:
            self.notice(getattr(self.host, "js8_error", None) or NO_BRIDGE, "warn", ws)
            return
        if verb not in ("arm", "disarm", "send", "hb"):
            self.notice(f"JS8: unknown command {verb!r}.", "warn", ws)
            return
        try:
            why = await b.handle(ws, {"do": verb, "radio": m.get("radio"), "minutes": m.get("minutes", 10),
                                      "text": m.get("text"), "offset_hz": m.get("offset_hz"),
                                      "speed": m.get("speed")})
        except ValueError as e:
            why = str(e)
        if why:
            self.notice(why, "warn", ws)

    def snapshot(self):
        b = self._js8()
        if b is None:
            why = getattr(self.host, "js8_error", None) or NO_BRIDGE
            return {"ready": False, "why": why}
        names = {r.id: getattr(r, "name", r.id) for r in getattr(self.host, "radios", [])}
        return {"ready": True, "radios": {rid: dict(e.snapshot(), name=names.get(rid, rid))
                                          for rid, e in b.engines.items()}}


ADDON = Js8call
