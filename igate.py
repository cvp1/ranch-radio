"""Receive-only iGate for the packet hub: packets heard on RF go to APRS-IS.

    hub = Hub(args); hub.igate = IGate("N9ABC-10", passcode, "noam.aprs2.net")
    hub.igate.start(); ... hub.igate.offer(frame)   # every frame the radio hears

Direwolf has its own iGate (direwolf_tnc.py --igate-login); this one is for
the hub's other modems (the UV-PRO over Bluetooth, a serial or network KISS
TNC), so any of them can gate. Only one of the two runs on a host: the
renderer refuses a host file that turns on both.

Receive only, like Direwolf's without IGTXVIA. Nothing from APRS-IS reaches
the radio; this module never holds the hub, only frames handed to offer().
Without observe= the login carries no filter and every server line is read
only for its login reply and to see the link is alive. With observe= (a
filter, "b/N9ABC* g/N9ABC*": traffic from our calls and messages to them,
APRS messaging plan 2026-11-07, 3.6) it is in the login line, and every
server line that is a packet goes to on_line(t, text) (the hub stores it as
port 'is' and shows it; it never reaches Hub.transmit()). Lines are split on
CR LF only and capped at LINE_MAX bytes: a longer one is dropped and counted.

What is gated (the APRS-IS iGate rules, as Direwolf applies them):
- UI frames with PID F0 (no layer 3) only; connected-mode traffic never.
- Not with TCPIP, TCPXX, NOGATE or RFONLY in the path, and not queries
  (info starting "?").
- Third-party packets ("}"): the inner packet, with the same checks on its
  own path; the RF header is dropped.
- Not packets whose source, outer or inner, is one of our own calls
  (own_bases): our frames heard back. Another station's iGate must carry
  them, which is what the round-trip probe needs to see.
- The info stops at the first CR or LF. The line gets ",qAO,<login>" after
  the path: heard on RF by an iGate that never gates to RF (aprs-is.net
  q.aspx: "Receive-only IGates will use this exclusively").
- The same packet (source, destination, info) heard again within DUP_S, as
  each digipeater's copy is, goes once.

While the server is unreachable, heard packets are dropped, not queued: an
old position resent minutes later would be wrong on every map.

Beacon (optional, beacon=): the iGate's own position, to APRS-IS only, never
RF: on login and every beacon_s after (30 min is the usual; 10 the floor).
Symbol R& (an "R" overlay on the gateway symbol: a receive-only iGate), so
maps show what the station is and the coverage tools can count it.
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
LINE_MAX = 512          # an APRS-IS line longer than this is dropped (and counted)
GAPS_MAX = 50           # observer gaps kept for the probe's coverage
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


def _base(call):
    return call.rstrip("*").split("-")[0].upper()


def gate_line(f, login, own_bases=()):
    """The APRS-IS line for a heard frame, as bytes without CRLF, or None
    when the frame isn't one to gate. Also returns the duplicate key."""
    if f.kind != "U" or f.utype != U_UI or f.pid != PID_NO_L3:
        return None, None
    if f.src.call in own_bases:
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
        if not src or not calls[0] or not _path_ok(calls) or _base(src) in own_bases:
            return None, None
        key_src, key_dest = src, calls[0]
    info = re.split(rb"[\r\n]", info, maxsplit=1)[0]
    if not info or info[:1] == b"?":
        return None, None
    line = f"{head},qAO,{login}:".encode("latin-1") + info
    return line, (key_src.upper(), key_dest.upper(), info)


BEACON_TOCALL = "APZRR0"            # APZ: experimental software, as aprs.py's examples
BEACON_COMMENT = "Ranch Radio RX-only iGate"


def _dm(deg, width, pos, neg):
    hemi = pos if deg >= 0 else neg
    deg = abs(deg)
    d = int(deg)
    m = round((deg - d) * 60, 2)
    if m >= 60:
        d, m = d + 1, 0.0
    return f"{d:0{width}d}{m:05.2f}{hemi}"


def beacon_info(lat, lon, comment=BEACON_COMMENT):
    """The beacon's APRS info: an uncompressed position, no timestamp, no
    messaging ("!"), symbol R&."""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError(f"position {lat}, {lon} is not on Earth")
    comment = re.sub(r"[\r\n|~]", " ", comment)[:43]
    return f"!{_dm(lat, 2, 'N', 'S')}R{_dm(lon, 3, 'E', 'W')}&{comment}".encode("latin-1")


class LineSplitter:
    """Bytes from the server -> whole lines, split on CR LF only. A line over
    LINE_MAX bytes is dropped whole (counted as too_long), however it arrives."""

    def __init__(self, counts):
        self.buf = bytearray()
        self.skipping = False
        self.counts = counts

    def feed(self, data):
        out = []
        self.buf += data
        while True:
            i = self.buf.find(b"\r\n")
            if i < 0:
                if len(self.buf) > LINE_MAX + 1:      # keep a lone CR that may start the next CR LF
                    if not self.skipping:
                        self.counts["too_long"] += 1
                    self.skipping = True
                    del self.buf[:-1]
                return out
            line = bytes(self.buf[:i])
            del self.buf[:i + 2]
            if self.skipping or len(line) > LINE_MAX:
                if not self.skipping:
                    self.counts["too_long"] += 1
                self.skipping = False
                continue
            out.append(line)


