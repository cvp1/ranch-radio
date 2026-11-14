"""A fake JTDX for the logbook sync tests: builds its UDP messages the way
JTDX does and sends them from one ephemeral socket, reading whatever comes
back on that socket (as MessageClient.cpp does).

The encoders follow JTDX's NetworkMessage.hpp field lists (read 2026-11-13,
github.com/jtdx-project/jtdx) and are written separately from the parser
under test (logsync_udp.py), so a shared mistake can't hide:

    header   quint32 magic 0xadbccbda, quint32 schema, quint32 type, utf8 id
    utf8     quint32 length + bytes (0xffffffff = null)
    QDateTime (schema 2/3)  qint64 Julian day, quint32 ms since midnight, quint8 timespec (1 = UTC)

    0 Heartbeat   id, quint32 max schema, utf8 version, utf8 revision
    1 Status      id, quint64 dial, mode, dx call, report, tx mode, bool tx enabled, bool transmitting,
                  bool decoding, qint32 rx df, qint32 tx df, de call, de grid, dx grid, bool watchdog,
                  sub-mode, bool fast, bool tx first
    2 Decode      id, bool new, QTime, qint32 snr, double dt, quint32 df, mode, message, bool low conf, bool off air
    3 Clear       id
    4 Reply       id, QTime, qint32 snr, double dt, quint32 df, mode, message, bool low conf, quint8 modifiers
    5 QSO Logged  id, QDateTime off, dx call, dx grid, quint64 dial, mode, rpt sent, rpt rcvd, tx power,
                  comments, name, QDateTime on, operator call, my call, my grid
    6 Close       id
    8 Halt Tx     id, bool auto only
    9 Free Text   id, text, bool send
   10 WSPR Decode id, bool new, QTime, qint32 snr, double dt, quint64 freq, qint32 drift, call, grid, qint32 dBm
   12 Logged ADIF id, utf8 ADIF text ("\\n<adif_ver:5>3.1.0\\n<programid:4>JTDX\\n<EOH>\\n" + record:
                  logqso.cpp, FT4 as <MODE:4>MFSK <SUBMODE:3>FT4)
"""
import datetime as dt
import socket
import struct

MAGIC = 0xADBCCBDA


def _s(text):
    if text is None:
        return struct.pack(">I", 0xFFFFFFFF)
    b = text.encode()
    return struct.pack(">I", len(b)) + b


def _b(v):
    return struct.pack(">?", bool(v))


def _qdt(t):
    """QDateTime, UTC (timespec 1)."""
    jd = (t.date() - dt.date(1970, 1, 1)).days + 2440588
    ms = ((t.hour * 60 + t.minute) * 60 + t.second) * 1000 + t.microsecond // 1000
    return struct.pack(">qIB", jd, ms, 1)


def _qtime(ms):
    return struct.pack(">I", ms)


def head(typ, cid="JTDX", schema=2):
    return struct.pack(">III", MAGIC, schema, typ) + _s(cid)


def heartbeat(cid="JTDX"):
    return head(0, cid) + struct.pack(">I", 3) + _s("2.2.159") + _s("")


def status(cid="JTDX", dial=14074000, mode="FT8", de="N0CALL"):
    return (head(1, cid) + struct.pack(">Q", dial) + _s(mode) + _s("N9ABC") + _s("-10") + _s(mode)
            + _b(False) + _b(False) + _b(True) + struct.pack(">ii", 1500, 1500) + _s(de) + _s("FN31") + _s("")
            + _b(False) + _s("") + _b(False) + _b(True))


def decode(cid="JTDX", msg="CQ N9ABC EN50", snr=-10, df=1500, mode="~"):
    return (head(2, cid) + _b(True) + _qtime(12 * 3600 * 1000) + struct.pack(">i", snr) + struct.pack(">d", 0.1)
            + struct.pack(">I", df) + _s(mode) + _s(msg) + _b(False) + _b(False))


