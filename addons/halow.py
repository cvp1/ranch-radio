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
  - its services (MediaMTX, the camera uploader, dnsmasq) and its log tail

Changes are two steps. The page asks for a plan ({"do": "plan", "what": ...});
the server validates it, builds the exact command and sends it back with what
it will do. Only a {"do": "apply", "id": ...} for that plan, within
PLAN_TTL seconds, runs it, once. A plan that is not confirmed expires. Every
applied command, and its result, is appended to halow-actions.log beside the
add-on state. Changes: reboot, disconnect a station, set a radio's channel or
power, a network's name, hidden flag, on/off, restart a service, switch the
bridge's DHCP off. Wi-Fi keys are never read, shown or set here.

Options (the host file's addons block):

    "halow": {"enabled": true, "host": "192.168.1.65",
              "key": "~/.config/radio-remote/halow_ed25519",
              "user": "root", "port": 22, "poll": 30}

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
import time

import halow_ap

from . import Addon

log = logging.getLogger("addons.halow")

HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
PLAN_TTL = 120          # seconds a plan waits for its confirm, then expires
SSH_TIMEOUT = 25        # seconds any one SSH call may take
MAX_PLANS = 8


class Halow(Addon):
    name = "halow"
    label = "HaLow"
    title = "The HaLow bridge: who's on it, how well, its settings"
    group = "tools"
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
        self.view = {}
        self.at = None            # when the last good read was
        self.error = None         # why the last read failed, or None
        self.plans = {}           # id -> {"plan", "made"}
        self._lock = asyncio.Lock()
        self._task = None
        self._wake = None
        self._known = set()       # station MACs seen on the last good read

    # --- SSH

    def _argv(self, cmd):
        known = os.path.join(self.state_dir or os.path.expanduser("~/.local/state/radio-remote"), "halow_known_hosts")
        return [self.ssh, "-i", self.key, "-p", str(self.port),
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=5",
                "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={known}",
                "-o", "IdentitiesOnly=yes", "-o", "LogLevel=ERROR", f"{self.user}@{self.target}", cmd]

    async def _run(self, cmd):
        """(exit code, output) of one command on the bridge; never raises for the box's sake."""
        async with self._lock:
            try:
                p = await asyncio.create_subprocess_exec(*self._argv(cmd), stdout=asyncio.subprocess.PIPE,
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
            macs = {s["mac"] for s in self.view["stations"]}
            if self._known:
                for m in sorted(self._known - macs):
                    self.notice(f"HaLow: {m} left the bridge.", "warn")
                for m in sorted(macs - self._known):
                    self.notice(f"HaLow: {m} joined the bridge.", "info")
            self._known = macs
        self.emit({"snapshot": self.snapshot()})

    async def _loop(self):
        while True:
            try:
                await self.read()
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
                "stale": self.error is not None and self.at is not None, **self.view}

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
