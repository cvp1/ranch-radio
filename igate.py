"""Receive-only iGate for the packet hub: packets heard on RF go to APRS-IS.

    hub = Hub(args); hub.igate = IGate("N9ABC-10", passcode, "noam.aprs2.net")
    hub.igate.start(); ... hub.igate.offer(frame)   # every frame the radio hears

Direwolf has its own iGate (direwolf_tnc.py --igate-login); this one is for
the hub's other modems (the UV-PRO over Bluetooth, a serial or network KISS
TNC), so any of them can gate. Only one of the two runs on a host: the
renderer refuses a host file that turns on both.

Receive only, like Direwolf's without IGTXVIA: the login carries no filter,
and every line the server sends is read only for its login reply and to see
the link is alive. Nothing from APRS-IS reaches the radio; this module never
holds the hub, only frames handed to offer().

What is gated (the APRS-IS iGate rules, as Direwolf applies them):
- UI frames with PID F0 (no layer 3) only; connected-mode traffic never.
- Not with TCPIP, TCPXX, NOGATE or RFONLY in the path, and not queries
  (info starting "?").
- Third-party packets ("}"): the inner packet, with the same checks on its
  own path; the RF header is dropped.
- The info stops at the first CR or LF. The line gets ",qAR,<login>" after
  the path, which tells APRS-IS an iGate heard it on RF.
- The same packet (source, destination, info) heard again within DUP_S, as
  each digipeater's copy is, goes once.

While the server is unreachable, heard packets are dropped, not queued: an
old position resent minutes later would be wrong on every map.
"""

import asyncio
import logging
import os
import re
import time

from ax25 import PID_NO_L3, U_UI

log = logging.getLogger("igate")

DEFAULT_SERVER = "noam.aprs2.net"
DEFAULT_PORT = 14580
NO_GATE = {"TCPIP", "TCPXX", "NOGATE", "RFONLY"}
DUP_S = 30
SILENT_S = 120          # servers send a "#" keepalive every 20 s or so; this long without one is a dead link
RETRY_S = (5, 15, 30, 60, 120, 300)
CONNECT_S = 20
VERSION = "RanchRadio 0.1"


def read_passcode(path):
    """The APRS-IS passcode in a file. Raises ValueError with the reason."""
    try:
        with open(os.path.expanduser(path)) as f:
            code = f.read().strip()
    except OSError as e:
        raise ValueError(f"iGate passcode file {path}: {e.strerror}") from None
    if not re.fullmatch(r"-?\d{1,5}", code):
        raise ValueError(f"iGate passcode file {path} doesn't hold a passcode (digits)")
    return code


def split_server(text):
    """'host' or 'host:port' -> (host, port)."""
    host, _, port = text.rpartition(":") if ":" in text else (text, "", "")
    return (host, int(port)) if port else (text, DEFAULT_PORT)


def _path_ok(calls):
    return not any(c.rstrip("*").split("-")[0].upper() in NO_GATE for c in calls)


def gate_line(f, login):
    """The APRS-IS line for a heard frame, as bytes without CRLF, or None
    when the frame isn't one to gate. Also returns the duplicate key."""
    if f.kind != "U" or f.utype != U_UI or f.pid != PID_NO_L3:
        return None, None
    if not _path_ok(str(d) for d in f.digis):
        return None, None
    info = f.info
    head = f.tnc2().split(":", 1)[0]
    key_src, key_dest = str(f.src), str(f.dest)
    if info[:1] == b"}":                     # third party: gate what's inside
        inner = info[1:]
        head_b, sep, info = inner.partition(b":")
        if not sep or b">" not in head_b:
            return None, None
        head = head_b.decode("latin-1")
        src, _, path = head.partition(">")
        calls = path.split(",")
        if not src or not calls[0] or not _path_ok(calls):
            return None, None
        key_src, key_dest = src, calls[0]
    info = re.split(rb"[\r\n]", info, maxsplit=1)[0]
    if not info or info[:1] == b"?":
        return None, None
    line = f"{head},qAR,{login}:".encode("latin-1") + info
    return line, (key_src.upper(), key_dest.upper(), info)


class IGate:
    def __init__(self, login, passcode, server=DEFAULT_SERVER):
        self.login = login.upper()
        self.passcode = passcode
        self.server = server
        self.host, self.port = split_server(server)
        self.connected = False
        self.verified = False
        self.since = None
        self.error = None
        self.counts = {"gated": 0, "skipped": 0, "dupes": 0, "dropped": 0, "connects": 0}
        self._writer = None
        self._seen = {}                      # dup key -> time sent
        self._task = None

    def start(self):
        self._task = asyncio.create_task(self._run())
        return self

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._close()

    def status(self):
        return {"enabled": True, "login": self.login, "server": self.server,
                "connected": self.connected, "verified": self.verified,
                "since": self.since, "error": self.error, **self.counts}

    def offer(self, f):
        """A frame the radio heard. Never raises: the hub's receive path
        must not stop for the iGate."""
        try:
            self._offer(f)
        except Exception:     # noqa: BLE001
            log.exception("iGate: frame not gated")

    def _offer(self, f):
        line, key = gate_line(f, self.login)
        if line is None:
            self.counts["skipped"] += 1
            return
        t = time.monotonic()
        if len(self._seen) > 1000:
            self._seen = {k: v for k, v in self._seen.items() if t - v < DUP_S}
        if t - self._seen.get(key, -DUP_S) < DUP_S:
            self.counts["dupes"] += 1
            return
        if not (self.connected and self.verified and self._writer):
            self.counts["dropped"] += 1
            return
        self._seen[key] = t
        self._writer.write(line + b"\r\n")
        self.counts["gated"] += 1
        log.debug("iGate: %s", line.decode("latin-1"))

    async def _run(self):
        fails = 0
        while True:
            try:
                await self._session()
                fails = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:     # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                log.warning("iGate: %s: %s", self.server, self.error)
            self._close()
            wait = RETRY_S[min(fails, len(RETRY_S) - 1)]
            fails += 1
            await asyncio.sleep(wait)

    async def _session(self):
        reader, self._writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), CONNECT_S)
        self.connected, self.verified, self.since = True, False, time.time()
        self.counts["connects"] += 1
        self._writer.write(f"user {self.login} pass {self.passcode} vers {VERSION}\r\n".encode())
        await self._writer.drain()
        while True:
            line = await asyncio.wait_for(reader.readline(), SILENT_S)
            if not line:
                raise ConnectionError("server closed the connection")
            if line.startswith(b"# logresp"):
                text = line.decode("latin-1").strip()
                if " verified" not in text:
                    # An unverified login can't send packets: say why, retry slowly.
                    raise PermissionError(f"login not verified ({text[2:]}): check the passcode")
                self.verified, self.error = True, None
                log.info("iGate: logged in to %s as %s", self.server, self.login)
            # Anything else (keepalives, and packets if a server sends any) is ignored.

    def _close(self):
        w, self._writer = self._writer, None
        self.connected = self.verified = False
        if w:
            w.close()