def clear(cid="JTDX"):
    return head(3, cid)


def reply(cid="JTDX", msg="CQ N9ABC EN50", snr=-10, df=1500, mode="~"):
    return (head(4, cid) + _qtime(12 * 3600 * 1000) + struct.pack(">i", snr) + struct.pack(">d", 0.1)
            + struct.pack(">I", df) + _s(mode) + _s(msg) + _b(False) + struct.pack(">B", 0))


def logged(t_on, call="N9ABC", grid="EN50", dial=14074000, mode="FT8", sent="-10", rcvd="-12",
           my="N0CALL", mygrid="FN31", cid="JTDX", t_off=None, pwr="50", comment="", name="", op=""):
    t_off = t_off or (t_on + dt.timedelta(seconds=75))
    return (head(5, cid) + _qdt(t_off) + _s(call) + _s(grid) + struct.pack(">Q", dial) + _s(mode)
            + _s(sent) + _s(rcvd) + _s(pwr) + _s(comment) + _s(name) + _qdt(t_on) + _s(op) + _s(my) + _s(mygrid))


def close(cid="JTDX"):
    return head(6, cid)


def halt(cid="JTDX", auto_only=True):
    return head(8, cid) + _b(auto_only)


def free_text(cid="JTDX", text="TNX 73", send=False):
    return head(9, cid) + _s(text) + _b(send)


def wspr(cid="JTDX"):
    return (head(10, cid) + _b(True) + _qtime(0) + struct.pack(">i", -20) + struct.pack(">d", 0.5)
            + struct.pack(">Q", 14097050) + struct.pack(">i", 0) + _s("N9ABC") + _s("EN50") + struct.pack(">i", 37))


def adif_record(t_on, call="N9ABC", grid="EN50", dial=14074000, mode="FT8", sent="-10", rcvd="-12",
                my="N0CALL", mygrid="FN31", band="20m", t_off=None, pwr="50"):
    """The record logqso.cpp builds (field order and spacing as there)."""
    t_off = t_off or (t_on + dt.timedelta(seconds=75))
    freq = f"{dial / 1e6:.6f}"

    def f(name, v):
        return f" <{name}:{len(v)}>{v}"
    r = f"<BAND:{len(band)}>{band}" + f("STATION_CALLSIGN", my) + f("MY_GRIDSQUARE", mygrid) + f("CALL", call)
    r += f("FREQ", freq)
    r += " <MODE:4>MFSK" + f("SUBMODE", mode) if mode == "FT4" else f("MODE", mode)
    r += f("QSO_DATE", t_on.strftime("%Y%m%d")) + f("TIME_ON", t_on.strftime("%H%M%S"))
    r += f("QSO_DATE_OFF", t_off.strftime("%Y%m%d")) + f("TIME_OFF", t_off.strftime("%H%M%S"))
    r += f("RST_SENT", sent) + f("RST_RCVD", rcvd) + f("TX_PWR", pwr)
    if len(grid) > 3:
        r += f("GRIDSQUARE", grid)
    return r + " <EOR>"


def logged_adif(t_on, cid="JTDX", **kw):
    text = "\n<adif_ver:5>3.1.0\n<programid:4>JTDX\n<EOH>\n" + adif_record(t_on, **kw)
    return head(12, cid) + _s(text)


def type_of(data):
    magic, _, typ = struct.unpack(">III", data[:12])
    return typ if magic == MAGIC else None


class FakeJtdx:
    """One ephemeral socket: sends to the server address, keeps what comes back."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.addr = self.sock.getsockname()
        self.got = []

    def multicast(self, iface="127.0.0.1"):
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
        return self

    def send(self, data, dest):
        self.sock.sendto(data, dest)

    def poll(self):
        while True:
            try:
                d, a = self.sock.recvfrom(65536)
            except (TimeoutError, BlockingIOError, OSError):
                return self.got
            self.got.append((d, a))

    def close(self):
        self.sock.close()
