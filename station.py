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
  Logsync every QSO JTDX logs, to Wavelog, QRZ and LoTW (logsync.py), from
          station.json's "logsync" block. Not an add-on: the Logbook tab only
          shows its status, and a broken logsync is reported, never fatal.

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
ARM_WAIT_S = 30.0              # an arm may wait for the radio's ATU tune (server.py WINLINK_TUNE_WAIT_S)
WATCH_RETRY_S = 2.0            # a radio's watch socket, after it drops
END_WAIT_S = winlink.ID_GRACE_S + 10
HOP = webguard.HOP


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
        # A radio with no "rigctl" in its host file listens on server.py's
        # default (4532): the first Mac host's did, and the station died on it (2026-11-10).
        self.rigctl = int(cfg["rigctl"]) if cfg.get("rigctl") is not None else 4532
        self.lane = int(cfg["lane"]) if cfg.get("lane") else None
        self.audio_in = cfg.get("audio_in")
        self.audio_out = cfg.get("audio_out")
        # The HF modems its lane has (station.json "modems": ARDOP, and VARA HF
        # where xara is set up), and xara's command port for Pat's check.
        self.modems = tuple(cfg.get("modems") or ("ardop",))
        self.vara_port = cfg.get("vara_port")
        self.vara_log = _expand(cfg.get("vara_log"))
        # Winlink can use it: the station can put ardopcf on its sound card, or
        # ardopcf is already there (a Mac's launchd agent; Station sets this).
        self.audio_ok = bool(self.audio_in and self.audio_out)
        self.present = False
        self.since = None              # when it became present (orders the shared port)

    def info(self, primary):
        return {"id": self.id, "name": self.name, "present": self.present,
                "rigctl": self.rigctl, "shared": primary is self,
                "winlink": bool(self.lane and self.audio_ok), "modems": list(self.modems)}


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
        self.page = None

    @property
    def armed(self):
        return bool(self.radio_state.get("armed"))

    def state(self):
        return {**self.radio_state, "scheme": getattr(self, "scheme", "ardop")}

    def max_level(self, since, until=None):
        """xara's highest speed level in the session, from the radio's xara log
        (station.json "vara_log"); None without one."""
        if not self.radio.vara_log:
            return None
        import station_id
        return station_id.max_level(station_id.vara_levels(self.radio.vara_log), since, until)

    async def arm(self, ws, url):
        st = self.station
        self.page = ws                     # the session's page: the radio's notices are relayed to it
        scheme = urlsplit(url).scheme
        self.scheme = scheme if scheme in winlink.HF_MODEMS else "ardop"
        if not self.radio.present:
            st.notice(f"The {self.radio.name} is not connected.", "warn", ws)
            return False
        if self.scheme == "ardop":           # VARA's modem (xara) is its own agent on the radio's lane
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
            wl = hello.get("winlink") or {}
            self._take(wl)
            if self.armed:
                raise RuntimeError("its Winlink lane is already armed by someone else")
            modems = wl.get("modems")
            if modems and self.scheme not in modems:       # the radio's server says what it has
                raise RuntimeError(f"its server has no {self.scheme} lane")
            self._reader = asyncio.ensure_future(self._read())
            await self.ws.send_str(json.dumps({"t": "winlink_arm", "v": True, "scheme": self.scheme,
                                               "freq": winlink.url_freq_hz(url),
                                               "qsy": winlink.url_hz(url)}))
            # Armed, or refused (a warning notice, or a disarm) as soon as the radio says.
            waits = [asyncio.ensure_future(self._armed.wait()), asyncio.ensure_future(self._disarmed.wait())]
            try:
                await asyncio.wait_for(asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED), ARM_WAIT_S)
            finally:
                for w in waits:
                    w.cancel()
            if not self._armed.is_set():
                raise RuntimeError(self._refusal or "the radio refused")
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError, RuntimeError) as e:
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
                    # the station-ID ledger reaches the pages through the radio's watch
                elif m.get("t") == "notice":
                    if m.get("level") == "warn" and not self._armed.is_set():
                        self._refusal = m.get("msg")      # a refusal: arm() reports it and stops waiting
                        self._disarmed.set()
                    else:                                 # the radio talking (tuning, holds): to the session's page
                        self.station.notice(m.get("msg", ""), m.get("level", "info"), getattr(self, "page", None))
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
                except TimeoutError:
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


