"""HaLow add-on: an OpenWrt Wi-Fi HaLow (802.11ah) bridge, such as one that
carries remote cameras onto the LAN: who is on it and how well, the box's
health, its radios' settings, and the changes an operator makes to them.

It reads the bridge over SSH, one fixed script per poll (halow_ap.py), and
installs nothing there: these boxes have little flash. What it shows:

  - each radio (the HaLow one marked) and each network on it: name, channel,
    power, on or off
  - every station on each network: signal, rates, retries, how long joined,
    its address from the bridge's neighbour table
  - the box: model, firmware, uptime, load, memory, free flash
  - the DHCP guard: a bridge on the main LAN must not hand out addresses; a
    pool that is serving is shown red, with the one-click fix
  - its services (MediaMTX, the camera uploader, dnsmasq, the web shell) and its log tail
  - outbound: every connection from the bridge to an address off the LAN,
    noticed the first time each address appears

Changes are two steps. The page asks for a plan ({"do": "plan", "what": ...});
the server validates it, builds the exact command and sends it back with what
it will do. Only a {"do": "apply", "id": ...} for that plan, within
PLAN_TTL seconds, runs it, once. A plan that is not confirmed expires. Every
applied command, and its result, is appended to halow-actions.log beside the
add-on state. Changes: reboot, disconnect a station, set a radio's channel or
power, a network's name, hidden flag, on/off, start/stop/restart a service or
switch it off at boot, switch the
bridge's DHCP off. Wi-Fi keys are never read, shown or set here.

Options (the host file's addons block):

    "halow": {"enabled": true, "host": "192.168.1.65",
              "key": "~/.config/radio-remote/halow_ed25519",
              "user": "root", "port": 22, "poll": 30,
              "position": {"lat": 40.0, "lon": -105.0},
              "places": {"aa:bb:cc:dd:ee:01": {"name": "Gate cam", "lat": 40.01, "lon": -105.01}}}

`mesh` reads an OpenMANET HaLow mesh as well (halow_mesh.py): its gate on
the LAN, and the nodes behind it, by name or address on the mesh, reached
through the gate. The same `key` must be in each node's
/etc/dropbear/authorized_keys. A node not named is still shown, from the
views of the nodes that were read.

    "mesh": {"gate": "192.168.1.197", "nodes": ["node2"]}

`position` (where the bridge is) and `places` (where each station is, by MAC)
put them on the tab's map; both are optional. A station with no place is
drawn beside the bridge and says its position isn't set. Every station the
bridge has had joined is kept (halow-seen.json beside the add-on state, at most
SEEN_MAX, for SEEN_KEEP_S), so the map still shows a camera that has dropped
off, fading with age as on the Mesh map.

`key` is a private key whose public half is in the bridge's
/etc/dropbear/authorized_keys. Its host key is pinned on first contact in
halow_known_hosts beside the add-on state. If the bridge can't be reached the
panel says so and keeps the last reading, marked stale; the station runs on.
"""
import asyncio
import json
import logging
import os
import re
import secrets
import shlex
import contextlib
import time

import halow_ap
import halow_mesh

from . import Addon

log = logging.getLogger("addons.halow")

HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
PLAN_TTL = 120          # seconds a plan waits for its confirm, then expires
SSH_TIMEOUT = 25        # seconds any one SSH call may take
MAX_PLANS = 8
SEEN_MAX = 64           # stations kept in the seen list (the oldest go first)
SEEN_KEEP_S = 30 * 86400
NAME = re.compile(r"^[\x20-\x7e]{1,32}$")
SEEN_FIELDS = ("mac", "name", "ip", "ifname", "signal", "signal_avg", "tx_rate_text", "rx_rate_text", "dir")


