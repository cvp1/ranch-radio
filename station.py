#!/usr/bin/env python3
"""Station: one page and one set of ports for every radio on this machine.

    python3 station.py --config ~/.config/radio-remote/station.json [--https]

Each HF radio keeps its own Radio Remote server (server.py, one per radio,
started when the radio is plugged in), with its own rigctl port and its own
transmit safety. This process is the front of the station:

  web     the station page (--listen, 7300): Radios, Winlink, Mesh and Packet,
          with each radio's own page under /r/<id>/ and the UV-PRO hub's under
          /packet/, both proxied from this machine.
  rigctl  the shared port (4532): each new connection goes to the radio that
          has been connected longest, so a lone radio is always on 4532. Every
          radio also has its own fixed port (G90 4533, QMX 4534), which never
          changes: an app that must reach one radio uses that port.
  Winlink one Pat and one mailbox. A session goes out over ARDOP on an HF
          radio (this process runs ardopcf on that radio's sound card and arms
          the radio's Winlink lane through its server, which keeps the lane,
          codec level and station-ID wait), over packet through the UV-PRO
          hub, or over telnet.
  Mesh    the Meshtastic node (mesh.py).

Safety the radio servers rely on: a session's lane is armed over this
process's WebSocket to that radio's server, so if this process dies the
radio disarms itself (as when a page closes). Any page on the LAN may switch
the UV-PRO hub's transmit on or off, as on the hub's own page; lending the
radio stays with this machine. Browser POSTs and WebSockets must come from
this station's own pages (cross-site guard, below).
"""

import argparse
import asyncio
import json
import logging
import os
import pathlib
import secrets
import shlex
import subprocess
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import aiohttp
from aiohttp import WSMsgType, web

import webguard
import winlink
from server import band_of, tls_context

HERE = pathlib.Path(__file__).resolve().parent
log = logging.getLogger("station")

PRESENCE_EVERY_S = 1.0
ARM_WAIT_S = 6.0               # a radio server answers an arm within this
END_WAIT_S = winlink.ID_GRACE_S + 10
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailers", "transfer-encoding", "upgrade", "host", "content-length"}


def _expand(p):
    return os.path.expanduser(p) if isinstance(p, str) else p


class Radio:
    """One HF radio's Radio Remote server, as the station sees it."""

    def __init__(self, cfg):
        self.id = cfg["id"]
        self.name = cfg.get("name", self.id.upper())
        self.device = cfg.get("device")
        self.unit = cfg.get("unit")
        self.web = int(cfg["web"])
        self.rigctl = int(cfg["rigctl"])
        self.lane = int(cfg["lane"]) if cfg.get("lane") else None
        self.audio_in = cfg.get("audio_in")
        self.audio_out = cfg.get("audio_out")
        self.present = False
        self.since = None              # when it became present (orders the shared port)

    def info(self, primary):
        return {"id": self.id, "name": self.name, "present": self.present,
                "rigctl": self.rigctl, "shared": primary is self,
                "winlink": bool(self.lane and self.audio_in and self.audio_out)}


