#!/usr/bin/env python3
"""mesh_feed.py against a fake MQTT broker in this process.

Checks:
  - CONNECT carries the user, password and an "offline" retained last will
  - on connect: status "online", info and nodes, all retained
  - a packet arrives JSON-safe: bytes as base64, the protobuf "raw" dropped
  - packets published while the broker is away are delivered once it's back
  - the broker refusing the login is retried, not fatal
  - with tx: a good send reaches the bridge with its channel; one inside the
    gap, an unknown channel, a too-long text, a bad node id and non-JSON are
    refused on PREFIX/sent and never reach the bridge
  - without tx: no subscription, and a send is ignored
  - close publishes "offline" retained and disconnects cleanly
  - the bridge and the server pass --feed through (mesh.py command line)

    python3 tests/mesh_feed_test.py
"""
import base64
import json
import os
import socket
import struct
import sys
import threading
import time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import mesh_feed  # noqa: E402
from testkit import free_port, wait_for  # noqa: E402

mesh_feed.RETRY_S = (0.1, 0.2)
mesh_feed.SEND_GAP_S = 0.5


class FakeBroker:
    def __init__(self, refuse=0):
        self.port = free_port()
        self.srv = socket.create_server(("127.0.0.1", self.port))
        self.refuse = refuse            # refuse this many logins first (code 5)
        self.connects = []              # parsed CONNECT dicts
        self.pubs = []                  # (topic, payload, retain)
        self.subs = []
        self.disconnects = 0
        self.conn = None
        self.lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            while True:
                kind, body = mesh_feed.read_packet(c)
                t = kind >> 4
                if t == 1:
                    self.connects.append(parse_connect(body))
                    if self.refuse:
                        self.refuse -= 1
                        c.sendall(b"\x20\x02\x00\x05")
                        c.close()
                        return
                    c.sendall(b"\x20\x02\x00\x00")
                    self.conn = c
                elif t == 3:
                    topic, payload = mesh_feed.parse_publish(kind, body)
                    with self.lock:
                        self.pubs.append((topic, payload.decode(), bool(kind & 1)))
                elif t == 8:
                    pid = body[:2]
                    tl = struct.unpack("!H", body[2:4])[0]
                    self.subs.append(body[4:4 + tl].decode())
                    c.sendall(b"\x90\x03" + pid + b"\x00")
                elif t == 12:
                    c.sendall(b"\xd0\x00")
                elif t == 14:
                    self.disconnects += 1
                    c.close()
                    return
        except (ConnectionError, OSError):
            return

    def push(self, topic, payload):
        self.conn.sendall(mesh_feed.publish_packet(topic, payload))

    def topic(self, t):
        with self.lock:
            return [p for p in self.pubs if p[0] == t]

    def close(self):
        self.srv.close()
        if self.conn:
            self.conn.close()


def parse_connect(body):
    i = 2 + struct.unpack("!H", body[:2])[0]
    level, flags = body[i], body[i + 1]
    i += 4

    def s():
        nonlocal i
        n = struct.unpack("!H", body[i:i + 2])[0]
        v = body[i + 2:i + 2 + n].decode()
        i += 2 + n
        return v
    out = {"level": level, "client": s()}
    if flags & 0x04:
        out["will"] = (s(), s(), bool(flags & 0x20))
    if flags & 0x80:
        out["user"] = s()
    if flags & 0x40:
        out["password"] = s()
    return out


def check(cond, what):
    if not cond:
        raise AssertionError(what)
    print("ok  ", what)


