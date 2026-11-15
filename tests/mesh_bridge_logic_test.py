"""mesh_bridge.Bridge against a scripted interface, with the real protobufs.

The fake bridge (fake_mesh_bridge.py) covers the protocol the server sees;
this covers what the bridge itself builds and parses:
  - a text goes out through sendText with its reply id; a reaction is a
    TEXT_MESSAGE_APP packet with emoji=1 and reply_id set, sent with an ACK
  - an incoming reply / reaction keeps reply_id and emoji
  - NeighborInfo is kept per node, with its neighbours' SNR
  - someone else's traceroute overheard (request or answer) is reported with
    who asked, the route and SNRs; ours are left to trace()
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
# Someone else's traceroute, overheard: BASE's answer to TRK's request, and TRK's request; ours are not
tr_ans = {"from": 0xbeef, "fromId": "!0000beef", "to": 0xdead, "id": 9001,
          "decoded": {"portnum": "TRACEROUTE_APP", "requestId": 9000,
                      "traceroute": {"route": [0xcafe], "snrTowards": [24, 18], "routeBack": [0xcafe], "snrBack": [-128]}}}
tr_req = {"from": 0xdead, "fromId": "!0000dead", "to": 0xbeef, "id": 9000,
          "decoded": {"portnum": "TRACEROUTE_APP", "traceroute": {"route": [0xffffffff], "snrTowards": [10]}}}
tr_ours = {"from": 0xbeef, "fromId": "!0000beef", "to": 0xb2a6fd78, "id": 9002,
           "decoded": {"portnum": "TRACEROUTE_APP", "requestId": 8000, "traceroute": {"route": []}}}
for p in (tr_ans, tr_req, tr_ours):
    b.on_receive(p, iface)
res["heard_traces"] = [{k: e[k] for k in ("origin", "to", "reply", "route", "snr_towards", "route_back", "snr_back")}
                       for e in out if e["ev"] == "heard_trace"]
out.clear()
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
check("overheard traceroutes: the answer (origin = who asked) and the request; not ours",
      r["heard_traces"],
      [{"origin": "!0000dead", "to": "!0000beef", "reply": True, "route": ["!0000cafe"], "snr_towards": [6.0, 4.5],
        "route_back": ["!0000cafe"], "snr_back": [None]},
       {"origin": "!0000dead", "to": "!0000beef", "reply": False, "route": ["?"], "snr_towards": [2.5],
        "route_back": None, "snr_back": None}])
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

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