class RadioWatch:
    """A standing, read-only WebSocket to one radio's server while it is
    present: its hello and every {"t": "winlink"} it sends (armed, codec_db,
    the station-ID ledger, the modems' status) are kept as the radio's lane state, and changes
    to the ledger and codec level are relayed to the station's pages when
    this is the radio the Winlink panel shows. A session has its own socket
    (RemoteLane); this one never arms or ends anything."""

    KEYS = ("armed", "codec_db", "id", "modem")
    RIG = ("connected", "tx", "freq", "mode", "data")    # the radio's own word on what it is doing, for the Map tab

    def __init__(self, station, radio):
        self.station, self.radio = station, radio
        self.state = {"armed": False, "codec_db": None, "id": None, "modem": None}
        self.rig = {}                  # RIG keys from the radio's hello and state changes; {} until a hello
        self.seen = False              # a hello has been read since the radio appeared
        self._task = None

    def start(self):
        if self._task is None or self._task.done():
            self.seen = False
            self._task = asyncio.ensure_future(self._run())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self.seen = False
        if self.rig:
            self.rig = {}
            self.station.rig_changed()

    async def _run(self):
        st = self.station
        while True:
            try:
                async with aiohttp.ClientSession() as s, \
                        s.ws_connect(f"http://127.0.0.1:{self.radio.web}/ws" + (f"?token={st.token}" if st.token else ""),
                                     heartbeat=5) as ws:
                    hello = json.loads((await ws.receive(timeout=5)).data)
                    self._take(hello.get("winlink") or {}, first=True)
                    self._take_rig(hello.get("state") or {})
                    async for msg in ws:
                        if msg.type == WSMsgType.TEXT:
                            m = json.loads(msg.data)
                            if m.get("t") == "winlink":
                                self._take(m)
                            elif m.get("t") == "state":
                                self._take_rig(m.get("d") or {})
                            elif m.get("t") == "codec" and "current" in m:      # the codec level moved
                                self._take({"codec_db": m["current"]})
            except (aiohttp.ClientError, OSError, ValueError, TimeoutError, AttributeError) as e:
                log.debug("watch %s: %s", self.radio.name, e)
            self._take_rig({"connected": False, "tx": False})     # the socket dropped: nothing known to be keyed
            await asyncio.sleep(WATCH_RETRY_S)

    def _take_rig(self, d):
        """Meter readings arrive many times a second; only a change to what
        the radio is doing (RIG) is passed on."""
        new = {k: d[k] for k in self.RIG if k in d and d[k] != self.rig.get(k)}
        if new:
            self.rig.update(new)
            self.station.rig_changed()

    def _take(self, d, first=False):
        changed = {k: d[k] for k in self.KEYS if k in d and d[k] != self.state.get(k)}
        self.state.update({k: d[k] for k in self.KEYS if k in d})
        self.seen = True
        eng = self.station.winlink
        if changed and not first and eng is not None and self.station.panel_radio() is self.radio:
            relay = {k: v for k, v in changed.items() if k in ("id", "codec_db", "modem")}
            if relay:
                eng._send(relay)


class IdleLane:
    """What the Winlink panel shows between sessions: the lane state of the
    radio a session would use (PatEngine.idle_lane)."""

    name = "idle"
    armed = False

    def __init__(self, station):
        self.station = station

    def state(self):
        r = self.station.panel_radio()
        w = self.station.watches.get(r.id) if r else None
        return dict(w.state) if w else {"armed": False, "codec_db": None, "id": None}


