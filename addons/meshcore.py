"""MeshCore: a MeshCore companion node as a tab on the radio page.

MeshCore is a LoRa mesh of its own, separate from Meshtastic (the Mesh tab):
the two don't hear each other, so a host can run both, each with its own
node. This add-on runs meshcore_bridge.py as a child process under the
Python that has the `meshcore` package, starts it again when it exits
(node unplugged, serial error), and shows the pages:
  - the node: name, model, firmware, radio settings, battery, its location
  - the nodes it has heard advertise (contacts), newest first
  - channel and direct messages (last MESSAGES_KEEP, also in "log", mode 600:
    private channels are stored in clear there), and sending to a channel
  - an "advert" button: announce this node now (zero-hop, or flooded)
  - the node's settings (the bridge's SETTINGS: name, location, radio, TX
    power, and on a build with Wi-Fi, Wi-Fi and the Bluetooth PIN), checked
    here against that schema before the bridge writes them, and a reboot.
    The Wi-Fi password is write-only: never in a snapshot, a log or a notice.
  - a map of the nodes that share a location, the route to a node drawn on it
  - per node: favourite, path discovery, trace (each hop's SNR), forget the
    route, remove; and for a repeater or room server: log in, status (its
    battery, noise floor, airtime, packet counts), telemetry, neighbours
    (drawn on the map), owner info, set its clock, and a remote command line
    for an admin login. Each of these goes on the air, so one at a time per
    node and one every REQUEST_GAP_S.
  - the node's own radio counters and the packets it hears on the air
  - joining a #hashtag channel (its key is the name's hash) and leaving one
  - adding a heard node by hand when "Add contacts by hand" is on

Passwords for repeaters can be kept in files, read when used and never sent
to a page, logged or kept in memory after the login:
    {"meshcore": {..., "logins": {"a1b2c3d4e5f6": "~/secrets/repeater_admin_password"}}}
A password typed on the page goes to the node the same way and is not kept.
Remote commands and their replies are shown, never written to the log.

Config (the host's add-ons file):
    {"meshcore": {"port": "/dev/ttyUSB0",
                  "python": "~/.local/share/venvs/meshcore/bin/python",
                  "log": "~/.local/state/radio-remote/meshcore.jsonl"}}

A node on Wi-Fi (the companion firmware with Wi-Fi on, TCP port 5000), with
Bluetooth to fall back on, and radio presets the Settings form offers:
    {"meshcore": {"port": "tcp:192.168.1.50:5000,ble:NODE",
                  "presets": [{"label": "Local", "freq": 906.875, "bw": 250, "sf": 11, "cr": 5}]}}
Its TCP port has no login: anything on the LAN can drive the node.

Off frees the node's serial port for other apps (the MeshCore web or phone
app over USB, meshcore-cli, a firmware flash), or its one TCP connection.
"""

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from pathlib import Path

from . import Addon

log = logging.getLogger("addon.meshcore")

HERE = Path(__file__).resolve().parent.parent
MESSAGES_KEEP = 300
TEXT_MAX_BYTES = 140           # MeshCore's text limit, less room for the sender's name on a channel
RESTART_S = (5.0, 15.0, 60.0)  # wait before restarting the bridge, growing while the node stays away
ADVERT_GAP_S = 30.0            # one advert per this long from the page: each one goes on the air
PRESETS = [{"label": "USA/Canada (recommended)", "freq": 910.525, "bw": 62.5, "sf": 7, "cr": 5}]
KEY_PREFIX = re.compile(r"^[0-9a-f]{12,64}$")
REQUEST_GAP_S = 2.0            # requests to other nodes: at most one per this long
REQUEST_KINDS = ("status", "telemetry", "neighbours", "owner", "path", "trace", "reset_path", "remove",
                 "fav", "add", "clock", "login", "cli")
LOCAL_KINDS = ("reset_path", "remove", "fav", "add")   # change this node only: nothing on the air
SERVER_KINDS = ("status", "telemetry", "neighbours", "owner", "clock", "login", "cli")  # repeaters, rooms
PASSWORD_MAX = 15              # MeshCore keeps 15 characters of a password
CLI_MAX = 160
CLI_KEEP = 40                  # remote command lines shown per node
CHANNEL_NAME = re.compile(r"^#[a-z0-9][a-z0-9_-]{0,29}$")
AIR_WINDOW_S = 600.0


