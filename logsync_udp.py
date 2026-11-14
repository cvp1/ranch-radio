"""The WSJT-X / JTDX UDP protocol, as the logbook sync (logsync.py) hears it,
and the two intake shapes PLAN v3 §4 allows.

Message layout: JTDX NetworkMessage.hpp (github.com/jtdx-project/jtdx,
read 2026-11-13). Big-endian QDataStream: magic 0xadbccbda, schema, type,
id (utf8 = quint32 length + bytes, 0xffffffff = null), then the type's
fields. JTDX starts at schema 2 (Qt_5_2) and moves up only if a server
answers with a higher one (MessageClient.cpp); QDateTime is the same in
both: qint64 Julian day, quint32 ms since midnight, quint8 timespec
(2 adds a qint32 offset).

    0 Heartbeat  out/in     5 QSO Logged  out     9 Free Text  in
    1 Status     out        6 Close       out    10 WSPR Decode out
    2 Decode     out        7 Replay      in     12 Logged ADIF out
    3 Clear      out        8 Halt Tx     in

JTDX sends from an ephemeral socket to the configured server address and
reads replies on that same socket (MessageClient.cpp: "bind to an
ephemeral port"); it acts on a Reply only when the id matches its own.
It has no multicast code of its own: a multicast server address relies on
the OS.

Intake shapes, chosen by the host file's logsync.udp block:
  Multicast  {"group", "port"}: join the group beside GridTracker
             (SO_REUSEADDR/SO_REUSEPORT). Both hear JTDX's own source
             address, so GridTracker's Reply goes straight to JTDX. Nothing
             here ever sends to JTDX.
  Relay      {"bind", "port", "relay"}: own the unicast port; forward each
             JTDX client's datagrams to GridTracker ("relay") from a socket
             of that client's own, and pass back to JTDX only Reply, Halt Tx
             and Free Text (4, 8, 9) that come from GridTracker's address
             with JTDX's id. Everything else inbound is dropped. A client
             silent for 5 minutes is forgotten.
Either way, "forward" targets get JTDX's traffic one way (a decode feed);
nothing from a forward target is ever forwarded or relayed.
"""

import asyncio
import datetime as dt
import logging
import socket
import struct

log = logging.getLogger("logsync")

MAGIC = 0xADBCCBDA
OUT_TYPES = {0, 1, 2, 3, 5, 6, 10, 12}       # JTDX -> servers
REPLY_TYPES = {4, 8, 9}                      # what the relay passes back to JTDX
RELAY_EXPIRE_S = 300.0
_JD_1970 = 2440588


class Reader:
    def __init__(self, data):
        self.b, self.p = data, 0

    def take(self, n):
        if self.p + n > len(self.b):
            raise ValueError("datagram ends early")
        v = self.b[self.p:self.p + n]
        self.p += n
        return v

    def u8(self):
        return self.take(1)[0]

    def u32(self):
        return struct.unpack(">I", self.take(4))[0]

    def i32(self):
        return struct.unpack(">i", self.take(4))[0]

    def u64(self):
        return struct.unpack(">Q", self.take(8))[0]

    def i64(self):
        return struct.unpack(">q", self.take(8))[0]

    def utf8(self):
        n = self.u32()
        if n == 0xFFFFFFFF:
            return ""
        return self.take(n).decode("utf-8", "replace")

    def datetime(self):
        """A QDateTime, as an aware UTC datetime (None if null)."""
        jd, ms, spec = self.i64(), self.u32(), self.u8()
        offset = 0
        if spec == 2:
            offset = self.i32()
        elif spec == 3:
            self.utf8()                   # a time zone id; JTDX sends UTC
        if ms == 0xFFFFFFFF or jd < 0 or jd > 5373484:
            return None
        day = dt.date(1970, 1, 1) + dt.timedelta(days=jd - _JD_1970)
        t = dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC) + dt.timedelta(milliseconds=ms)
        if spec == 2:
            t -= dt.timedelta(seconds=offset)
        elif spec == 0:                   # local time on JTDX's machine: this machine's
            t = t.replace(tzinfo=None).astimezone(dt.UTC)
        return t