class Ardop:
    """ardopcf for HF Winlink, on one radio's sound card at a time (Pat talks
    to 127.0.0.1:<port>). Starting it, or moving it to another radio, also
    restarts Pat: Pat opens its rig (the station's lane port, which goes to
    the ARDOP radio) and the modem when it starts and keeps them."""

    def __init__(self, station, cfg):
        self.station = station
        a = cfg.get("ardopcf", "~/.local/bin/ardopcf")
        # A Mac's ardopcf is a launchd agent of its own, on one radio's sound
        # card (the host file's dict: label, bin, port, audio_in, audio_out).
        # The station never starts or stops it; a session only checks its port.
        self.external = isinstance(a, dict)
        self.cmd = [] if self.external else [_expand(w) for w in shlex.split(str(a))]
        self.port = int(a.get("port", 8515) if self.external else cfg.get("ardop_port", 8515))
        self.logs = pathlib.Path(_expand(cfg.get("ardop_logs", "~/.local/state/radio-remote/ardop")))
        self.radio = None
        self.proc = None

    async def _listening(self):
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.port), 1.0)
            w.close()
            return True
        except (TimeoutError, OSError):
            return False

    def logdir(self, radio):
        d = self.logs / radio.id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def session_logdir(self):
        """ardopcf's --logdir now: an agent's (external) is the host file's
        ardop_logs itself; one the station runs logs per radio. None: none yet."""
        if self.external:
            return self.logs
        return self.logs / self.radio.id if self.radio is not None else None

    async def use(self, radio):
        if self.external:
            if not await self._listening():
                raise RuntimeError(f"ardopcf is not listening on 127.0.0.1:{self.port} (its own agent runs it)")
            self.radio = radio
            return
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
        if self.external:
            self.radio = None
            return
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
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
        if self.ardop.external:
            for r in self.radios:
                if r.lane:
                    r.audio_ok = True
        # Every radio is watched (2026-11-15; before, only those with a Winlink
        # lane): the Map tab shows what each one is doing. A watch only reads.
        self.watches = {r.id: RadioWatch(self, r) for r in self.radios}
        self.packet_cfg = cfg.get("packet") or {}
        self.packet_url = self.packet_cfg.get("url")
        self.packet_agwpe = self.packet_cfg.get("agwpe")
        self.packet_up = False
        self.addons = None            # addons.Addons from the config's "addons" (UV-PRO, ...)
        self.modules = None           # ranch_radio.pageapi.ModulesService from the config's "host_file"
        self.logsync = None           # logsync.LogSync from the config's "logsync" block
        self.logsync_error = None     # why it isn't running: shown on the Logbook tab
        self.settings_plans = None    # logsync_settings.Plans: the Logbook tab's planned host-file edits
        self.wavelog_cat = None       # wavelog_cat.WavelogCat: each radio's frequency to Wavelog, its clicks back
        self.wavelog_cat_error = None # why it isn't running
        self.js8 = None               # js8_bridge.Js8Bridge: each radio's JS8 engine, receive only
        self.js8_error = None         # why it isn't running
        self._tasks = []
        self._rigctl = None
        self._lane = None
        self.shared_port = self.args.rigctl_port if getattr(self.args, "rigctl_port", 0) > 0 else 0
        self.shared_port_enabled = self.shared_port > 0

    band_of = staticmethod(band_of)

    # --- radios

    def primary(self):
        if not self.shared_port_enabled:
            return None
        present = [r for r in self.radios if r.present]
        return min(present, key=lambda r: r.since) if present else None

    def radio(self, rid):
        return next((r for r in self.radios if r.id == rid), None)

    def panel_radio(self):
        """The radio the Winlink panel's lane state and ID ledger are about:
        the one ARDOP is on, else the only Winlink-able radio present, else
        the shared port's radio."""
        if self.ardop.radio is not None and self.ardop.radio.present:
            return self.ardop.radio
        able = [r for r in self.radios if r.present and r.lane and r.audio_ok]
        if len(able) == 1:
            return able[0]
        p = self.primary()
        return p if p in able else (able[0] if able else None)

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
        except (TimeoutError, OSError):
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
                    w = self.watches.get(r.id)
                    if w is not None:
                        if up:
                            w.start()
                        else:
                            await w.stop()
            if self.packet_url:
                hub = urlsplit(self.packet_url)
                up = await self._up(hub.port or 80)
                if up != self.packet_up:
                    self.packet_up, changed = up, True
            if changed or first:
                p = self.primary()
                if self.shared_port_enabled:
                    log.info("shared rigctl port %d: %s", self.shared_port, p.name if p else "no radio")
                self._broadcast(json.dumps({"t": "radios", **self.radios_info()}))
            first = False
            await asyncio.sleep(PRESENCE_EVERY_S)

    def radios_info(self):
        p = self.primary()
        pkt = {"present": self.packet_up, "configured": bool(self.packet_url)}
        if hasattr(self, "packet_cfg") and isinstance(self.packet_cfg, dict):
            if self.packet_cfg.get("name"):
                pkt["name"] = self.packet_cfg["name"]
            if self.packet_cfg.get("title"):
                pkt["title"] = self.packet_cfg["title"]
        return {"radios": [r.info(p) for r in self.radios], "shared_port": self.shared_port,
                "packet": pkt}

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

    async def winlink_hub_links(self):
        """The packet hub's links (its /api/status), or None if there is no
        hub or it can't be asked: as server.Station.winlink_hub_links."""
        if not self.packet_url:
            return None
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s, \
                    s.get(self.packet_url.rstrip("/") + "/api/status") as r:
                links = (await r.json(content_type=None)).get("links")
            return links if isinstance(links, list) else None
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError, AttributeError):
            return None

    def winlink_listen_target(self, need_hub=True, need_up=True):
        """As server.Station.winlink_listen_target: the hub and the tuner a
        listen window uses, or (None, why not)."""
        name = self.packet_cfg.get("name") or "UV-PRO"
        uv = self.addons.get("uvpro") if self.addons and self.addons.is_on("uvpro") else None
        hub = self.packet_url.rstrip("/") if self.packet_url else None
        if not need_hub:
            return {"hub": hub, "name": name, "tuner": uv}, None
        if not (self.packet_agwpe and hub):
            return None, "No packet radio is set up on this station."
        if need_up and not self.packet_up:
            return None, f"The {name} packet hub is not running."
        return {"hub": hub, "name": name, "tuner": uv}, None

    def lane_for(self, url, via):
        scheme = urlsplit(url).scheme
        if scheme == "telnet":
            return winlink.NoLane("telnet", self.notice), None
        if scheme.startswith("ax25"):
            name = self.packet_cfg.get("name") or "UV-PRO"
            if not self.packet_agwpe:
                return None, "No packet radio is set up on this station."
            if not self.packet_up:
                return None, f"The {name} packet hub is not running."
            # As server.Station.winlink_lane_for: the UV-PRO add-on, on, tunes
            # the hub's radio (control-port mode; borrow mode refuses, saying why).
            uv = self.addons.get("uvpro") if self.addons and self.addons.is_on("uvpro") else None
            return (winlink.TunedLane(name, uv, self.notice) if uv else winlink.NoLane(name, self.notice)), None
        if scheme not in winlink.HF_MODEMS or not any(scheme in r.modems for r in self.radios):
            return None, f"{scheme} is not set up on this station."
        what = "VARA HF" if scheme == "varahf" else "Winlink"
        able = [r for r in self.radios if r.present and r.info(None)["winlink"] and scheme in r.modems]
        # via: the page's radio id, with the modem after a colon when the page
        # offers one via per modem ("ic7300:varahf"; the URL's scheme decides);
        # a name that is no radio of ours ("radio", from a page that knows one)
        # means the one radio that could take it.
        rid = str(via).partition(":")[0] if via else ""
        r = self.radio(rid) if rid and self.radio(rid) else (able[0] if len(able) == 1 else None)
        if r is None:
            return None, (f"No HF radio with {what} is connected." if not able
                          else "Pick the radio for this session.")
        if r not in able:
            return None, f"The {r.name} is not connected or has no {what} set up."
        if scheme == "varahf":
            why = self.vara_pat_refusal(r)
            if why:
                return None, why
        return RemoteLane(self, r), None

    def vara_pat_refusal(self, r):
        """Why Pat's config would let a VARA session key or tune outside the
        radio's lanes, or None: the modem must be xara on this machine (its
        command port) and Pat's QSY must go through that radio's lane; Pat
        never keys (pat_config.vara_refusal). Read at every arm, through
        pat_config.py only (the file holds the password)."""
        import pat_config
        path = getattr(self.winlink, "config_path", None)
        if not path:
            return "VARA HF: Pat's config file is not known here; not connecting."
        if not r.vara_port:
            return f"VARA HF: the {r.name} has no xara port in the station's config; not connecting."
        why = pat_config.vara_refusal(path, r.vara_port, r.lane)
        return f"VARA HF not ready: {why}." if why else None

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
                "modules": self.modules is not None, **self.identity()}

    def identity(self):
        """{"call", "grid"} for the page's top bar (2026-11-19, UX plan PR 0.4),
        derived from what the station already has; the host file gains no field.
        call: the logbook sync's block, else the callsign Pat reads from its own
        config (Winlink), else the Propagation add-on's PSKReporter call. grid:
        the one JTDX reports to the logbook sync, once it has. None when unknown.
        Read-only: nothing here changes what any module does."""
        def norm(v):
            v = str(v or "").strip()
            return v or None
        call = norm((self.cfg.get("logsync") or {}).get("call"))
        if not call:
            call = norm(getattr(self.winlink, "mycall", None))
        if not call and self.addons:
            call = norm(getattr(self.addons.get("propagation"), "psk_call", None))
        roster = getattr(self.logsync, "roster", None)
        grid = norm((getattr(roster, "st", None) or {}).get("de_grid"))
        return {"call": call.upper() if call else None, "grid": grid}

    async def handle(self, ws):
        self.clients.add(ws)
        await ws.send_str(json.dumps({"t": "hello", "config": self.config(), **self.radios_info(),
                                      "winlink": (self.addons.own_snapshot("winlink")
                                                  if self.addons and self.addons.get("winlink") else None),
                                      "mesh": self.addons.own_snapshot("mesh") if self.addons else None,
                                      "addons": self.addons.snapshot() if self.addons else {},
                                      "logsync": self.logsync_status(),
                                      "wavelog_cat": self.wavelog_cat_status(),
                                      "js8": self.js8_status()}))
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
        if not isinstance(t, str):     # a non-string type closed the page's socket (2026-11-19)
            raise TypeError("the message type must be a string")
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
        elif t == "logsync" and m.get("do") == "lookup":
            await self._send(ws, json.dumps(self.call_lookup(m)))      # the asking page only
        elif t == "logsync":
            if self.logsync is None:
                self.notice("Logbook sync is not running on this station.", "warn", ws)
            elif m.get("do") == "correct":
                why = await self.logsync.correct(int(m["id"]), str(m["dest"]))
                self.notice(why or f"Correction queued for {m['dest']}.", "warn" if why else "info", ws)
        elif t == "logsync_settings":
            await self._logsync_settings(ws, m)
        elif t == "js8":
            if self.js8 is None:
                self.notice("JS8 is not running on this station.", "warn", ws)
            else:
                await self.js8.handle(ws, m)
        elif t == "ping":
            await ws.send_str(json.dumps({"t": "pong", "ts": m.get("ts")}))

    # --- lifecycle

    async def start(self):
        a = self.args
        if self.shared_port_enabled:
            try:
                self._rigctl = await asyncio.start_server(self._rigctl_client, a.rigctl_host, a.rigctl_port)
                log.info("shared rigctl port on %s:%d", a.rigctl_host, a.rigctl_port)
            except OSError as e:
                log.error("Failed to start shared rigctl server on %s:%d: %s", a.rigctl_host, a.rigctl_port, e)
                raise SystemExit(f"Cannot bind to rigctl port {a.rigctl_port}. Is another process using it?") from e
        else:
            log.info("shared rigctl port disabled")
        wl = self.cfg.get("winlink") or {}
        if wl.get("lane_port"):
            self._lane = await asyncio.start_server(self._lane_client, "127.0.0.1", int(wl["lane_port"]))
        if self.addons:
            await self.addons.start()
        await self.start_logsync()
        await self.start_wavelog_cat()
        await self.start_js8()
        self._tasks.append(asyncio.create_task(self.presence_loop()))

    # --- Wavelog CAT (wavelog_cat.py): WaveLogGate's radio-to-Wavelog jobs; its failure stays its own

    def make_wavelog_cat(self, block):
        self.wavelog_cat, self.wavelog_cat_error = None, None
        try:
            import importlib
            self.wavelog_cat = importlib.import_module("wavelog_cat").WavelogCat(block, self)
        except Exception as e:
            self.wavelog_cat_error = f"not loaded: {type(e).__name__}: {str(e)[:200]}"
            log.error("wavelog_cat %s", self.wavelog_cat_error)

    async def start_wavelog_cat(self):
        if self.wavelog_cat is None:
            return
        try:
            await self.wavelog_cat.start()
        except Exception as e:
            self.wavelog_cat_error = f"did not start: {type(e).__name__}: {str(e)[:200]}"
            log.error("wavelog_cat %s", self.wavelog_cat_error)
            self.wavelog_cat = None

    def wavelog_cat_status(self):
        if self.wavelog_cat is not None:
            return self.wavelog_cat.status()
        if self.wavelog_cat_error:
            return {"loaded": False, "error": self.wavelog_cat_error}
        return None

    # --- JS8 (js8_bridge.py): each radio's engine, receive only; its failure stays its own

    def make_js8(self, block):
        self.js8, self.js8_error = None, None
        try:
            import importlib
            self.js8 = importlib.import_module("js8_bridge").Js8Bridge(block, self)
        except Exception as e:
            self.js8_error = f"not loaded: {type(e).__name__}: {str(e)[:200]}"
            log.error("js8 %s", self.js8_error)

    async def start_js8(self):
        if self.js8 is None:
            return
        try:
            await self.js8.start()
        except Exception as e:
            self.js8_error = f"did not start: {type(e).__name__}: {str(e)[:200]}"
            log.error("js8 %s", self.js8_error)
            self.js8 = None

    def js8_status(self):
        if self.js8 is not None:
            return self.js8.status()
        if self.js8_error:
            return {"loaded": False, "error": self.js8_error}
        return None

    # --- the logbook sync (logsync.py): a station module; its failure stays its own

    async def start_logsync(self):
        if self.logsync is None:
            return
        try:
            await self.logsync.start()
        except Exception as e:
            why = _logsync_module().scrub(f"{type(e).__name__}: {e}", cap=200)
            log.error("logsync did not start: %s", why)
            self.logsync_error = f"did not start: {why}"
            try:
                await self.logsync.stop()
            except Exception as e2:
                log.debug("logsync: stop after a failed start: %s", type(e2).__name__)
            self.logsync = None

    def make_logsync(self, block):
        """A LogSync from a logsync block, or None with logsync_error set: a
        broken block must not take the station down."""
        self.logsync, self.logsync_error = None, None
        try:
            self.logsync = _logsync_module().LogSync(block, notify=self.logsync_changed,
                                                     alert=lambda msg: self.notice(msg, "warn"),
                                                     roster=self.roster_changed, rig=self.rig_changed)
        except Exception as e:
            try:
                why = _logsync_module().scrub(f"{type(e).__name__}: {e}", cap=200)
            except Exception:            # it did not even import: the type alone, no text to scrub
                why = type(e).__name__
            self.logsync_error = f"not loaded: {why}"
            log.error("logsync %s", self.logsync_error)
        return self.logsync

    async def restart_logsync(self, block):
        """The settings changed: stop the sync, start it on the new block.
        The radios and the rest of the station carry on."""
        if self.logsync is not None:
            await self.logsync.stop()
        self.cfg["logsync"] = block
        self.make_logsync(block)
        await self.start_logsync()
        self.logsync_changed(self.logsync_status())
        # The block may carry a new callsign: the top bar follows without a reload.
        self._broadcast(json.dumps({"t": "identity", **self.identity()}))

    # --- the Logbook tab's settings (logsync_settings.py, loaded by name like logsync)

    async def _logsync_settings(self, ws, m):
        """{"t": "logsync_settings", "do": get|plan|apply|secret|test, ...} ->
        {"t": "logsync_settings", "do", ...} to that page. A credential's value
        is never logged, echoed or sent back to any page."""
        import importlib
        S = importlib.import_module("logsync_settings")
        do = m.get("do")
        reply = {"t": "logsync_settings", "do": do}
        try:
            block = self.cfg.get("logsync")
            if not isinstance(block, dict):
                raise S.SettingsError("the logbook sync isn't set up on this station (ranch-radio add logsync)")
            if do == "get":
                reply["view"] = await asyncio.to_thread(S.view, block)
            elif do == "secret":
                dest = str(m.get("dest"))
                reply["dest"], reply["state"] = dest, await asyncio.to_thread(S.write_secret, block, dest, m.get("value"))
                log.info("logsync settings: the %s credential was set from the page (%s chars)",
                         dest, reply["state"].get("chars"))
                reply["view"] = await asyncio.to_thread(S.view, block)
            elif do in ("plan", "apply"):
                if self.modules is None:
                    raise S.SettingsError("this station's config names no host file; run ranch-radio install")
                if self.settings_plans is None:
                    self.settings_plans = S.Plans()
                if do == "plan":
                    from ranch_radio import render
                    reply["plan"] = await asyncio.to_thread(self.settings_plans.make, str(self.modules.path),
                                                            m.get("changes"), render.validate)
                else:
                    h = await asyncio.to_thread(self.settings_plans.apply, str(m.get("id")))
                    new = await asyncio.to_thread(S.write_station_json, h)
                    log.info("logsync settings: host file and station.json updated from the page; restarting the sync")
                    await self.restart_logsync(new)
                    reply["applied"] = True
                    reply["view"] = await asyncio.to_thread(S.view, new)
            elif do == "test":
                import aiohttp
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=S.TEST_TIMEOUT_S)) as http:
                    reply["result"] = await S.test(str(m.get("dest")), block, http, _logsync_module().scrub)
            else:
                raise S.SettingsError(f"unknown settings action {do!r}")
        except S.SettingsError as e:
            reply["error"] = str(e)
        except Exception as e:                       # never the message text: it can't carry a value, but be sure
            log.error("logsync settings: %s failed: %s", do, type(e).__name__)
            reply["error"] = f"{do} failed ({type(e).__name__})"
        await ws.send_str(json.dumps(reply))

    CARD_ADDONS = ("roster", "map")     # the pages that show a call card

    def call_lookup(self, m):
        """{"t": "logsync", "do": "lookup", "id", "call", "addon", [grid, band,
        mode]} -> the call card's answer for the asking page (2026-11-19, UX
        plan PR 4b). Read-only: the ledger's latest QSOs with that exact call
        (Ledger.history: indexed, capped at 20, date/band/mode/confirmed only)
        and, when the page sends the decode's grid, band and mode, the need
        from the sync's in-memory index (never one built per request:
        Needs.from_ledger reads every QSO). Nothing is written, sent to a
        destination, or put on the air.

        The answer rides {"t": "addon", "addon": <the asking tab>} because
        that is the route both pages already deliver to the Roster and the
        Map; a new message type would need a branch in station.js."""
        rid, addon = m.get("id"), m.get("addon")
        if isinstance(rid, bool) or not isinstance(rid, (int, str)) or len(str(rid)) > 40:
            raise ValueError("a lookup needs a request id")
        if addon not in self.CARD_ADDONS:
            raise ValueError("a lookup names the Roster or the Map")
        out = {"id": rid}
        reply = {"t": "addon", "addon": addon, "callcard": out}
        import importlib
        R = importlib.import_module("logsync_roster")       # loaded by name, like logsync itself
        call = R.lookup_call(m.get("call"))
        if call is None:
            out["error"] = "not a callsign"
            return reply
        out["call"] = call
        ledger = getattr(self.logsync, "ledger", None)
        if ledger is None:
            out["error"] = "the logbook sync isn't running on this station"
            return reply
        try:
            out["qsos"] = ledger.history(call)
        except Exception as e:                        # a locked or broken ledger: say so, the station runs on
            log.error("call card: ledger lookup failed: %s", type(e).__name__)
            out["error"] = f"the logbook couldn't be read ({type(e).__name__})"
            return reply
        out["more"] = len(out["qsos"]) >= ledger.HISTORY_MAX
        grid, band, mode = (m.get(k) for k in ("grid", "band", "mode"))
        needs = getattr(self.logsync, "needs", None)
        out["need"] = None                            # None: unavailable (no decode to judge, or no index yet)
        if (needs is not None and isinstance(grid, str) and R.is_grid(grid.upper())
                and isinstance(band, str) and 0 < len(band) <= 8 and isinstance(mode, str) and 0 < len(mode) <= 12):
            level, reasons = needs.need(call, grid.upper(), band, mode)
            out["need"] = {"level": level, "reasons": list(reasons)}
        return reply

    def logsync_status(self):
        """For the pages (and the Logbook tab): an allowlist, see LogSync.status."""
        if self.logsync is not None:
            return self.logsync.status()
        if self.logsync_error:
            return {"loaded": False, "error": self.logsync_error}
        return None

    def roster_changed(self):
        """New decodes on the sync's roster: to pages, if the Roster tab is on."""
        for name in ("roster", "map"):
            if self.addons and self.addons.is_on(name):
                self.addons.get(name).push()

    def rigs_view(self):
        """What each radio's own server says it is doing: [{id, name, present,
        connected, tx, freq, mode, data}], the last five None until its watch
        has read a hello. Read-only: the radio server owns the transmitter."""
        out = []
        for r in self.radios:
            w = self.watches.get(r.id)
            rig = w.rig if w is not None and r.present else {}
            out.append({"id": r.id, "name": r.name, "present": r.present,
                        **{k: rig.get(k) for k in RadioWatch.RIG}})
        return out

    def rig_changed(self):
        """A radio keyed, unkeyed or moved, or JTDX's Status changed: to the Map
        tab, if on, and to Wavelog (wavelog_cat), if set up."""
        if self.wavelog_cat is not None:
            self.wavelog_cat.kick()
        if self.addons and self.addons.is_on("map"):
            try:
                self.addons.get("map").push_rig()
            except Exception as e:           # a broken add-on must not stop a radio's watch
                log.error("map: on-the-air push: %s", type(e).__name__)

    def logsync_changed(self, status):
        self._broadcast(json.dumps({"t": "addon", "addon": "logbook", "logsync": status}))

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        for w in self.watches.values():
            await w.stop()
        if self.addons:
            await self.addons.stop()
        if self.logsync is not None:
            await self.logsync.stop()
        if self.wavelog_cat is not None:
            await self.wavelog_cat.stop()
        if self.js8 is not None:
            await self.js8.stop()
        await self.ardop.stop()
        for srv in (self._rigctl, self._lane):
            if srv:
                srv.close()


