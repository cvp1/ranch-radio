"""The Grids add-on (addons/grids.py, grid_targets.py):

  - a good block loads, on, in the tools group, with no error, starts, stops
  - in the module catalog and the manifest
  - grid_targets: the Lower 48 is 488 valid, unique squares, ours among them
  - without the logbook sync: the snapshot says why, and tick sends nothing
  - before the needs index is built: not ready, no cells
  - with it: the cells from the index, the Lower-48 set, the stations on the
    roster that carry a grid (4 characters, upper case) and our own grid;
    decodes without a grid are left out
  - tick pushes the tally once per index (again only when the sync replaces
    it) and the on-the-air list only when the roster's grids change
  - "refresh" answers the asking page alone with the whole snapshot
  - a roster that raises does not stop the loop (it logs and goes on)

    python3 tests/grids_addon_test.py
"""
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import grid_targets  # noqa: E402
import logsync_needs as N  # noqa: E402
from addons import Addons  # noqa: E402
from addons import grids as G  # noqa: E402
from ranch_radio import catalog, manifest  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


class Host:
    def __init__(self):
        self.sent, self.to = [], []
        self.logsync = None

    def notice(self, *a):
        pass

    def _broadcast(self, payload):
        self.sent.append(json.loads(payload))

    async def _send(self, ws, payload):
        self.to.append((ws, json.loads(payload)))


class Sync:
    """The logbook sync as the add-on sees it: its needs index and map_view."""
    def __init__(self):
        self.needs, self.decodes, self.me, self.boom = None, [], "EM12", False

    def map_view(self, band=None):
        if self.boom:
            raise RuntimeError("roster broke")
        return {"me": self.me, "band": "20m", "showing": "all", "grids": {}, "decodes": self.decodes}


reg = Addons(Host(), {"grids": {"enabled": True}}, state_path=None)
d = {x["name"]: x for x in reg.describe()}["grids"]
check("loads, on, in its group, no error", (d["label"], d["group"], d["on"], d["error"]), ("Grids", "tools", True, None))
async def start_stop():                       # one loop: start's tick task must be stopped on the loop that made it
    await reg.start()
    up = reg.items["grids"]["running"]
    await reg.stop()
    return up, reg.items["grids"]["running"]
check("starts, then stops", asyncio.run(start_stop()), (True, False))
check("in the catalog (ranch_radio/catalog.py)", "grids" in catalog.by_name(), True)
check("in the manifest, an add-on in the addons block (ranch_radio/manifest.py)",
      (manifest.MANIFESTS.get("grids") or {}).get("block"), ("addons", "grids"))

L = grid_targets.LOWER_48
check("Lower 48: 488 squares, each a valid one", (len(L), all(re.fullmatch(r"[A-R]{2}[0-9]{2}", g) for g in L)), (488, True))
check("Lower 48: the corners and ours in, Hawaii and Alaska out",
      ([g in L for g in ("EM12", "CN85", "FN54", "EL95", "DL88")], "BL11" in L, "BP51" in L), ([True] * 5, False, False))

host = Host()
a = G.Grids(host, {})
check("no logbook sync: the snapshot says why", a.snapshot(), {"error": G.NO_SYNC})
a.tick()
check("no logbook sync: tick sends nothing", host.sent, [])

sync = Sync()
host.logsync = sync
check("index not built yet: not ready, no cells", {k: a.snapshot()[k] for k in ("ready", "cells")}, {"ready": False, "cells": {}})

idx = N.Needs()
idx.add("W1AW", "20m", "FT8", "", "<GRIDSQUARE:4>FN31", True)
idx.add("K7ABC", "40m", "MFSK", "FT4", "<GRIDSQUARE:6>DM42ab", False)
sync.needs = idx
sync.decodes = [{"call": "N9ABC", "grid": "em10", "band": "20m", "mode": "FT8", "snr": -12},
                {"call": "N0CALL", "grid": "", "band": "20m", "mode": "FT8", "snr": -3}]
s = a.snapshot()
check("ready, with the index's cells and QSO count",
      (s["ready"], s["cells"], s["qsos"]), (True, {"FN31": [["20m", "FT8", 1]], "DM42": [["40m", "FT4", 0]]}, 2))
check("the Lower-48 set goes to the page", (s["sets"]["l48"]["label"], len(s["sets"]["l48"]["grids"])), ("Lower 48", 488))
check("on the air: only decodes with a grid, 4 characters, upper case; our grid",
      (s["heard"], s["me"]), ([{"grid": "EM10", "call": "N9ABC", "band": "20m", "mode": "FT8", "snr": -12}], "EM12"))

host.sent.clear()
a.tick()
check("first tick: the tally and the on-the-air list", [sorted(k for k in p if k not in ("t", "addon")) for p in host.sent],
      [["cells", "qsos", "ready", "sets"], ["heard", "me"]])
host.sent.clear()
a.tick()
check("nothing changed: tick sends nothing", host.sent, [])
sync.decodes = sync.decodes + [{"call": "N9ABD", "grid": "EM11", "band": "20m", "mode": "FT8", "snr": -20}]
a.tick()
check("a new grid on the roster: only the on-the-air list", [sorted(k for k in p if k not in ("t", "addon")) for p in host.sent],
      [["heard", "me"]])
host.sent.clear()
sync.needs = N.Needs()                     # the sync rebuilt its index
a.tick()
check("a rebuilt index: the tally again", [p.get("ready") for p in host.sent], [True])


async def refresh():
    await a.command("WS", "refresh", {})
    await asyncio.sleep(0)
asyncio.run(refresh())
check("refresh: the whole snapshot to the asking page only", (host.to[-1][0], "cells" in host.to[-1][1], "heard" in host.to[-1][1]),
      ("WS", True, True))


async def loop_survives():
    sync.boom = True
    G.TICK_S = 0.01
    t = asyncio.ensure_future(a._tick_loop())
    await asyncio.sleep(0.05)
    alive = not t.done()
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    return alive
check("a roster that raises does not stop the loop", asyncio.run(loop_survives()), True)

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
