#!/usr/bin/env python3
"""Meshtastic node <-> Radio Remote, as JSON lines on stdin/stdout.

Runs as a child of server.py (mesh.py), under the Python that has the
`meshtastic` package (its own venv; the server's Python doesn't need it).
Keeping the library in its own process means a stuck or crashing serial
link never takes the radio server with it: this process exits and the
server starts it again.

    mesh_bridge.py /dev/meshtastic [--key NAME=PATH ...]
    mesh_bridge.py ble:NODEM_7720 [--key NAME=PATH ...]   (Bluetooth: name or address)
    mesh_bridge.py /dev/meshtastic,ble:NODEA_fd78,ble:NODEB_2560   (first that answers)

Bluetooth needs the node bonded to this host once (its PIN) and the node's
single BLE connection free: while a phone app holds it, this bridge can't
connect, and while this bridge holds it, the phone can't.

Out (stdout, one JSON object per line):
    {"ev": "status", "connected": bool, "error": str|None}
    {"ev": "info", "me": {...}, "channels": [{"index", "name", "role", "key_ok"}]}
    {"ev": "nodes", "nodes": [...]}               (on change, at most every NODES_EVERY_S)
    {"ev": "msg", "id", "from", "to", "ch", "dm", "text", "snr", "rssi", "hops"}
    {"ev": "sent", "ref", "id"} / {"ev": "send_failed", "ref", "error"}
    {"ev": "ack", "id", "ok", "by", "reason"}
    {"ev": "config", "sections": [...], "values": {section: {field: value}}}
    {"ev": "done", "ref", "ok", "error"}         (a set or favorite was written, or not)
    {"ev": "rx", "from": "!xxxxxxxx", "port": str}  (any packet heard: activity on the map)
    {"ev": "trace", "ref", "to", "ok", "error", "route", "snr_towards", "route_back", "snr_back"}
In (stdin):
    {"cmd": "send", "ref": n, "text": str, "ch": int, "to": "!xxxxxxxx"|null}
    {"cmd": "set", "ref": n, "section": str, "values": {field: value}}
    {"cmd": "favorite", "ref": n, "to": "!xxxxxxxx", "fav": bool}
    {"cmd": "trace", "ref": n, "to": "!xxxxxxxx"}

--key NAME=PATH: the channel called NAME should use the base64 PSK in PATH.
The key is compared here and only the verdict (key_ok) leaves this process;
no PSK is ever written to stdout or the log.

Settings: only the fields in SETTINGS are read out or written, so the
node's private and admin keys, channel PSKs, Wi-Fi and MQTT passwords never
leave this process, and nothing here can turn off the link the bridge
itself uses (device.serial_enabled, security.*; over Bluetooth, the whole
Bluetooth section).
"""

import argparse
import base64
import json
import logging
import os
import sys
import threading

log = logging.getLogger("mesh_bridge")

NODES_EVERY_S = 5.0
TRACE_TIMEOUT_S = 90.0        # a traceroute over several hops on LongFast can take a minute
UNKNOWN_SNR = -128            # RouteDiscovery's "no SNR for this hop"
NODE_FIELDS_USER = ("longName", "shortName", "hwModel", "role")
CLOSE_WAIT_S = 3.0               # longest we wait for the node link to close
ROLES = {0: "DISABLED", 1: "PRIMARY", 2: "SECONDARY"}

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


