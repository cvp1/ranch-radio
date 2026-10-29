"""The Modules tab's inventory (ranch_radio/inventory.py) and doctor's budget.

Falsifiers from plan revision 2 (M1):
  - a running Mesh (in Addons.items, not in describe()) reads "on"
  - the macOS widget in the host file reads installed / install pending, never "pending"
  - an add-on with an error and on=False reads "broken", not "off"
  - mesh in the host file but not in the runtime reads "pending"
  - an add-on running but gone from the host file reads "removed"
  - a stale page "off" beside the host file's enabled=true says so
  - doctor with its budget spent reports the rest as not checked

    python3 tests/inventory_test.py
"""
import copy
import json
import sys
import os as _os
# Host files come from examples/hosts/ (or a scratch copy) only, never a
# real ~/.config/ranch-radio/hosts on the machine running the tests.
_os.environ["RANCH_RADIO_HOSTS"] = ""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ranch_radio import doctor, inventory  # noqa: E402
from ranch_radio.render import Artifact  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


mac = json.loads((ROOT / "examples/hosts/mac-shack.json").read_text())
cam = json.loads((ROOT / "examples/hosts/linux-station.json").read_text())


def items(h, runtime, files=(), checks=()):
    return {i["name"]: i for i in inventory.build(h, runtime, list(files), list(checks))}


ALL_ON = {"winlink": {"on": True, "error": None, "loaded": True},
          "mesh": {"on": True, "error": None, "loaded": True},
          "opengd77": {"on": True, "error": None, "loaded": True}}

it = items(mac, ALL_ON)
check("running Mesh reads on", it["mesh"]["state"], "on")
check("  with a switch", it["mesh"]["switch"]["on"], True)
check("running Winlink reads on", it["winlink"]["state"], "on")
check("a module this OS doesn't ship reads unsupported", it["uvpro"]["state"], "unsupported")
check("the packet hub on a Mac: available (a Mac renders it since the Direwolf TNC)", it["packet"]["state"], "available")

art_w = Artifact("macos-widget", "Library/LaunchAgents/w.plist", "plist", {})
it = items(mac, ALL_ON, [(art_w, None, "same")])
check("the widget, installed, reads installed", it["macos-widget"]["state"], "installed")
it = items(mac, ALL_ON, [(art_w, None, "missing")])
check("the widget, not installed, reads install_pending (never pending)", it["macos-widget"]["state"], "install_pending")
check("  and has no switch (no run-time on/off)", it["macos-widget"]["switch"], None)

rt = copy.deepcopy(ALL_ON)
rt["opengd77"] = {"on": False, "error": "not loaded: ImportError: x", "loaded": False}
check("an error with on=False reads broken", items(mac, rt)["opengd77"]["state"], "broken")
check("  and says why", items(mac, rt)["opengd77"]["detail"], "not loaded: ImportError: x")
rt["opengd77"] = {"on": True, "error": "did not start: OSError: busy", "loaded": True}
check("an error wins over on=True too", items(mac, rt)["opengd77"]["state"], "broken")

rt = copy.deepcopy(ALL_ON)
rt["opengd77"]["on"] = False
it = items(mac, rt)["opengd77"]
check("switched off reads off", it["state"], "off")
check("  beside the host file's enabled=true", it["switch"]["enabled"], True)
check("  and says the page's switch wins", "page's switch wins" in it["detail"], True)

rt = {k: v for k, v in ALL_ON.items() if k != "mesh"}
check("mesh in the host file, not running here, reads pending", items(mac, rt)["mesh"]["state"], "pending")

h = copy.deepcopy(mac)
del h["addons"]
check("running but gone from the host file reads removed", items(h, ALL_ON)["opengd77"]["state"], "removed")

it = items(cam, {"uvpro": {"on": True, "error": None, "loaded": True}})
check("linux-station: packet, a service, is never pending", it["packet"]["state"] in ("installed", "install_pending", "configured"), True)
check("linux-station: the omarchy bar is configured (its own installer)", it["omarchy-bar"]["state"], "configured")
check("linux-station: the macOS widget is unsupported", it["macos-widget"]["state"], "unsupported")

cv = json.loads((ROOT / "examples/hosts/linux-laptop.json").read_text())
it = items(cv, {})
p = it["packet"]
check("an available module carries its add command", p["command"].startswith("./ranch-radio add packet --host linux-laptop"), True)
check("  and the fields only you can give, with their rules",
      [(a["field"], a["rule"]) for a in p["asks"]], [("tnc", "tnc"), ("mycall", "callsign")])
check("radios come first after core, with their profile", [i["kind"] for i in inventory.build(cv, {}, [], [])][:2],
      ["core", "radio"])

checks = doctor.static(cv) + [doctor.Check("mesh", doctor.MISSING, "meshtastic does not import", "pip install")]
check("needs are attached to their module", items(cv, {}, [], checks)["mesh"]["needs"][0]["status"], "missing")

# doctor's budget: once spent, the rest are reported, not run
h = copy.deepcopy(mac)
h["python"] = sys.executable
out = doctor.machine(h, budget_s=1e-9, include_available=True)
unchecked = [c for c in out if c.status == doctor.UNCHECKED]
check("doctor with no budget left runs no venv or binary checks", len(unchecked) > 3, True)
check("  and says so in words", all("not checked" in c.what for c in unchecked), True)
hv = copy.deepcopy(cv)
hv["python"] = sys.executable
mods = {c.module for c in doctor.machine(hv, budget_s=None, include_available=True)}
check("with time, available modules' needs are checked too (linux-laptop: winlink, uvpro)",
      {"winlink", "uvpro"} <= mods, True)
mods = {c.module for c in doctor.machine(hv, budget_s=None, include_available=False)}
check("  and only with include_available", "uvpro" in mods, False)

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