class RemoteLane:
    """A Winlink session keying one of the station's HF radios: armed over a
    WebSocket to that radio's server, which holds the lane, the codec level
    and the end-of-session ID wait. The socket closing disarms the radio."""

    def __init__(self, station, radio):
        self.station, self.radio, self.name = station, radio, radio.name
        self.ws = self.http = None
        self.radio_state = {"armed": False, "codec_db": None, "id": None}
        self._armed = asyncio.Event()
        self._disarmed = asyncio.Event()
        self._refusal = None
        self._reader = None
        self._ending = False

    @property
    def armed(self):
        return bool(self.radio_state.get("armed"))

    def state(self):
        return dict(self.radio_state)

    async def arm(self, ws, url):
        st = self.station
        if not self.radio.present:
            st.notice(f"The {self.radio.name} is not connected.", "warn", ws)
            return False
        try:
            await st.ardop.use(self.radio)
        except (OSError, RuntimeError) as e:
            st.notice(f"Could not start the ARDOP modem for the {self.radio.name}: {e}", "warn", ws)
            return False
        if not await st.winlink._ready():
            st.notice("The Winlink engine (Pat) is not answering; not connecting.", "warn", ws)
            return False
        self.http = aiohttp.ClientSession()
        try:
            self.ws = await self.http.ws_connect(f"http://127.0.0.1:{self.radio.web}/ws"
                                                 + (f"?token={st.token}" if st.token else ""),
                                                 heartbeat=5)
            hello = json.loads((await self.ws.receive(timeout=5)).data)
            self._take(hello.get("winlink") or {})
            if self.armed:
                raise RuntimeError("its Winlink lane is already armed by someone else")
            self._reader = asyncio.ensure_future(self._read())
            await self.ws.send_str(json.dumps({"t": "winlink_arm", "v": True,
                                               "freq": winlink.url_freq_hz(url),
                                               "qsy": winlink.url_hz(url)}))
            await asyncio.wait_for(self._armed.wait(), ARM_WAIT_S)
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError, ValueError, RuntimeError) as e:
            why = self._refusal or f"the {self.radio.name}'s server did not arm ({e or 'no answer'})"
            st.notice(f"Winlink not started: {why}", "warn", ws)
            await self._close()
            return False
        log.info("winlink: %s lane armed for %s", self.radio.name, url)
        return True

    def _take(self, d):
        for k in ("armed", "codec_db", "id"):
            if k in d:
                self.radio_state[k] = d[k]

    async def _read(self):
        try:
            async for msg in self.ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                m = json.loads(msg.data)
                if m.get("t") == "winlink":
                    self._take(m)
                    if m.get("armed") is True:
                        self._armed.set()
                    elif m.get("armed") is False:
                        self._disarmed.set()
                    if "id" in m:
                        self.station.winlink._send({"id": m["id"]})
                elif m.get("t") == "notice" and m.get("level") == "warn" and not self._armed.is_set():
                    self._refusal = m.get("msg")
        except (aiohttp.ClientError, OSError, ValueError) as e:
            log.info("winlink: %s lane socket: %s", self.radio.name, e)
        finally:
            log.info("winlink: %s lane socket ended (close code %s)", self.radio.name,
                     self.ws.close_code if self.ws is not None else None)
            self.radio_state["armed"] = False
            self._disarmed.set()
            if self._armed.is_set() and not self._ending:
                # The radio disarmed (or its server went away) mid-session.
                await self.station.winlink.abort(f"the {self.radio.name} disarmed")

    async def end(self, started, ok, reason, closing, notify):
        self._ending = True
        try:
            if self.ws is not None and not self.ws.closed and self.armed:
                await self.ws.send_str(json.dumps({"t": "winlink_end", "started": started, "ok": ok,
                                                   "reason": reason}))
                try:
                    await asyncio.wait_for(self._disarmed.wait(), END_WAIT_S)
                except asyncio.TimeoutError:
                    log.warning("winlink: the %s did not disarm; closing its lane", self.radio.name)
        finally:
            await self._close()            # closing the socket disarms in any case

    async def _close(self):
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self._reader:
            await asyncio.gather(self._reader, return_exceptions=True)
        if self.http:
            await self.http.close()
        self.ws = self.http = self._reader = None


