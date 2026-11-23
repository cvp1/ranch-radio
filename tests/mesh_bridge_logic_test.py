"""mesh_bridge.Bridge against a scripted interface, with the real protobufs.

The fake bridge (fake_mesh_bridge.py) covers the protocol the server sees;
this covers what the bridge itself builds and parses:
  - a text goes out through sendText with its reply id; a reaction is a
    TEXT_MESSAGE_APP packet with emoji=1 and reply_id set, sent with an ACK
  - an incoming reply / reaction keeps reply_id and emoji
  - NeighborInfo is kept per node, with its neighbours' SNR
  - requests: an empty Position, an empty device or environment Telemetry
    (the variant set, so the far end knows what to answer), our own User
    for user info, a dummy NeighborInfo for neighbours, each with want_response; an answer, a NAK and a bad kind
  - ignore / unignore / remove reach the local node's admin calls and update
    the node DB; reboot
  - after Reboot or a settings save (not the owner) the bridge leaves the link
    RELINK_S later, even when the write hangs; a failed write keeps it
  - the node list carries environment telemetry, the node's own counters
    (LocalStats, this node only), air time, uptime, ignored and PKI flags

Needs the meshtastic package (its own venv, like the bridge); without it
this prints SKIP. MESHTASTIC_PY overrides the interpreter.

    python3 tests/mesh_bridge_logic_test.py
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.environ.get("MESHTASTIC_PY") or os.path.expanduser("~/.local/share/venvs/meshtastic/bin/python")

CHILD = r'''
import json, sys, types
sys.path.insert(0, sys.argv[1])
import mesh_bridge as mb
from meshtastic.protobuf import mesh_pb2, portnums_pb2, telemetry_pb2

out = []
mb.emit = lambda **d: out.append(d)
mb.REQUEST_TIMEOUT_S = 60
mb.RELINK_S = 0.3
res = {}
import threading, time
from meshtastic.protobuf import localonly_pb2

class Node:
    def __init__(self):
        self.calls = []
        self.localConfig = localonly_pb2.LocalConfig()
        self.moduleConfig = localonly_pb2.LocalModuleConfig()
        self.hang = None              # a threading.Event: writeConfig blocks on it (the USB hang)
        self.fail = False
    def writeConfig(self, section):
        self.calls.append(("writeConfig", section))
        if self.fail:
            raise OSError("write failed")
        if self.hang is not None:
            self.hang.wait()
    def setOwner(self, **kw):
        self.calls.append(("setOwner", kw.get("long_name")))
    def __getattr__(self, name):
        if name in ("setIgnored", "removeIgnored", "removeNode", "reboot"):
            return lambda *a: self.calls.append((name, *a))
        raise AttributeError(name)

class Pkt:
    def __init__(self, id):
        self.id = id

class Iface:
    def __init__(self):
        self.myInfo = types.SimpleNamespace(my_node_num=0xb2a6fd78)
        self.localNode = Node()
        self.sent_text, self.sent_data, self.sent_pkts = [], [], []
        self.nodesByNum = {
            0xb2a6fd78: {"num": 0xb2a6fd78, "user": {"id": "!b2a6fd78", "longName": "Test Node", "shortName": "TEST",
                                                    "hwModel": "HELTEC_V4", "publicKey": "AAAA"},
                         "deviceMetrics": {"batteryLevel": 101, "airUtilTx": 1.5, "uptimeSeconds": 3600},
                         "localStats": {"numPacketsTx": 40, "numPacketsRx": 900, "numOnlineNodes": 3,
                                        "noiseFloor": -110}},
            0xbeef: {"num": 0xbeef, "user": {"id": "!0000beef", "longName": "Ranch Base", "shortName": "BASE"},
                     "environmentMetrics": {"temperature": 21.5, "relativeHumidity": 30.0},
                     "localStats": {"numPacketsRx": 5}},
            0xdead: {"num": 0xdead, "user": {"id": "!0000dead", "longName": "Old Truck", "shortName": "TRK"}},
        }
        self.nodes = {n["user"]["id"]: n for n in self.nodesByNum.values()}
        self._id = 7000
    def _generatePacketId(self):
        self._id += 1
        return self._id
    def sendText(self, text, **kw):
        self.sent_text.append((text, kw))
        return Pkt(self._generatePacketId())
    def _sendPacket(self, p, dest, wantAck=False):
        self.sent_pkts.append((p, dest, wantAck))
        return p
    def sendData(self, data, **kw):
        self.sent_data.append((data, kw))
        return Pkt(self._generatePacketId())

iface = Iface()
b = mb.Bridge(iface, {}, "serial")

# --- sending
b.send({"ref": 1, "text": "hello", "ch": 1, "to": None})
b.send({"ref": 2, "text": "me too", "ch": 1, "to": None, "reply_to": 1001})
b.send({"ref": 3, "text": "\U0001F44D", "ch": 1, "to": "!0000beef", "reply_to": 1001, "emoji": True})
res["text_kw"] = [(t, kw.get("replyId"), kw.get("channelIndex"), kw.get("wantAck")) for t, kw in iface.sent_text]
p, dest, ack = iface.sent_pkts[0]
res["emoji_pkt"] = [p.decoded.portnum == portnums_pb2.PortNum.TEXT_MESSAGE_APP, p.decoded.emoji,
                    p.decoded.reply_id, p.decoded.payload.decode(), p.channel, dest, ack, p.id > 0]
res["sent_events"] = [e["ev"] for e in out if e["ev"] in ("sent", "send_failed")]
out.clear()
b.send({"ref": 4, "text": "x", "ch": 0, "to": None, "emoji": True})       # a reaction to nothing
res["emoji_no_target"] = [e["ev"] for e in out]
out.clear()

# --- receiving
b.on_receive({"from": 0xbeef, "fromId": "!0000beef", "to": 0xffffffff, "toId": "^all", "id": 9001, "channel": 1,
              "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "\U0001F44D", "replyId": 7002, "emoji": 1}}, iface)
b.on_receive({"from": 0xbeef, "fromId": "!0000beef", "to": 0xffffffff, "toId": "^all", "id": 9002, "channel": 1,
              "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "plain"}}, iface)
msgs = [e for e in out if e["ev"] == "msg"]
res["rx_msgs"] = [(m["text"], m["reply_id"], m["emoji"]) for m in msgs]
b.on_receive({"from": 0xbeef, "fromId": "!0000beef", "rxTime": 1700000000,
              "decoded": {"portnum": "NEIGHBORINFO_APP", "neighborinfo": {
                  "nodeId": 0xbeef, "neighbors": [{"nodeId": 0xcafe, "snr": 6.5}, {"nodeId": 0xdead, "snr": -3.0}]}}}, iface)
res["neighbors"] = [b.neighbors_dirty, json.loads(json.dumps(b.neighbors))]
out.clear()

# --- requests
for i, kind in enumerate(("position", "telemetry", "env", "nodeinfo", "neighbors"), 10):
    b.request({"ref": i, "to": "!0000beef", "kind": kind})
d = iface.sent_data
res["req_ports"] = [portnums_pb2.PortNum.Name(kw["portNum"]) for _, kw in d]
res["req_want"] = [kw["wantResponse"] for _, kw in d]
res["req_dest"] = [kw["destinationId"] for _, kw in d]
res["pos_empty"] = d[0][0].ByteSize()
res["tel_variant"] = [d[1][0].WhichOneof("variant"), d[2][0].WhichOneof("variant")]
res["user_sent"] = [d[3][0].long_name, d[3][0].short_name, d[3][0].id]
res["nb_ask"] = [(x.node_id, x.snr) for x in d[4][0].neighbors]
d[0][1]["onResponse"]({"decoded": {"portnum": "POSITION_APP"}})
d[1][1]["onResponse"]({"decoded": {"portnum": "ROUTING_APP", "routing": {"errorReason": "NO_RESPONSE"}}})
d[1][1]["onResponse"]({"decoded": {"portnum": "TELEMETRY_APP"}})          # too late: already settled
b.request({"ref": 20, "to": "!0000beef", "kind": "admin"})
res["req_events"] = [(e["ref"], e["kind"], e["ok"], e["error"]) for e in out if e["ev"] == "req"]
out.clear()

# --- node actions
b.node_action({"ref": 30, "to": "!0000dead", "do": "ignore"})
res["ignored"] = iface.nodesByNum[0xdead].get("isIgnored")
b.node_action({"ref": 31, "to": "!0000dead", "do": "unignore"})
res["unignored"] = iface.nodesByNum[0xdead].get("isIgnored")
b.node_action({"ref": 32, "to": "!0000beef", "do": "remove"})
res["removed"] = [0xbeef in iface.nodesByNum, "!0000beef" in iface.nodes, "!0000beef" in b.neighbors]
b.node_action({"ref": 33, "to": "!0000dead", "do": "factory_reset"})
b.reboot({"ref": 34})
res["admin_calls"] = list(iface.localNode.calls)
res["done"] = [(e["ref"], e["ok"]) for e in out if e["ev"] == "done"]
out.clear()

# --- the node restarts after Reboot / a settings save: a new link, whatever the old one does
def relinked(wait=1.0):
    end = time.time() + wait
    while time.time() < end and not b.lost.is_set():
        time.sleep(0.02)
    st = [e.get("error") for e in out if e["ev"] == "status" and e.get("connected") is False]
    got = [b.lost.is_set(), st]
    b.lost = threading.Event()
    out.clear()
    return got
res["relink_reboot"] = relinked()
b.config = b.info = lambda: None                  # re-reads after a save: not what this covers
iface.localNode.hang = threading.Event()        # the write never returns
threading.Thread(target=b.set_config, args=({"ref": 40, "section": "neighbor_info",
                 "values": {"enabled": True}},), daemon=True).start()
res["relink_hung_write"] = relinked()
iface.localNode.hang.set()
time.sleep(0.1)
out.clear()
b.lost = threading.Event()
iface.localNode.hang, iface.localNode.fail = None, True
b.set_config({"ref": 41, "section": "neighbor_info", "values": {"enabled": False}})
res["failed_write_done"] = [(e["ref"], e["ok"]) for e in out if e["ev"] == "done"]
res["failed_write_relink"] = relinked(0.6)
iface.localNode.fail = False
b.set_config({"ref": 42, "section": "owner", "values": {"long_name": "Test Node"}})
res["owner_relink"] = relinked(0.6)

# --- the node list
iface.nodesByNum[0xbeef] = {"num": 0xbeef, "user": {"id": "!0000beef", "shortName": "BASE"},
                            "environmentMetrics": {"temperature": 21.5, "relativeHumidity": 30.0},
                            "localStats": {"numPacketsRx": 5}}
b.nodes()
nodes = {n["id"]: n for n in out[-1]["nodes"]}
res["me_node"] = [nodes["!b2a6fd78"][k] for k in ("air_tx", "uptime", "pkc", "stats")]
res["other_node"] = [nodes["!0000beef"][k] for k in ("env", "stats", "pkc", "ignored")]
out.clear()

# --- remote admin: a scripted far node that answers admin messages
from meshtastic.protobuf import admin_pb2
mb.REMOTE_STEP_S, mb.REMOTE_READBACK_WAIT_S = 0.5, 0.05

class Far:
    """The far node: its own LoRa/device config, a session passkey, and the
    admin messages it got. adopt=False: it ACKs a write and keeps the old value."""
    def __init__(self):
        self.cfg = localonly_pb2.LocalConfig()
        self.cfg.lora.region, self.cfg.lora.hop_limit, self.cfg.lora.channel_num = 1, 7, 18
        self.cfg.lora.tx_power = 30
        self.passkey, self.got, self.adopt, self.nak, self.silent = b"pk-1", [], True, None, False
    def answer(self, a, kw):
        self.got.append(a)
        cb = kw["onResponse"]
        if self.silent:
            return
        if self.nak:
            return cb({"decoded": {"portnum": "ROUTING_APP", "routing": {"errorReason": self.nak}}})
        if a.HasField("get_config_request"):
            cb({"decoded": {"portnum": "ROUTING_APP", "routing": {"errorReason": "NONE"}}})   # the ACK first
            r = admin_pb2.AdminMessage(session_passkey=self.passkey)
            name = admin_pb2.AdminMessage.ConfigType.Name(a.get_config_request)[:-len("_CONFIG")].lower()
            getattr(r.get_config_response, name).CopyFrom(getattr(self.cfg, name))
            return cb({"decoded": {"portnum": "ADMIN_APP", "admin": {"raw": r}}})
        if a.session_passkey != self.passkey:
            return cb({"decoded": {"portnum": "ROUTING_APP", "routing": {"errorReason": "ADMIN_BAD_SESSION_KEY"}}})
        if a.HasField("set_config") and self.adopt:
            v = a.set_config.WhichOneof("payload_variant")
            getattr(self.cfg, v).CopyFrom(getattr(a.set_config, v))
        cb({"decoded": {"portnum": "ROUTING_APP", "routing": {"errorReason": "NONE"}}})

far = Far()
plain_send = iface.sendData
def send_data(data, **kw):
    if kw.get("portNum") == portnums_pb2.PortNum.ADMIN_APP:
        res.setdefault("admin_kw", []).append([kw["destinationId"], kw["pkiEncrypted"], kw["wantAck"],
                                               kw["onResponseAckPermitted"]])
        threading.Timer(0.01, far.answer, args=(data, kw)).start()
        return Pkt(iface._generatePacketId())
    return plain_send(data, **kw)
iface.sendData = send_data

def finished(ref, wait=3.0):
    end = time.time() + wait
    while time.time() < end:
        for e in out:
            if e["ev"] == "done" and e["ref"] == ref:
                return [e["ok"], e["error"]]
        time.sleep(0.01)
    return "timeout"

def refused(m, fn):
    try:
        fn(m)
    except (KeyError, TypeError, ValueError) as e:
        return type(e).__name__
    return "accepted"

b.remote_get({"ref": 50, "to": "!0000beef", "section": "lora"})
res["rget_done"] = finished(50)
rc = [e for e in out if e["ev"] == "remote_config"]
res["rget_values"] = [rc[0]["to"], rc[0]["section"], rc[0]["values"]["region"], rc[0]["values"]["hop_limit"],
                      rc[0]["values"]["channel_num"]] if rc else None
res["rget_nokeys"] = sorted(set(rc[0]["values"]) - {f[0] for f in next(s for s in mb.SETTINGS if s[0] == "lora")[3]}) if rc else None
out.clear()

b.remote_set({"ref": 51, "to": "!0000beef", "section": "lora", "values": {"hop_limit": 5}})
res["rset_done"] = finished(51)
w = [a for a in far.got if a.HasField("set_config")]
res["rset_wrote"] = [w[-1].session_passkey.decode(), w[-1].set_config.lora.hop_limit, w[-1].set_config.lora.region,
                     w[-1].set_config.lora.channel_num, w[-1].set_config.lora.tx_power] if w else None
res["rset_far"] = far.cfg.lora.hop_limit
out.clear()

far.adopt = False
b.remote_set({"ref": 52, "to": "!0000beef", "section": "lora", "values": {"hop_limit": 3}})
res["rset_not_adopted"] = finished(52)
far.adopt = True
out.clear()

far.passkey = b"pk-2"                      # the far node restarted: a new passkey, read fresh each job
b.remote_reboot({"ref": 53, "to": "!0000beef"})
res["rreboot_done"] = finished(53)
res["rreboot_sent"] = [far.got[-1].reboot_seconds, far.got[-1].session_passkey.decode()]
out.clear()

far.nak = "ADMIN_PUBLIC_KEY_UNAUTHORIZED"
b.remote_get({"ref": 54, "to": "!0000beef", "section": "device"})
res["rget_nak"] = finished(54)
far.nak, far.silent = None, True
b.remote_get({"ref": 55, "to": "!0000beef", "section": "device"})
b.remote_get({"ref": 56, "to": "!0000beef", "section": "device"})       # while 55 waits
res["rget_busy"] = finished(56, 0.3)
res["rget_silent"] = finished(55)
far.silent = False
out.clear()

res["refused"] = [refused({"ref": 60, "to": "!0000beef", "section": "security", "values": {"admin_key": []}}, b.remote_set),
                  refused({"ref": 61, "to": "!0000beef", "section": "network", "values": {"wifi_enabled": True}}, b.remote_set),
                  refused({"ref": 62, "to": "!0000beef", "section": "owner", "values": {"long_name": "x"}}, b.remote_set),
                  refused({"ref": 63, "to": "!0000beef", "section": "lora", "values": {"ignore_incoming": [1]}}, b.remote_set),
                  refused({"ref": 64, "to": "!0000beef", "section": "power", "values": {"is_power_saving": True}}, b.remote_set),
                  refused({"ref": 65, "to": "!0000beef", "section": "security"}, b.remote_get)]

# --- the MQTT feed never gets an admin packet
class Feed:
    def __init__(self):
        self.got = []
    def publish_packet(self, p):
        self.got.append(p["decoded"]["portnum"])
b.feed = Feed()
b.on_receive({"from": 0xbeef, "fromId": "!0000beef", "to": 0xb2a6fd78, "id": 9100,
              "decoded": {"portnum": "ADMIN_APP", "admin": {"raw": admin_pb2.AdminMessage(session_passkey=b"secret")}}}, iface)
b.on_receive({"from": 0xbeef, "fromId": "!0000beef", "to": 0xffffffff, "toId": "^all", "id": 9101, "channel": 0,
              "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi"}}, iface)
res["feed_ports"] = b.feed.got
print(json.dumps(res))
'''

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} got {got!r}" + ("" if ok else f" want {want!r}"))


if not os.path.exists(PY) or subprocess.run([PY, "-c", "import meshtastic"], capture_output=True).returncode:
    print(f"SKIP mesh_bridge_logic_test: no meshtastic package at {PY} (set MESHTASTIC_PY)")
    sys.exit(0)

p = subprocess.run([PY, "-c", CHILD, ROOT], capture_output=True, text=True, timeout=60)
if p.returncode:
    print(p.stdout, p.stderr)
    raise SystemExit("child failed")
# mesh_bridge points fd 1 at stderr on import; the child's result line is on its stderr.
line = next(l for l in reversed((p.stdout + p.stderr).splitlines()) if l.startswith("{"))
r = json.loads(line)

check("text: sendText with its reply id, an ACK, the channel", r["text_kw"],
      [["hello", None, 1, True], ["me too", 1001, 1, True]])
check("reaction: TEXT_MESSAGE_APP, emoji=1, reply_id, the emoji, ch, dest, ACK",
      r["emoji_pkt"], [True, 1, 1001, "\U0001F44D", 1, "!0000beef", True, True])
check("  each send answered 'sent'", r["sent_events"], ["sent", "sent", "sent"])
check("a reaction without a target fails, not crashes", r["emoji_no_target"], ["send_failed"])
check("incoming: reply_id and emoji kept", r["rx_msgs"], [["\U0001F44D", 7002, True], ["plain", None, False]])
check("NeighborInfo kept per node with SNR", r["neighbors"],
      [True, {"!0000beef": {"t": 1700000000, "neighbors": [{"id": "!0000cafe", "snr": 6.5},
                                                           {"id": "!0000dead", "snr": -3.0}]}}])
check("requests: the right port each", r["req_ports"],
      ["POSITION_APP", "TELEMETRY_APP", "TELEMETRY_APP", "NODEINFO_APP", "NEIGHBORINFO_APP"])
check("  all want a response, to the node's number", (r["req_want"], r["req_dest"]), ([True] * 5, [0xbeef] * 5))
check("  neighbours: the apps' dummy (one neighbour, id 0, SNR 0), which the far end ignores",
      r["nb_ask"], [[0, 0.0]])
check("  the position asked with is empty (ours never sent)", r["pos_empty"], 0)
check("  telemetry: device / environment variant set", r["tel_variant"], ["device_metrics", "environment_metrics"])
check("  user info: our own User", r["user_sent"], ["Test Node", "TEST", "!b2a6fd78"])
check("  answer ok, NAK failed (once), bad kind failed", r["req_events"],
      [[10, "position", True, None], [11, "telemetry", False, "NO_RESPONSE"],
       [20, "admin", False, "ValueError: not a request: admin"]])
check("ignore / unignore update the node DB", (r["ignored"], r["unignored"]), (True, False))
check("remove: gone from both node maps and the neighbours", r["removed"], [False, False, False])
check("admin calls on the local node", r["admin_calls"],
      [["setIgnored", 0xdead], ["removeIgnored", 0xdead], ["removeNode", 0xbeef], ["reboot", 5]])
check("after Reboot: a new link (the old one is not trusted)", r["relink_reboot"],
      [True, ["the node restarts after Reboot; connecting again"]])
check("  a settings save whose write hangs: a new link anyway", r["relink_hung_write"],
      [True, ["the node restarts after saving Neighbors; connecting again"]])
check("  a write that fails: done false, the link kept", (r["failed_write_done"], r["failed_write_relink"]),
      ([[41, False]], [False, []]))
check("  the owner (no restart): the link kept", r["owner_relink"], [False, []])
check("  done for each; an unknown action fails", r["done"], [[30, True], [31, True], [32, True], [33, False], [34, True]])
check("this node: air tx, uptime, PKI, its counters", r["me_node"],
      [1.5, 3600, True, {"tx": 40, "rx": 900, "online": 3, "noise": -110}])
check("another node: environment; LocalStats not taken from it", r["other_node"],
      [{"temp": 21.5, "humidity": 30.0}, None, False, False])

check("remote get: the far node's section, done", (r["rget_done"], r["rget_values"]),
      ([True, None], ["!0000beef", "lora", "US", 7, 18]))
check("  allow-listed fields only", r["rget_nokeys"], [])
check("  every admin packet: to the far node, PKI, ACK, ACK-aware handler",
      sorted({tuple(x) for x in r["admin_kw"]}), [(0xbeef, True, True, True)])
check("remote set: read first, then written with the passkey; other fields kept",
      (r["rset_done"], r["rset_wrote"], r["rset_far"]), ([True, None], ["pk-1", 5, 1, 18, 30], 5))
check("  ACKed but not adopted: done false, says what it reads back", r["rset_not_adopted"],
      [False, "RuntimeError: acknowledged, but the node reads back {'hop_limit': 5}"])
check("remote reboot: a fresh passkey, then reboot in REBOOT_S", (r["rreboot_done"], r["rreboot_sent"]),
      ([True, None], [5, "pk-2"]))
check("  a NAK (not an admin there): done false with the reason", r["rget_nak"],
      [False, "RuntimeError: ADMIN_PUBLIC_KEY_UNAUTHORIZED"])
check("  one job at a time: a second is refused while one waits", r["rget_busy"],
      [False, "another remote admin job is running"])
check("  silence: done false after REMOTE_STEP_S", r["rget_silent"], [False, "RuntimeError: no reply"])
check("refused: security, network (Wi-Fi), owner, a field off the list, power saving, a security read",
      r["refused"], ["ValueError"] * 6)
check("the MQTT feed gets a text but never an admin packet", r["feed_ports"], ["TEXT_MESSAGE_APP"])

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
