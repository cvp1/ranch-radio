"""Add-ons: optional modules on a Radio Remote page (server.py) or the station
page (station.py), each one switchable without touching the core.

An add-on is a module in this package that sets ADDON to a subclass of Addon,
plus its page script static/addons/<name>.js (and optional <name>.css). The
host lists the add-ons it runs in its config:

    station.json:        "addons": {"uvpro": {"enabled": true, "address": "..."}}
    server.py:           --addons FILE   (a JSON file holding that same object)

- **Add**: an entry in the config. **Remove**: delete the entry (or the
  add-on's files); nothing in the core names an add-on.
- **Off / on**: "enabled" is the default; the page's switch turns a loaded
  add-on off or on at run time and the choice is kept across restarts in the
  state file (--addons-state). An add-on that is off holds nothing: its bridge
  is stopped and its device released (for the phone app, say).
- **Failure stays local.** An add-on that can't be imported or started is
  shown as broken, with the reason, and the radio server runs on without it.

Messages: page -> server {"t": "addon", "addon": NAME, "do": VERB, ...};
server -> pages {"t": "addon", "addon": NAME, ...}. The verb "power" is the
registry's own (the on/off switch); every other verb goes to the add-on, and
only while it is on.

One process per add-on: two hosts on one machine must not both list an add-on
that owns a device (two bridges would fight over the radio).

An add-on with a page of its own (page = False, e.g. mesh: the Mesh tab)
gets no add-on panel; it lists the message types its page sends in ROUTES,
and the host hands those to it with "do" set to the type. It still has the
registry's on/off switch ("power"), which its page shows itself.
"""

import asyncio
import importlib
import json
import logging
import os
import re

log = logging.getLogger("addons")

NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
DEFAULT_STATE = "~/.local/state/radio-remote/addons.json"


class Addon:
    """Base class. The host gives notice(msg, level, ws), _broadcast(payload)
    and _send(ws, payload), as server.Station and station.Station do."""

    name = ""
    label = ""
    title = ""
    css = False                   # static/addons/<name>.css exists
    page = True                   # False: its page is its own tab, not an add-on panel
    routes = ()                   # page message types ("t") the host hands to it

    def __init__(self, host, opts):
        self.host = host
        self.opts = opts

    async def start(self):
        """Take the device and begin. Called when the add-on is switched on."""

    async def stop(self):
        """Release everything. Called when switched off and at shutdown."""

    def snapshot(self):
        """Everything a page opened now needs."""
        return {}

    async def command(self, ws, verb, m):
        self.notice(f"{self.label}: unknown command {verb!r}.", "warn", ws)

    def can_stop(self):
        """None, or why it can't be switched off right now (e.g. a session running)."""
        return None

    def http(self, app, authorised):
        """Register its HTTP routes, once, before the server starts."""

    # helpers
    def emit(self, d, ws=None):
        payload = json.dumps({"t": "addon", "addon": self.name, **d})
        if ws is not None:
            asyncio.ensure_future(self.host._send(ws, payload))
        else:
            self.host._broadcast(payload)

    def notice(self, msg, level="info", ws=None):
        self.host.notice(msg, level, ws)


