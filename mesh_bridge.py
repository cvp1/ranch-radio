#!/usr/bin/env python3
"""Meshtastic node <-> Radio Remote, as JSON lines on stdin/stdout.

Runs as a child of server.py (mesh.py), under the Python that has the
`meshtastic` package (its own venv; the server's Python doesn't need it).
Keeping the library in its own process means a stuck or crashing serial
link never takes the radio server with it: this process exits and the
server starts it again.

    mesh_bridge.py /dev/meshtastic [--key NAME=PATH ...]
    mesh_bridge.py ble:NODEM_7720 [--key NAME=PATH ...]   (Bluetooth: name or address)
    mesh_bridge.py tcp:192.168.1.60 [--key NAME=PATH ...]   (Wi-Fi/Ethernet: HOST[:PORT], port 4403)
    mesh_bridge.py usb:1A2B3C4D5E6F7A8B [--key NAME=PATH ...]   (USB, by the board's serial number)
    mesh_bridge.py /dev/meshtastic,ble:NODEA_fd78,ble:NODEB_2560   (first that answers)
    mesh_bridge.py ble:NODEM_7720 --feed mqtt://BROKER/PREFIX --feed-cred FILE [--feed-tx]

usb:SERIAL is the node on a cable whichever socket it is in: a Mac names a
USB serial device after the socket (/dev/cu.usbmodem1101), so a node moved
to another one, or to a hub, is gone from the path the host file named.
udev gives Linux a fixed name (/dev/meshtastic); this needs no rule.
`python -m serial.tools.list_ports -v` shows the number (SER=).

In a list, a bridge that had to take a later entry (Bluetooth, the cable
being out) goes back to an earlier cable when that is plugged in again
(Bridge.cable_loop). Bluetooth after a cable takes a while to answer: the
firmware turns a node's Bluetooth off while a USB client is attached and
gives a client that went without saying so 15 minutes (2.7.26:
PowerFSM.cpp serialEnter, SerialConsole.cpp SERIAL_CONNECTION_TIMEOUT), so
the node is back on Bluetooth that long after the cable drops, and at once
after this bridge closes its link itself.

Bluetooth needs the node bonded to this host once (its PIN) and the node's
single BLE connection free: while a phone app holds it, this bridge can't
connect, and while this bridge holds it, the phone can't.

Wi-Fi (tcp:HOST or tcp:HOST:PORT) needs the node's Wi-Fi or Ethernet on; its
API port (4403) has no login, so anything on the LAN can drive the node: keep
it on a network you trust. It leaves the node's Bluetooth free for a phone.

Out (stdout, one JSON object per line):
    {"ev": "status", "connected": bool, "error": str|None}
    {"ev": "info", "me": {...}, "channels": [{"index", "name", "role", "key_ok"}],
     "link": "serial"|"ble"|"tcp"}
    {"ev": "nodes", "nodes": [...]}               (on change, at most every NODES_EVERY_S)
    {"ev": "msg", "id", "from", "to", "ch", "dm", "text", "snr", "rssi", "hops",
     "reply_id", "emoji"}                         (emoji: a reaction to reply_id)
    {"ev": "sent", "ref", "id"} / {"ev": "send_failed", "ref", "error"}
    {"ev": "ack", "id", "ok", "by", "reason"}
    {"ev": "config", "sections": [...], "values": {section: {field: value}}}
    {"ev": "done", "ref", "ok", "error"}         (a set or favorite was written, or not)
    {"ev": "rx", "from": "!xxxxxxxx", "port": str, "hops", "hop_start", "snr", "rssi", "mqtt",
     "relay", "ch", "to_me", "size"}             (any packet heard: activity, and the Health view)
    {"ev": "trace", "ref", "to", "ok", "error", "route", "snr_towards", "route_back", "snr_back"}
    {"ev": "req", "ref", "to", "kind", "ok", "error"}   (a request's answer came, or didn't)
    {"ev": "neighbors", "neighbors": {"!xxxxxxxx": {"t", "neighbors": [{"id", "snr"}]}}}
In (stdin):
    {"cmd": "send", "ref": n, "text": str, "ch": int, "to": "!xxxxxxxx"|null,
     "reply_to": packet id|null, "emoji": bool}
    {"cmd": "request", "ref": n, "to": "!xxxxxxxx", "kind": "position"|"telemetry"|"env"|"nodeinfo"}
    {"cmd": "node", "ref": n, "to": "!xxxxxxxx", "do": "ignore"|"unignore"|"remove"}
    {"cmd": "reboot", "ref": n}
    {"cmd": "set", "ref": n, "section": str, "values": {field: value}}
    {"cmd": "favorite", "ref": n, "to": "!xxxxxxxx", "fav": bool}
    {"cmd": "trace", "ref": n, "to": "!xxxxxxxx"}

--feed: also publish what the node hears to an MQTT broker, so a bridge on
another host can use this node (mesh_feed.py has the topics). --feed-tx lets
that broker send text through the node too, checked and rate-limited.

--key NAME=PATH: the channel called NAME should use the base64 PSK in PATH.
The key is compared here and only the verdict (key_ok) leaves this process;
no PSK is ever written to stdout or the log.

Settings: only the fields in SETTINGS are read out or written, so the
node's private and admin keys, channel PSKs, Wi-Fi and MQTT passwords never
leave this process, and nothing here can turn off the link the bridge
itself uses (device.serial_enabled, security.*; over Bluetooth, the whole
Bluetooth section; over Bluetooth or Wi-Fi, power saving, which turns both
off on an ESP32 node).
"""

import argparse
import base64
import json
import logging
import os
import sys
import threading
import time