class Ardop:
    """ardopcf for HF Winlink, on one radio's sound card at a time (Pat talks
    to 127.0.0.1:<port>). Starting it, or moving it to another radio, also
    restarts Pat: Pat opens its rig (the station's lane port, which goes to
    the ARDOP radio) and the modem when it starts and keeps them."""

    def __init__(self, station, cfg):
        self.station = station
        self.cmd = [_expand(w) for w in shlex.split(cfg.get("ardopcf", "~/.local/bin/ardopcf"))]
        self.port = int(cfg.get("ardop_port", 8515))
        self.logs = pathlib.Path(_expand(cfg.get("ardop_logs", "~/.local/state/radio-remote/ardop")))
        self.radio = None
        self.proc = None

    def logdir(self, radio):
        d = self.logs / radio.id
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def use(self, radio):
        if self.radio is radio and self.proc and self.proc.returncode is None:
            return
        await self.stop()
        if not (radio.audio_in and radio.audio_out):
            raise RuntimeError(f"no sound card configured for the {radio.name}")
        cmd = [*self.cmd, str(self.port), f"pipewire:NODE={radio.audio_in}",
               f"pipewire:NODE={radio.audio_out}", "--logdir", str(self.logdir(radio))]
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        self.radio = radio
        log.info("ardopcf started (pid %d) on the %s's sound card", self.proc.pid, radio.name)
        for _ in range(50):
            try:
                _, w = await asyncio.open_connection("127.0.0.1", self.port)
                w.close()
                break
            except OSError:
                if self.proc.returncode is not None:
                    raise RuntimeError(f"ardopcf exited ({self.proc.returncode})")
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError("ardopcf did not open its port")
        await self.station.winlink.restart_pat(f"ARDOP now on the {radio.name}")

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None