def main():
    tmp = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"feedcred-{os.getpid()}")
    with open(tmp, "w") as f:
        f.write("meshuser\nsecretpw\n")

    # --- queued before the broker exists, refused login retried, then delivered
    broker_port = free_port()
    feed = mesh_feed.Feed(f"mqtt://127.0.0.1:{broker_port}/mesh/feed/test", cred=tmp, tx=True)
    sends = []
    feed.send = lambda m, feed=False: sends.append(m)
    feed.channels = lambda: [0, 1]
    feed.publish_info({"num": 1, "id": "!00000001"})
    feed.publish_nodes({"!00000001": {"user": {"id": "!00000001", "publicKey": b"\x01\x02"}}}, force=True)
    feed.start()
    feed.publish_packet({"fromId": "!0000beef", "raw": object(),
                         "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"hi", "text": "hi",
                                     "telemetry": {"raw": object(), "deviceMetrics": {"batteryLevel": 80}}}})
    time.sleep(0.3)
    b = FakeBroker(refuse=1)
    b.srv.close()
    b.port = broker_port
    b.srv = socket.create_server(("127.0.0.1", broker_port))
    threading.Thread(target=b._accept, daemon=True).start()

    check(wait_for(lambda: len(b.connects) >= 2 and feed.connected, 5), "refused login retried, then connected")
    c = b.connects[-1]
    check(c["level"] == 4 and c["user"] == "meshuser" and c["password"] == "secretpw", "CONNECT has user and password")
    check(c.get("will") == ("mesh/feed/test/status", "offline", True), "last will is retained offline")
    check(wait_for(lambda: b.topic("mesh/feed/test/status"), 3), "status published")
    check(b.topic("mesh/feed/test/status")[0][1:] == ("online", True), "status online, retained")
    check(wait_for(lambda: b.topic("mesh/feed/test/info") and b.topic("mesh/feed/test/nodes"), 3),
          "info and nodes published")
    check(all(p[2] for p in b.topic("mesh/feed/test/nodes")), "nodes retained")
    nodes = json.loads(b.topic("mesh/feed/test/nodes")[-1][1])
    check(nodes["!00000001"]["user"]["publicKey"] == base64.b64encode(b"\x01\x02").decode(), "node bytes as base64")
    check(wait_for(lambda: b.topic("mesh/feed/test/packet"), 3), "packet queued while away is delivered")
    pkt = json.loads(b.topic("mesh/feed/test/packet")[0][1])
    check("raw" not in pkt and "raw" not in pkt["decoded"]["telemetry"], "protobuf raw dropped")
    check(pkt["decoded"]["payload"] == base64.b64encode(b"hi").decode(), "payload bytes as base64")
    check(pkt["decoded"]["telemetry"]["deviceMetrics"]["batteryLevel"] == 80, "telemetry kept")
    check(not b.topic("mesh/feed/test/packet")[0][2], "packets not retained")
    check(wait_for(lambda: "mesh/feed/test/send" in b.subs, 3), "tx subscribes to PREFIX/send")

    # --- sends from the broker
    b.push("mesh/feed/test/send", json.dumps({"text": "hello", "ch": 1, "to": None}))
    check(wait_for(lambda: sends, 3), "good send reaches the bridge")
    check(sends[0]["text"] == "hello" and sends[0]["ch"] == 1, "with its text and channel")
    b.push("mesh/feed/test/send", json.dumps({"text": "again", "ch": 1}))
    for bad in ({"text": "x", "ch": 5}, {"text": "x" * 201, "ch": 0}, {"text": "x", "ch": 0, "to": "bob"},
                {"text": "", "ch": 0}, "not json{"):
        time.sleep(0.6)
        b.push("mesh/feed/test/send", bad if isinstance(bad, str) else json.dumps(bad))
    check(wait_for(lambda: len(b.topic("mesh/feed/test/sent")) >= 6, 5), "each refusal answered on PREFIX/sent")
    errs = [json.loads(p[1])["error"] for p in b.topic("mesh/feed/test/sent")]
    check(len(sends) == 1, f"refused sends never reach the bridge ({errs})")
    check(any("apart" in e for e in errs), "send inside the gap refused")
    check(any("channel 5" in e for e in errs), "unknown channel refused")
    check(any("too long" in e for e in errs), "too long refused")
    check(any("node id" in e for e in errs), "bad node id refused")
    check(any("JSON" in e for e in errs), "non-JSON refused")
    feed.sent(True, id=42)
    check(wait_for(lambda: any(json.loads(p[1]).get("id") == 42 for p in b.topic("mesh/feed/test/sent")), 3),
          "the bridge's result goes to PREFIX/sent")

    # --- clean close
    feed.close()
    check(wait_for(lambda: b.disconnects == 1, 3), "close disconnects cleanly")
    check(b.topic("mesh/feed/test/status")[-1][1:] == ("offline", True), "close publishes offline, retained")
    b.close()

    # --- no tx: no subscription, a send is ignored
    b2 = FakeBroker()
    f2 = mesh_feed.Feed(f"mqtt://127.0.0.1:{b2.port}/p", tx=False)
    s2 = []
    f2.send = lambda m, feed=False: s2.append(m)
    f2.channels = lambda: [0]
    f2.start()
    check(wait_for(lambda: f2.connected, 3), "no-cred feed connects")
    check(not b2.subs and "user" not in b2.connects[0], "without tx and cred: no subscribe, no login")
    b2.push("p/send", json.dumps({"text": "x", "ch": 0}))
    time.sleep(0.5)
    check(not s2 and not b2.topic("p/sent"), "send ignored without tx")
    f2.close()
    b2.close()

    # --- a busy feed (publishing more often than it pings) stays connected
    old_ka = mesh_feed.KEEPALIVE_S
    mesh_feed.KEEPALIVE_S = 2
    b3 = FakeBroker()
    f3 = mesh_feed.Feed(f"mqtt://127.0.0.1:{b3.port}/busy")
    f3.start()
    check(wait_for(lambda: f3.connected, 3), "busy feed connects")
    end = time.time() + 5            # 2.5 x the keepalive, a packet every 0.2 s
    while time.time() < end:
        f3.publish_packet({"n": 1})
        time.sleep(0.2)
    check(len(b3.connects) == 1, f"a busy feed is not mistaken for a dead broker ({len(b3.connects)} connects)")
    f3.close()
    b3.close()
    mesh_feed.KEEPALIVE_S = old_ka

    # --- the bridge refreshes the feed's node list when a packet arrives
    import mesh_bridge
    mesh_bridge.NODES_EVERY_S = 0.05

    class FakeFeed:
        def __init__(self):
            self.nodes_sent = []
        def publish_packet(self, p):
            pass
        def publish_nodes(self, nodes, force=False):
            self.nodes_sent.append(nodes)

    beef = {"num": 2, "lastHeard": 1, "snr": 1.0, "hopsAway": 3}
    iface = SimpleNamespace(myInfo=SimpleNamespace(my_node_num=1), nodesByNum={2: beef},
                            nodes={"!0000beef": beef})
    br = mesh_bridge.Bridge(iface, {})
    br.nodes_dirty = False
    br.feed = FakeFeed()
    t = threading.Thread(target=br.nodes_loop, daemon=True)
    t.start()
    time.sleep(0.2)
    before = len(br.feed.nodes_sent)
    # the library does NOT touch lastHeard on a packet: the bridge must stamp it
    br.on_receive({"from": 2, "fromId": "!0000beef", "rxTime": 1000, "rxSnr": 7.5,
                   "hopStart": 3, "hopLimit": 3, "decoded": {"portnum": "TELEMETRY_APP"}}, iface)
    check(wait_for(lambda: len(br.feed.nodes_sent) > before, 2), "a packet refreshes the feed's node list")
    last = br.feed.nodes_sent[-1]["!0000beef"]
    check((last["lastHeard"], last["snr"], last["hopsAway"]) == (1000, 7.5, 0),
          "the bridge stamps lastHeard, SNR and hops from the packet")
    br.on_receive({"from": 1, "fromId": "!00000001", "rxTime": 2000}, iface)
    check(beef["lastHeard"] == 1000, "our own packets stamp nothing")
    br.on_receive({"from": 9, "fromId": "!00000009", "rxTime": 3000}, iface)
    check(9 not in iface.nodesByNum, "an unknown sender isn't invented")
    br.lost.set()

    # --- bad URLs are refused up front
    for url in ("http://x/p", "mqtt://host", "mqtt:///p"):
        try:
            mesh_feed.Feed(url)
        except ValueError:
            continue
        raise AssertionError(f"accepted {url}")
    print("ok   bad feed URLs refused")

    # --- the server passes the feed through to the bridge
    from mesh import MeshEngine
    args = SimpleNamespace(mesh_python="py", mesh_bridge="b.py", mesh_port="ble:X", mesh_key=[],
                           mesh_log=None, mesh_feed="mqtt://h/p", mesh_feed_cred="~/c", mesh_feed_tx=True)
    cmd = MeshEngine(None, args).cmd
    check(cmd[-3:] == ["--feed=mqtt://h/p", "--feed-cred=~/c", "--feed-tx"], "MeshEngine passes --feed on")
    args.mesh_feed = None
    check(not any(a.startswith("--feed") for a in MeshEngine(None, args).cmd), "no feed, no flags")
    from addons.mesh import MeshAddon
    a = MeshAddon(SimpleNamespace(), {"port": "ble:X", "feed": "mqtt://h/p", "feed_tx": True})
    check(a.args.mesh_feed == "mqtt://h/p" and a.args.mesh_feed_tx, "add-on config carries the feed")
    os.unlink(tmp)
    print("PASS mesh_feed_test")


if __name__ == "__main__":
    main()