log = logging.getLogger("mesh_bridge")

NODES_EVERY_S = 5.0
TRACE_TIMEOUT_S = 90.0        # a traceroute over several hops on LongFast can take a minute
REQUEST_TIMEOUT_S = 90.0      # a position/telemetry/user request: the same mesh, the same wait
REQUEST_KINDS = ("position", "telemetry", "env", "nodeinfo", "neighbors")
REBOOT_S = 5
# After a write that restarts the node (Reboot, any settings section but the
# owner), this bridge exits RELINK_S later and the server starts a new one.
# The old link is not to be trusted: seen live 2026-11-15, a node rebooted
# after a Neighbors save and Bluetooth died without a word (the page still said
# connected, the next command hung for good); over USB the write itself hung.
RELINK_S = REBOOT_S + 10.0
UNKNOWN_SNR = -128            # RouteDiscovery's "no SNR for this hop"
NODE_FIELDS_USER = ("longName", "shortName", "hwModel", "role")
TCP_PORT = 4403                  # the firmware's API port over Wi-Fi or Ethernet
LINK_NAMES = {"serial": "USB", "ble": "Bluetooth", "tcp": "Wi-Fi"}
CLOSE_WAIT_S = 3.0               # longest we wait for the node link to close
# A bridge on a later port of its list looks for an earlier cable this often,
# and moves when it has been there twice running (a cable being plugged in
# shows up and goes once or twice before it stays). Seen live 2026-11-22: a
# node's USB link was out for 11 hours with the node itself up on its battery
# and its Bluetooth in reach; a list with ble: after the cable covers that,
# and this brings the bridge back to the cable afterwards.
CABLE_AGAIN_S = 30.0
ROLES = {0: "DISABLED", 1: "PRIMARY", 2: "SECONDARY"}
# The node DB's telemetry, by the short names the page uses.
ENV_FIELDS = (("temp", "temperature"), ("humidity", "relativeHumidity"),
              ("pressure", "barometricPressure"), ("iaq", "iaq"), ("lux", "lux"),
              ("wind", "windSpeed"), ("volts", "voltage"))
# The node's own counters (LocalStats telemetry, sent to the client only, never on the air).
STATS_FIELDS = (("tx", "numPacketsTx"), ("rx", "numPacketsRx"), ("rx_bad", "numPacketsRxBad"),
                ("dupes", "numRxDupe"), ("relayed", "numTxRelay"), ("online", "numOnlineNodes"),
                ("total", "numTotalNodes"), ("util", "channelUtilization"), ("air_tx", "airUtilTx"),
                ("uptime", "uptimeSeconds"), ("noise", "noiseFloor"))

# The settings the page can see and change: (section, label, where, fields),
# where is "owner" (setOwner), "config" (localConfig) or "module" (moduleConfig),
# and each field is (name, label, extra). extra may hold: unit, hint, min, max,
# and risky (why changing it can cut the node off; the page asks first).
OFF_MESH = "Every node on the mesh must use the same value, or this one drops off it."
SETTINGS = (
    ("owner", "Owner", "owner", (
        ("long_name", "Long name", {"max_bytes": 39}),
        ("short_name", "Short name", {"max_bytes": 4, "hint": "Up to 4 characters"}),
    )),
    ("device", "Device", "config", (
        ("role", "Role", {"risky": "CLIENT_MUTE / CLIENT_BASE / ROUTER change what this node relays."}),
        ("rebroadcast_mode", "Rebroadcast", {"hint": "ALL relays channels it can't decrypt too"}),
        ("node_info_broadcast_secs", "Node info every", {"unit": "s"}),
        ("tzdef", "Time zone (POSIX TZ)", {"max_bytes": 64}),
        ("led_heartbeat_disabled", "LED heartbeat off", {}),
        ("buzzer_mode", "Buzzer", {}),
        ("double_tap_as_button_press", "Double tap = button", {}),
        ("disable_triple_click", "Triple click off", {}),
    )),
    ("lora", "LoRa", "config", (
        ("region", "Region", {"risky": "The wrong region transmits where it isn't legal and can't reach the mesh."}),
        ("use_preset", "Use preset", {"risky": OFF_MESH}),
        ("modem_preset", "Preset", {"risky": OFF_MESH}),
        ("channel_num", "Frequency slot", {"hint": "0 = from the primary channel's name", "risky": OFF_MESH}),
        ("hop_limit", "Hop limit", {"min": 0, "max": 7}),
        ("tx_enabled", "Transmit", {"risky": "Off, the node sends nothing at all, ACKs included."}),
        ("tx_power", "TX power", {"unit": "dBm", "min": 0, "max": 30, "hint": "0 = the region's legal maximum"}),
        ("override_duty_cycle", "Ignore duty-cycle limit", {"risky": "Lets the node exceed the region's duty-cycle limit."}),
        ("sx126x_rx_boosted_gain", "RX boosted gain", {}),
        ("ignore_mqtt", "Ignore MQTT", {}),
        ("config_ok_to_mqtt", "OK to MQTT", {}),
    )),
    ("position", "Position", "config", (
        ("gps_mode", "GPS", {}),
        ("fixed_position", "Fixed position", {}),
        ("position_broadcast_secs", "Broadcast every", {"unit": "s"}),
        ("position_broadcast_smart_enabled", "Smart broadcast", {}),
        ("broadcast_smart_minimum_distance", "Smart min. distance", {"unit": "m"}),
        ("broadcast_smart_minimum_interval_secs", "Smart min. interval", {"unit": "s"}),
        ("gps_update_interval", "GPS update every", {"unit": "s"}),
    )),
    ("power", "Power", "config", (
        ("is_power_saving", "Power saving", {}),
        ("on_battery_shutdown_after_secs", "Shut down on battery after", {"unit": "s", "hint": "0 = never"}),
        ("wait_bluetooth_secs", "Bluetooth wait", {"unit": "s"}),
        ("ls_secs", "Light sleep", {"unit": "s"}),
        ("min_wake_secs", "Min. wake", {"unit": "s"}),
        ("sds_secs", "Super deep sleep", {"unit": "s"}),
    )),
    ("display", "Display", "config", (
        ("screen_on_secs", "Screen on for", {"unit": "s"}),
        ("auto_screen_carousel_secs", "Carousel every", {"unit": "s", "hint": "0 = off"}),
        ("units", "Units", {}),
        ("flip_screen", "Flip screen", {}),
        ("use_12h_clock", "12-hour clock", {}),
        ("compass_north_top", "Compass north up", {}),
        ("heading_bold", "Bold headings", {}),
        ("wake_on_tap_or_motion", "Wake on tap/motion", {}),
    )),
    ("bluetooth", "Bluetooth", "config", (
        ("enabled", "Bluetooth", {}),
        ("mode", "Pairing", {"hint": "RANDOM_PIN shows the PIN on the node's screen"}),
    )),
    ("telemetry", "Telemetry", "module", (
        ("device_telemetry_enabled", "Send device telemetry", {}),
        ("device_update_interval", "Device every", {"unit": "s"}),
        ("environment_measurement_enabled", "Environment sensors", {}),
        ("environment_update_interval", "Environment every", {"unit": "s"}),
    )),
    ("neighbor_info", "Neighbors", "module", (
        ("enabled", "Neighbor info", {}),
        ("update_interval", "Every", {"unit": "s"}),
        ("transmit_over_lora", "Send over LoRa", {}),
    )),
)
_TYPES = {8: "bool", 13: "int", 5: "int", 2: "float", 14: "enum", 9: "str"}
_RANGE = {13: (0, 2**32 - 1), 5: (-2**31, 2**31 - 1)}