class IGate:
    def __init__(self, login, passcode, server=DEFAULT_SERVER, beacon=None, beacon_s=1800, own_bases=(),
                 observe=None, on_line=None):
        self.login = login.upper()
        self.own_bases = {c.upper() for c in own_bases}
        self.observe = observe                # the filter, or None: no observer
        self.on_line = on_line or (lambda t, text: None)
        self.obs = {"lines": 0, "packets": 0, "too_long": 0, "bad": 0, "last_line_t": None}
        self.gaps = [[time.time(), None]]     # observer not connected and verified: [start, end]
        self.passcode = passcode
        self.server = server
        self.host, self.port = split_server(server)
        self.connected = False
        self.verified = False
        self.since = None
        self.error = None
        self.counts = {"gated": 0, "skipped": 0, "dupes": 0, "dropped": 0, "connects": 0, "beacons": 0}
        self.beacon, self.beacon_s = beacon, beacon_s
        self.last_beacon = None
        self._beacon_task = None
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
        out = {"enabled": True, "login": self.login, "server": self.server,
               "connected": self.connected, "verified": self.verified,
               "since": self.since, "error": self.error, "q": "qAO",
               "beacon_min": self.beacon_s // 60 if self.beacon else 0,
               "last_beacon": self.last_beacon, **self.counts}
        if self.observe:
            out["observe"] = {"connected": self.connected, "verified": self.verified, "filter": self.observe,
                              "since": self.since, **self.obs, "gaps": [list(g) for g in self.gaps]}
        return out

    def _observer_up(self, up):
        """Keep the observer's coverage gaps (the probe reads them)."""
        if up and self.gaps and self.gaps[-1][1] is None:
            self.gaps[-1][1] = time.time()
        elif not up and (not self.gaps or self.gaps[-1][1] is not None):
            self.gaps.append([time.time(), None])
            del self.gaps[:-GAPS_MAX]

    def beacon_line(self):
        return f"{self.login}>{BEACON_TOCALL},TCPIP*:".encode("latin-1") + self.beacon

    async def _beacon_loop(self):
        if self.last_beacon is not None:      # a reconnect doesn't reset the interval
            await asyncio.sleep(max(0.0, self.last_beacon + self.beacon_s - time.time()))
        while self.connected and self.verified and self._writer:
            self._writer.write(self.beacon_line() + b"\r\n")
            self.counts["beacons"] += 1
            self.last_beacon = time.time()
            log.info("iGate: beacon %s", self.beacon_line().decode("latin-1"))
            await asyncio.sleep(self.beacon_s)

    def offer(self, f):
        """A frame the radio heard. Never raises: the hub's receive path
        must not stop for the iGate."""
        try:
            self._offer(f)
        except Exception:     # noqa: BLE001
            log.exception("iGate: frame not gated")

    def _offer(self, f):
        line, key = gate_line(f, self.login, self.own_bases)
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
        self._writer.write(self.login_line() + b"\r\n")
        await self._writer.drain()
        lines = LineSplitter(self.obs)
        while True:
            data = await asyncio.wait_for(reader.read(4096), SILENT_S)
            if not data:
                raise ConnectionError("server closed the connection")
            for line in lines.feed(data):
                self._line(line)

    def login_line(self):
        filt = f" filter {self.observe}" if self.observe else ""
        return f"user {self.login} pass {self.passcode} vers {VERSION}{filt}".encode()

    def _line(self, line):
        if line.startswith(b"#"):
            if line.startswith(b"# logresp"):
                self._logresp(line)
            return
        if not self.observe:
            return                     # receive only, no observer: packet lines are ignored
        self.obs["lines"] += 1
        self.obs["last_line_t"] = time.time()
        try:
            self.on_line(time.time(), line.decode("latin-1"))
            self.obs["packets"] += 1
        except Exception:      # noqa: BLE001 - one bad line never ends the session
            self.obs["bad"] += 1
            log.exception("iGate observer: line not handled")

    def _logresp(self, line):
        """The login reply. Raising ends the session (retried slowly)."""
        text = line.decode("latin-1").strip()
        if " verified" not in text:
            # An unverified login can't send packets: say why, retry slowly.
            raise PermissionError(f"login not verified ({text[2:]}): check the passcode")
        self.verified, self.error = True, None
        self._observer_up(True)
        log.info("iGate: logged in to %s as %s%s", self.server, self.login,
                 f" (observing: {self.observe})" if self.observe else "")
        if self.beacon and not self._beacon_task:
            self._beacon_task = asyncio.create_task(self._beacon_loop())

    def _close(self):
        if self._beacon_task:
            self._beacon_task.cancel()
            self._beacon_task = None
        w, self._writer = self._writer, None
        self.connected = self.verified = False
        self._observer_up(False)
        if w:
            w.close()