# --- HTTP -------------------------------------------------------------------

PLAIN_PATH, canonical_tail = webguard.PLAIN_PATH, webguard.canonical_tail   # shared with the packet add-on


same_origin = webguard.same_origin


def cross_site_guard(allow_hosts=()):
    """The shared guard (webguard.cross_site_guard), logging as the station."""
    return webguard.cross_site_guard(allow_hosts, log)


def make_app(station, args):
    allow = webguard.allowed_names(args)
    app = web.Application(client_max_size=64 * 1024 * 1024, middlewares=[cross_site_guard(allow)])
    webguard.add_security_headers(app)
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
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in HOP | {"origin", "x-forwarded-for"}}
        headers["X-Ranch-Radio"] = "1"
        headers["X-Forwarded-For"] = webguard.forwarded_for(request)   # the backend's from_this_machine
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
        if tail in ("api/lend", "api/return") and not webguard.from_this_machine(request):
            # The hub lends the radio only to its own machine (the UV-PRO
            # add-on's control channel); through this proxy, check the real caller.
            return web.json_response({"error": "lending the radio is only allowed from this machine"},
                                     status=403)
        if tail == "api/hold" and not webguard.from_this_machine(request):
            # An off-channel hold is the UV-PRO add-on's, on this machine (the
            # hub sees the proxy as loopback). Releasing one is the operator's,
            # from any page, as transmit Off is.
            return web.json_response({"error": "holds are only set from this machine"}, status=403)
        # Transmit on/off: any page on the LAN, as on the hub's own page (D7).
        return await proxy(request, station.packet_url.rstrip("/"), tail)

    async def lone_audio(request):
        """/audio, where a radio's own server had it before the station stood
        in front of it: a remote listener still asks there (2026-11-10,
        every 5 s since the deploy). A station of one radio hands it that
        radio's /audio, the same backend /r/<id>/audio reaches. With more
        than one radio there is no telling which, so it says where each is."""
        if len(station.radios) != 1:
            where = ", ".join(f"/r/{r.id}/audio" for r in station.radios) or "no radio here"
            return web.json_response({"error": f"more than one radio: ask for one of {where}"}, status=404)
        r = station.radios[0]
        return await proxy(request, f"http://127.0.0.1:{r.web}", "audio")

    async def wavelog_qsy(request):
        if station.wavelog_cat is None:
            return web.json_response({"error": "Wavelog CAT is not set up here"}, status=404)
        return await station.wavelog_cat.handle_qsy(request)

    def slash(prefix):
        async def h(request):
            raise web.HTTPFound(f"{prefix}{request.match_info.get('rid', '')}/" +
                                (f"?{request.query_string}" if request.query_string else ""))
        return h

    r = app.router
    r.add_get("/", index)
    r.add_get("/ws", ws_control)
    r.add_get("/audio", lone_audio)
    r.add_get("/r/{rid}", slash("/r/"))
    r.add_route("*", "/r/{rid}/{tail:.*}", radio_page)
    r.add_get("/packet", slash("/packet"))
    r.add_route("*", "/packet/{tail:.*}", packet_page)
    if station.addons is not None:
        station.addons.add_routes(app, authorised)      # Winlink's mailbox under /winlink/api/
    # Wavelog's spot clicks (wavelog_cat.py checks their Origin: a GET passes the guard above)
    r.add_get("/wavelog/qsy/{rid}/{hz}/{mode}", wavelog_qsy)
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


