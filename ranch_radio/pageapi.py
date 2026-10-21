"""The Modules tab's server side, shared by server.py (a Mac's one radio) and
station.py (a Linux station). A Linux radio server behind a station has none:
its page is embedded, and the station runs the add-ons.

Messages on the page's WebSocket:
    {"t": "modules"}         -> {"t": "modules", ...} to that page (inventory.report)
    {"t": "modules_check"}   run doctor again; then {"t": "modules"} to every page
    {"t": "module_plan", "do": "add"|"remove", "module": N, "set": {...}}
                             -> {"t": "module_plan", ...} to that page   (modules.py)
    {"t": "module_apply", "plan": ID}
                             write the host file; {"t": "module_applied"} to that page,
                             {"t": "modules"} to every page

Nothing here blocks the event loop: the host file, the installed files and doctor
are read in worker threads (doctor's venv pythons can take seconds each), and the
radio's safety loop runs on regardless (review 2026-10-21, G4/G6). Nothing here
installs or restarts anything.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from . import doctor, inventory, modules, paths, render

log = logging.getLogger("modules")

TYPES = ("modules", "modules_check", "module_plan", "module_apply")
DOCTOR_BUDGET_S = 60


class ModulesService:
    def __init__(self, host, repo, registry, send, broadcast, home=None, budget_s=DOCTOR_BUDGET_S,
                 state_path=None):
        """host: the host file's name (hosts/<host>.json in repo); registry:
        a callable giving the process's addons.Addons (or None); send(ws, str)
        a coroutine, broadcast(str) not."""
        self.host = host
        self.repo = Path(repo)
        self.registry = registry
        self.send = send
        self.broadcast = broadcast
        self.home = home or os.path.expanduser("~")
        self.budget_s = budget_s
        self.machine = None            # doctor.machine's last result
        self.checked_at = None
        self._doctor = None            # the one doctor run in flight (a Future)
        # The page's add/remove plans (M3/M4). state_path: the add-on switches'
        # file, known even before this process runs any add-on.
        self.plans = modules.Plans(state_path)

    @property
    def path(self):
        return paths.find(self.repo / "hosts", self.host) or self.repo / "hosts" / f"{self.host}.json"

    # --- reading (worker threads)

    def _runtime(self):
        """Read on the loop thread: the registry isn't thread-safe."""
        reg = self.registry()
        return reg.runtime() if reg is not None else {}

    def _gather(self):
        h = render.load(self.path)
        files = list(render.installed_states(h, self.home))
        return h, files

    async def report(self):
        runtime = self._runtime()
        try:
            h, files = await asyncio.to_thread(self._gather)
        except Exception as e:            # a host file that doesn't load: say so, don't crash
            return {"t": "modules", "host": self.host, "host_file": False,
                    "error": f"hosts/{self.host}.json: {type(e).__name__}: {e}", "items": []}
        checks = doctor.static(h) + (self.machine or [])
        return inventory.report(h, runtime, files, checks, self.checked_at, self._doctor is not None)

    def _start_doctor(self):
        """Start doctor.machine in a worker thread, unless one is running;
        the run in flight (a Task) either way. Synchronous, so a report sent
        right after it already says "checking"."""
        if self._doctor is None:
            loop = asyncio.get_running_loop()

            def run():
                h = render.load(self.path)
                return doctor.machine(h, self.budget_s, include_available=True)

            async def finish(fut):
                try:
                    self.machine = await fut
                    self.checked_at = time.time()
                except Exception as e:
                    log.warning("doctor: %s: %s", type(e).__name__, e)
                finally:
                    self._doctor = None
                await self.push()

            self._doctor = asyncio.ensure_future(finish(loop.run_in_executor(None, run)))
        return self._doctor

    async def check(self):
        """Run doctor once at a time; callers share the run in flight."""
        await asyncio.shield(self._start_doctor())

    async def push(self):
        self.broadcast(json.dumps(await self.report()))

    # --- messages

    def handle(self, ws, m):
        """Start the work and return at once: the page's read loop (and its
        heartbeat) must never wait on doctor or the disk."""
        asyncio.ensure_future(self._handle(ws, m))

    async def _handle(self, ws, m):
        t = m.get("t")
        try:
            if t == "modules":
                if self.machine is None:
                    self._start_doctor()   # first look: doctor runs, then every page gets it
                await self.send(ws, json.dumps(await self.report()))
            elif t == "modules_check":
                await self.check()
            elif t in ("module_plan", "module_apply") and self.plans is not None:
                await self.plans.handle(self, ws, m)
            else:
                await self.send(ws, json.dumps({"t": "notice", "level": "warn",
                                                "msg": f"Modules: {t} is not available here."}))
        except Exception as e:
            log.exception("modules: %s", t)
            await self.send(ws, json.dumps({"t": "notice", "level": "warn",
                                            "msg": f"Modules: {type(e).__name__}: {e}"}))