# The library prints some errors to stdout; the protocol gets its own copy of
# fd 1 and everything else written to stdout goes to stderr (the server log).
_out = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr
_out_lock = threading.Lock()


def emit(**d):
    with _out_lock:
        _out.write(json.dumps(d) + "\n")


def preset_name(enum_name):
    """LONG_FAST -> LongFast: the name Meshtastic apps show for an unnamed primary."""
    return "".join(w.capitalize() for w in enum_name.split("_"))


def read_keys(specs):
    keys = {}
    for spec in specs:
        name, _, path = spec.partition("=")
        try:
            with open(os.path.expanduser(path)) as f:
                keys[name] = base64.b64decode(f.read().strip(), validate=True)
        except (OSError, ValueError) as e:
            # Vault locked, file missing, or not base64: verdict unknown.
            log.warning("channel key for %s unreadable (%s)", name, type(e).__name__)
            keys[name] = None
    return keys


def rx_detail(packet, my_num):
    """What the Health view counts of a packet (mesh_stats.py): its hops and the
    hop limit its sender set, signal, MQTT, the relay byte (the last byte of the
    node that passed it to us; firmware 2.6+, 0 = not said), its size."""
    hops = hop_start = None
    if packet.get("hopStart"):
        hop_start = packet["hopStart"]
        if "hopLimit" in packet:
            hops = hop_start - packet["hopLimit"]
    raw = packet.get("raw")
    try:
        size = raw.ByteSize() if raw is not None else None
    except Exception:
        size = None
    return {"hops": hops if hops is None or hops >= 0 else None, "hop_start": hop_start,
            "snr": packet.get("rxSnr"), "rssi": packet.get("rxRssi") or None, "mqtt": bool(packet.get("viaMqtt")),
            "relay": packet.get("relayNode") or None, "ch": packet.get("channel", 0),
            "to_me": packet.get("to") == my_num, "size": size}


