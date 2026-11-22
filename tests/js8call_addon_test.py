"""The JS8 tab's add-on (addons/js8call.py): it relays the station's JS8 bridge.

  - a good block loads, on, in the messaging group, with no error, and starts
  - no bridge on the station: not ready, and it says how to set JS8 up
  - a bridge: ready, each radio's frames, heard and status, with its name
  - relay: a bridge push reaches the pages as {"t": "addon", "addon": "js8call"};
    a waterfall row (to listeners only) reaches them once, as its spectrum event
  - stop takes its listener off the bridge; a failing listener never stops a push
  - commands: arm, send, hb and disarm go to the bridge with only radio,
    minutes, text, offset and speed; a refusal or a malformed request comes back to that page as a
    notice; any other verb is refused and never reaches the bridge
  - it is in the module catalog and the manifest

    python3 tests/js8call_addon_test.py
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import js8_bridge  # noqa: E402
from addons import Addons  # noqa: E402
from ranch_radio import catalog, manifest  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


class Host:
    def notice(self, *a):
        pass

    def _broadcast(self, payload):
        pass

    async def _send(self, ws, payload):
        pass


reg = Addons(Host(), {"js8call": {"enabled": True}}, state_path=None)
d = {x["name"]: x for x in reg.describe()}["js8call"]
check("loads, on, in its group, no error", (d["label"], d["group"], d["on"], d["error"]), ("JS8", "messaging", True, None))
snap = reg.snapshot()["js8call"]
check("no bridge: not ready, says how", (snap["ready"], "ranch-radio add js8" in snap["why"]), (False, True))
asyncio.run(reg.start())
check("starts", reg.items["js8call"]["running"], True)
asyncio.run(reg.stop())
check("stops", reg.items["js8call"]["running"], False)



class Radio:
    def __init__(self, rid, name):
        self.id, self.name = rid, name


class BridgeHost(Host):
    def __init__(self):
        self.radios = [Radio("ic7300", "IC-7300")]
        self.pushed = []
        self.js8 = js8_bridge.Js8Bridge({"radios": {"ic7300": 4537}}, self)

    def _broadcast(self, payload):
        self.pushed.append(json.loads(payload))


host = BridgeHost()
reg = Addons(host, {"js8call": {"enabled": True}}, state_path=None)
asyncio.run(reg.start())
snap = reg.snapshot()["js8call"]
check("a bridge: ready, the radio by name", (snap["ready"], snap["radios"]["ic7300"]["name"]), (True, "IC-7300"))
check("a bridge: frames, heard and status", sorted(snap["radios"]["ic7300"]), ["frames", "heard", "name", "radio", "status"])
check("its listener is on the bridge", len(host.js8.listeners), 1)
host.js8.listeners.insert(0, lambda m: 1 / 0)                  # another listener that fails
host.js8._push({"t": "js8", "radio": "ic7300", "frame": {"kind": "frame", "text": "N9ABC: CQ"}})
host.js8._push({"t": "js8_status", "radio": "ic7300", "status": {"connected": True}})
relayed = [m for m in host.pushed if m.get("t") == "addon" and m.get("addon") == "js8call"]
check("relay: a frame and a status reach the pages as the add-on's",
      [(m["event"], m["radio"]) for m in relayed], [("frame", "ic7300"), ("status", "ic7300")])
check("relay: the bridge's own broadcast still goes out", [m["t"] for m in host.pushed if m["t"] != "addon"],
      ["js8", "js8_status"])
n = len(host.pushed)
host.js8._listeners({"t": "js8_spectrum", "radio": "ic7300", "hz": 10, "row": [0] * 300})
check("relay: a waterfall row reaches the pages as the add-on's spectrum event, once",
      [(m.get("t"), m.get("event"), m.get("hz"), len(m.get("row") or [])) for m in host.pushed[n:]],
      [("addon", "spectrum", 10, 300)])
asyncio.run(reg.stop())
check("stop takes its listener off", [fn for fn in host.js8.listeners if getattr(fn, "__self__", None) is not None], [])



class StubBridge:
    def __init__(self):
        self.calls, self.listeners, self.engines = [], [], {}

    async def handle(self, ws, m):
        self.calls.append((ws, m))
        if m["do"] == "send" and m["text"] == "bad":
            raise ValueError("JS8 send: text must be a string")
        return "Can't arm JS8: transmit is off on this radio (--no-tx)." if m["do"] == "arm" else None


class CmdHost(Host):
    def __init__(self):
        self.radios, self.notices, self.js8 = [], [], StubBridge()

    def notice(self, msg, level="info", ws=None):
        self.notices.append((msg, ws))


ch = CmdHost()
reg = Addons(ch, {"js8call": {"enabled": True}}, state_path=None)
page = object()
for m in ({"do": "arm", "radio": "ic7300", "minutes": 5, "extra": "dropped"}, {"do": "send", "radio": "ic7300", "text": "CQ"},
          {"do": "send", "radio": "ic7300", "text": "bad"}, {"do": "disarm", "radio": "ic7300"},
          {"do": "hb", "radio": "ic7300"},
          {"do": "radio.ptt.set", "radio": "ic7300"}):
    asyncio.run(reg.command(page, dict(m, t="addon", addon="js8call")))
check("commands reach the bridge: arm, send, send, disarm, hb", [m["do"] for _, m in ch.js8.calls],
      ["arm", "send", "send", "disarm", "hb"])
check("only radio, minutes, text, offset, speed are passed", sorted(ch.js8.calls[0][1]),
      ["do", "minutes", "offset_hz", "radio", "speed", "text"])
check("a refusal goes to that page", ch.notices[0], ("Can't arm JS8: transmit is off on this radio (--no-tx).", page))
check("a malformed request goes to that page", ch.notices[1], ("JS8 send: text must be a string", page))
check("another verb refused, never reaches the bridge", ("radio.ptt.set" in ch.notices[-1][0], len(ch.js8.calls)), (True, 5))
ch.js8 = None
asyncio.run(reg.command(page, {"t": "addon", "addon": "js8call", "do": "arm", "radio": "ic7300"}))
check("no bridge: arm says how to set JS8 up", "ranch-radio add js8" in ch.notices[-1][0], True)

check("in the catalog (ranch_radio/catalog.py)", "js8call" in catalog.by_name(), True)
check("in the manifest, an add-on in the addons block (ranch_radio/manifest.py)",
      (manifest.MANIFESTS.get("js8call") or {}).get("block"), ("addons", "js8call"))

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
