#!/usr/bin/env python3
"""MeshCore companion node <-> Radio Remote, as JSON lines on stdin/stdout.

Runs as a child of server.py (addons/meshcore.py), under the Python that has
the `meshcore` package (its own venv; the server's Python doesn't need it).
A stuck or crashing serial link never takes the radio server with it: this
process exits and the add-on starts it again.

    meshcore_bridge.py /dev/ttyUSB0            (USB: companion_radio_usb firmware)
    meshcore_bridge.py tcp:192.168.1.50:5000   (Wi-Fi: a companion with Wi-Fi on, port 5000)
    meshcore_bridge.py ble:NODE                (Bluetooth: MeshCore-NODE, bonded once)
    meshcore_bridge.py tcp:192.168.1.50:5000,ble:NODE   (the first that answers)

Bluetooth needs the node paired with this host once (the PIN on its
screen; macOS asks for it), and its one Bluetooth link free: while a phone
holds it this bridge can't connect, and the other way round. A serial
device that isn't plugged in is skipped without a try.

The node must run MeshCore's companion firmware: a repeater or room server
answers a different protocol. Over Wi-Fi the companion port has no login,
so anything on the LAN can drive the node; keep it on a network you trust.
A link that stops answering (Wi-Fi dropped, node rebooted) is noticed by
the battery read every BATTERY_EVERY_S failing LINK_FAILS times running:
the bridge exits and the add-on starts it again.

Out (stdout, one JSON object per line):
    {"ev": "status", "connected": bool, "error": str|None}
    {"ev": "info", "me": {...}, "device": {...}, "radio": {...}}
    {"ev": "channels", "channels": [{"index", "name"}]}
    {"ev": "contacts", "contacts": [{"key", "name", "type", "hops", "last_advert", "lat", "lon"}]}
    {"ev": "battery", "mv": int}
    {"ev": "msg", "from", "name", "ch", "dm", "text", "snr", "hops", "sent_t"}
    {"ev": "sent", "ref", "ack"} / {"ev": "send_failed", "ref", "error"}
    {"ev": "ack", "ack"}                       (a direct message was delivered)
    {"ev": "advert", "key"}                    (a node advertised: activity)
    {"ev": "done", "ref", "ok", "error", "reboot"}  (an advert or a settings write)
    {"ev": "config", "link", "sections": [...], "values": {...}, "wifi": {...}|None}
    {"ev": "delivered", "ref", "attempts", "flood", "trip_ms"}  (a direct message's ACK came back)
    {"ev": "stats", "noise_floor", "last_rssi", "last_snr", "tx_air_secs", "rx_air_secs",
     "recv", "sent", "flood_tx", "direct_tx", "flood_rx", "direct_rx", "recv_errors",
     "uptime_secs", "queue_len"}             (the node's own radio counters, every STATS_EVERY_S)
    {"ev": "rx", "snr", "rssi", "type", "route", "hops", "last"}  (a packet heard on the air;
                                               last = the hash of the repeater it came from)
    {"ev": "pending", "contacts": [...]}       (heard, not added: "Add contacts by hand" is on)
    {"ev": "result", "ref", "kind", "key", "ok", "data"|"error"}  (a request to another node)
    {"ev": "cli_reply", "key", "text"}         (a repeater's answer to a remote command)
    {"ev": "notice", "text", "level"}
In (stdin):
    {"cmd": "send", "ref": n, "text": str, "ch": int}            (a channel)
    {"cmd": "send", "ref": n, "text": str, "to": "<key prefix>"} (a direct message)
    {"cmd": "advert", "ref": n, "flood": bool}
    {"cmd": "set", "ref": n, "section": str, "values": {field: value}}
    {"cmd": "reboot", "ref": n}
    {"cmd": KIND, "ref": n, "key": "<key>"}    KIND: status, telemetry, neighbours, owner,
                                               path (discovery), trace, reset_path, remove,
                                               fav (+ "fav": bool), add (a pending contact),
                                               clock (set a repeater's clock to ours)
    {"cmd": "login", "ref": n, "key", "password"}  (never logged or emitted)
    {"cmd": "cli", "ref": n, "key", "text"}    (a remote command; needs an admin login)
    {"cmd": "channel_join", "ref": n, "name": "#name"} / {"cmd": "channel_leave", "ref": n, "index": i}

A direct message is retried (MAX_TRIES, flooded from the FLOOD_AFTER-th try)
until its ACK comes back. Requests to other nodes run in the background:
sending a message never waits behind a status request. Every exchange with
the node itself is serialised (the library has no lock of its own).

Settings: only the fields in SETTINGS are read out or written. The Wi-Fi
password is write-only: it goes to the node and is never read back, logged
or emitted. Wi-Fi and Bluetooth changes take effect at the node's next
reboot ("done" says so with reboot: true). What never leaves this process:
the node's private key, channel secrets and the Wi-Fi password. On connect
the bridge sets the node's clock from this host's (MeshCore stamps every
message with it).
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time

log = logging.getLogger("meshcore_bridge")

CONTACTS_EVERY_S = 5.0        # re-read the contact list at most this often after an advert
BATTERY_EVERY_S = 60.0
CHANNELS_MAX = 8              # channels read from the node (it has up to 40; apps show the first few)
CONNECT_TIMEOUT_S = 15.0
BLE_CONNECT_TIMEOUT_S = 40.0  # a scan, then the connect (and the pairing prompt the first time)
LINK_FAILS = 2                # battery reads failing in a row before the link counts as lost
STATS_EVERY_S = 60.0
MAX_TRIES = 3                 # a direct message: attempts before it counts as not delivered
FLOOD_AFTER = 2               # from this attempt on, forget the route and flood
ACK_WAIT_MIN_S = 6.0
ANSWER_MIN_S, ANSWER_MAX_S = 8.0, 60.0   # waiting for another node's answer
FAV = 0x01                    # contact flag: favourite
TXT_CLI = 1                   # a text message that is a command reply
TYPES = {1: "chat", 2: "repeater", 3: "room", 4: "sensor"}
REQUESTS = ("status", "telemetry", "neighbours", "owner", "login", "path", "trace", "reset_path",
            "remove", "fav", "add", "cli", "clock")
STATUS_FIELDS = ("bat", "uptime", "airtime", "noise_floor", "last_rssi", "last_snr", "nb_recv", "nb_sent",
                 "sent_flood", "sent_direct", "recv_flood", "recv_direct", "direct_dups", "flood_dups",
                 "tx_queue_len", "full_evts")
BANDWIDTHS = [7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125.0, 250.0, 500.0]
OFF_MESH = "Every node on the mesh must use the same value, or this one can't hear it."
WIFI_CUT = "Takes effect at the next reboot; a wrong value takes the node off Wi-Fi."

# (section, label, fields); a field is (name, label, type, extra). extra may
# hold min, max, max_bytes, options, unit, hint, write_only and risky (why a
# change can cut the node off; the page asks first). "wifi" and "bluetooth"
# appear only when the firmware has Wi-Fi (the ESP32 Bluetooth and Wi-Fi builds).
SETTINGS = (
    ("node", "Node", (
        ("name", "Name", "str", {"max_bytes": 31}),
        ("lat", "Latitude", "float", {"min": -90, "max": 90}),
        ("lon", "Longitude", "float", {"min": -180, "max": 180}),
        ("share_location", "Share location in adverts", "bool", {}),
        ("manual_add_contacts", "Add contacts by hand", "bool", {"hint": "Off: every node heard is added"}),
        ("multi_acks", "Extra ACKs", "bool", {"hint": "Send direct-message ACKs twice"}),
    )),
    ("radio", "Radio", (
        ("freq", "Frequency", "float", {"unit": "MHz", "min": 150, "max": 960, "risky": OFF_MESH}),
        ("bw", "Bandwidth", "enum", {"unit": "kHz", "options": BANDWIDTHS, "risky": OFF_MESH}),
        ("sf", "Spreading factor", "int", {"min": 5, "max": 12, "risky": OFF_MESH}),
        ("cr", "Coding rate", "int", {"min": 5, "max": 8, "risky": OFF_MESH}),
        ("tx_power", "TX power", "int", {"unit": "dBm", "min": 1, "max": 30}),
    )),
    ("wifi", "Wi-Fi", (
        ("enabled", "Wi-Fi", "bool", {"risky": WIFI_CUT}),
        ("ssid", "Network", "str", {"max_bytes": 32, "risky": WIFI_CUT}),
        ("password", "Password", "str", {"max_bytes": 63, "write_only": True, "risky": WIFI_CUT,
                                         "hint": "Not shown or read back; leave empty to keep it"}),
    )),
    ("bluetooth", "Bluetooth", (
        ("pin", "PIN", "int", {"min": 0, "max": 999999, "write_only": True,
                               "hint": "6 digits (100000-999999), or 0 for a random PIN on the screen at each boot. "
                                       "Not shown or read back; leave empty to keep it",
                               "risky": "A paired phone or host must pair again with the new PIN."}),
    )),
)

# The library logs to stdout in places: the protocol gets its own copy of
# fd 1, and everything else written to stdout goes to stderr (the server log).
_out = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr


def emit(**d):
    _out.write(json.dumps(d) + "\n")


def parse_port(spec):
    """'/dev/ttyUSB0' -> ("serial", "/dev/ttyUSB0"); 'tcp:HOST:PORT' -> ("tcp", HOST, PORT);
    'ble:NAME' -> ("ble", NAME) (a name, part of one, or an address)."""
    spec = spec.strip()
    if spec.startswith("tcp:"):
        host, sep, port = spec[4:].rpartition(":")
        if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError(f"not tcp:HOST:PORT: {spec!r}")
        return ("tcp", host.strip("[]"), int(port))
    if spec.startswith("ble:"):
        if not spec[4:]:
            raise ValueError(f"not ble:NAME: {spec!r}")
        return ("ble", spec[4:])
    if not spec:
        raise ValueError("no port")
    return ("serial", spec)


def candidates(spec):
    """'tcp:H:5000,ble:NODE,/dev/ttyUSB0' -> the links to try, in order. A serial
    device that isn't there is left out, so a node on the cable is used when
    plugged in and the others when not."""
    out = [parse_port(p) for p in spec.split(",") if p.strip()]
    if not out:
        raise ValueError("no port")
    return [l for l in out if l[0] != "serial" or os.path.exists(l[1])]


def link_name(link):
    return {"tcp": lambda l: f"tcp:{l[1]}:{l[2]}", "ble": lambda l: f"ble:{l[1]}",
            "serial": lambda l: l[1]}[link[0]](link)


def hops(path_len):
    """MeshCore's path length: 255 / -1 = direct (heard first hand, or no route
    known yet: flood), else the number of repeaters on the way."""
    return None if path_len is None or path_len < 0 or path_len == 255 else path_len


def split_path(hexpath, size):
    """'a1b2c3', 1 -> ['a1', 'b2', 'c3']: the repeaters on a route, by key hash."""
    step = 2 * max(1, size or 1)
    return [hexpath[i:i + step] for i in range(0, len(hexpath or ""), step)]


def route_len(c):
    """Repeaters on the known route to C: 0 = direct, -1 = none known (flood)."""
    n = c.get("out_path_len")
    return -1 if n is None or n < 0 or n == 255 else n


class Locked:
    """The library's commands, each one holding the bridge's lock while it
    talks to the node."""

    def __init__(self, commands, lock):
        self._c, self._lock = commands, lock

    def __getattr__(self, name):
        fn = getattr(self._c, name)

        async def call(*a, **k):
            async with self._lock:
                return await fn(*a, **k)
        return call


def contact(c):
    lat, lon = c.get("adv_lat"), c.get("adv_lon")
    size = (c.get("out_path_hash_mode") or 0) + 1
    return {"key": c.get("public_key"), "name": c.get("adv_name") or "",
            "type": TYPES.get(c.get("type"), str(c.get("type"))),
            "hops": hops(c.get("out_path_len")),
            "path": split_path(c.get("out_path") or "", size) if route_len(c) > 0 else [],
            "fav": bool((c.get("flags") or 0) & FAV),
            "last_advert": c.get("last_advert") or None,
            "lat": lat if lat or lon else None, "lon": lon if lat or lon else None}


class Bridge:
    def __init__(self, mc, EventType, link=("serial", "")):
        self.mc = mc
        self.E = EventType
        self.link = link
        self.has_wifi = False
        self.device = {}
        self.contacts = {}
        self.contacts_dirty = False
        self.closing = False
        self.pending = {}             # key -> a contact heard but not added (manual add mode)
        self.lock = asyncio.Lock()    # one exchange with the node at a time
        self.acks = {}                # expected ACK code -> asyncio.Event, for retried messages
        self.ack_trip = {}            # ACK code -> its round trip (ms)
        self.tasks = set()
        self.channel_slots = CHANNELS_MAX
        self.channel_names = {}
        self.R = None                 # (BinaryReqType, AnonReqType), set by run()

    async def hello(self):
        await self.mc.commands.set_time(int(time.time()))
        si = dict(self.mc.self_info or {})
        dev = await self.mc.commands.send_device_query()
        d = self.device = dev.payload if dev.type == self.E.DEVICE_INFO else {}
        self.emit_info()
        self.channel_slots = min(CHANNELS_MAX, d.get("max_channels") or CHANNELS_MAX)
        await self.read_channels()
        await self.config()
        await self.read_contacts()
        await self.battery()
        await self.stats()

    async def read_channels(self):
        chans = []
        for i in range(self.channel_slots):
            async with self.lock:
                r = await self.mc.commands.get_channel(i)
            if r.type != self.E.CHANNEL_INFO:
                break
            name = (r.payload or {}).get("channel_name") or ""
            if name:                              # an unnamed slot is unused
                chans.append({"index": i, "name": name})
        self.channel_names = {c["index"]: c["name"] for c in chans}
        emit(ev="channels", channels=chans)
        return chans

    def emit_info(self):
        si, d = dict(self.mc.self_info or {}), self.device
        emit(ev="info",
             me={"key": si.get("public_key"), "name": si.get("name"),
                 "lat": si.get("adv_lat") or None, "lon": si.get("adv_lon") or None,
                 "share_loc": bool(si.get("adv_loc_policy"))},
             device={"model": d.get("model"), "fw": d.get("ver"), "build": d.get("fw_build"),
                     "max_contacts": d.get("max_contacts")},
             radio={"freq": si.get("radio_freq"), "bw": si.get("radio_bw"), "sf": si.get("radio_sf"),
                    "cr": si.get("radio_cr"), "tx_power": si.get("tx_power")},
             link=link_name(self.link))

    async def cli(self, line):
        """A firmware CLI command's reply text ('> ...' stripped), or None."""
        async with self.lock:
            r = await self.mc.commands.run_cli_command(line)
        if r.type != self.E.CLI_REPLY:
            return None
        text = ((r.payload or {}).get("text") or "").strip()
        return text[1:].strip() if text.startswith(">") else text

    async def wifi_state(self):
        """{"ssid", "enabled", "status", "ip"} from the firmware, or None when it has no Wi-Fi."""
        ssid = await self.cli("get wifi.ssid")
        enabled = await self.cli("get wifi.enabled")
        if enabled not in ("0", "1"):             # no such command: a build without Wi-Fi
            return None
        status = await self.cli("get wifi.status")
        ip = await self.cli("get wifi.ip") if status == "connected" else None
        return {"ssid": "" if ssid in (None, "(not set)") else ssid, "enabled": enabled == "1",
                "status": status, "ip": ip if ip and ip[0].isdigit() else None}

    async def config(self):
        """SETTINGS and the node's values (never the Wi-Fi password)."""
        si = dict(self.mc.self_info or {})
        wifi = await self.wifi_state()
        self.has_wifi = wifi is not None
        values = {
            "node": {"name": si.get("name") or "", "lat": round(si.get("adv_lat") or 0.0, 6),
                     "lon": round(si.get("adv_lon") or 0.0, 6),
                     "share_location": bool(si.get("adv_loc_policy")),
                     "manual_add_contacts": bool(si.get("manual_add_contacts")),
                     "multi_acks": bool(si.get("multi_acks"))},
            "radio": {"freq": si.get("radio_freq"), "bw": si.get("radio_bw"), "sf": si.get("radio_sf"),
                      "cr": si.get("radio_cr"), "tx_power": si.get("tx_power")},
        }
        if wifi is not None:
            values["wifi"] = {"enabled": wifi["enabled"], "ssid": wifi["ssid"], "password": ""}
            values["bluetooth"] = {"pin": ""}       # write-only: the pairing PIN never leaves the node
        sections = []
        for name, label, fields in SETTINGS:
            if name not in values:
                continue
            out = []
            for fname, flabel, ftype, extra in fields:
                f = {"name": fname, "label": flabel, "type": ftype, **extra}
                if name == "radio" and fname == "tx_power" and si.get("max_tx_power"):
                    f["max"] = si["max_tx_power"]
                out.append(f)
            sections.append({"name": name, "label": label, "fields": out})
        emit(ev="config", link=link_name(self.link), sections=sections, values=values, wifi=wifi and
             {k: wifi[k] for k in ("status", "ip")}, pin_fixed=bool(self.device.get("ble_pin")))

    async def set_config(self, m):
        """Write one section's fields (the server has checked them against
        SETTINGS), then read the node back. Wi-Fi and the Bluetooth PIN take
        effect at the next reboot."""
        ref, section, values = m.get("ref"), m["section"], dict(m["values"])
        spec = next((s for s in SETTINGS if s[0] == section), None)
        si = dict(self.mc.self_info or {})
        reboot = False
        c = Locked(self.mc.commands, self.lock)

        def ok(r, what):
            if r is None or r.type == self.E.ERROR:
                raise RuntimeError(f"the node refused {what}")

        try:
            if spec is None or not set(values) <= {f[0] for f in spec[2]}:
                raise ValueError(f"not a setting: {section} {sorted(values)}")
            if section in ("wifi", "bluetooth") and not self.has_wifi:
                raise ValueError("this firmware has no Wi-Fi or Bluetooth settings")
            if section == "node":
                if "name" in values:
                    ok(await c.set_name(str(values["name"])), "the name")
                if "lat" in values or "lon" in values:
                    ok(await c.set_coords(float(values.get("lat", si.get("adv_lat") or 0.0)),
                                          float(values.get("lon", si.get("adv_lon") or 0.0))), "the location")
                if "share_location" in values:
                    ok(await c.set_advert_loc_policy(1 if values["share_location"] else 0), "location sharing")
                if "manual_add_contacts" in values:
                    ok(await c.set_manual_add_contacts(bool(values["manual_add_contacts"])), "contact adding")
                if "multi_acks" in values:
                    ok(await c.set_multi_acks(1 if values["multi_acks"] else 0), "extra ACKs")
            elif section == "radio":
                if set(values) & {"freq", "bw", "sf", "cr"}:
                    ok(await c.set_radio(float(values.get("freq", si.get("radio_freq"))),
                                         float(values.get("bw", si.get("radio_bw"))),
                                         int(values.get("sf", si.get("radio_sf"))),
                                         int(values.get("cr", si.get("radio_cr")))), "the radio settings")
                if "tx_power" in values:
                    ok(await c.set_tx_power(int(values["tx_power"])), "the TX power")
            elif section == "wifi":
                if values.get("ssid") is not None and "ssid" in values:
                    reply = await self.cli(f"set wifi.ssid {values['ssid']}")
                    if not reply or "wifi.ssid" not in reply:
                        raise RuntimeError("the node refused the Wi-Fi network name")
                if values.get("password"):
                    reply = await self.cli(f"set wifi.pwd {values['password']}")
                    if not reply or "wifi.pwd" not in reply:
                        raise RuntimeError("the node refused the Wi-Fi password")
                if "enabled" in values:
                    reply = await self.cli(f"set wifi.enabled {1 if values['enabled'] else 0}")
                    if not reply or "wifi.enabled" not in reply:
                        raise RuntimeError("the node refused Wi-Fi on/off")
                reboot = True
            elif section == "bluetooth":
                ok(await c.set_devicepin(int(values["pin"])), "the PIN")
                reboot = True
        except Exception as e:
            values.pop("password", None)
            emit(ev="done", ref=ref, ok=False, error=f"{type(e).__name__}: {e}")
            return
        values.pop("password", None)
        await c.send_appstart()                   # read the node's settings back
        emit(ev="done", ref=ref, ok=True, error=None, reboot=reboot)
        self.emit_info()
        await self.config()

    async def reboot(self, m):
        emit(ev="done", ref=m.get("ref"), ok=True, error=None, rebooting=True)
        try:
            async with self.lock:
                await self.mc.commands.reboot()
        except Exception as e:
            log.info("reboot: %s: %s", type(e).__name__, e)

    async def read_contacts(self):
        async with self.lock:
            r = await self.mc.commands.get_contacts()
        if r.type == self.E.CONTACTS:
            self.contacts = dict(r.payload or {})
        emit(ev="contacts", contacts=sorted((contact(c) for c in self.contacts.values()),
                                            key=lambda c: -(c["last_advert"] or 0)))

    async def battery(self):
        """Read the battery; False when the node didn't answer."""
        async with self.lock:
            r = await self.mc.commands.get_bat()
        if r.type == self.E.BATTERY:
            emit(ev="battery", mv=(r.payload or {}).get("level"))
            return True
        return False

    async def stats(self):
        """The node's own radio counters (noise floor, airtime, packets); firmware
        without them sends nothing."""
        out = {}
        for fn, want in (("get_stats_core", "STATS_CORE"), ("get_stats_radio", "STATS_RADIO"),
                         ("get_stats_packets", "STATS_PACKETS")):
            try:
                async with self.lock:
                    r = await getattr(self.mc.commands, fn)()
            except Exception as e:                # an old library or firmware
                log.debug("%s: %s", fn, e)
                continue
            if r.type == getattr(self.E, want, None):
                out.update(r.payload or {})
        if out:
            out.pop("battery_mv", None)           # the battery read has it
            out.pop("errors", None)
            emit(ev="stats", **{k: v for k, v in out.items() if isinstance(v, (int, float)) or v is None})

    def find(self, key):
        """The library's contact for a full key or a key prefix, or None."""
        key = str(key or "").lower()
        if not key:
            return None
        return self.contacts.get(key) or next((c for k, c in self.contacts.items() if k.startswith(key)), None)

    def name_for(self, prefix):
        for k, c in self.contacts.items():
            if prefix and k.startswith(prefix):
                return c.get("adv_name")
        return None

    # --- node -> server

    async def on_msg(self, event):
        m = event.payload or {}
        dm = m.get("type") == "PRIV"
        text, name, frm = m.get("text") or "", None, None
        if dm and m.get("txt_type") == TXT_CLI:   # a repeater answering a remote command
            emit(ev="cli_reply", key=m.get("pubkey_prefix"), text=text)
            return
        if dm:
            frm = m.get("pubkey_prefix")
            name = self.name_for(frm)
        elif ": " in text:                       # channel text arrives as "Name: message"
            name, text = text.split(": ", 1)
        emit(ev="msg", **{"from": frm}, name=name, ch=None if dm else m.get("channel_idx"), dm=dm,
             text=text, snr=m.get("SNR"), hops=hops(m.get("path_len")), sent_t=m.get("sender_timestamp"))

    async def on_advert(self, event):
        emit(ev="advert", key=(event.payload or {}).get("public_key"))
        self.contacts_dirty = True

    async def on_ack(self, event):
        p = event.payload or {}
        code = p.get("code")
        waiter = self.acks.get(code)
        if waiter is not None:                    # a retried direct message: send_dm settles it
            self.ack_trip[code] = p.get("trip_time")
            waiter.set()
            return
        emit(ev="ack", ack=code)

    async def on_rx(self, event):
        p = event.payload or {}
        path = split_path(p.get("path") or "", p.get("path_hash_size") or 1)
        emit(ev="rx", snr=p.get("snr"), rssi=p.get("rssi"), type=p.get("payload_typename"),
             route=p.get("route_typename"), hops=len(path), last=path[-1] if path else None)

    async def on_new_contact(self, event):
        c = event.payload or {}
        if c.get("public_key") and c["public_key"] not in self.contacts:
            self.pending[c["public_key"]] = c
            emit(ev="pending", contacts=[contact(x) for x in self.pending.values()])

    async def on_contact_deleted(self, event):
        self.contacts_dirty = True

    async def on_contacts_full(self, event):
        emit(ev="notice", level="warn", text="The node's contact list is full: remove nodes you don't need, "
                                             "or new ones are not added.")

    async def on_disconnect(self, event):
        if not self.closing:
            emit(ev="status", connected=False, error="connection to the node lost")
            self.closing = True

    async def loops(self):
        last_bat, fails = time.time(), 0
        while not self.closing:
            await asyncio.sleep(CONTACTS_EVERY_S)
            try:
                if self.contacts_dirty:
                    self.contacts_dirty = False
                    await self.read_contacts()
                if time.time() - last_bat >= BATTERY_EVERY_S:
                    last_bat = time.time()
                    ok = await self.battery()
                    fails = 0 if ok else fails + 1
                    if ok:
                        await self.stats()
            except Exception as e:
                fails += 1
                log.warning("periodic read: %s: %s", type(e).__name__, e)
            if fails >= LINK_FAILS:
                emit(ev="status", connected=False, error="the node stopped answering")
                self.closing = True

    # --- server -> node

    async def send(self, m):
        ref, text = m.get("ref"), str(m["text"])
        if m.get("to"):
            c = self.find(m["to"])
            if c is None:
                emit(ev="send_failed", ref=ref, error="not a contact of this node")
                return
            self.spawn(self.send_dm(ref, c, text))
            return
        try:
            async with self.lock:
                r = await self.mc.commands.send_chan_msg(int(m["ch"]), text)
        except Exception as e:
            emit(ev="send_failed", ref=ref, error=f"{type(e).__name__}: {e}")
            return
        if r.type == self.E.ERROR:
            emit(ev="send_failed", ref=ref, error=str(r.payload))
            return
        ack = (r.payload or {}).get("expected_ack") if isinstance(r.payload, dict) else None
        emit(ev="sent", ref=ref, ack=ack.hex() if isinstance(ack, bytes) else ack)

    async def advert(self, m):
        ref = m.get("ref")
        try:
            async with self.lock:
                r = await self.mc.commands.send_advert(flood=bool(m.get("flood")))
            ok = r.type != self.E.ERROR
            emit(ev="done", ref=ref, ok=ok, error=None if ok else str(r.payload))
        except Exception as e:
            emit(ev="done", ref=ref, ok=False, error=f"{type(e).__name__}: {e}")

    async def send_dm(self, ref, c, text):
        """Send, wait for the ACK, try again; from the FLOOD_AFTER-th try the
        route is forgotten and the message flooded. Every try keeps its ACK
        code, so a late ACK for an earlier one still counts."""
        done, codes, ts, flood, why = asyncio.Event(), [], int(time.time()), False, "no ACK"
        try:
            for attempt in range(MAX_TRIES):
                if attempt == FLOOD_AFTER and route_len(c) >= 0:
                    async with self.lock:
                        await self.mc.commands.reset_path(c)
                    c["out_path_len"], c["out_path"] = -1, ""
                    self.contacts_dirty = True
                async with self.lock:
                    r = await self.mc.commands.send_msg(c, text, ts, attempt=attempt)
                if r.type == self.E.ERROR:
                    why = f"the node refused it: {r.payload}"
                    continue
                flood = (r.payload or {}).get("type") == 1
                code = r.payload["expected_ack"]
                code = code.hex() if isinstance(code, bytes) else str(code)
                codes.append(code)
                self.acks[code] = done
                if attempt == 0:
                    emit(ev="sent", ref=ref, ack=None, flood=flood)
                wait = max(ACK_WAIT_MIN_S, (r.payload.get("suggested_timeout") or 0) / 1000 * 1.2)
                try:
                    await asyncio.wait_for(done.wait(), wait)
                except TimeoutError:
                    continue
                trip = next((self.ack_trip.pop(k) for k in codes if k in self.ack_trip), None)
                emit(ev="delivered", ref=ref, attempts=attempt + 1, flood=flood, trip_ms=trip)
                return
            if not codes:
                emit(ev="send_failed", ref=ref, error=why)
            else:
                emit(ev="send_failed", ref=ref, error=f"no ACK after {MAX_TRIES} tries")
        except Exception as e:
            emit(ev="send_failed", ref=ref, error=f"{type(e).__name__}: {e}")
        finally:
            for k in codes:
                self.acks.pop(k, None)
                self.ack_trip.pop(k, None)

    def spawn(self, coro):
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    async def answer(self, sent, etype, filters, what):
        """Wait for another node's answer to a request the node just sent."""
        if sent is None or sent.type == self.E.ERROR:
            raise RuntimeError(f"the node did not send the {what}")
        t = (sent.payload or {}).get("suggested_timeout") or 0
        t = min(max(t / 800, ANSWER_MIN_S), ANSWER_MAX_S)
        ev = await self.mc.dispatcher.wait_for_event(etype, attribute_filters=filters, timeout=t)
        if ev is None:
            raise TimeoutError(f"no answer to the {what} in {t:.0f} s")
        return ev

    @staticmethod
    def tag(sent):
        t = (sent.payload or {}).get("expected_ack")
        return t.hex() if isinstance(t, bytes) else t

    async def binary(self, c, kind, data=None, what="request"):
        BinaryReqType = self.R[0]
        async with self.lock:
            if kind == "neighbours":
                s = await self.mc.commands.req_neighbours_async(c, count=64)
            else:
                s = await self.mc.commands.send_binary_req(c, getattr(BinaryReqType, kind.upper()), data=data)
        want = {"status": self.E.STATUS_RESPONSE, "telemetry": self.E.TELEMETRY_RESPONSE,
                "neighbours": self.E.NEIGHBOURS_RESPONSE}[kind]
        if s is None or s.type == self.E.ERROR:
            raise RuntimeError(f"the node did not send the {what}")
        return (await self.answer(s, want, {"tag": self.tag(s)}, what)).payload or {}

    async def request(self, m):
        """One request to another node, answered with a "result" line."""
        kind, ref, key = m["cmd"], m.get("ref"), str(m.get("key") or "").lower()
        c = self.pending.get(key) if kind == "add" else self.find(key)
        try:
            if c is None:
                raise ValueError("not a contact of this node")
            data = await getattr(self, f"req_{kind}")(c, m)
        except Exception as e:
            msg = str(e) if isinstance(e, (ValueError, RuntimeError, TimeoutError)) else f"{type(e).__name__}: {e}"
            emit(ev="result", ref=ref, kind=kind, key=key, ok=False, error=msg or type(e).__name__)
            return
        emit(ev="result", ref=ref, kind=kind, key=(c.get("public_key") or key), ok=True, data=data)

    async def req_status(self, c, m):
        p = await self.binary(c, "status", what="status request")
        return {k: p.get(k) for k in STATUS_FIELDS}

    async def req_telemetry(self, c, m):
        p = await self.binary(c, "telemetry", what="telemetry request")
        return {"lpp": p.get("lpp") or []}

    async def req_neighbours(self, c, m):
        p = await self.binary(c, "neighbours", what="neighbour request")
        return {"total": p.get("neighbours_count"),
                "neighbours": [{"key": n.get("pubkey"), "snr": n.get("snr"), "secs_ago": n.get("secs_ago")}
                               for n in p.get("neighbours") or []]}

    async def req_owner(self, c, m):
        AnonReqType = self.R[1]
        async with self.lock:
            s = await self.mc.commands.send_anon_req(c, AnonReqType.OWNER, data=b"\0")
        ev = await self.answer(s, self.E.BINARY_RESPONSE, {"tag": self.tag(s)}, "owner request")
        raw = bytes.fromhex((ev.payload or {}).get("data") or "")[4:]
        name, _, owner = raw.decode("utf-8", "ignore").partition("\n")
        return {"name": name.strip("\x00"), "owner": owner.strip("\x00")}

    async def req_login(self, c, m):
        pwd = str(m.get("password") or "")
        async with self.lock:
            s = await self.mc.commands._send_login_raw(c, pwd)
        pwd = m["password"] = None                # gone from this process's command too
        if s is None or s.type == self.E.ERROR:
            raise RuntimeError("the node did not send the login")
        t = min(max(((s.payload or {}).get("suggested_timeout") or 0) / 800, ANSWER_MIN_S), ANSWER_MAX_S)
        f = {"pubkey_prefix": c["public_key"][:12]}
        ok = asyncio.ensure_future(self.mc.dispatcher.wait_for_event(self.E.LOGIN_SUCCESS, f, t))
        bad = asyncio.ensure_future(self.mc.dispatcher.wait_for_event(self.E.LOGIN_FAILED, f, t))
        done, pending = await asyncio.wait([ok, bad], return_when=asyncio.FIRST_COMPLETED)
        for x in pending:
            x.cancel()
        if ok in done and ok.result() is not None:
            p = ok.result().payload or {}
            return {"admin": bool(p.get("is_admin")), "permissions": p.get("permissions")}
        if bad in done and bad.result() is not None:
            raise RuntimeError("wrong password")
        raise TimeoutError(f"no answer to the login in {t:.0f} s")

    async def req_path(self, c, m):
        async with self.lock:
            s = await self.mc.commands._send_path_discovery_raw(c)
        p = (await self.answer(s, self.E.PATH_RESPONSE, {"pubkey_pre": c["public_key"][:12]},
                               "path discovery")).payload or {}
        self.contacts_dirty = True                # the node keeps the route it found
        return {"out": split_path(p.get("out_path") or "", p.get("out_path_hash_len") or 1),
                "back": split_path(p.get("in_path") or "", p.get("in_path_hash_len") or 1)}

    async def req_trace(self, c, m):
        """Along the known route to C and back, with each hop's SNR. Only
        repeaters pass a trace on: to a companion it goes as far as its last
        repeater."""
        if route_len(c) < 0:
            raise ValueError("no route known yet: find the path first")
        size = (c.get("out_path_hash_mode") or 0) + 1
        if size not in (1, 2, 4):
            raise ValueError(f"a {size}-byte route can't be traced")
        out = split_path(c.get("out_path") or "", size)
        if TYPES.get(c.get("type")) in ("repeater", "room"):
            hops_ = out + [c["public_key"][:2 * size]] + out[::-1]
        elif out:
            hops_ = out + out[-2::-1]
        else:
            raise ValueError("a companion in direct range: nothing to trace")
        tag = int.from_bytes(os.urandom(4), "little") or 1
        async with self.lock:
            s = await self.mc.commands.send_trace(tag=tag, flags={1: 0, 2: 1, 4: 2}[size], path=",".join(hops_))
        p = (await self.answer(s, self.E.TRACE_DATA, {"tag": tag}, "trace")).payload or {}
        return {"hops": [{"hash": h.get("hash"), "snr": h.get("snr")} for h in p.get("path") or []]}

    async def req_reset_path(self, c, m):
        async with self.lock:
            r = await self.mc.commands.reset_path(c)
        if r.type == self.E.ERROR:
            raise RuntimeError("the node refused")
        await self.read_contacts()
        return {}

    async def req_remove(self, c, m):
        async with self.lock:
            r = await self.mc.commands.remove_contact(c)
        if r.type == self.E.ERROR:
            raise RuntimeError("the node refused")
        await self.read_contacts()
        return {}

    async def req_fav(self, c, m):
        flags = ((c.get("flags") or 0) | FAV) if m.get("fav") else ((c.get("flags") or 0) & ~FAV)
        async with self.lock:
            r = await self.mc.commands.change_contact_flags(c, flags)
        if r.type == self.E.ERROR:
            raise RuntimeError("the node refused")
        await self.read_contacts()
        return {"fav": bool(flags & FAV)}

    async def req_add(self, c, m):
        async with self.lock:
            r = await self.mc.commands.add_contact(c)
        if r.type == self.E.ERROR:
            raise RuntimeError("the node refused")
        self.pending.pop(c["public_key"], None)
        emit(ev="pending", contacts=[contact(x) for x in self.pending.values()])
        await self.read_contacts()
        return {}

    async def req_cli(self, c, m):
        async with self.lock:
            r = await self.mc.commands.send_cmd(c, str(m["text"]))
        if r.type == self.E.ERROR:
            raise RuntimeError("the node did not send the command")
        return {"sent": True}

    async def req_clock(self, c, m):
        return await self.req_cli(c, {"text": f"time {int(time.time())}"})

    async def channel(self, m):
        ref = m.get("ref")
        try:
            if m["cmd"] == "channel_join":
                name = str(m["name"])
                if name in self.channel_names.values():
                    raise ValueError(f"already on {name}")
                free = next((i for i in range(self.channel_slots) if i not in self.channel_names), None)
                if free is None:
                    raise ValueError("every channel slot on the node is taken")
                async with self.lock:
                    r = await self.mc.commands.set_channel(free, name)   # a #name's key is its hash
            else:
                i = int(m["index"])
                if i not in self.channel_names:
                    raise ValueError(f"no channel {i}")
                async with self.lock:
                    r = await self.mc.commands.set_channel(i, "", bytes(16))
            if r.type == self.E.ERROR:
                raise RuntimeError("the node refused")
        except Exception as e:
            emit(ev="done", ref=ref, ok=False, error=str(e))
            return
        await self.read_channels()
        emit(ev="done", ref=ref, ok=True, error=None)

    async def stdin_loop(self):
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        while True:
            line = await reader.readline()
            if not line:                          # the server closed our stdin: exit
                return
            try:
                m = json.loads(line)
            except ValueError:
                continue
            try:
                if m.get("cmd") == "send":
                    await self.send(m)
                elif m.get("cmd") == "advert":
                    await self.advert(m)
                elif m.get("cmd") == "set":
                    await self.set_config(m)
                elif m.get("cmd") == "reboot":
                    await self.reboot(m)
                elif m.get("cmd") in REQUESTS:
                    self.spawn(self.request(m))
                elif m.get("cmd") in ("channel_join", "channel_leave"):
                    await self.channel(m)
            except (KeyError, TypeError, ValueError) as e:
                emit(ev="done", ref=m.get("ref"), ok=False, error=f"bad command: {e}")