class Addons:
    """The add-ons one host runs."""

    def __init__(self, host, cfg, state_path=DEFAULT_STATE):
        self.host = host
        self.state_path = os.path.expanduser(state_path) if state_path else None
        self.items = {}           # name -> {"addon": Addon|None, "on": bool, "error": str|None, "running": bool}
        saved = self._read_state()
        for name, opts in (cfg or {}).items():
            opts = dict(opts or {})
            default_on = bool(opts.pop("enabled", True))
            rec = {"addon": None, "on": saved.get(name, default_on), "error": None, "running": False}
            self.items[name] = rec
            if not NAME.match(name):
                rec["error"], rec["on"] = f"not an add-on name: {name!r}", False
                continue
            try:
                mod = importlib.import_module(f"addons.{name}")
                cls = getattr(mod, "ADDON")
                if not (isinstance(cls, type) and issubclass(cls, Addon)):
                    raise TypeError("ADDON is not an Addon subclass")
                rec["addon"] = cls(host, opts)
            except Exception as e:   # a broken add-on must not take the radio server down
                log.error("add-on %s not loaded: %s: %s", name, type(e).__name__, e)
                rec["error"], rec["on"] = f"not loaded: {type(e).__name__}: {e}", False

    def __bool__(self):
        return bool(self.items)

    def get(self, name):
        rec = self.items.get(name)
        return rec and rec["addon"]

    def route(self, t):
        """The loaded add-on whose page sends messages of type t, or None."""
        for name, rec in self.items.items():
            if rec["addon"] is not None and t in rec["addon"].routes:
                return name
        return None

    def is_on(self, name):
        rec = self.items.get(name)
        return bool(rec and rec["on"] and rec["addon"])

    def own_snapshot(self, name):
        """For an add-on with its own page: its snapshot, on or off."""
        a = self.get(name)
        return a.snapshot() if a else None

    def engine(self, name):
        """An add-on's engine while it is switched on, else None (the host's
        `winlink` property: off reads as no Pat here)."""
        a = self.get(name)
        return getattr(a, "engine", None) if a is not None and self.is_on(name) else None

    def add_routes(self, app, authorised):
        for name, rec in self.items.items():
            if rec["addon"] is not None:
                try:
                    rec["addon"].http(app, authorised)
                except Exception as e:      # a broken add-on must not take the server down
                    log.error("add-on %s: routes not added: %s: %s", name, type(e).__name__, e)

    def _panels(self):
        return {n: r for n, r in self.items.items() if r["addon"] is None or r["addon"].page}

    def runtime(self):
        """Every add-on this process runs, panel or not (the Modules tab):
        {name: {"on", "error", "loaded"}}."""
        return {n: {"on": bool(r["on"]), "error": r["error"], "loaded": r["addon"] is not None}
                for n, r in self.items.items()}

    # --- what the pages see

    def describe(self):
        out = []
        for name, rec in self._panels().items():
            a = rec["addon"]
            out.append({"name": name, "label": a.label if a else name, "title": a.title if a else "",
                        "css": bool(a and a.css), "on": rec["on"], "error": rec["error"]})
        return out

    def snapshot(self):
        return {name: {"on": rec["on"], "error": rec["error"],
                       **(rec["addon"].snapshot() if rec["addon"] and rec["on"] else {})}
                for name, rec in self._panels().items()}

    # --- lifecycle

    async def start(self):
        for name, rec in self.items.items():
            if rec["on"] and rec["addon"]:
                await self._start(name, rec)

    async def stop(self):
        for name, rec in self.items.items():
            if rec["running"]:
                await self._stop(name, rec)

    async def _start(self, name, rec):
        try:
            await rec["addon"].start()
            rec["running"] = True
            log.info("add-on %s on", name)
        except Exception as e:
            log.error("add-on %s did not start: %s: %s", name, type(e).__name__, e)
            rec["error"], rec["on"] = f"did not start: {type(e).__name__}: {e}", False

    async def _stop(self, name, rec):
        try:
            await rec["addon"].stop()
        except Exception as e:
            log.warning("add-on %s: stop: %s: %s", name, type(e).__name__, e)
        rec["running"] = False
        log.info("add-on %s off", name)

    # --- pages -> add-ons

    async def command(self, ws, m):
        if m.get("t") != "addon":              # a routed page message (ROUTES)
            name = self.route(m.get("t"))
            m = {**m, "addon": name, "do": m.get("t")}
        name, verb = str(m.get("addon")), str(m.get("do"))
        rec = self.items.get(name)
        if rec is None:
            self.host.notice(f"No add-on {name!r} on this server.", "warn", ws)
            return
        if verb == "power":
            await self.power(ws, name, bool(m.get("on")))
            return
        if rec["error"] or not rec["addon"]:
            self.host.notice(f"{name}: {rec['error']}", "warn", ws)
        elif not rec["on"]:
            self.host.notice(f"{rec['addon'].label} is switched off.", "warn", ws)
        else:
            await rec["addon"].command(ws, verb, m)

    async def power(self, ws, name, on):
        rec = self.items[name]
        if rec["addon"] is None:
            self.host.notice(f"{name}: {rec['error']}", "warn", ws)
            return
        if on == rec["on"]:
            return
        why = None if on else rec["addon"].can_stop()
        if why:
            self.host.notice(why, "warn", ws)
            return
        rec["error"] = None
        rec["on"] = on
        if on:
            await self._start(name, rec)
        elif rec["running"]:
            await self._stop(name, rec)
        self._write_state()
        a = rec["addon"]
        self.host._broadcast(json.dumps({"t": "addon", "addon": name, "on": rec["on"], "error": rec["error"],
                                         **({"snapshot": a.snapshot()} if rec["on"] or not a.page else {})}))
        self.host.notice(f"{a.label} {'on' if rec['on'] else 'off'}.", "info" if not rec["error"] else "warn", ws)

    # --- the on/off choices, across restarts

    def _read_state(self):
        if not self.state_path:
            return {}
        try:
            with open(self.state_path) as f:
                d = json.load(f)
            return {k: bool(v) for k, v in d.items()} if isinstance(d, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("add-on state %s not read (%s); using each add-on's default", self.state_path, e)
            return {}

    def _write_state(self):
        if not self.state_path:
            return
        d = self._read_state()            # other hosts' add-ons share the file: keep theirs
        d.update({name: rec["on"] for name, rec in self.items.items() if rec["addon"]})
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f, indent=1, sort_keys=True)
            os.replace(tmp, self.state_path)
        except OSError as e:
            log.warning("add-on state %s not written: %s", self.state_path, e)