class Station:
    """The station page's server: radios, the shared rigctl port, Winlink,
    Mesh and the packet hub."""

    def __init__(self, cfg, args):
        self.cfg, self.args = cfg, args
        self.token = args.token
        self.radios = [Radio(r) for r in cfg.get("radios", [])]
        self.clients = set()
        self.ardop = Ardop(self, cfg.get("winlink", {}))
        packet = cfg.get("packet") or {}
        self.packet_url = packet.get("url")
        self.packet_agwpe = packet.get("agwpe")
        self.packet_up = False
        self.addons = None            # addons.Addons from the config's "addons" (UV-PRO, ...)
        self.modules = None           # ranch_radio.pageapi.ModulesService from the config's "host_file"
        self._tasks = []
        self._rigctl = None
        self._lane = None

    band_of = staticmethod(band_of)

    # --- radios

    def primary(self):
        present = [r for r in self.radios if r.present]
        return min(present, key=lambda r: r.since) if present else None

    def radio(self, rid):
        return next((r for r in self.radios if r.id == rid), None)

    def _unit_since(self, r):
        """When the radio's server started (orders the shared port across a
        restart of this process); now if unknown."""
        if r.unit:
            try:
                out = subprocess.run(["systemctl", "--user", "show", "-p",
                                      "ActiveEnterTimestampMonotonic", "--value", r.unit],
                                     capture_output=True, text=True, timeout=3).stdout.strip()
                if out and int(out) > 0:
                    return int(out) / 1e6
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        return time.monotonic()

    async def _up(self, port):
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 0.5)
            w.close()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    async def presence_loop(self):
        first = True
        while True:
            changed = False
            for r in self.radios:
                up = (not r.device or os.path.exists(r.device)) and await self._up(r.web)
                if up != r.present:
                    r.present, changed = up, True
                    r.since = (self._unit_since(r) if first else time.monotonic()) if up else None
                    log.info("radio %s %s", r.name, "connected" if up else "gone")
                    if not up and self.ardop.radio is r:
                        await self.ardop.stop()
            if self.packet_url:
                hub = urlsplit(self.packet_url)
                up = await self._up(hub.port or 80)
                if up != self.packet_up:
                    self.packet_up, changed = up, True
            if changed or first:
                p = self.primary()
                log.info("shared rigctl port %d: %s", self.args.rigctl_port, p.name if p else "no radio")
                self._broadcast(json.dumps({"t": "radios", **self.radios_info()}))
            first = False
            await asyncio.sleep(PRESENCE_EVERY_S)

    def radios_info(self):
        p = self.primary()
        return {"radios": [r.info(p) for r in self.radios], "shared_port": self.args.rigctl_port,
                "packet": {"present": self.packet_up, "configured": bool(self.packet_url)}}

    # --- the shared rigctl port and Pat's rig lane: plain TCP relays

    async def _relay(self, reader, writer, target, what):
        peer = writer.get_extra_info("peername")
        if target is None:
            log.info("%s %s: no radio connected; closing", what, peer)
            writer.close()
            return
        name, port = target
        try:
            r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        except OSError as e:
            log.warning("%s %s: the %s is not answering on %d (%s)", what, peer, name, port, e)
            writer.close()
            return
        log.info("%s %s -> %s (port %d)", what, peer, name, port)

        async def pipe(a, b):
            try:
                while data := await a.read(65536):
                    b.write(data)
                    await b.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                b.close()

        await asyncio.gather(pipe(reader, w2), pipe(r2, writer))
        log.info("%s %s closed (%s)", what, peer, name)

    async def _rigctl_client(self, reader, writer):
        p = self.primary()
        await self._relay(reader, writer, (p.name, p.rigctl) if p else None, "rigctl")

    async def _lane_client(self, reader, writer):
        # Pat's hamlib rig: the radio ARDOP runs on (before any session: the
        # shared radio, so Pat's startup check finds a rig). The radio's lane
        # refuses PTT unless a session armed it, whatever comes through here.
        r = self.ardop.radio
        if r is None or not r.present:
            able = [x for x in self.radios if x.present and x.lane]
            p = self.primary()
            r = p if p in able else (able[0] if able else None)
        await self._relay(reader, writer, (r.name, r.lane) if r and r.lane else None, "winlink lane")

    # --- Winlink session routing

    def lane_for(self, url, via):
        scheme = urlsplit(url).scheme
        if scheme == "telnet":
            return winlink.NoLane("telnet"), None
        if scheme.startswith("ax25"):
            if not self.packet_agwpe:
                return None, "No packet radio is set up on this station."
            if not self.packet_up:
                return None, "The UV-PRO packet hub is not running."
            return winlink.NoLane("UV-PRO"), None
        if scheme != "ardop":
            return None, f"{scheme} is not set up on this station."
        able = [r for r in self.radios if r.present and r.info(None)["winlink"]]
        r = self.radio(via) if via else (able[0] if len(able) == 1 else None)
        if r is None:
            return None, ("No HF radio with Winlink is connected." if not able
                          else "Pick the radio for this session.")
        if r not in able:
            return None, f"The {r.name} is not connected or has no Winlink set up."
        return RemoteLane(self, r), None

    # --- pages

    def _broadcast(self, msg):
        for ws in list(self.clients):
            if not ws.closed:
                asyncio.ensure_future(self._send(ws, msg))

    async def _send(self, ws, msg):
        try:
            await ws.send_str(msg)
        except (ConnectionResetError, RuntimeError):
            pass

    def notice(self, msg, level="info", ws=None):
        payload = json.dumps({"t": "notice", "level": level, "msg": msg})
        if ws is not None:
            asyncio.ensure_future(self._send(ws, payload))
        else:
            self._broadcast(payload)

    @property
    def winlink(self):
        """winlink.PatEngine while the Winlink add-on (addons/winlink.py) is
        switched on, else None: off reads exactly like "no Pat here", so every
        lane and safety path below is unchanged."""
        return self.addons.engine("winlink") if self.addons else None

    def config(self):
        return {"name": self.cfg.get("name", "Station"),
                "winlink": bool(self.addons and self.addons.get("winlink")),
                "mesh": bool(self.addons and self.addons.get("mesh")), "station": True,
                "addons": self.addons.describe() if self.addons else [],
                "modules": self.modules is not None}

    async def handle(self, ws):
        self.clients.add(ws)
        await ws.send_str(json.dumps({"t": "hello", "config": self.config(), **self.radios_info(),
                                      "winlink": (self.addons.own_snapshot("winlink")
                                                  if self.addons and self.addons.get("winlink") else None),
                                      "mesh": self.addons.own_snapshot("mesh") if self.addons else None,
                                      "addons": self.addons.snapshot() if self.addons else {}}))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    await self._command(ws, json.loads(msg.data))
                except (ValueError, KeyError, TypeError) as e:
                    self.notice(f"Rejected: {e}", "warn", ws)
        finally:
            self.clients.discard(ws)
            if self.winlink and self.winlink.session_owner is ws:
                # Operator gone: the radio disarms at once (its socket closes),
                # as when the radio's own page closes; then Pat stops.
                lane = self.winlink.lane
                if isinstance(lane, RemoteLane):
                    await lane._close()
                await self.winlink.abort("operator's page closed")

    async def _command(self, ws, m):
        t = m["t"]
        if t in ("winlink_connect", "winlink_stop", "winlink_prompt"):
            if self.addons and self.addons.route(t):
                await self.addons.command(ws, m)          # the Winlink add-on (on, or says it's off)
            elif t == "winlink_connect":
                self.notice("Winlink is not enabled on this station.", "warn", ws)
        elif t.startswith("mesh_") and not (self.addons and self.addons.route(t)):
            self.notice("Mesh is not enabled on this station.", "warn", ws)
        elif self.addons and self.addons.route(t):     # an add-on's own page (Mesh)
            await self.addons.command(ws, m)
        elif t == "addon":
            if self.addons is None:
                self.notice("No add-ons on this station.", "warn", ws)
            else:
                await self.addons.command(ws, m)
        elif t in ("modules", "modules_check", "module_plan", "module_apply"):
            if self.modules is None:
                self.notice("Modules: this station's config names no host file; run ranch-radio install.",
                            "warn", ws)
            else:
                self.modules.handle(ws, m)
        elif t == "ping":
            await ws.send_str(json.dumps({"t": "pong", "ts": m.get("ts")}))

    # --- lifecycle

    async def start(self):
        a = self.args
        self._rigctl = await asyncio.start_server(self._rigctl_client, a.rigctl_host, a.rigctl_port)
        log.info("shared rigctl port on %s:%d", a.rigctl_host, a.rigctl_port)
        wl = self.cfg.get("winlink") or {}
        if wl.get("lane_port"):
            self._lane = await asyncio.start_server(self._lane_client, "127.0.0.1", int(wl["lane_port"]))
        if self.addons:
            await self.addons.start()
        self._tasks.append(asyncio.create_task(self.presence_loop()))

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        if self.addons:
            await self.addons.stop()
        await self.ardop.stop()
        for srv in (self._rigctl, self._lane):
            if srv:
                srv.close()