async def connect(link, MeshCore):
    if link[0] == "tcp":
        return await asyncio.wait_for(MeshCore.create_tcp(link[1], link[2]), CONNECT_TIMEOUT_S)
    if link[0] == "ble":
        return await asyncio.wait_for(MeshCore.create_ble(link[1]), BLE_CONNECT_TIMEOUT_S)
    return await asyncio.wait_for(MeshCore.create_serial(link[1], 115200), CONNECT_TIMEOUT_S)


async def run(port):
    try:
        links = candidates(port)
    except ValueError as e:
        emit(ev="status", connected=False, error=str(e))
        return 2
    from meshcore import EventType, MeshCore
    from meshcore.packets import AnonReqType, BinaryReqType
    mc, link, errors = None, None, []
    for link in links:                         # the first node that answers wins
        try:
            mc = await connect(link, MeshCore)
        except Exception as e:
            errors.append(f"{link_name(link)}: {type(e).__name__}: {e}")
            mc = None
            continue
        if mc is not None and mc.self_info:
            break
        why = {"tcp": "Wi-Fi on, port 5000 reachable?", "ble": "in range, paired, Bluetooth free?",
               "serial": "USB companion firmware?"}[link[0]]
        errors.append(f"{link_name(link)}: no answer from a MeshCore companion ({why})")
        try:
            if mc is not None:
                await asyncio.wait_for(mc.disconnect(), 3)
        except Exception:
            pass
        mc = None
    if mc is None:
        emit(ev="status", connected=False, error="; ".join(errors) or f"none of {port} is present")
        return 2
    b = Bridge(mc, EventType, link)
    b.R = (BinaryReqType, AnonReqType)
    mc.subscribe(EventType.CONTACT_MSG_RECV, b.on_msg)
    mc.subscribe(EventType.CHANNEL_MSG_RECV, b.on_msg)
    mc.subscribe(EventType.ADVERTISEMENT, b.on_advert)
    mc.subscribe(EventType.PATH_UPDATE, b.on_advert)
    mc.subscribe(EventType.ACK, b.on_ack)
    mc.subscribe(EventType.DISCONNECTED, b.on_disconnect)
    mc.subscribe(EventType.RX_LOG_DATA, b.on_rx)
    mc.subscribe(EventType.NEW_CONTACT, b.on_new_contact)
    mc.subscribe(EventType.CONTACT_DELETED, b.on_contact_deleted)
    mc.subscribe(EventType.CONTACTS_FULL, b.on_contacts_full)
    emit(ev="status", connected=True, error=None)
    await b.hello()
    await mc.start_auto_message_fetching()
    # Messages that arrived while nobody was connected wait on the node.
    while (await mc.commands.get_msg()).type not in (EventType.NO_MORE_MSGS, EventType.ERROR):
        pass
    tasks = [asyncio.create_task(b.stdin_loop()), asyncio.create_task(b.loops())]
    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    b.closing = True
    try:
        await asyncio.wait_for(mc.disconnect(), 3)
    except Exception:
        pass
    return 1


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("port", help="serial device of the node, e.g. /dev/ttyUSB0, tcp:HOST:PORT for a "
                                "companion on Wi-Fi (e.g. tcp:192.168.1.50:5000), or ble:NAME; "
                                "comma-separate several to try in order")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, stream=sys.stderr,
                        format="meshcore_bridge %(levelname)s %(message)s")
    code = asyncio.run(run(args.port))
    _out.flush()
    os._exit(code)


if __name__ == "__main__":
    main()