class Bridge:
    def __init__(self, iface, keys, link="serial"):
        self.iface = iface
        self.keys = keys
        self.link = link              # "serial", "ble" or "tcp": the link set_config must not cut
        self.my_num = iface.myInfo.my_node_num
        self.pending = set()          # packet ids we sent with wantAck
        self.nodes_dirty = True
        self.lost = threading.Event()
        self.closing = False
        self.feed = None              # mesh_feed.Feed with --feed
        self.feed_dirty = False       # a packet changed lastHeard/SNR: the feed's node list is stale
        self.neighbors = {}           # node id -> {"t", "neighbors": [{"id", "snr"}]} from NEIGHBORINFO_APP
        self.neighbors_dirty = False

    # --- node -> server

    def info(self):
        node = self.iface.localNode
        lora = node.localConfig.lora
        from meshtastic.protobuf import config_pb2
        preset = config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset)
        chans = []
        for c in node.channels or []:
            role = ROLES.get(c.role, str(c.role))
            if role == "DISABLED":
                continue
            name = c.settings.name or (preset_name(preset) if role == "PRIMARY" else f"Channel {c.index}")
            key_ok = None
            if c.settings.name in self.keys:
                want = self.keys[c.settings.name]
                key_ok = None if want is None else bytes(c.settings.psk) == want
            chans.append({"index": c.index, "name": name, "role": role, "key_ok": key_ok})
        me = self.iface.nodesByNum.get(self.my_num, {}).get("user", {})
        meta = getattr(self.iface, "metadata", None)
        emit(ev="info", me={"num": self.my_num, "id": me.get("id"), "long": me.get("longName"),
                            "short": me.get("shortName"), "hw": me.get("hwModel"),
                            "fw": getattr(meta, "firmware_version", None)},
             channels=chans, preset=preset_name(preset), link=self.link,
             missing=[n for n in self.keys
                      if not any(c.settings.name == n for c in node.channels or [])])

    def nodes(self):
        out = []
        for n in list((self.iface.nodesByNum or {}).values()):
            u = n.get("user", {})
            pos = n.get("position", {})
            dm = n.get("deviceMetrics", {})
            env = n.get("environmentMetrics", {})
            ls = n.get("localStats") if n.get("num") == self.my_num else None
            out.append({
                "num": n.get("num"), "id": u.get("id"),
                **{k: u.get(k) for k in NODE_FIELDS_USER},
                "last_heard": n.get("lastHeard"), "snr": n.get("snr"),
                "hops": n.get("hopsAway"), "battery": dm.get("batteryLevel"),
                "lat": pos.get("latitude"), "lon": pos.get("longitude"),
                "alt": pos.get("altitude"), "voltage": dm.get("voltage"),
                "util": dm.get("channelUtilization"), "mqtt": bool(n.get("viaMqtt")),
                "fav": bool(n.get("isFavorite")), "me": n.get("num") == self.my_num,
                "air_tx": dm.get("airUtilTx"), "uptime": dm.get("uptimeSeconds"),
                "pos_t": pos.get("time"), "ignored": bool(n.get("isIgnored")),
                "pkc": bool(u.get("publicKey")), "licensed": bool(u.get("isLicensed")),
                "env": {k: env[f] for k, f in ENV_FIELDS if env.get(f) is not None} or None,
                "stats": {k: ls[f] for k, f in STATS_FIELDS if ls.get(f) is not None} if ls else None,
            })
        emit(ev="nodes", nodes=out)

    def note_heard(self, packet):
        """Stamp the sender's lastHeard, SNR and hops from this packet. The
        meshtastic library only takes those from the node database it reads at
        connect, so without this every node's "last heard" freezes there."""
        num = packet.get("from")
        if num is None or num == self.my_num:
            return False
        nodes = [n for n in (self.iface.nodesByNum.get(num),
                             (self.iface.nodes or {}).get(packet.get("fromId") or "")) if n]
        if not nodes:
            return False              # unknown yet: its NodeInfo will add it
        hops = None
        if "hopStart" in packet and "hopLimit" in packet:
            hops = packet["hopStart"] - packet["hopLimit"]
        for n in nodes:
            n["lastHeard"] = int(packet.get("rxTime") or time.time())
            if packet.get("rxSnr") is not None:
                n["snr"] = packet["rxSnr"]
            if hops is not None and hops >= 0:
                n["hopsAway"] = hops
        return True

    def on_receive(self, packet, interface):
        if self.note_heard(packet):
            self.nodes_dirty = True   # the page's node list and the feed's are both stale now
        if self.feed is not None:
            self.feed_dirty = True
            try:
                self.feed.publish_packet(packet)
            except Exception:
                log.exception("feed packet")
        d = packet.get("decoded") or {}
        port = d.get("portnum")
        if packet.get("from") != self.my_num and packet.get("fromId"):
            emit(ev="rx", **{"from": packet["fromId"]}, port=port or "ENCRYPTED", **rx_detail(packet, self.my_num))
        if port == "TEXT_MESSAGE_APP":
            to = packet.get("to")
            hops = None
            if "hopStart" in packet and "hopLimit" in packet:
                hops = packet["hopStart"] - packet["hopLimit"]
            emit(ev="msg", id=packet.get("id"), **{"from": packet.get("fromId")},
                 to=packet.get("toId"), ch=packet.get("channel", 0), dm=to == self.my_num,
                 text=d.get("text", ""), snr=packet.get("rxSnr"), rssi=packet.get("rxRssi"),
                 hops=hops, reply_id=d.get("replyId") or None, emoji=bool(d.get("emoji")))
        elif port == "NEIGHBORINFO_APP":
            self.note_neighbors(packet)
        elif port == "ROUTING_APP":
            rid = d.get("requestId")
            if rid in self.pending:
                reason = (d.get("routing") or {}).get("errorReason", "NONE")
                self.pending.discard(rid)
                # From ourselves = our node heard a neighbour rebroadcast it
                # (implicit ack); from the destination = delivered.
                emit(ev="ack", id=rid, ok=reason == "NONE", reason=reason,
                     by=packet.get("fromId"), relayed=packet.get("from") == self.my_num)

    def note_neighbors(self, packet):
        """A node's NeighborInfo: who it hears directly, with SNR. Kept per
        node (the latest wins) and sent to the page for the map's links."""
        ni = (packet.get("decoded") or {}).get("neighborinfo") or {}
        num = ni.get("nodeId") or packet.get("from")
        if not num:
            return
        self.neighbors[f"!{num:08x}"] = {
            "t": int(packet.get("rxTime") or time.time()),
            "neighbors": [{"id": f"!{x['nodeId']:08x}", "snr": x.get("snr")}
                          for x in ni.get("neighbors") or [] if x.get("nodeId")]}
        self.neighbors_dirty = True

    def on_node(self, node, interface):
        self.nodes_dirty = True

    def expect_restart(self, why):
        """The node is about to restart: start over on a new link RELINK_S
        from now, whatever the old one does (it may hang this very write)."""
        def relink():
            if self.closing or self.lost.is_set():
                return
            emit(ev="status", connected=False, error=f"the node restarts after {why}; connecting again")
            self.lost.set()
        t = threading.Timer(RELINK_S, relink)
        t.daemon = True
        t.start()
        return t

    def on_lost(self, interface):
        if self.closing:
            return
        emit(ev="status", connected=False, error="connection to the node lost")
        self.lost.set()

    def cable_loop(self, spec, using):
        """On `using`, a later port of the list `spec`: leave it for an
        earlier cable once that is back (CABLE_AGAIN_S says how sure). The
        server starts the bridge again and the list is tried from the top."""
        seen = 0
        while not self.lost.wait(CABLE_AGAIN_S):
            try:
                cable = earlier_cable(spec, using)
            except ValueError:
                return
            seen = seen + 1 if cable else 0
            if seen >= 2 and not self.closing:
                emit(ev="status", connected=False, error=f"{cable} is plugged in again; moving to it")
                self.lost.set()

    def nodes_loop(self):
        while not self.lost.is_set():
            if self.nodes_dirty:
                self.nodes_dirty = False
                self.feed_dirty = True
                try:
                    self.nodes()
                except Exception:
                    log.exception("node list")
            if self.neighbors_dirty:
                self.neighbors_dirty = False
                emit(ev="neighbors", neighbors=self.neighbors)
            if self.feed is not None and self.feed_dirty:
                self.feed_dirty = False   # Feed keeps the latest and sends it at most every NODES_EVERY_S
                try:
                    self.feed.publish_nodes(dict(self.iface.nodes or {}))
                except Exception:
                    log.exception("feed node list")
            self.lost.wait(NODES_EVERY_S)

    def _section(self, where, section):
        node = self.iface.localNode
        return getattr(node.localConfig if where == "config" else node.moduleConfig, section)

    def config(self):
        """SETTINGS, typed from the node's protobufs, and the node's values."""
        sections, values = [], {}
        user = self.iface.nodesByNum.get(self.my_num, {}).get("user", {})
        for section, label, where, fields in SETTINGS:
            out, vals = [], {}
            msg = None if where == "owner" else self._section(where, section)
            for name, flabel, extra in fields:
                f = {"name": name, "label": flabel, **extra}
                if msg is None:
                    f["type"] = "str"
                    vals[name] = user.get("longName" if name == "long_name" else "shortName") or ""
                else:
                    d = msg.DESCRIPTOR.fields_by_name.get(name)
                    if d is None or d.type not in _TYPES:   # not in this firmware's protobufs
                        continue
                    f["type"] = _TYPES[d.type]
                    v = getattr(msg, name)
                    if d.type == 14:
                        f["options"] = [e.name for e in d.enum_type.values]
                        v = d.enum_type.values_by_number[v].name if v in d.enum_type.values_by_number else str(v)
                    elif d.type in _RANGE:
                        lo, hi = _RANGE[d.type]
                        f["min"], f["max"] = max(lo, f.get("min", lo)), min(hi, f.get("max", hi))
                    elif d.type == 2:
                        v = round(v, 6)
                    vals[name] = v
                out.append(f)
            sections.append({"name": section, "label": label, "fields": out})
            values[section] = vals
        emit(ev="config", sections=sections, values=values)

    # --- server -> node

    def send(self, m, feed=False):
        """feed=True: the MQTT feed asked (mesh_feed checked it); the answer goes
        back there, and the page sees it as a message from this node."""
        from meshtastic import BROADCAST_ADDR
        ref = m.get("ref")
        try:
            pkt = self.send_text(str(m["text"]), m.get("to") or BROADCAST_ADDR, int(m.get("ch", 0)),
                                 m.get("reply_to"), bool(m.get("emoji")))
        except Exception as e:
            if feed:
                self.feed.sent(False, error=str(e))
            else:
                emit(ev="send_failed", ref=ref, error=str(e))
            return
        self.pending.add(pkt.id)
        if feed:
            self.feed.sent(True, id=pkt.id)
            emit(ev="msg", id=pkt.id, **{"from": self.iface.nodesByNum.get(self.my_num, {})
                                         .get("user", {}).get("id")},
                 to=m.get("to"), ch=int(m.get("ch", 0)), dm=False, text=str(m["text"]),
                 snr=None, rssi=None, hops=None, via="feed")
        else:
            emit(ev="sent", ref=ref, id=pkt.id)

    def send_text(self, text, dest, ch, reply_to=None, emoji=False):
        """A text message, optionally a reply to (or, with emoji, a reaction
        to) packet reply_to. sendText knows reply ids but not the emoji flag
        the apps use for a reaction, so a reaction is built here the way
        sendData builds any packet."""
        if not emoji:
            return self.iface.sendText(text, destinationId=dest, wantAck=True, channelIndex=ch,
                                       replyId=int(reply_to) if reply_to else None)
        from meshtastic.protobuf import mesh_pb2, portnums_pb2
        p = mesh_pb2.MeshPacket()
        p.channel = ch
        p.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
        p.decoded.payload = text.encode()
        p.decoded.reply_id = int(reply_to)
        p.decoded.emoji = 1
        p.id = self.iface._generatePacketId()
        return self.iface._sendPacket(p, dest, wantAck=True)

    def request(self, m):
        """Ask a node for its position, telemetry, user info or neighbours. Its
        answer lands in the node DB through the library (and the node list with
        it), a NeighborInfo answer in note_neighbors (the library publishes the
        reply as a received packet too); this only says whether one came. What goes out carries nothing of
        ours a broadcast wouldn't: an empty position and empty metrics (the
        firmware answers want_response regardless), and for user info our own
        User, because the far end stores what we send as our NodeInfo."""
        from meshtastic.protobuf import mesh_pb2, portnums_pb2, telemetry_pb2
        ref, to, kind = m.get("ref"), m["to"], m["kind"]
        done = threading.Event()

        def finish(ok, error=None):
            if not done.is_set():
                done.set()
                emit(ev="req", ref=ref, to=to, kind=kind, ok=ok, error=error)

        def on_response(p):
            d = p.get("decoded") or {}
            if d.get("portnum") == "ROUTING_APP":
                reason = (d.get("routing") or {}).get("errorReason", "NONE")
                if reason != "NONE":
                    finish(False, reason)
                return
            self.nodes_dirty = True
            finish(True)

        try:
            if kind == "position":
                data, port = mesh_pb2.Position(), portnums_pb2.PortNum.POSITION_APP
            elif kind in ("telemetry", "env"):
                data, port = telemetry_pb2.Telemetry(), portnums_pb2.PortNum.TELEMETRY_APP
                (data.device_metrics if kind == "telemetry" else data.environment_metrics).SetInParent()
            elif kind == "neighbors":
                # What the apps send: one dummy neighbour (id 0, SNR 0), which
                # the far end recognises and does not add to its own list
                # (NeighborInfoModule.cpp). It answers only with its NeighborInfo
                # module on, and at most once every 3 minutes.
                data = mesh_pb2.NeighborInfo(neighbors=[mesh_pb2.Neighbor(node_id=0, snr=0)])
                port = portnums_pb2.PortNum.NEIGHBORINFO_APP
            elif kind == "nodeinfo":
                from google.protobuf import json_format
                me = self.iface.nodesByNum.get(self.my_num, {}).get("user", {})
                data = json_format.ParseDict(me, mesh_pb2.User(), ignore_unknown_fields=True)
                port = portnums_pb2.PortNum.NODEINFO_APP
            else:
                raise ValueError(f"not a request: {kind}")
            self.iface.sendData(data, destinationId=int(to[1:], 16), portNum=port, wantResponse=True,
                                onResponse=on_response, channelIndex=0)
        except BaseException as e:
            finish(False, f"{type(e).__name__}: {e}")
            return
        t = threading.Timer(REQUEST_TIMEOUT_S, finish, args=(False, "no reply"))
        t.daemon = True
        t.start()

    def node_action(self, m):
        """Ignore, unignore or remove a node in this node's DB (admin to our
        own node only; nothing goes on the air)."""
        ref, to, do = m.get("ref"), m["to"], m["do"]
        try:
            num = int(to[1:], 16)
            node = self.iface.localNode
            if do == "ignore":
                node.setIgnored(num)
            elif do == "unignore":
                node.removeIgnored(num)
            elif do == "remove":
                node.removeNode(num)
            else:
                raise ValueError(f"not a node action: {do}")
        except BaseException as e:
            emit(ev="done", ref=ref, ok=False, error=f"{type(e).__name__}: {e}")
            return
        db = self.iface.nodesByNum or {}
        if do == "remove":
            n = db.pop(num, None)
            uid = ((n or {}).get("user") or {}).get("id")
            if uid and self.iface.nodes:
                self.iface.nodes.pop(uid, None)
            self.neighbors.pop(to, None)
        elif num in db:
            db[num]["isIgnored"] = do == "ignore"
        self.nodes_dirty = True
        emit(ev="done", ref=ref, ok=True, error=None)

    def reboot(self, m):
        relink = self.expect_restart("Reboot")
        try:
            self.iface.localNode.reboot(REBOOT_S)
        except BaseException as e:
            relink.cancel()
            emit(ev="done", ref=m.get("ref"), ok=False, error=f"{type(e).__name__}: {e}")
            return
        emit(ev="done", ref=m.get("ref"), ok=True, error=None)

    def set_config(self, m):
        """Write one section's changed fields. The server has checked them
        against the schema; the protobufs check them again here. The node
        restarts after a settings write (all but the owner's): this bridge
        does not wait for the link to notice (expect_restart)."""
        ref, section, values = m.get("ref"), m["section"], m["values"]
        spec = next((s for s in SETTINGS if s[0] == section), None)
        try:
            if spec is None or not set(values) <= {f[0] for f in spec[3]}:
                raise ValueError(f"not a setting: {section} {sorted(values)}")
            if self.link == "ble" and section == "bluetooth":
                raise ValueError("this bridge reaches the node over Bluetooth; "
                                 "change Bluetooth settings over USB or the phone app")
            if self.link in ("ble", "tcp") and section == "power" and values.get("is_power_saving") is True:
                raise ValueError(f"this bridge reaches the node over {LINK_NAMES[self.link]}, which power "
                                 "saving turns off on an ESP32 node; turn it on over USB")
            node = self.iface.localNode
            if spec[2] == "owner":
                user = self.iface.nodesByNum.get(self.my_num, {}).get("user", {})
                node.setOwner(long_name=values.get("long_name"), short_name=values.get("short_name"),
                              is_licensed=bool(user.get("isLicensed")))
                user.update({k: v for k, v in (("longName", values.get("long_name")),
                                               ("shortName", values.get("short_name"))) if v})
                self.nodes_dirty = True
            else:
                msg = self._section(spec[2], section)
                before = type(msg)()
                before.CopyFrom(msg)
                relink = None
                try:
                    for name, v in values.items():
                        d = msg.DESCRIPTOR.fields_by_name[name]
                        if d.type == 14:
                            v = d.enum_type.values_by_name[v].number
                        setattr(msg, name, v)
                    relink = self.expect_restart(f"saving {spec[1]}")
                    node.writeConfig(section)
                except BaseException:
                    if relink is not None:
                        relink.cancel()
                    msg.CopyFrom(before)        # keep our copy what the node has
                    raise
        except BaseException as e:              # the library's our_exit() raises SystemExit
            emit(ev="done", ref=ref, ok=False, error=f"{type(e).__name__}: {e}")
            return
        emit(ev="done", ref=ref, ok=True, error=None)
        self.config()
        self.info()

    def trace(self, m):
        """Traceroute to a node: the hops there and back, with each hop's SNR.
        The reply comes on the library's thread; a NAK (no route, no
        response) or silence for TRACE_TIMEOUT_S ends it as failed."""
        from meshtastic.protobuf import mesh_pb2, portnums_pb2
        ref, to = m.get("ref"), m["to"]
        done = threading.Event()

        def finish(**d):
            if not done.is_set():
                done.set()
                emit(ev="trace", ref=ref, to=to, **d)

        def nid(n):
            return "?" if n == 0xFFFFFFFF else f"!{n:08x}"

        def snrs(xs):
            return [None if x == UNKNOWN_SNR else x / 4 for x in xs]

        def on_response(p):
            d = p.get("decoded") or {}
            if d.get("portnum") == "ROUTING_APP":
                reason = (d.get("routing") or {}).get("errorReason", "NONE")
                if reason != "NONE":
                    finish(ok=False, error=reason)
                return
            rd = mesh_pb2.RouteDiscovery()
            try:
                rd.ParseFromString(d["payload"])
            except Exception as e:
                finish(ok=False, error=f"bad reply: {e}")
                return
            back_ok = "hopStart" in p and len(rd.snr_back) == len(rd.route_back) + 1
            finish(ok=True, error=None, route=[nid(n) for n in rd.route],
                   snr_towards=snrs(rd.snr_towards), route_back=[nid(n) for n in rd.route_back] if back_ok else None,
                   snr_back=snrs(rd.snr_back) if back_ok else None)

        try:
            hop_limit = self.iface.localNode.localConfig.lora.hop_limit or 3
            self.iface.sendData(mesh_pb2.RouteDiscovery(), destinationId=int(to[1:], 16),
                                portNum=portnums_pb2.PortNum.TRACEROUTE_APP, wantResponse=True,
                                onResponse=on_response, channelIndex=0, hopLimit=hop_limit)
        except BaseException as e:
            finish(ok=False, error=f"{type(e).__name__}: {e}")
            return
        t = threading.Timer(TRACE_TIMEOUT_S, finish, kwargs={"ok": False, "error": "no reply"})
        t.daemon = True
        t.start()

    def favorite(self, m):
        ref, to, fav = m.get("ref"), m["to"], bool(m["fav"])
        try:
            num = int(to[1:], 16)
            node = self.iface.localNode
            (node.setFavorite if fav else node.removeFavorite)(num)
        except BaseException as e:
            emit(ev="done", ref=ref, ok=False, error=f"{type(e).__name__}: {e}")
            return
        if num in (self.iface.nodesByNum or {}):
            self.iface.nodesByNum[num]["isFavorite"] = fav
        self.nodes_dirty = True
        emit(ev="done", ref=ref, ok=True, error=None)


