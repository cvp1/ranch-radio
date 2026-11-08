"""The Meshtastic node's traffic on an MQTT broker, for a bridge on another host.

mesh_bridge.py holds the node's one link (USB or Bluetooth). With --feed it
also publishes what the node hears to an MQTT broker, so a program elsewhere
(a Home Assistant bridge, a chat bridge, a watchdog) can use this node
without a node of its own. Standard library only: the bridge's venv needs
nothing beyond `meshtastic`.

Topics, under PREFIX from the --feed URL (mqtt://HOST[:PORT]/PREFIX):
    PREFIX/status   "online" / "offline", retained; "offline" is also the
                    broker-side last will, so a crash or a pulled cable shows
    PREFIX/info     {"num", "id", "long", "short"} of this node, retained
    PREFIX/nodes    the node database, {"!xxxxxxxx": {...}}, retained
    PREFIX/packet   every packet heard, as the meshtastic library decodes it,
                    made JSON-safe (bytes -> base64, protobuf objects dropped)
With --feed-tx a program may also send text through the node:
    PREFIX/send     in:  {"text": str, "ch": int, "to": "!xxxxxxxx"|null}
    PREFIX/sent     out: {"ok": bool, "id": int|null, "error": str|null}
Sends are checked here (length, channel, node id) and spaced at least
SEND_GAP_S apart; anything else is refused on PREFIX/sent.

--feed-cred FILE: two lines, user then password. Neither is logged.
"""

import base64
import json
import logging
import re
import select
import socket
import struct
import threading
import time
from collections import deque
from urllib.parse import urlparse

log = logging.getLogger("mesh_feed")

KEEPALIVE_S = 60
RETRY_S = (2.0, 5.0, 15.0, 60.0)   # reconnect waits, growing while the broker stays away
QUEUE_MAX = 500                    # packets kept while the broker is away; oldest dropped
NODES_EVERY_S = 30.0               # the node database is republished at most this often
SEND_GAP_S = 3.0
TEXT_MAX_BYTES = 200
NODE_ID = re.compile(r"^![0-9a-f]{8}$")


# --- MQTT 3.1.1, the part a QoS 0 client needs

def _varlen(n):
    out = bytearray()
    while True:
        b, n = n % 128, n // 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _str(s):
    b = s.encode() if isinstance(s, str) else s
    return struct.pack("!H", len(b)) + b


def _packet(kind, body):
    return bytes([kind]) + _varlen(len(body)) + body


def connect_packet(client_id, user=None, password=None, will=None, keepalive=KEEPALIVE_S):
    """will = (topic, payload, retain)."""
    flags = 0x02                                    # clean session
    payload = _str(client_id)
    if will:
        flags |= 0x04 | (0x20 if will[2] else 0)
        payload += _str(will[0]) + _str(will[1])
    if user is not None:
        flags |= 0x80
        payload += _str(user)
        if password is not None:
            flags |= 0x40
            payload += _str(password)
    return _packet(0x10, _str("MQTT") + bytes([4, flags]) + struct.pack("!H", keepalive) + payload)


def publish_packet(topic, payload, retain=False):
    if isinstance(payload, str):
        payload = payload.encode()
    return _packet(0x30 | (1 if retain else 0), _str(topic) + payload)


def subscribe_packet(pid, topic):
    return _packet(0x82, struct.pack("!H", pid) + _str(topic) + b"\x00")


PINGREQ = b"\xc0\x00"
DISCONNECT = b"\xe0\x00"