def _latlon(v, what):
    """{"lat", "lon"} -> (lat, lon), or ValueError naming WHAT."""
    if not isinstance(v, dict):
        raise ValueError(f"halow: '{what}' must be {{\"lat\": .., \"lon\": ..}}")
    try:
        lat, lon = float(v["lat"]), float(v["lon"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"halow: '{what}' needs numeric lat and lon")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError(f"halow: '{what}' is off the Earth")
    return lat, lon


class Halow(Addon):
    name = "halow"
    label = "HaLow"
    title = "The HaLow bridge: who's on it, how well, its settings"
    group = "mesh"          # with Meshtastic and MeshCore: the ranch's other long-range network (2026-11-27)
    css = True

    def __init__(self, host, opts):
        super().__init__(host, opts)
        self.target = str(opts.get("host", ""))
        if not HOST.match(self.target):
            raise ValueError("halow: 'host' must be the bridge's address or name")
        self.user = str(opts.get("user", "root"))
        if not re.match(r"^[a-z_][a-z0-9_-]{0,31}$", self.user):
            raise ValueError("halow: 'user' is not a user name")
        try:
            self.port = int(opts.get("port", 22))
            self.poll = int(opts.get("poll", 30))
        except (TypeError, ValueError):
            raise ValueError("halow: 'port' and 'poll' must be whole numbers")
        if not 1 <= self.port <= 65535:
            raise ValueError("halow: 'port' must be 1-65535")
        if not 10 <= self.poll <= 3600:
            raise ValueError("halow: 'poll' must be 10-3600 seconds")
        self.key = os.path.expanduser(str(opts.get("key", "~/.config/radio-remote/halow_ed25519")))
        self.ssh = str(opts.get("ssh", "ssh"))          # tests point this at a fake
        self.position = None
        if opts.get("position") is not None:
            lat, lon = _latlon(opts["position"], "position")
            self.position = {"lat": lat, "lon": lon}
        places = opts.get("places") or {}
        if not isinstance(places, dict):
            raise ValueError("halow: 'places' must map a station's MAC to its place")
        self.places = {}
        for mac, v in places.items():
            m = str(mac).lower()
            if not halow_ap.MAC.match(m):
                raise ValueError(f"halow: 'places' key {mac!r} is not a MAC address")
            lat, lon = _latlon(v, f"places.{mac}")
            name = str(v.get("name", ""))
            if name and not NAME.match(name):
                raise ValueError(f"halow: 'places.{mac}.name' must be 1-32 plain characters")
            self.places[m] = {"lat": lat, "lon": lon, "name": name}
        self.seen = {}            # mac -> the last reading of a station that has joined (halow-seen.json)
        self.mesh_cfg = None
        mesh = opts.get("mesh")
        if mesh is not None:
            if not isinstance(mesh, dict) or not HOST.match(str(mesh.get("gate", ""))):
                raise ValueError("halow: 'mesh.gate' must be the mesh gate's address or name")
            nodes = mesh.get("nodes") or []
            if not isinstance(nodes, list) or len(nodes) > halow_mesh.MAX_NODES \
                    or not all(isinstance(n, str) and HOST.match(n) for n in nodes):
                raise ValueError(f"halow: 'mesh.nodes' must be a list of at most {halow_mesh.MAX_NODES} names or addresses")
            user = str(mesh.get("user", "root"))
            if not re.match(r"^[a-z_][a-z0-9_-]{0,31}$", user):
                raise ValueError("halow: 'mesh.user' is not a user name")
            self.mesh_cfg = {"gate": str(mesh["gate"]), "nodes": list(dict.fromkeys(nodes)), "user": user}
        self.mesh = {}            # the last mesh view: halow_mesh.mesh_view + at, error, unread
        self.mesh_seen = {}       # mac -> {mac, hostname, last, hops} (halow-mesh-seen.json)
        self.view = {}
        self.at = None            # when the last good read was
        self.error = None         # why the last read failed, or None
        self.plans = {}           # id -> {"plan", "made"}
        self._lock = asyncio.Lock()
        self._task = None
        self._wake = None
        self._known = set()       # station MACs seen on the last good read
        self._seen_remote = set() # off-LAN addresses already noticed (once each per run)

    # --- SSH

    def _argv(self, cmd, target=None, user=None, via=None):
        """ssh to the bridge, or to TARGET; through VIA (a mesh node behind its gate).
        A jump host takes no -i/-o from the command line, so the hop is a
        ProxyCommand that spells them out (the same key and known hosts)."""
        known = os.path.join(self.state_dir or os.path.expanduser("~/.local/state/radio-remote"), "halow_known_hosts")
        opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=5",
                "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={known}",
                "-o", "IdentitiesOnly=yes", "-o", "LogLevel=ERROR"]
        hop = []
        if via:
            inner = [self.ssh, "-i", self.key, *opts, "-W", "%h:%p", f"{user or self.user}@{via}"]
            hop = ["-o", "ProxyCommand=" + " ".join(shlex.quote(x) for x in inner)]
        port = ["-p", str(self.port)] if target is None else []
        return [self.ssh, "-i", self.key, *port, *opts, *hop, f"{user or self.user}@{target or self.target}", cmd]

    async def _run(self, cmd, target=None, user=None, via=None):
        """(exit code, output) of one command on the bridge (or TARGET); never raises for the box's sake."""
        async with (self._lock if target is None else contextlib.nullcontext()):
            try:
                p = await asyncio.create_subprocess_exec(*self._argv(cmd, target, user, via), stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.PIPE,
                                                         stdin=asyncio.subprocess.DEVNULL)
            except OSError as e:
                return 255, f"ssh did not start: {e}"

            async def drain(stream, cap):
                # read() returns the first chunk that arrives, not the whole
                # stream: read to EOF, keeping at most cap bytes.
                buf = bytearray()
                while True:
                    chunk = await stream.read(65536)
                    if not chunk:
                        return bytes(buf)
                    if len(buf) < cap:
                        buf += chunk[:cap - len(buf)]

            try:
                out, err = await asyncio.wait_for(asyncio.gather(drain(p.stdout, halow_ap.MAX_OUTPUT),
                                                                 drain(p.stderr, 8192)), SSH_TIMEOUT)
                await asyncio.wait_for(p.wait(), 5)
            except asyncio.TimeoutError:
                p.kill()
                await p.wait()
                return 124, f"no answer from the bridge in {SSH_TIMEOUT}s"
            text = out.decode("utf-8", "replace")
            if p.returncode != 0:
                # On failure the reason is ssh's (or the command's) stderr.
                text += err.decode("utf-8", "replace")
            return p.returncode, text

    # --- the stations seen (kept across restarts, bounded)

    def _seen_path(self):
        return os.path.join(self.state_dir, "halow-seen.json") if self.state_dir else None

    def _load_seen(self):
        path = self._seen_path()
        if not path:
            return
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return              # none yet, or unreadable: start empty; the next read rebuilds it
        now = time.time()
        for r in data if isinstance(data, list) else []:
            if isinstance(r, dict) and halow_ap.MAC.match(str(r.get("mac", ""))) \
                    and isinstance(r.get("last"), (int, float)) and now - r["last"] < SEEN_KEEP_S:
                self.seen[r["mac"]] = {k: r.get(k) for k in (*SEEN_FIELDS, "first", "last")}
        self._trim_seen(now)

    def _trim_seen(self, now):
        for m in [m for m, r in self.seen.items() if now - r["last"] >= SEEN_KEEP_S]:
            del self.seen[m]
        for m in sorted(self.seen, key=lambda m: self.seen[m]["last"])[:max(0, len(self.seen) - SEEN_MAX)]:
            del self.seen[m]

    def _note_seen(self, stations, now):
        for st in stations:
            # last: when it was last heard (its inactive time back from now), not just when we read.
            heard = now - (st.get("inactive_ms") or 0) / 1000
            old = self.seen.get(st["mac"], {})
            self.seen[st["mac"]] = {**{k: st.get(k) for k in SEEN_FIELDS}, "name": st.get("name") or old.get("name", ""),
                                    "ip": st.get("ip") or old.get("ip", ""),
                                    "first": old.get("first", now), "last": max(heard, old.get("last", 0))}
        self._trim_seen(now)
        path = self._seen_path()
        if not path or not stations:
            return
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(sorted(self.seen.values(), key=lambda r: -r["last"]), f)
            os.replace(tmp, path)
        except OSError as e:
            log.warning("halow: seen list not written: %s", e)

    # --- reading

    async def read(self):
        rc, out = await self._run(halow_ap.READ_SCRIPT)
        was = self.error
        if rc != 0 or "@@uptime" not in out:
            tail = out.strip().splitlines()[-1:] or [f"exit {rc}"]
            self.error = f"can't read the bridge at {self.target}: {tail[0][:200]}"
            if was is None:
                self.notice(f"HaLow: {self.error}", "warn")
        else:
            old_serving = set(self.view.get("dhcp", {}).get("serving", [])) if self.view else set()
            self.view, self.at, self.error = halow_ap.parse_read(out), time.time(), None
            clock = self.view["host"].get("clock")
            self.view["host"]["clock_skew_s"] = int(clock - self.at) if clock else None
            if was is not None:
                self.notice("HaLow: the bridge answers again.", "info")
            serving = set(self.view["dhcp"]["serving"])
            if serving and serving != old_serving:
                self.notice(f"HaLow: the bridge is serving DHCP on the LAN ({', '.join(sorted(serving))}); "
                            "that races the router. Fix it on the HaLow tab.", "warn")
            remotes = {c["remote_ip"] for c in self.view["outbound"]}
            for ip in sorted(remotes - self._seen_remote):
                ports = sorted({c["remote"] for c in self.view["outbound"] if c["remote_ip"] == ip})
                self.notice(f"HaLow: the bridge is talking off the LAN, to {', '.join(ports)}.", "warn")
            self._seen_remote |= remotes
            macs = {s["mac"] for s in self.view["stations"]}
            if self._known:
                for m in sorted(self._known - macs):
                    self.notice(f"HaLow: {m} left the bridge.", "warn")
                for m in sorted(macs - self._known):
                    self.notice(f"HaLow: {m} joined the bridge.", "info")
            self._known = macs
            self._note_seen(self.view["stations"], self.at)
        self.emit({"snapshot": self.snapshot()})

    # --- the OpenMANET mesh

    async def read_mesh(self):
        """The gate's view, then every named node's through the gate, at once."""
        c = self.mesh_cfg
        if not c:
            return
        was = self.mesh.get("error")
        rc, out = await self._run(halow_mesh.MESH_READ, c["gate"], c["user"])
        gate = halow_mesh.parse_mesh_read(out) if rc == 0 else None
        if gate is None:
            tail = out.strip().splitlines()[-1:] or [f"exit {rc}"]
            self.mesh = {**self.mesh, "error": f"can't read the mesh gate at {c['gate']}: {tail[0][:200]}"}
            if was is None:
                self.notice(f"HaLow mesh: {self.mesh['error']}", "warn")
            return
        reads, unread = {gate["mac"]: gate}, {}
        results = await asyncio.gather(*(self._run(halow_mesh.MESH_READ, n, c["user"], c["gate"]) for n in c["nodes"]))
        for name, (nrc, nout) in zip(c["nodes"], results):
            r = halow_mesh.parse_mesh_read(nout) if nrc == 0 else None
            if r is None:
                unread[name] = (nout.strip().splitlines()[-1:] or [f"exit {nrc}"])[0][:200]
            else:
                r["name"] = name
                reads[r["mac"]] = r
        now = time.time()
        view = halow_mesh.mesh_view(gate["mac"], reads)
        for n in view["nodes"]:
            n["name"] = (reads.get(n["mac"]) or {}).get("name", "")
        if was is not None:
            self.notice("HaLow mesh: the gate answers again.", "info")
        self._note_mesh(view, now)
        self.mesh = {**view, "at": now, "error": None, "unread": unread, "gate_host": c["gate"]}

    def _mesh_path(self):
        return os.path.join(self.state_dir, "halow-mesh-seen.json") if self.state_dir else None

    def _load_mesh_seen(self):
        path = self._mesh_path()
        if not path:
            return
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        now = time.time()
        for r in data if isinstance(data, list) else []:
            if isinstance(r, dict) and halow_ap.MAC.match(str(r.get("mac", ""))) \
                    and isinstance(r.get("last"), (int, float)) and now - r["last"] < SEEN_KEEP_S:
                self.mesh_seen[r["mac"]] = {k: r.get(k) for k in ("mac", "hostname", "name", "last", "hops")}

    def _note_mesh(self, view, now):
        """Notice nodes joining and leaving (edges only), and keep every node seen."""
        live = {n["mac"] for n in view["nodes"] if n["hops"] is not None}
        before = {m for m, r in self.mesh_seen.items() if r.get("live")}
        known = {n["mac"]: n for n in view["nodes"]}
        if self.mesh.get("at"):             # not before the first good read after a start
            for m in sorted(live - before):
                self.notice(f"HaLow mesh: {known[m].get('hostname') or m} joined the mesh.", "info")
            for m in sorted(before - live):
                self.notice(f"HaLow mesh: {self.mesh_seen[m].get('hostname') or m} left the mesh.", "warn")
        for r in self.mesh_seen.values():
            r["live"] = False
        for n in view["nodes"]:
            if n["mac"] in live:
                old = self.mesh_seen.get(n["mac"], {})
                self.mesh_seen[n["mac"]] = {"mac": n["mac"], "hostname": n["hostname"] or old.get("hostname", ""),
                                            "name": n.get("name") or old.get("name", ""), "last": now,
                                            "hops": n["hops"], "live": True}
        for m in [m for m, r in self.mesh_seen.items() if now - r["last"] >= SEEN_KEEP_S]:
            del self.mesh_seen[m]
        for m in sorted(self.mesh_seen, key=lambda m: self.mesh_seen[m]["last"])[:max(0, len(self.mesh_seen) - SEEN_MAX)]:
            del self.mesh_seen[m]
        path = self._mesh_path()
        if not path:
            return
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(path + ".tmp", "w") as f:
                json.dump(sorted(self.mesh_seen.values(), key=lambda r: -r["last"]), f)
            os.replace(path + ".tmp", path)
        except OSError as e:
            log.warning("halow: mesh seen list not written: %s", e)

    async def _loop(self):
        while True:
            try:
                await self.read()
                if self.mesh_cfg:
                    await self.read_mesh()
                    self.emit({"snapshot": self.snapshot()})
            except asyncio.CancelledError:
                raise
            except Exception as e:      # a parse surprise must not end the polling
                log.exception("halow read")
                self.error = f"reading failed: {type(e).__name__}: {e}"
                self.emit({"snapshot": self.snapshot()})
            self._wake = asyncio.Event()
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll)
            except asyncio.TimeoutError:
                pass

    def _refresh(self):
        if self._wake is not None:
            self._wake.set()

    async def start(self):
        self._load_seen()
        self._load_mesh_seen()
        self._task = asyncio.create_task(self._loop(), name="halow-poll")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self.plans.clear()

    def snapshot(self):
        return {"target": self.target, "at": self.at, "error": self.error, "poll": self.poll,
                "stale": self.error is not None and self.at is not None, **self.view,
                "position": self.position, "places": self.places,
                "seen": sorted(self.seen.values(), key=lambda r: -r["last"]),
                "mesh": ({**self.mesh, "configured": True, "seen": sorted(self.mesh_seen.values(), key=lambda r: -r["last"])}
                         if self.mesh_cfg else None)}

    # --- changes: plan, then apply

    def _plan(self, m):
        what, v = str(m.get("what", "")), self.view
        if not v:
            raise halow_ap.Refused("the bridge hasn't been read yet")
        if what == "reboot":
            return halow_ap.plan_reboot(v)
        if what == "kick":
            return halow_ap.plan_kick(v, str(m.get("ifname", "")), str(m.get("mac", "")))
        if what == "set":
            return halow_ap.plan_set(v, str(m.get("section", "")), str(m.get("option", "")), m.get("value", ""))
        if what == "service":
            return halow_ap.plan_service(v, str(m.get("service", "")), str(m.get("action", "")))
        if what == "dhcp_off":
            return halow_ap.plan_dhcp_off(v)
        raise halow_ap.Refused(f"not a change: {what!r}")

    def _expire(self):
        now = time.monotonic()
        for k in [k for k, p in self.plans.items() if now - p["made"] > PLAN_TTL]:
            del self.plans[k]

    def _audit(self, plan, rc, out):
        if not self.state_dir:
            return
        rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "bridge": self.target, "title": plan["title"],
               "cmd": plan["cmd"], "rc": rc, "out": out[-500:]}
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(os.path.join(self.state_dir, "halow-actions.log"), "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError as e:
            log.warning("halow: action log not written: %s", e)

    async def command(self, ws, verb, m):
        if verb == "refresh":
            self._refresh()
            return
        if verb == "plan":
            self._expire()
            try:
                plan = self._plan(m)
            except halow_ap.Refused as e:
                self.notice(f"HaLow: {e}", "warn", ws)
                return
            if len(self.plans) >= MAX_PLANS:
                self.plans.pop(next(iter(self.plans)))
            pid = secrets.token_hex(8)
            self.plans[pid] = {"plan": plan, "made": time.monotonic()}
            self.emit({"event": "plan", "id": pid, "ttl": PLAN_TTL, **plan}, ws)
            return
        if verb == "cancel":
            self.plans.pop(str(m.get("id", "")), None)
            return
        if verb == "apply":
            self._expire()
            rec = self.plans.pop(str(m.get("id", "")), None)
            if rec is None:
                self.notice("HaLow: that change expired or was already made; ask again.", "warn", ws)
                return
            plan = rec["plan"]
            rc, out = await self._run(plan["cmd"])
            if plan["cmd"] == "reboot" and rc in (0, 255):
                rc = 0          # the box drops the connection as it goes down
            self._audit(plan, rc, out)
            ok = rc == 0
            self.emit({"event": "applied", "ok": ok, "title": plan["title"], "out": out[-2000:],
                       "undo": plan.get("undo") if ok else None}, ws)
            self.notice(f"HaLow: {plan['title']}: {'done' if ok else f'failed (exit {rc})'}.",
                        "info" if ok else "warn", ws)
            self._refresh()
            return
        if verb == "ping":
            try:
                cmd = halow_ap.ping_cmd(m.get("ip", ""))
            except halow_ap.Refused as e:
                self.notice(f"HaLow: {e}", "warn", ws)
                return
            rc, out = await self._run(cmd)
            self.emit({"event": "ping", "ip": m.get("ip"), "ok": rc == 0, "out": out[-1500:]}, ws)
            return
        await super().command(ws, verb, m)


ADDON = Halow