# --- HTTP -------------------------------------------------------------------

PLAIN_PATH = __import__("re").compile(r"[A-Za-z0-9._~!$&'()*+,;=:@/-]*")


def canonical_tail(request):
    """The proxied path as the backend will receive it, or None if it isn't a
    plain path. aiohttp keeps `x/../api/lend` in the route's tail while the
    outgoing URL normalises it to `api/lend`, so a check on the tail could be
    walked around (review 2026-10-19, Codex; reproduced on linux-station). Refuse
    dot segments (also %2e-encoded, decoded by then), backslashes, a leftover
    percent sign (double encoding) and empty segments other than a trailing one."""
    tail = request.match_info["tail"]
    # The tail is already percent-decoded: a decoded "?", "#" or control
    # character would change what the outgoing URL means (review 2026-10-19,
    # Codex: /packet/api/lend%3fx reached lend). Only plain path characters.
    if not PLAIN_PATH.fullmatch(tail):
        return None
    parts = tail.split("/")
    for i, seg in enumerate(parts):
        if seg in (".", "..") or (seg == "" and i != len(parts) - 1):
            return None
    return tail


same_origin = webguard.same_origin


def cross_site_guard(allow_hosts=()):
    """The shared guard (webguard.cross_site_guard), logging as the station."""
    return webguard.cross_site_guard(allow_hosts, log)