def header(data):
    """(schema, type, id), or raise ValueError."""
    r = Reader(data)
    if r.u32() != MAGIC:
        raise ValueError("not a WSJT-X/JTDX datagram")
    schema, typ = r.u32(), r.u32()
    return schema, typ, r.utf8()


def parse_logged(data):
    """QSO Logged (type 5) -> the ADIF fields it carries."""
    r = Reader(data)
    if r.u32() != MAGIC:
        raise ValueError("not a WSJT-X/JTDX datagram")
    r.u32()
    if r.u32() != 5:
        raise ValueError("not a QSO Logged message")
    r.utf8()
    t_off = r.datetime()
    call, grid, freq, mode = r.utf8(), r.utf8(), r.u64(), r.utf8()
    sent, rcvd, pwr, comment, name = r.utf8(), r.utf8(), r.utf8(), r.utf8(), r.utf8()
    t_on = r.datetime()
    op, mycall, mygrid = r.utf8(), r.utf8(), r.utf8()
    if t_on is None:
        raise ValueError("QSO Logged without a time on")
    from logsync_adif import band_of, canon_mode
    m, sub = canon_mode(mode)
    mhz = f"{freq / 1e6:.6f}"
    f = {"CALL": call.strip().upper(), "GRIDSQUARE": grid.strip(), "MODE": m, "SUBMODE": sub,
         "RST_SENT": sent, "RST_RCVD": rcvd, "QSO_DATE": t_on.strftime("%Y%m%d"),
         "TIME_ON": t_on.strftime("%H%M%S"), "FREQ": mhz, "BAND": band_of(mhz),
         "STATION_CALLSIGN": mycall.strip().upper(), "MY_GRIDSQUARE": mygrid.strip(),
         "OPERATOR": op.strip().upper(), "TX_PWR": pwr, "NAME": name, "COMMENT": comment}
    if t_off is not None:
        f["QSO_DATE_OFF"], f["TIME_OFF"] = t_off.strftime("%Y%m%d"), t_off.strftime("%H%M%S")
    return {k: v for k, v in f.items() if v}


def parse_adif_message(data):
    """Logged ADIF (type 12) -> the ADIF fields of its one record."""
    r = Reader(data)
    if r.u32() != MAGIC:
        raise ValueError("not a WSJT-X/JTDX datagram")
    r.u32()
    if r.u32() != 12:
        raise ValueError("not a Logged ADIF message")
    r.utf8()
    from logsync_adif import parse
    _, recs = parse(r.utf8(), strict=True)
    if len(recs) != 1:
        raise ValueError(f"Logged ADIF with {len(recs)} records")
    return recs[0]


def _addr(text):
    host, _, port = str(text).rpartition(":")
    return socket.gethostbyname(host or "127.0.0.1"), int(port)


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, on):
        self.on = on

    def datagram_received(self, data, addr):
        try:
            self.on(data, addr)
        except Exception as e:           # one bad datagram never stops the intake
            log.warning("logsync: datagram from %s dropped: %s", addr[0], type(e).__name__)

    def error_received(self, exc):
        log.debug("logsync: udp error %s", type(exc).__name__)


class Forwarder:
    """One-way copies of JTDX's traffic to "forward" targets (a decode feed)."""

    def __init__(self, targets):
        self.targets = [_addr(t) for t in targets or ()]
        self.sock = None

    def open(self):
        if self.targets:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setblocking(False)

    def is_target(self, addr):
        return (addr[0], addr[1]) in self.targets

    def send(self, data, src):
        if self.sock is None or self.is_target(src):
            return
        for t in self.targets:
            try:
                self.sock.sendto(data, t)
            except OSError as e:
                log.debug("logsync: forward to %s: %s", t[0], type(e).__name__)

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None