def tcp_target(spec):
    """'tcp:HOST', 'tcp:HOST:PORT' or 'tcp:[V6ADDR]:PORT' -> (host, port), or
    None when it isn't one (no host, a port outside 1..65535, a bare IPv6)."""
    rest, port = spec[4:], TCP_PORT
    if rest.startswith("["):
        end = rest.find("]")
        if end < 0:
            return None
        host, tail = rest[1:end], rest[end + 1:]
        if tail:
            if not tail.startswith(":"):
                return None
            port = tail[1:]
    elif rest.count(":") == 1:
        host, port = rest.split(":")
    elif ":" in rest:
        return None                   # an IPv6 address needs its brackets
    else:
        host = rest
    try:
        port = int(port)
    except ValueError:
        return None
    if not host or not 0 < port < 65536:
        return None
    return host, port


def usb_ports():
    """The host's serial ports as pyserial lists them ([] without pyserial,
    which the meshtastic package brings)."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return list_ports.comports()


def usb_device(serial, ports=None):
    """'1A2B3C4D5E6F7A8B' -> the serial device of the USB board with that
    serial number, or None when it is not plugged in. Letter case is not
    told apart. A Mac shows one board as /dev/cu.* and /dev/tty.*: the cu
    one, which opens without waiting for a carrier."""
    want = serial.strip().lower()
    if not want:
        return None
    found = [p.device for p in (usb_ports() if ports is None else ports)
             if (getattr(p, "serial_number", None) or "").strip().lower() == want]
    return min(found, key=lambda d: (not d.startswith("/dev/cu."), d)) if found else None


def candidates(spec):
    """'/dev/meshtastic,tcp:192.168.1.60,ble:NODEA_fd78' -> the ports to try, in
    order. A serial device that isn't there is skipped without a try, so a
    laptop uses its node on the cable when plugged in and Bluetooth when not;
    usb:SERIAL becomes that board's device, or is skipped the same way.
    A tcp: entry that isn't HOST[:PORT], or a usb: entry with no number, is a
    ValueError: a typo in the host file is said, not quietly skipped."""
    out = []
    for p in (x.strip() for x in spec.split(",")):
        if p.startswith("tcp:"):
            if tcp_target(p) is None:
                raise ValueError(f"{p!r} is not tcp:HOST or tcp:HOST:PORT")
            out.append(p)
        elif p.startswith("usb:"):
            if not p[4:].strip():
                raise ValueError(f"{p!r} is not usb:SERIAL (the board's USB serial number)")
            dev = usb_device(p[4:])
            if dev:
                out.append(dev)
        elif p and (p.startswith("ble:") or os.path.exists(p)):
            out.append(p)
    return out


def earlier_cable(spec, using):
    """The cable port that is there now and comes before `using` (the port
    in use) in the list `spec`, or None. Only cables count: a device file
    being there is a fact, while Bluetooth or Wi-Fi would have to be tried,
    and trying means dropping the link that works."""
    for p in candidates(spec):
        if p == using:
            return None
        if not p.startswith(("ble:", "tcp:")):
            return p
    return None


def connect(port):
    """Open one port ('ble:NAME', 'tcp:HOST[:PORT]' or a serial device) ->
    (interface, link)."""
    if port.startswith("ble:"):
        from meshtastic.ble_interface import BLEInterface
        return BLEInterface(port[4:] or None), "ble"
    if port.startswith("tcp:"):
        target = tcp_target(port)
        if target is None:
            raise ValueError(f"not tcp:HOST or tcp:HOST:PORT: {port}")
        from meshtastic.tcp_interface import TCPInterface
        return TCPInterface(target[0], portNumber=target[1]), "tcp"
    from meshtastic.serial_interface import SerialInterface
    return SerialInterface(port), "serial"


def close(iface, wait=CLOSE_WAIT_S):
    """Close the node link, but give up after `wait` seconds: a Bluetooth
    disconnect can stall for minutes, and the server kills us at 5 s anyway."""
    def run():
        try:
            iface.close()
        except Exception:
            pass
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(wait)


def start_feed(args, iface, b):
    """The MQTT feed, or None when its settings are wrong: a broker problem
    must never take the Mesh tab down with it."""
    import mesh_feed
    try:
        feed = mesh_feed.Feed(args.feed, cred=os.path.expanduser(args.feed_cred) if args.feed_cred else None,
                              tx=args.feed_tx)
    except ValueError as e:
        log.error("feed off: %s", e)
        return None
    feed.send = b.send
    feed.channels = lambda: [c.index for c in (iface.localNode.channels or []) if c.role != 0]
    me = iface.nodesByNum.get(b.my_num, {}).get("user", {})
    feed.publish_info({"num": b.my_num, "id": me.get("id"), "long": me.get("longName"),
                       "short": me.get("shortName")})
    feed.publish_nodes(dict(iface.nodes or {}), force=True)
    feed.start()
    return feed


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("port", help="serial device of the node, e.g. /dev/meshtastic, "
                                "usb:SERIAL for the USB board with that serial number, "
                                "ble:NAME_OR_ADDRESS for Bluetooth, e.g. ble:NODEM_7720, "
                                "or tcp:HOST[:PORT] for Wi-Fi, e.g. tcp:192.168.1.60; "
                                "comma-separate several to try in order")
    p.add_argument("--key", action="append", default=[], metavar="NAME=PATH",
                   help="check that channel NAME uses the base64 PSK in PATH")
    p.add_argument("--feed", metavar="mqtt://HOST[:PORT]/PREFIX",
                   help="also publish the node's traffic to this MQTT broker (see mesh_feed.py)")
    p.add_argument("--feed-cred", metavar="FILE", help="broker user and password, one per line")
    p.add_argument("--feed-tx", action="store_true",
                   help="let the broker send text through the node (PREFIX/send)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, stream=sys.stderr,
                        format="mesh_bridge %(levelname)s %(message)s")

    try:
        ports = candidates(args.port)
    except ValueError as e:
        emit(ev="status", connected=False, error=str(e))
        return 2                  # before the meshtastic imports: checked on any Python
    from pubsub import pub
    keys = read_keys(args.key)
    iface, link, errors, using = None, None, [], None
    for port in ports:                        # first node that answers wins
        try:
            iface, link = connect(port)
        except Exception as e:
            errors.append(f"{port}: {type(e).__name__}: {e}")
            continue
        if iface.myInfo is None:
            errors.append(f"{port}: node did not send its configuration")
            close(iface)
            iface = None
            continue
        using = port
        break
    if iface is None:
        emit(ev="status", connected=False, error="; ".join(errors) or f"none of {args.port} is present")
        return 2
    b = Bridge(iface, keys, link)
    pub.subscribe(b.on_receive, "meshtastic.receive")
    pub.subscribe(b.on_node, "meshtastic.node.updated")
    pub.subscribe(b.on_lost, "meshtastic.connection.lost")
    emit(ev="status", connected=True, error=None)
    if "," in args.port:              # a list: on a later port, go back to an earlier cable when it returns
        threading.Thread(target=b.cable_loop, args=(args.port, using), daemon=True).start()
    b.info()
    b.config()
    if args.feed:
        b.feed = start_feed(args, iface, b)
    threading.Thread(target=b.nodes_loop, daemon=True).start()

    def stdin_loop():
        for line in sys.stdin:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            cmd = m.get("cmd")
            try:
                if cmd == "send":
                    b.send(m)
                elif cmd == "set":
                    b.set_config(m)
                elif cmd == "favorite":
                    b.favorite(m)
                elif cmd == "trace":
                    b.trace(m)
                elif cmd == "request":
                    b.request(m)
                elif cmd == "node":
                    b.node_action(m)
                elif cmd == "reboot":
                    b.reboot(m)
            except (KeyError, TypeError, ValueError) as e:
                emit(ev="done", ref=m.get("ref"), ok=False, error=f"bad command: {e}")
        b.closing = True              # server closed our stdin: exit
        b.lost.set()

    threading.Thread(target=stdin_loop, daemon=True).start()
    b.lost.wait()
    if b.feed is not None:
        b.feed.close()
    close(iface)
    _out.flush()
    os._exit(1)                   # the BLE library's own threads must not hold the exit


if __name__ == "__main__":
    sys.exit(main())