def make_app(station, args):
    allow = webguard.allowed_names(args)
    app = web.Application(client_max_size=64 * 1024 * 1024, middlewares=[cross_site_guard(allow)])
    sockets = set()

    def authorised(request):
        return not args.token or secrets.compare_digest(request.query.get("token", ""), args.token)

    async def index(request):
        return web.FileResponse(HERE / "static" / "station.html", headers={"Cache-Control": "no-cache"})

    async def ws_control(request):
        ws = web.WebSocketResponse(heartbeat=5, max_msg_size=1 << 16)
        await ws.prepare(request)
        if not authorised(request):
            await ws.close(code=4401, message=b"token required")
            return ws
        sockets.add(ws)
        try:
            await station.handle(ws)
        finally:
            sockets.discard(ws)
        return ws

    http = {}

    async def proxy(request, base, tail, body=None):
        """HTTP and WebSocket to a local backend (a radio's page, the hub)."""
        url = f"{base}/{tail}"
        if request.query_string:
            url += "?" + request.query_string
        if request.headers.get("Upgrade", "").lower() == "websocket":
            ws = web.WebSocketResponse(heartbeat=5, max_msg_size=1 << 20)
            await ws.prepare(request)
            sockets.add(ws)
            try:
                async with http["s"].ws_connect(url.replace("http", "ws", 1), heartbeat=5,
                                                max_msg_size=1 << 20) as up:
                    async def down():
                        async for m in up:
                            if m.type == WSMsgType.TEXT:
                                await ws.send_str(m.data)
                            elif m.type == WSMsgType.BINARY:
                                await ws.send_bytes(m.data)
                        await ws.close()
                    t = asyncio.ensure_future(down())
                    try:
                        async for m in ws:
                            if m.type == WSMsgType.TEXT:
                                await up.send_str(m.data)
                            elif m.type == WSMsgType.BINARY:
                                await up.send_bytes(m.data)
                    finally:
                        t.cancel()
                        await up.close()
            except (aiohttp.ClientError, OSError) as e:
                log.info("proxy ws %s: %s", url, e)
                await ws.close(code=1011, message=b"radio not answering")
            finally:
                sockets.discard(ws)
            return ws
        # The browser's Origin was checked above (cross_site_guard); the
        # backend sees this station, so it gets the station's header instead.
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP | {"origin"}}
        headers["X-Ranch-Radio"] = "1"
        try:
            async with http["s"].request(request.method, url, headers=headers,
                                         data=body if body is not None else
                                         (await request.read() if request.body_exists else None),
                                         allow_redirects=False) as r:
                body = await r.read()
                out = {k: v for k, v in r.headers.items() if k.lower() not in HOP}   # body as sent
                # A backend that says nothing about caching gets no-cache, so a
                # browser always revalidates (it kept the hub's pre-M2 page,
                # 2026-10-19). ETag/Last-Modified still make that a cheap 304.
                if not any(k.lower() == "cache-control" for k in out):
                    out["Cache-Control"] = "no-cache"
                return web.Response(status=r.status, body=body, headers=out)
        except (aiohttp.ClientError, OSError) as e:
            return web.Response(status=502, text=f"not answering: {e}")

    async def radio_page(request):
        r = station.radio(request.match_info["rid"])
        if r is None:
            raise web.HTTPNotFound()
        tail = canonical_tail(request)
        if tail is None:
            return web.json_response({"error": "not a plain path"}, status=400)
        return await proxy(request, f"http://127.0.0.1:{r.web}", tail)

    async def packet_page(request):
        if not station.packet_url:
            raise web.HTTPNotFound()
        tail = canonical_tail(request)
        if tail is None:
            return web.json_response({"error": "not a plain path"}, status=400)
        if tail in ("api/lend", "api/return") and request.remote not in ("127.0.0.1", "::1"):
            # The hub lends the radio only to its own machine (the UV-PRO
            # add-on's control channel); through this proxy, check the real caller.
            return web.json_response({"error": "lending the radio is only allowed from this machine"},
                                     status=403)
        # Transmit on/off: any page on the LAN, as on the hub's own page (D7).
        return await proxy(request, station.packet_url.rstrip("/"), tail)

    def slash(prefix):
        async def h(request):
            raise web.HTTPFound(f"{prefix}{request.match_info.get('rid', '')}/" +
                                (f"?{request.query_string}" if request.query_string else ""))
        return h

    r = app.router
    r.add_get("/", index)
    r.add_get("/ws", ws_control)
    r.add_get("/r/{rid}", slash("/r/"))
    r.add_route("*", "/r/{rid}/{tail:.*}", radio_page)
    r.add_get("/packet", slash("/packet"))
    r.add_route("*", "/packet/{tail:.*}", packet_page)
    if station.addons is not None:
        station.addons.add_routes(app, authorised)      # Winlink's mailbox under /winlink/api/
    r.add_static("/static", HERE / "static")

    @web.middleware
    async def no_cache(request, handler):
        resp = await handler(request)
        if request.path.startswith("/static") or request.path == "/":
            resp.headers["Cache-Control"] = "no-cache"
        return resp
    app.middlewares.append(no_cache)

    async def on_startup(app):
        http["s"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=3),
                                          auto_decompress=False)
        await station.start()

    async def on_shutdown(app):
        closes = [ws.close() for ws in list(sockets) if not ws.closed]
        if closes:
            await asyncio.wait([asyncio.ensure_future(c) for c in closes], timeout=1.0)
        await station.stop()

    async def on_cleanup(app):
        await http["s"].close()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--config", default="~/.config/radio-remote/station.json",
                   help="the station's radios, ports and services (see linux/station.example.json)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--listen", type=int, default=7300)
    p.add_argument("--https", action="store_true")
    p.add_argument("--tls-name", action="append", default=[])
    p.add_argument("--allow-host", action="append", default=[],
                   help="a dotted host name browsers use for this station (IPs, single-label, .local, "
                        ".home.arpa and .ts.net names are always allowed)")
    p.add_argument("--token", default=os.environ.get("IC7300_TOKEN"))
    p.add_argument("--rigctl-host", default="0.0.0.0")
    p.add_argument("--rigctl-port", type=int, default=4532,
                   help="the shared rigctl port: the radio connected longest (default 4532)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    with open(_expand(args.config)) as f:
        cfg = json.load(f)
    station = Station(cfg, args)

    addon_cfg = dict(cfg.get("addons") or {})
    wl = cfg.get("winlink") or {}
    if wl.get("pat"):             # Winlink (Pat) is an add-on (addons/winlink.py); the lanes are not
        addon_cfg.setdefault("winlink", {"lane_for": station.lane_for, "engine_args": SimpleNamespace(
            winlink_pat=" ".join(shlex.quote(_expand(w)) for w in shlex.split(wl["pat"])), winlink_pat_port=int(wl.get("pat_port", 8095)),
            winlink_session_max=float(wl.get("session_max", 900)))})
    m = cfg.get("mesh") or {}
    if m.get("port"):             # Mesh is an add-on (addons/mesh.py); "mesh" configures it
        addon_cfg.setdefault("mesh", dict(m))
    if addon_cfg:
        from addons import Addons
        station.addons = Addons(station, addon_cfg, cfg.get("addons_state", "~/.local/state/radio-remote/addons.json"))
    if cfg.get("host_file"):
        from ranch_radio import pageapi
        # RANCH_RADIO_REPO / RANCH_RADIO_HOME: tests point these at scratch copies.
        station.modules = pageapi.ModulesService(cfg["host_file"], os.environ.get("RANCH_RADIO_REPO") or HERE,
                                                 lambda: station.addons, station._send, station._broadcast,
                                                 home=os.environ.get("RANCH_RADIO_HOME"),
                                                 state_path=cfg.get("addons_state",
                                                                    "~/.local/state/radio-remote/addons.json"))

    ctx = tls_context(HERE / "certs", args.tls_name) if args.https else None
    print(f"\n  Station on {'https' if ctx else 'http'}://<this-machine>:{args.listen}/\n")
    web.run_app(make_app(station, args), host=args.host, port=args.listen, ssl_context=ctx, print=None)


if __name__ == "__main__":
    main()
