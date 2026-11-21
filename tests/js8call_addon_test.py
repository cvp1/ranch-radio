"""The JS8 tab's add-on (addons/js8call.py): it relays the station's JS8 bridge.

  - a good block loads, on, in the messaging group, with no error, and starts
  - no bridge on the station: not ready, and it says how to set JS8 up
  - a bridge: ready, each radio's frames, heard and status, with its name
  - relay: a bridge push reaches the pages as {"t": "addon", "addon": "js8call"}
  - stop takes its listener off the bridge; a failing listener never stops a push
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
asyncio.run(reg.stop())
check("stop takes its listener off", [fn for fn in host.js8.listeners if getattr(fn, "__self__", None) is not None], [])

check("in the catalog (ranch_radio/catalog.py)", "js8call" in catalog.by_name(), True)
check("in the manifest, an add-on in the addons block (ranch_radio/manifest.py)",
      (manifest.MANIFESTS.get("js8call") or {}).get("block"), ("addons", "js8call"))

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