class MulticastIntake:
    """Join the group beside GridTracker; hear JTDX; never send to it."""

    shape = "multicast"

    def __init__(self, group, port, on_message, forwarder, iface="0.0.0.0"):
        self.group, self.port, self.iface = group, int(port), iface
        self.on_message, self.fwd = on_message, forwarder
        self.transport = None

    def socket(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            s.bind(("", self.port))
            mreq = struct.pack("4s4s", socket.inet_aton(self.group), socket.inet_aton(self.iface))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            s.setblocking(False)
        except OSError:
            s.close()
            raise
        return s

    async def start(self):
        loop = asyncio.get_running_loop()
        self.fwd.open()
        self.transport, _ = await loop.create_datagram_endpoint(lambda: _Proto(self._got), sock=self.socket())

    def _got(self, data, addr):
        if self.fwd.is_target(addr):
            return                        # a forward target talking back: never a loop
        try:
            _, typ, _ = header(data)
        except ValueError:
            return
        if typ not in OUT_TYPES:
            return                        # GridTracker's Reply to JTDX, if the OS loops it: not ours
        self.fwd.send(data, addr)
        self.on_message(typ, data, addr)

    async def stop(self):
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        self.fwd.close()


class RelayIntake:
    """Own the unicast port; relay JTDX <-> GridTracker with the reply path
    narrowed to Reply/Halt Tx/Free Text from GridTracker only."""

    shape = "unicast"

    def __init__(self, bind, port, on_message, forwarder, relay=None, expire_s=RELAY_EXPIRE_S):
        self.bind, self.port = bind, int(port)
        self.relay = _addr(relay) if relay else None
        self.on_message, self.fwd, self.expire_s = on_message, forwarder, expire_s
        self.transport = None
        self.clients = {}                 # (id, addr) -> {"transport", "addr", "id", "seen"}
        self._sweeper = None

    async def start(self):
        loop = asyncio.get_running_loop()
        self.fwd.open()
        self.transport, _ = await loop.create_datagram_endpoint(lambda: _Proto(self._got),
                                                                local_addr=(self.bind, self.port))
        self._sweeper = asyncio.ensure_future(self._sweep())

    def _got(self, data, addr):
        if self.fwd.is_target(addr) or (self.relay and addr[:2] == self.relay):
            return                        # replies come to the client's own socket, never here
        try:
            _, typ, cid = header(data)
        except ValueError:
            return
        if typ not in OUT_TYPES:
            log.info("logsync: type %d from %s on the intake port dropped", typ, addr[0])
            return
        self.fwd.send(data, addr)
        if self.relay:
            self._upstream(cid, addr, data)
        self.on_message(typ, data, addr)

    def _upstream(self, cid, addr, data):
        """Send to GridTracker from this client's own socket, made here and now
        (synchronously: a datagram must never wait on the socket being made);
        its replies are read once the endpoint is up."""
        k = (cid, addr[:2])
        c = self.clients.get(k)
        loop = asyncio.get_running_loop()
        if c is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setblocking(False)
            s.bind(("127.0.0.1" if self.relay[0].startswith("127.") else "0.0.0.0", 0))
            c = {"addr": addr[:2], "id": cid, "seen": loop.time(), "sock": s, "transport": None}
            self.clients[k] = c

            async def listen(c=c):
                c["transport"], _ = await loop.create_datagram_endpoint(
                    lambda: _Proto(lambda d, a: self._reply(c, d, a)), sock=c["sock"])
            c["task"] = asyncio.ensure_future(listen())
        c["seen"] = loop.time()
        try:
            c["sock"].sendto(data, self.relay)
        except OSError as e:
            log.debug("logsync: relay to GridTracker: %s", type(e).__name__)

    def _reply(self, c, data, addr):
        if addr[:2] != self.relay:
            log.info("logsync: datagram for JTDX from %s (not the relay target) dropped", addr[0])
            return
        try:
            _, typ, cid = header(data)
        except ValueError:
            return
        if typ not in REPLY_TYPES or cid != c["id"]:
            return
        if self.transport is not None:
            self.transport.sendto(data, c["addr"])

    async def _sweep(self):
        while True:
            await asyncio.sleep(min(30.0, self.expire_s / 2))
            self.expire()

    def expire(self, now=None):
        now = asyncio.get_running_loop().time() if now is None else now
        for k, c in list(self.clients.items()):
            if now - c["seen"] > self.expire_s:
                self._close(c)
                del self.clients[k]

    @staticmethod
    def _close(c):
        if c["transport"] is not None:
            c["transport"].close()
        else:
            c["task"].cancel()
            c["sock"].close()

    async def stop(self):
        if self._sweeper is not None:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
        for c in self.clients.values():
            self._close(c)
        self.clients.clear()
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        self.fwd.close()