def _logsync_module():
    """logsync.py, loaded by name the way addons/__init__.py loads an add-on:
    only a station whose config has a "logsync" block ever imports it, and the
    test selector (tests/affected.py) keeps it a seam, so a logsync change runs
    logsync_test (which spawns this station) rather than every station test."""
    import importlib
    return importlib.import_module("logsync")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--config", default="~/.config/radio-remote/station.json",
                   help="the station's radios, ports and services (see linux/station.example.json)")
    p.add_argument("--host", default=os.environ.get("RANCH_RADIO_DEFAULT_HOST") or "0.0.0.0")
    p.add_argument("--listen", type=int, default=7300)
    p.add_argument("--https", action="store_true")
    p.add_argument("--tls-name", action="append", default=[])
    p.add_argument("--allow-host", action="append", default=[],
                   help="a dotted host name browsers use for this station (IPs, single-label, .local, "
                        ".home.arpa and .ts.net names are always allowed)")
    p.add_argument("--token", default=os.environ.get("IC7300_TOKEN"))
    p.add_argument("--rigctl-host", default=os.environ.get("RANCH_RADIO_DEFAULT_HOST") or "0.0.0.0")
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
    if addon_cfg.pop("packet", None) is not None:
        # The station has its own Packet tab (/packet/, from the "packet" block);
        # the add-on is for a radio's own page and would double the route here.
        log.info("addons: \"packet\" is the station's own tab; its add-on entry is ignored here")
    wl = cfg.get("winlink") or {}
    from listen_plan import HfListenPlan, ListenPlan
    if wl.get("pat"):             # Winlink (Pat) is an add-on (addons/winlink.py); the lanes are not
        modems = [m for m in winlink.HF_MODEMS if any(m in r.modems for r in station.radios)]
        addon_cfg.setdefault("winlink", {"lane_for": station.lane_for, "hf_modems": modems, "engine_args": SimpleNamespace(
            winlink_pat=" ".join(shlex.quote(_expand(w)) for w in shlex.split(wl["pat"])), winlink_pat_port=int(wl.get("pat_port", 8095)),
            winlink_pat_config=_expand(wl["pat_config"]) if wl.get("pat_config") else None,
            winlink_session_max=float(wl.get("session_max", 900)),
            # The listen window (winlink.listen), parsed as render.py checked it.
            winlink_listen=ListenPlan.from_block(wl["listen"]) if "listen" in wl else None,
            winlink_directory=wl.get("directory"),       # absent: on, default URL; false: off
            # ardopcf's --logdir, where its sound-card errors show (Ardop.logdir).
            winlink_ardop_logdir=station.ardop.session_logdir,
            # Attended HF listening (winlink.listen_hf), parsed as render.py checked it.
            winlink_listen_hf=HfListenPlan.from_block(wl["listen_hf"]) if "listen_hf" in wl else None,
            addons_state=cfg.get("addons_state", "~/.local/state/radio-remote/addons.json"))})
    m = cfg.get("mesh") or {}
    if m.get("port"):             # Mesh is an add-on (addons/mesh.py); "mesh" configures it
        addon_cfg.setdefault("mesh", dict(m))
    if addon_cfg:
        from addons import Addons
        station.addons = Addons(station, addon_cfg, cfg.get("addons_state", "~/.local/state/radio-remote/addons.json"))
        wa = station.addons.get("winlink")
        if wa is not None:                   # between sessions the panel shows the radio's own lane state
            wa.engine.idle_lane = IdleLane(station)
    if cfg.get("logsync"):
        station.make_logsync(cfg["logsync"])        # a broken logsync must not take the station down
    if cfg.get("wavelog_cat"):
        station.make_wavelog_cat(cfg["wavelog_cat"])  # a broken block must not take the station down
    if cfg.get("js8"):
        station.make_js8(cfg["js8"])                  # a broken block must not take the station down
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