class Bridge:
    def __init__(self, iface, keys, link="serial"):
        self.iface = iface
        self.keys = keys
        self.link = link              # "serial" or "ble": the link set_config must not cut
        self.my_num = iface.myInfo.my_node_num
        self.pending = set()          # packet ids we sent with wantAck
        self.nodes_dirty = True
        self.lost = threading.Event()
        self.closing = False

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
             channels=chans, preset=preset_name(preset),
             missing=[n for n in self.keys
                      if not any(c.settings.name == n for c in node.channels or [])])

    def nodes(self):
        out = []
        for n in list((self.iface.nodesByNum or {}).values()):
            u = n.get("user", {})
            pos = n.get("position", {})
            dm = n.get("deviceMetrics", {})
            out.append({
                "num": n.get("num"), "id": u.get("id"),
                **{k: u.get(k) for k in NODE_FIELDS_USER},
                "last_heard": n.get("lastHeard"), "snr": n.get("snr"),
                "hops": n.get("hopsAway"), "battery": dm.get("batteryLevel"),
                "lat": pos.get("latitude"), "lon": pos.get("longitude"),
                "alt": pos.get("altitude"), "voltage": dm.get("voltage"),
                "util": dm.get("channelUtilization"), "mqtt": bool(n.get("viaMqtt")),
                "fav": bool(n.get("isFavorite")), "me": n.get("num") == self.my_num,
            })
        emit(ev="nodes", nodes=out)

    def on_receive(self, packet, interface):
        d = packet.get("decoded") or {}
        port = d.get("portnum")
        if packet.get("from") != self.my_num and packet.get("fromId"):
            emit(ev="rx", **{"from": packet["fromId"]}, port=port or "ENCRYPTED")
        if port == "TEXT_MESSAGE_APP":
            to = packet.get("to")
            hops = None
            if "hopStart" in packet and "hopLimit" in packet:
                hops = packet["hopStart"] - packet["hopLimit"]
            emit(ev="msg", id=packet.get("id"), **{"from": packet.get("fromId")},
                 to=packet.get("toId"), ch=packet.get("channel", 0), dm=to == self.my_num,
                 text=d.get("text", ""), snr=packet.get("rxSnr"), rssi=packet.get("rxRssi"),
                 hops=hops)
        elif port == "ROUTING_APP":
            rid = d.get("requestId")
            if rid in self.pending:
                reason = (d.get("routing") or {}).get("errorReason", "NONE")
                self.pending.discard(rid)
                # From ourselves = our node heard a neighbour rebroadcast it
                # (implicit ack); from the destination = delivered.
                emit(ev="ack", id=rid, ok=reason == "NONE", reason=reason,
                     by=packet.get("fromId"), relayed=packet.get("from") == self.my_num)

    def on_node(self, node, interface):
        self.nodes_dirty = True

    def on_lost(self, interface):
        if self.closing:
            return
        emit(ev="status", connected=False, error="connection to the node lost")
        self.lost.set()

    def nodes_loop(self):
        while not self.lost.is_set():
            if self.nodes_dirty:
                self.nodes_dirty = False
                try:
                    self.nodes()
                except Exception:
                    log.exception("node list")
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

    def send(self, m):
        from meshtastic import BROADCAST_ADDR
        ref = m.get("ref")
        try:
            pkt = self.iface.sendText(str(m["text"]), destinationId=m.get("to") or BROADCAST_ADDR,
                                      wantAck=True, channelIndex=int(m.get("ch", 0)))
        except Exception as e:
            emit(ev="send_failed", ref=ref, error=str(e))
            return
        self.pending.add(pkt.id)
        emit(ev="sent", ref=ref, id=pkt.id)

    def set_config(self, m):
        """Write one section's changed fields. The server has checked them
        against the schema; the protobufs check them again here. The node
        restarts after some changes (LoRa, role): the link drops and the
        server starts this bridge again."""
        ref, section, values = m.get("ref"), m["section"], m["values"]
        spec = next((s for s in SETTINGS if s[0] == section), None)
        try:
            if spec is None or not set(values) <= {f[0] for f in spec[3]}:
                raise ValueError(f"not a setting: {section} {sorted(values)}")
            if self.link == "ble" and section == "bluetooth":
                raise ValueError("this bridge reaches the node over Bluetooth; "
                                 "change Bluetooth settings over USB or the phone app")
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
                try:
                    for name, v in values.items():
                        d = msg.DESCRIPTOR.fields_by_name[name]
                        if d.type == 14:
                            v = d.enum_type.values_by_name[v].number
                        setattr(msg, name, v)
                    node.writeConfig(section)
                except BaseException:
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


def candidates(spec):
    """'/dev/meshtastic,ble:NODEA_fd78,ble:NODEB_2560' -> the ports to try, in
    order. A serial device that isn't there is skipped without a try, so a
    laptop uses its node on the cable when plugged in and Bluetooth when not."""
    out = []
    for p in (x.strip() for x in spec.split(",")):
        if p and (p.startswith("ble:") or os.path.exists(p)):
            out.append(p)
    return out


def connect(port):
    """Open one port: ('ble:NAME' or a serial device) -> (interface, link)."""
    if port.startswith("ble:"):
        from meshtastic.ble_interface import BLEInterface
        return BLEInterface(port[4:] or None), "ble"
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


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("port", help="serial device of the node, e.g. /dev/meshtastic, "
                                "or ble:NAME_OR_ADDRESS for Bluetooth, e.g. ble:NODEM_7720; "
                                "comma-separate several to try in order")
    p.add_argument("--key", action="append", default=[], metavar="NAME=PATH",
                   help="check that channel NAME uses the base64 PSK in PATH")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, stream=sys.stderr,
                        format="mesh_bridge %(levelname)s %(message)s")

    from pubsub import pub
    keys = read_keys(args.key)
    iface, link, errors = None, None, []
    for port in candidates(args.port):        # first node that answers wins
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
        break
    if iface is None:
        emit(ev="status", connected=False, error="; ".join(errors) or f"none of {args.port} is present")
        return 2
    b = Bridge(iface, keys, link)
    pub.subscribe(b.on_receive, "meshtastic.receive")
    pub.subscribe(b.on_node, "meshtastic.node.updated")
    pub.subscribe(b.on_lost, "meshtastic.connection.lost")
    emit(ev="status", connected=True, error=None)
    b.info()
    b.config()
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
            except (KeyError, TypeError, ValueError) as e:
                emit(ev="done", ref=m.get("ref"), ok=False, error=f"bad command: {e}")
        b.closing = True              # server closed our stdin: exit
        b.lost.set()

    threading.Thread(target=stdin_loop, daemon=True).start()
    b.lost.wait()
    close(iface)
    _out.flush()
    os._exit(1)                   # the BLE library's own threads must not hold the exit


if __name__ == "__main__":
    sys.exit(main())
