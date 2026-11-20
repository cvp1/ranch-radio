"""Grids add-on: the grid squares you still need, by operating mode and band
(GridTracker2's grid tracking, from the station's own logbook).

The Grids tab shows, for the mode and band you pick (FT8, FT4, SSB, CW...;
one band or all):

- a tally per mode: Lower-48 grids worked, confirmed and missing, and grids
  worked anywhere in the world;
- the Lower 48 as a lattice of its 488 squares, each confirmed, worked (not
  yet confirmed) or missing; your own square marked;
- the missing squares as a list, nearest first, and any of them on the air
  right now (a station JTDX decoded in it in the last two minutes) at the top.

"Missing" counts against the 488 squares of the contiguous US (grid_targets:
ARRL's FFMA set, GridTracker2's TL48), the target an HF station in the US
can actually close; the world tally is worked/confirmed only.

Everything comes from the logbook sync (logsync.py): worked and confirmed
from its needs index (logsync_needs.grid_cells: every QSO in the ledger,
confirmed meaning LoTW or QRZ confirmed it), rebuilt by the sync when the
ledger grows and never here; on the air from its roster. Read-only: nothing
is written, sent or put on the air. Without the logbook sync the tab says so.

Options (the host file's addons block):

    "grids": {"enabled": true}
"""
import asyncio
import logging
import time

from . import Addon

log = logging.getLogger("addons.grids")
TICK_S = 15                      # how often a rebuilt index or new decodes are looked for (a JTDX cycle)
NO_SYNC = "the logbook sync isn't running on this station, so there are no grids to count"


class Grids(Addon):
    name = "grids"
    label = "Grids"
    title = "The grids you still need, by mode and band"
    group = "tools"
    css = True

    def __init__(self, host, opts):
        super().__init__(host, opts)
        self.clock = time.time
        self._needs_sent = None          # the needs index the pages have (the sync replaces it on a rebuild)
        self._heard_sent = None          # the on-the-air list the pages have
        self._tasks = []

    async def start(self):
        self._tasks = [asyncio.ensure_future(self._tick_loop())]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._needs_sent = self._heard_sent = None

    def _sync(self):
        return getattr(self.host, "logsync", None)

    def _needs(self):
        ls = self._sync()
        return getattr(ls, "needs", None) if ls is not None else None

    # --- what the pages get

    def _cells_part(self):
        import grid_targets
        n = self._needs()
        if n is None:
            return {"ready": False, "cells": {}, "qsos": 0, "sets": {}}
        return {"ready": True, "cells": n.grid_cells(), "qsos": n.qsos,
                "sets": {k: {"label": lab, "grids": sorted(gs)} for k, (lab, gs) in grid_targets.SETS.items()}}

    def heard(self):
        """The stations on the roster now that carry a grid, and our own grid:
        ([{grid, call, band, mode, snr}], me)."""
        ls = self._sync()
        if ls is None or not hasattr(ls, "map_view"):
            return [], ""
        mv = ls.map_view("all")
        out = [{"grid": d["grid"][:4].upper(), "call": d["call"], "band": d.get("band") or "",
                "mode": d.get("mode") or "", "snr": d.get("snr")} for d in mv.get("decodes", []) if d.get("grid")]
        return sorted(out, key=lambda h: (h["grid"], h["call"])), mv.get("me", "")

    def _heard_part(self):
        heard, me = self.heard()
        return {"heard": heard, "me": me}

    def snapshot(self):
        if self._sync() is None:
            return {"error": NO_SYNC}
        return {**self._cells_part(), **self._heard_part()}

    def tick(self):
        """Push what changed: the whole tally when the sync rebuilt its index,
        the on-the-air list when the roster's grids changed; nothing otherwise."""
        if self._sync() is None:
            return
        n = self._needs()
        if n is not self._needs_sent:
            self._needs_sent = n
            self.emit(self._cells_part())
        part = self._heard_part()
        stamp = tuple((h["grid"], h["call"], h["band"], h["mode"]) for h in part["heard"])
        if stamp != self._heard_sent:
            self._heard_sent = stamp
            self.emit(part)

    async def _tick_loop(self):
        while True:
            try:
                self.tick()
            except Exception as e:           # a broken roster or index must not stop the tab
                log.warning("grids: %s", type(e).__name__)
            await asyncio.sleep(TICK_S)

    async def command(self, ws, verb, m):
        if verb == "refresh":
            self.emit(self.snapshot(), ws)
            return
        await super().command(ws, verb, m)


ADDON = Grids