def read_packet(sock):
    """One MQTT packet -> (type byte, body). Raises ConnectionError on EOF."""
    def take(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("broker closed the connection")
            buf += chunk
        return buf
    kind = take(1)[0]
    n, mult = 0, 1
    for _ in range(4):
        b = take(1)[0]
        n += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
    return kind, take(n) if n else b""


def parse_publish(kind, body):
    """PUBLISH body -> (topic, payload). QoS 1/2 carry a packet id after the topic."""
    tlen = struct.unpack("!H", body[:2])[0]
    topic = body[2:2 + tlen].decode(errors="replace")
    rest = body[2 + tlen:]
    if (kind >> 1) & 0x03:
        rest = rest[2:]
    return topic, rest


# --- the node's data, JSON-safe

def jsonable(x, depth=0):
    """The meshtastic library's packet and node dicts carry bytes and protobuf
    objects ("raw"). Bytes become base64; "raw" and anything else that isn't
    plain data is dropped, so the broker gets what the library decoded."""
    if depth > 12:
        return None
    if isinstance(x, dict):
        return {str(k): jsonable(v, depth + 1) for k, v in x.items()
                if k != "raw" and _plain(v)}
    if isinstance(x, (list, tuple)):
        return [jsonable(v, depth + 1) for v in x if _plain(v)]
    if isinstance(x, (bytes, bytearray)):
        return base64.b64encode(bytes(x)).decode()
    return x


def _plain(v):
    return v is None or isinstance(v, (dict, list, tuple, bytes, bytearray, str, int, float, bool))


def read_cred(path):
    with open(path) as f:
        lines = [ln.rstrip("\n") for ln in f]
    if not lines or not lines[0]:
        raise ValueError("credential file has no user line")
    return lines[0], (lines[1] if len(lines) > 1 else None)


class Feed:
    """Publishes to the broker on its own thread and reconnects while the
    bridge runs. publish_* never block the node's callbacks: they queue.

    send(m) is the bridge's sender (takes {"text","ch","to","ref"}) and
    channels() the node's channel indexes; both only matter with tx=True."""

    def __init__(self, url, cred=None, tx=False, client_id=None):
        u = urlparse(url)
        if u.scheme != "mqtt" or not u.hostname:
            raise ValueError(f"feed URL must be mqtt://HOST[:PORT]/PREFIX, not {url!r}")
        self.host, self.port = u.hostname, u.port or 1883
        self.prefix = u.path.strip("/")
        if not self.prefix:
            raise ValueError("feed URL needs a topic prefix: mqtt://HOST/PREFIX")
        self.cred = cred
        self.tx = tx
        self.client_id = client_id or f"rr-mesh-{socket.gethostname().split('.')[0]}"[:23]
        self.send = None
        self.channels = lambda: []
        self._q = deque(maxlen=QUEUE_MAX)
        self._retained = {}             # topic -> payload, latest only, resent on every connect
        self._lock = threading.Lock()
        self._wlock = threading.Lock()     # one writer on the socket at a time
        self._stop = threading.Event()
        self._sock = None
        self._nodes_t = 0.0
        self._nodes_pending = None
        self._last_send = 0.0
        self.connected = False
        self.dropped = 0

    def topic(self, leaf):
        return f"{self.prefix}/{leaf}"

    # --- from the bridge

    def publish_packet(self, packet):
        with self._lock:
            if len(self._q) == self._q.maxlen:
                self.dropped += 1
            self._q.append((self.topic("packet"), json.dumps(jsonable(packet)), False))

    def publish_info(self, info):
        self._retain("info", json.dumps(jsonable(info)))

    def publish_nodes(self, nodes, force=False):
        """At most every NODES_EVERY_S; the latest is kept and sent when due."""
        now = time.monotonic()
        with self._lock:
            self._nodes_pending = nodes
            due = force or now - self._nodes_t >= NODES_EVERY_S
        if due:
            self._flush_nodes(now)

    def _flush_nodes(self, now):
        with self._lock:
            nodes, self._nodes_pending = self._nodes_pending, None
            if nodes is None:
                return
            self._nodes_t = now
        self._retain("nodes", json.dumps(jsonable(nodes)))

    def _retain(self, leaf, payload):
        with self._lock:
            self._retained[self.topic(leaf)] = payload
            self._q.append((self.topic(leaf), payload, True))

    # --- lifecycle

    def start(self):
        self._retained[self.topic("status")] = "online"
        threading.Thread(target=self._run, name="mesh-feed", daemon=True).start()

    def close(self, wait=2.0):
        """Say offline and disconnect cleanly (a clean disconnect skips the will)."""
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                if self._wlock.acquire(timeout=wait):
                    sock.settimeout(wait)
                    sock.sendall(publish_packet(self.topic("status"), "offline", retain=True) + DISCONNECT)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _run(self):
        fails = 0
        while not self._stop.is_set():
            try:
                self._session()
                fails = 0
            except (OSError, ConnectionError, ValueError) as e:
                if self._stop.is_set():
                    return
                (log.warning if fails == 0 else log.info)(
                    "feed: broker %s:%d: %s: %s", self.host, self.port, type(e).__name__, e)
                fails += 1
            finally:
                self.connected = False
                self._sock = None
            self._stop.wait(RETRY_S[min(fails, len(RETRY_S) - 1)])

    def _session(self):
        user = password = None
        if self.cred:
            user, password = read_cred(self.cred)   # re-read: the vault may unlock later
        sock = socket.create_connection((self.host, self.port), timeout=10)
        try:
            self._write(sock, connect_packet(self.client_id, user, password,
                                        will=(self.topic("status"), "offline", True)))
            kind, body = read_packet(sock)
            if kind >> 4 != 2 or len(body) < 2:
                raise ConnectionError("no CONNACK from the broker")
            if body[1] != 0:
                why = {4: "bad user name or password", 5: "not authorized"}.get(body[1], f"code {body[1]}")
                raise ConnectionError(f"broker refused the connection: {why}")
            if self.tx:
                self._write(sock, subscribe_packet(1, self.topic("send")))
            self._sock = sock
            self.connected = True
            log.warning("feed: connected to %s:%d as %s, prefix %s", self.host, self.port,
                        self.client_id, self.prefix)
            with self._lock:
                retained = list(self._retained.items())
                self._q = deque((x for x in self._q if not x[2]), maxlen=QUEUE_MAX)
            for topic, payload in retained:
                self._write(sock, publish_packet(topic, payload, retain=True))
            last_out = last_in = time.monotonic()
            while not self._stop.is_set():
                while True:
                    with self._lock:
                        item = self._q.popleft() if self._q else None
                    if item is None:
                        break
                    self._write(sock, publish_packet(*item))
                    last_out = time.monotonic()
                now = time.monotonic()
                if self._nodes_pending is not None and now - self._nodes_t >= NODES_EVERY_S:
                    self._flush_nodes(now)
                    continue
                if now - last_in > KEEPALIVE_S * 1.5:   # pings unanswered: the link is dead
                    raise ConnectionError("broker stopped answering")
                if now - last_out >= KEEPALIVE_S / 2:
                    self._write(sock, PINGREQ)
                    last_out = now
                ready, _, _ = select.select([sock], [], [], 0.2)
                if not ready:
                    continue
                kind, body = read_packet(sock)   # whole packets only: the 10 s timeout covers a slow one
                last_in = time.monotonic()
                if kind >> 4 == 3:
                    self._incoming(*parse_publish(kind, body))
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _write(self, sock, data):
        with self._wlock:
            if self._stop.is_set():
                raise ConnectionError("closing")
            sock.sendall(data)

    # --- broker -> node

    def _incoming(self, topic, payload):
        if topic != self.topic("send") or not self.tx:
            return
        try:
            m = json.loads(payload)
        except ValueError:
            return self._sent(False, error="not JSON")
        why = self.check_send(m)
        if why:
            log.warning("feed: send refused: %s", why)
            return self._sent(False, error=why)
        self._last_send = time.monotonic()
        log.warning("feed: sending %d bytes on channel %d%s", len(m["text"].encode()), m["ch"],
                    f" to {m['to']}" if m.get("to") else "")
        self.send({"ref": None, "text": m["text"], "ch": m["ch"], "to": m.get("to")}, feed=True)

    def check_send(self, m):
        if not isinstance(m, dict):
            return "not an object"
        text, ch, to = m.get("text"), m.get("ch", 0), m.get("to")
        if not isinstance(text, str) or not text.strip():
            return "nothing to send"
        if len(text.encode()) > TEXT_MAX_BYTES:
            return f"too long ({len(text.encode())} of {TEXT_MAX_BYTES} bytes)"
        if not isinstance(ch, int) or isinstance(ch, bool) or ch not in self.channels():
            return f"the node has no channel {ch!r}"
        if to is not None and not (isinstance(to, str) and NODE_ID.match(to)):
            return f"not a node id: {to!r}"
        if self.send is None:
            return "the node is not connected"
        if time.monotonic() - self._last_send < SEND_GAP_S:
            return f"sends must be {SEND_GAP_S:g} s apart"
        m["ch"] = ch
        return None

    def _sent(self, ok, id=None, error=None):
        with self._lock:
            self._q.append((self.topic("sent"), json.dumps({"ok": ok, "id": id, "error": error}), False))

    def sent(self, ok, id=None, error=None):
        """The bridge reports how a feed send went."""
        self._sent(ok, id, error)