class MeshCoreAddon(Addon):
    name = "meshcore"
    group = "mesh"
    label = "MeshCore"
    title = "MeshCore companion node"
    css = True

    def __init__(self, host, opts):
        super().__init__(host, opts)
        if not opts.get("port"):
            raise ValueError("meshcore needs a \"port\" (the node's serial device)")
        self.cmd = [os.path.expanduser(opts.get("python", "~/.local/share/venvs/meshcore/bin/python")),
                    os.path.expanduser(opts.get("bridge", str(HERE / "meshcore_bridge.py"))), opts["port"]]
        self.presets = [p for p in (opts.get("presets") or []) if isinstance(p, dict)] + PRESETS
        log_path = opts.get("log", "~/.local/state/radio-remote/meshcore.jsonl")
        self.log_path = os.path.expanduser(log_path) if log_path else None
        self.logins = {}
        for prefix, path in (opts.get("logins") or {}).items():
            prefix = str(prefix).lower()
            if not KEY_PREFIX.match(prefix):
                raise ValueError(f"meshcore logins: not a node key prefix: {prefix!r}")
            self.logins[prefix] = os.path.expanduser(str(path))
        self.messages = deque(maxlen=MESSAGES_KEEP)
        self._loaded = False
        self._reset()

    def _reset(self):
        self.proc = None
        self.connected, self.error = False, None
        self.info = None
        self.channels = []
        self.contacts = []
        self.battery_mv = None
        self.config = None            # {"link", "sections", "values", "wifi"} from the bridge
        self._writes = {}             # set / reboot ref -> (page, what)
        self.heard = {}               # key -> when it last advertised (this run)
        self._ref = 0
        self._by_ref = {}             # send ref -> our message record
        self._by_ack = {}             # expected ack code -> our message record
        self._adverts = {}            # advert ref -> page
        self._advert_t = 0.0
        self.pending = []             # heard, not added (manual add mode)
        self.stats = None             # the node's own radio counters
        self.results = {}             # key -> {kind: the last answer}
        self.cli = {}                 # key -> deque of {"t", "out"|"in"} lines
        self.logged_in = {}           # key -> {"admin": bool}
        self.rx = deque(maxlen=5000)  # when packets were heard
        self.rx_last = None
        self._requests = {}           # ref -> (page, kind, key)
        self._busy = set()            # keys with a request in flight
        self._request_t = 0.0
        self._closing = False
        self._task = None

    # --- lifecycle

    async def start(self):
        self._reset()
        if not self._loaded:
            self._load()
            self._loaded = True
        self._task = asyncio.create_task(self._supervise(), name="meshcore-bridge")

    async def stop(self):
        self._closing = True
        if self._task:
            self._task.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.close()   # the bridge closes the node cleanly on EOF
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                self.proc.kill()
        self.connected = False

    async def _supervise(self):
        fails = 0
        while not self._closing:
            reached = False
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *self.cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE)
            except OSError as e:
                log.error("cannot start the MeshCore bridge (%s): %s", self.cmd[0], e)
                self._status(False, f"bridge not started: {e}")
                await asyncio.sleep(RESTART_S[-1])
                continue
            log.info("MeshCore bridge started (pid %d) on %s", self.proc.pid, self.cmd[2])
            err = asyncio.create_task(self._stderr(self.proc.stderr))
            async for raw in self.proc.stdout:
                try:
                    self._on_bridge(json.loads(raw))
                except (ValueError, KeyError, TypeError) as e:
                    log.warning("MeshCore bridge: bad line %r (%s)", raw[:200], e)
                reached = reached or self.connected
            code = await self.proc.wait()
            await err
            if self._closing:
                return
            for rec in list(self._by_ref.values()):
                self._settle(rec, "failed", "the node went away before sending")
            self._by_ref.clear()
            for ref, (ws, what) in list(self._writes.items()):
                if what != "reboot":
                    self.notice(f"MeshCore: the node went away; {what} may not be saved.", "warn", ws)
            self._writes.clear()
            for ref, (ws, kind, key) in list(self._requests.items()):
                if key is not None:
                    self.emit({"result": {"kind": kind, "key": key, "t": time.time(), "ok": False, "data": None,
                                          "error": "the node went away before the answer"}})
            self._requests.clear()
            self._busy.clear()
            if self.connected:
                self._status(False, self.error or "MeshCore bridge stopped")
            fails = 0 if reached else fails + 1
            wait = RESTART_S[min(fails, len(RESTART_S) - 1)]
            (log.info if fails > 1 else log.warning)(
                "MeshCore bridge exited (code %s: %s); restarting in %gs", code, self.error, wait)
            await asyncio.sleep(wait)

    async def _stderr(self, stream):
        async for raw in stream:
            log.info("bridge: %s", raw.decode(errors="replace").rstrip())

    # --- bridge -> pages

    def _on_bridge(self, m):
        ev = m["ev"]
        if ev == "status":
            self._status(m["connected"], m.get("error"))
        elif ev == "info":
            self.info = {k: m.get(k) for k in ("me", "device", "radio")}
            self.emit({"info": self.info})
        elif ev == "channels":
            self.channels = m["channels"]
            self.emit({"channels": self.channels})
        elif ev == "contacts":
            self.contacts = m["contacts"]
            self.emit({"contacts": self.contacts})
        elif ev == "battery":
            self.battery_mv = m.get("mv")
            self.emit({"battery_mv": self.battery_mv})
        elif ev == "advert":
            if m.get("key"):
                self.heard[m["key"]] = time.time()
                self.emit({"heard": {"key": m["key"], "t": self.heard[m["key"]]}})
        elif ev == "msg":
            self._add({"k": "msg", "t": time.time(), "from": m.get("from"), "name": m.get("name"),
                       "ch": m.get("ch"), "dm": bool(m.get("dm")), "text": m.get("text", ""),
                       "snr": m.get("snr"), "hops": m.get("hops"), "sent_t": m.get("sent_t")})
        elif ev == "sent":
            rec = self._by_ref.get(m["ref"])
            if rec is not None:
                if rec["dm"] and m.get("ack"):
                    self._by_ack[m["ack"]] = rec
                if not (rec["dm"] and m.get("ack") is None):   # a retried DM: wait for "delivered"
                    self._by_ref.pop(m["ref"], None)
                self._settle(rec, "sent", flood=m.get("flood"))
        elif ev == "delivered":
            rec = self._by_ref.pop(m.get("ref"), None)
            if rec is not None:
                self._settle(rec, "delivered", tries=m.get("attempts"), flood=m.get("flood"),
                             trip_ms=m.get("trip_ms"))
        elif ev == "stats":
            self.stats = {k: v for k, v in m.items() if k != "ev"}
            self.emit({"stats": self.stats})
        elif ev == "rx":
            now = time.time()
            self.rx.append(now)
            self.rx_last = {k: m.get(k) for k in ("snr", "rssi", "type", "route", "hops", "last")}
            self.rx_last["t"] = now
            self.emit({"rx": {**self.rx_last, "n": self._air()}})
        elif ev == "pending":
            self.pending = m.get("contacts") or []
            self.emit({"pending": self.pending})
        elif ev == "result":
            self._result(m)
        elif ev == "cli_reply":
            self._cli_line(m.get("key"), "in", m.get("text") or "")
        elif ev == "notice":
            self.notice(f"MeshCore: {m.get('text')}", m.get("level") or "info")
        elif ev == "send_failed":
            rec = self._by_ref.pop(m["ref"], None)
            if rec is not None:
                self._settle(rec, "failed", m.get("error"))
        elif ev == "ack":
            rec = self._by_ack.pop(m.get("ack"), None)
            if rec is not None:
                self._settle(rec, "delivered")
        elif ev == "config":
            self.config = {k: m.get(k) for k in ("link", "sections", "values", "wifi", "pin_fixed")}
            self.emit({"config": self.config, "presets": self.presets})
        elif ev == "done":
            if m.get("ref") in self._requests:     # a channel join or leave
                ws, what, _ = self._requests.pop(m["ref"])
                self.notice(f"MeshCore: {what}." if m["ok"] else f"MeshCore: {what} failed: {m.get('error')}",
                            "info" if m["ok"] else "warn", ws)
                return
            if m.get("ref") in self._writes:
                ws, what = self._writes.pop(m["ref"])
                if what == "reboot":
                    self.notice("MeshCore: the node is rebooting; it reconnects in a moment.", "info", ws)
                elif m["ok"]:
                    self.notice(f"MeshCore: {what} saved to the node." +
                                (" Reboot the node to apply." if m.get("reboot") else ""), "info", ws)
                else:
                    self.notice(f"MeshCore: {what} not saved: {m.get('error')}", "warn", ws)
                return
            ws = self._adverts.pop(m.get("ref"), None)
            if m["ok"]:
                self.notice("MeshCore: advert sent.", "info", ws)
            else:
                self.notice(f"MeshCore: advert not sent: {m.get('error')}", "warn", ws)

    def _status(self, connected, error):
        self.connected, self.error = bool(connected), error
        log.info("MeshCore: %s%s", "connected" if connected else "disconnected", f" ({error})" if error else "")
        self.emit({"status": {"connected": self.connected, "error": error}})

    def _settle(self, rec, state, why=None, **extra):
        rec["ack"] = state
        if why:
            rec["why"] = why
        rec.update({k: v for k, v in extra.items() if v is not None})
        self._write({"k": "ack", "ref_t": rec["t"], "ack": state, "why": why,
                     **{k: v for k, v in extra.items() if v is not None}})
        self.emit({"update": rec})

    def _add(self, rec):
        self.messages.append(rec)
        self._write(rec)
        self.emit({"msg": rec})

    def snapshot(self):
        return {"status": {"connected": self.connected, "error": self.error},
                "info": self.info, "channels": self.channels, "contacts": self.contacts,
                "battery_mv": self.battery_mv, "heard": self.heard,
                "config": self.config, "presets": self.presets,
                "messages": list(self.messages), "text_max": TEXT_MAX_BYTES,
                "pending": self.pending, "stats": self.stats, "results": self.results,
                "cli": {k: list(v) for k, v in self.cli.items()}, "logged_in": self.logged_in,
                "logins": sorted(self.logins), "air": {"n": self._air(), "last": self.rx_last,
                                                       "window_s": AIR_WINDOW_S}}

    def _air(self):
        cut = time.time() - AIR_WINDOW_S
        while self.rx and self.rx[0] < cut:
            self.rx.popleft()
        return len(self.rx)

    def _result(self, m):
        ws, kind, key = self._requests.pop(m.get("ref"), (None, m.get("kind"), m.get("key")))
        self._busy.discard(key)
        full = m.get("key") or key
        rec = {"kind": kind, "key": full, "t": time.time(), "ok": bool(m.get("ok")),
               "data": m.get("data") if m.get("ok") else None, "error": None if m.get("ok") else m.get("error")}
        if kind == "login" and rec["ok"]:
            self.logged_in[full] = {"admin": bool((rec["data"] or {}).get("admin"))}
            self.emit({"logged_in": self.logged_in})
        if kind in ("cli", "clock"):
            if not rec["ok"]:
                self._cli_line(full, "err", rec["error"])
            return
        if kind not in ("reset_path", "remove", "fav", "add"):
            self.results.setdefault(full, {})[kind] = rec
            while len(self.results) > 200:
                self.results.pop(next(iter(self.results)))
        self.emit({"result": rec})
        if not rec["ok"]:
            self.notice(f"MeshCore: {kind.replace('_', ' ')} failed: {rec['error']}", "warn", ws)

    def _cli_line(self, key, way, text):
        key = str(key or "")
        full = next((c["key"] for c in self.contacts if (c.get("key") or "").startswith(key)), key) if key else key
        q = self.cli.setdefault(full, deque(maxlen=CLI_KEEP))
        line = {"t": time.time(), "way": way, "text": str(text)[:2000]}
        q.append(line)
        self.emit({"cli": {"key": full, **line}})

    # --- pages -> node

    def _usable(self, ws):
        if not self.connected or self.proc is None or self.proc.returncode is not None:
            self.notice("The MeshCore node is not connected.", "warn", ws)
            return False
        return True

    async def _cmd(self, d):
        self.proc.stdin.write((json.dumps(d) + "\n").encode())
        await self.proc.stdin.drain()

    async def command(self, ws, verb, m):
        if verb == "send":
            await self.send_text(ws, m.get("text"), m.get("ch"), m.get("to"))
        elif verb == "advert":
            await self.advert(ws, bool(m.get("flood")))
        elif verb == "set":
            await self.set_config(ws, m.get("section"), m.get("values"))
        elif verb == "reboot":
            await self.reboot(ws)
        elif verb in REQUEST_KINDS:
            await self.request(ws, verb, m)
        elif verb in ("channel_join", "channel_leave"):
            await self.channel(ws, verb, m)
        else:
            await super().command(ws, verb, m)

    def _contact(self, key, pending=False):
        key = str(key or "").lower()
        if not KEY_PREFIX.match(key):
            return None
        pool = self.pending if pending else self.contacts
        return next((c for c in pool if (c.get("key") or "").startswith(key)), None)

    def _password(self, key, typed):
        """The password for KEY: typed on the page, else its file. Never logged."""
        if typed not in (None, ""):
            return str(typed), None
        path = next((p for pre, p in self.logins.items() if key.startswith(pre)), None)
        if path is None:
            return None, "Type the node's password (none is saved for it)."
        try:
            with open(path) as f:
                pwd = f.read().strip()
        except OSError as e:
            return None, f"The saved password can't be read ({type(e).__name__})."
        return pwd, None

    async def request(self, ws, kind, m):
        """A request to another node (or a change to one of this node's contacts)."""
        if not self._usable(ws):
            return
        c = self._contact(m.get("key"), pending=kind == "add")
        if c is None:
            self.notice("That node is not one of this node's contacts.", "warn", ws)
            return
        key = c["key"]
        if kind in SERVER_KINDS and c.get("type") not in ("repeater", "room"):
            self.notice(f"Only a repeater or room server answers {kind}.", "warn", ws)
            return
        if kind == "trace" and c.get("hops") is None:
            self.notice("No route to that node is known yet: find the path first.", "warn", ws)
            return
        cmd = {"cmd": kind, "key": key}
        if kind == "fav":
            cmd["fav"] = bool(m.get("fav"))
        if kind == "cli":
            text = str(m.get("text") or "").strip()
            if not text or "\n" in text or "\r" in text or len(text.encode()) > CLI_MAX:
                self.notice(f"A remote command is one line of at most {CLI_MAX} bytes.", "warn", ws)
                return
            if not (self.logged_in.get(key) or {}).get("admin"):
                self.notice("Log in as admin first: a repeater only takes commands from its admin.", "warn", ws)
                return
            cmd["text"] = text
        if kind == "clock" and not (self.logged_in.get(key) or {}).get("admin"):
            self.notice("Log in as admin first: only the admin can set a repeater's clock.", "warn", ws)
            return
        if kind == "login":
            typed = m.get("password")
            if typed not in (None, "") and (not isinstance(typed, str) or len(typed) > PASSWORD_MAX
                                            or "\n" in typed or "\r" in typed):
                self.notice(f"A MeshCore password is one line of at most {PASSWORD_MAX} characters.", "warn", ws)
                return
            pwd, why = self._password(key, typed)
            if why:
                self.notice(why, "warn", ws)
                return
            cmd["password"] = pwd
        on_air = kind not in LOCAL_KINDS
        if on_air:
            if key in self._busy:
                self.notice("A request to that node is already waiting for its answer.", "warn", ws)
                return
            wait = REQUEST_GAP_S - (time.time() - self._request_t)
            if wait > 0:
                self.notice(f"One request every {REQUEST_GAP_S:g} s (each goes on the air); try again.", "warn", ws)
                return
            self._request_t = time.time()
            self._busy.add(key)
        self._ref += 1
        cmd["ref"] = self._ref
        self._requests[self._ref] = (ws, kind, key)
        if kind in ("cli", "clock"):
            self._cli_line(key, "out", cmd.get("text") or "time (set to this host's clock)")
        await self._cmd(cmd)
        cmd.pop("password", None)
        log.info("MeshCore: %s for %s", kind, key[:12])        # never a password or a command's text
        if not on_air:
            return
        self.emit({"request": {"kind": kind, "key": key, "t": time.time()}})

    async def channel(self, ws, verb, m):
        if not self._usable(ws):
            return
        if verb == "channel_join":
            name = str(m.get("name") or "").strip().lower()
            if not name.startswith("#"):
                name = "#" + name
            if not CHANNEL_NAME.match(name):
                self.notice("A hashtag channel is # and up to 30 letters, digits, - or _.", "warn", ws)
                return
            cmd, what = {"cmd": verb, "name": name}, f"joined {name}"
        else:
            i = m.get("index")
            ch = next((c for c in self.channels if c["index"] == i and not isinstance(i, bool)), None)
            if ch is None:
                self.notice(f"The node has no channel {i!r}.", "warn", ws)
                return
            cmd, what = {"cmd": verb, "index": i}, f"left {ch['name']}"
        self._ref += 1
        cmd["ref"] = self._ref
        self._requests[self._ref] = (ws, what, None)
        await self._cmd(cmd)
        log.info("MeshCore: %s", what)

    async def send_text(self, ws, text, ch, to=None):
        text = str(text or "").strip()
        why = None
        if not text:
            why = "Nothing to send."
        elif len(text.encode()) > TEXT_MAX_BYTES:
            why = f"Too long for one MeshCore message ({len(text.encode())} of {TEXT_MAX_BYTES} bytes)."
        elif not self._usable(ws):
            return
        elif to is not None:
            to = str(to).lower()
            if not KEY_PREFIX.match(to):
                why = f"Not a node key: {to!r}."
            elif not any((c.get("key") or "").startswith(to) for c in self.contacts):
                why = "That node is not one of this node's contacts."
        elif not isinstance(ch, int) or isinstance(ch, bool) or not any(c["index"] == ch for c in self.channels):
            why = f"The node has no channel {ch!r}."
        if why:
            self.notice(why, "warn", ws)
            return
        self._ref += 1
        me = (self.info or {}).get("me") or {}
        rec = {"k": "msg", "t": time.time(), "from": (me.get("key") or "")[:12] or None, "name": me.get("name"),
               "ch": None if to else ch, "dm": to is not None, "to": to, "text": text, "mine": True,
               "ack": "sending"}
        self._by_ref[self._ref] = rec
        self._add(rec)
        await self._cmd({"cmd": "send", "ref": self._ref, "text": text,
                         **({"to": to} if to else {"ch": ch})})
        log.info("MeshCore: sending %d bytes %s", len(text.encode()), f"to {to[:12]}" if to else f"on channel {ch}")

    async def advert(self, ws, flood):
        if not self._usable(ws):
            return
        wait = ADVERT_GAP_S - (time.time() - self._advert_t)
        if wait > 0:
            self.notice(f"One advert every {ADVERT_GAP_S:g} s; try again in {wait:.0f} s.", "warn", ws)
            return
        self._advert_t = time.time()
        self._ref += 1
        self._adverts[self._ref] = ws
        await self._cmd({"cmd": "advert", "ref": self._ref, "flood": flood})
        log.info("MeshCore: %s advert", "flood" if flood else "zero-hop")

    def check_settings(self, section, values):
        """Why the page's values for SECTION can't be written, or None."""
        spec = next((s for s in (self.config or {}).get("sections") or [] if s["name"] == section), None)
        if spec is None:
            return f"The node has no settings section {section!r}."
        if not isinstance(values, dict) or not values:
            return "Nothing to save."
        fields = {f["name"]: f for f in spec["fields"]}
        values = {k: v for k, v in values.items()            # an empty write-only field: keep the old value
                  if not (fields.get(k, {}).get("write_only") and v in ("", None))}
        if not values:
            return "Nothing to save."
        for name, v in values.items():
            f = fields.get(name)
            if f is None:
                return f"{spec['label']} has no setting {name!r}."
            t, label = f["type"], f["label"]
            if t == "bool" and not isinstance(v, bool):
                return f"{label}: not on/off."
            if t in ("int", "float"):
                if isinstance(v, bool) or not isinstance(v, (int, float)) or (t == "int" and v != int(v)):
                    return f"{label}: not a {'whole ' if t == 'int' else ''}number."
                if not f.get("min", v) <= v <= f.get("max", v):
                    return f"{label}: {v} is outside {f.get('min', '')}..{f.get('max', '')}."
            if t == "enum" and v not in f["options"]:
                return f"{label}: {v!r} is not one of the choices."
            if t == "str":
                if not isinstance(v, str):
                    return f"{label}: not text."
                if "\n" in v or "\r" in v:
                    return f"{label}: one line only."
                if name in ("name", "ssid") and not v.strip() and not (section == "wifi" and not v):
                    return f"{label}: can't be empty."
                if len(v.encode()) > f.get("max_bytes", 200):
                    return f"{label}: longer than {f['max_bytes']} bytes."
        if section == "wifi" and values.get("password") and len(values["password"].encode()) < 8:
            return "Password: a Wi-Fi password has at least 8 characters."
        if section == "bluetooth" and 0 < values.get("pin", 0) < 100000:
            return "PIN: 6 digits (100000-999999), or 0 for a random one."
        return None

    async def set_config(self, ws, section, values):
        if not self._usable(ws):
            return
        section = str(section)
        why = self.check_settings(section, values)
        if why:
            self.notice(why, "warn", ws)
            return
        spec = next(s for s in self.config["sections"] if s["name"] == section)
        types = {f["name"]: f["type"] for f in spec["fields"]}
        wo = {f["name"] for f in spec["fields"] if f.get("write_only")}
        values = {k: int(v) if types[k] == "int" else v for k, v in values.items()
                  if not (k in wo and v in ("", None))}      # an empty write-only field: keep the old value
        if not values:
            self.notice("Nothing to save.", "warn", ws)
            return
        self._ref += 1
        what = f"{spec['label']} settings"
        self._writes[self._ref] = (ws, what)
        await self._cmd({"cmd": "set", "ref": self._ref, "section": section, "values": values})
        log.info("MeshCore: writing %s (%s)", what, ", ".join(sorted(values)))   # names, never values

    async def reboot(self, ws):
        if not self._usable(ws):
            return
        self._ref += 1
        self._writes[self._ref] = (ws, "reboot")
        await self._cmd({"cmd": "reboot", "ref": self._ref})
        log.info("MeshCore: reboot")

    # --- history on disk

    def _load(self):
        if not self.log_path:
            return
        msgs, mine = [], {}
        try:
            with open(self.log_path) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("k") == "msg":
                        msgs.append(r)
                        if r.get("mine"):
                            mine[r["t"]] = r
                    elif r.get("k") == "ack" and r.get("ref_t") in mine:
                        mine[r["ref_t"]]["ack"] = r["ack"]
        except FileNotFoundError:
            return
        except OSError as e:
            log.warning("MeshCore log %s not read: %s", self.log_path, e)
            return
        for r in msgs[-MESSAGES_KEEP:]:
            if r.get("ack") in ("sending", "sent") and r.get("dm"):
                r["ack"] = "unknown"          # the server restarted before its ACK
            self.messages.append(r)
        if len(msgs) > 4 * MESSAGES_KEEP:      # keep the file to what is shown
            tmp = self.log_path + ".tmp"
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.writelines(json.dumps(r) + "\n" for r in self.messages)
                os.replace(tmp, self.log_path)
            except OSError as e:
                log.warning("MeshCore log %s not compacted: %s", self.log_path, e)

    def _write(self, rec):
        if not self.log_path:
            return
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError as e:
            log.warning("MeshCore log %s not written: %s", self.log_path, e)


ADDON = MeshCoreAddon
