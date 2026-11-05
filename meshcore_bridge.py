#!/usr/bin/env python3
"""MeshCore companion node <-> Radio Remote, as JSON lines on stdin/stdout.

Runs as a child of server.py (addons/meshcore.py), under the Python that has
the `meshcore` package (its own venv; the server's Python doesn't need it).
A stuck or crashing serial link never takes the radio server with it: this
process exits and the add-on starts it again.

    meshcore_bridge.py /dev/ttyUSB0            (USB: companion_radio_usb firmware)
    meshcore_bridge.py tcp:192.168.1.50:5000   (Wi-Fi: a companion with Wi-Fi on, port 5000)

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
    {"ev": "done", "ref", "ok", "error"}       (an advert was sent, or not)
In (stdin):
    {"cmd": "send", "ref": n, "text": str, "ch": int}            (a channel)
    {"cmd": "send", "ref": n, "text": str, "to": "<key prefix>"} (a direct message)
    {"cmd": "advert", "ref": n, "flood": bool}

What never leaves this process: the node's private key and channel secrets.
Nothing here changes the node's radio settings; on connect it sets the
node's clock from this host's (MeshCore stamps every message with it).
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
LINK_FAILS = 2                # battery reads failing in a row before the link counts as lost
TYPES = {1: "chat", 2: "repeater", 3: "room", 4: "sensor"}

# The library logs to stdout in places: the protocol gets its own copy of
# fd 1, and everything else written to stdout goes to stderr (the server log).
_out = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr


def emit(**d):
    _out.write(json.dumps(d) + "\n")


def parse_port(spec):
    """'/dev/ttyUSB0' -> ("serial", "/dev/ttyUSB0"); 'tcp:HOST:PORT' -> ("tcp", HOST, PORT)."""
    if spec.startswith("tcp:"):
        host, sep, port = spec[4:].rpartition(":")
        if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError(f"not tcp:HOST:PORT: {spec!r}")
        return ("tcp", host.strip("[]"), int(port))
    if not spec:
        raise ValueError("no port")
    return ("serial", spec)


def hops(path_len):
    """MeshCore's path length: 255 / -1 = direct (heard first hand, or no route
    known yet: flood), else the number of repeaters on the way."""
    return None if path_len is None or path_len < 0 or path_len == 255 else path_len


def contact(c):
    lat, lon = c.get("adv_lat"), c.get("adv_lon")
    return {"key": c.get("public_key"), "name": c.get("adv_name") or "",
            "type": TYPES.get(c.get("type"), str(c.get("type"))),
            "hops": hops(c.get("out_path_len")),
            "last_advert": c.get("last_advert") or None,
            "lat": lat if lat or lon else None, "lon": lon if lat or lon else None}


class Bridge:
    def __init__(self, mc, EventType):
        self.mc = mc
        self.E = EventType
        self.contacts = {}
        self.contacts_dirty = False
        self.closing = False

    async def hello(self):
        await self.mc.commands.set_time(int(time.time()))
        si = dict(self.mc.self_info or {})
        dev = await self.mc.commands.send_device_query()
        d = dev.payload if dev.type == self.E.DEVICE_INFO else {}
        emit(ev="info",
             me={"key": si.get("public_key"), "name": si.get("name"),
                 "lat": si.get("adv_lat") or None, "lon": si.get("adv_lon") or None,
                 "share_loc": bool(si.get("adv_loc_policy"))},
             device={"model": d.get("model"), "fw": d.get("ver"), "build": d.get("fw_build"),
                     "max_contacts": d.get("max_contacts")},
             radio={"freq": si.get("radio_freq"), "bw": si.get("radio_bw"), "sf": si.get("radio_sf"),
                    "cr": si.get("radio_cr"), "tx_power": si.get("tx_power")})
        chans = []
        for i in range(min(CHANNELS_MAX, d.get("max_channels") or CHANNELS_MAX)):
            r = await self.mc.commands.get_channel(i)
            if r.type != self.E.CHANNEL_INFO:
                break
            name = (r.payload or {}).get("channel_name") or ""
            if name:                              # an unnamed slot is unused
                chans.append({"index": i, "name": name})
        emit(ev="channels", channels=chans)
        await self.read_contacts()
        await self.battery()

    async def read_contacts(self):
        r = await self.mc.commands.get_contacts()
        if r.type == self.E.CONTACTS:
            self.contacts = dict(r.payload or {})
        emit(ev="contacts", contacts=sorted((contact(c) for c in self.contacts.values()),
                                            key=lambda c: -(c["last_advert"] or 0)))

    async def battery(self):
        """Read the battery; False when the node didn't answer."""
        r = await self.mc.commands.get_bat()
        if r.type == self.E.BATTERY:
            emit(ev="battery", mv=(r.payload or {}).get("level"))
            return True
        return False

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
        emit(ev="ack", ack=(event.payload or {}).get("code"))

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
            except Exception as e:
                fails += 1
                log.warning("periodic read: %s: %s", type(e).__name__, e)
            if fails >= LINK_FAILS:
                emit(ev="status", connected=False, error="the node stopped answering")
                self.closing = True

    # --- server -> node

    async def send(self, m):
        ref, text = m.get("ref"), str(m["text"])
        try:
            if m.get("to"):
                prefix = str(m["to"]).lower()
                c = next((c for k, c in self.contacts.items() if k.startswith(prefix)), None)
                if c is None:
                    raise ValueError("not a contact of this node")
                r = await self.mc.commands.send_msg(c, text)
            else:
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
            r = await self.mc.commands.send_advert(flood=bool(m.get("flood")))
            ok = r.type != self.E.ERROR
            emit(ev="done", ref=ref, ok=ok, error=None if ok else str(r.payload))
        except Exception as e:
            emit(ev="done", ref=ref, ok=False, error=f"{type(e).__name__}: {e}")

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
            except (KeyError, TypeError, ValueError) as e:
                emit(ev="done", ref=m.get("ref"), ok=False, error=f"bad command: {e}")


async def run(port):
    try:
        link = parse_port(port)
    except ValueError as e:
        emit(ev="status", connected=False, error=str(e))
        return 2
    from meshcore import EventType, MeshCore
    try:
        if link[0] == "tcp":
            mc = await asyncio.wait_for(MeshCore.create_tcp(link[1], link[2]), CONNECT_TIMEOUT_S)
        else:
            mc = await asyncio.wait_for(MeshCore.create_serial(link[1], 115200), CONNECT_TIMEOUT_S)
    except Exception as e:
        emit(ev="status", connected=False, error=f"{port}: {type(e).__name__}: {e}")
        return 2
    if mc is None or not mc.self_info:
        why = "Wi-Fi on, and port 5000 reachable?" if link[0] == "tcp" else "USB companion firmware?"
        emit(ev="status", connected=False, error=f"{port}: no answer from a MeshCore companion ({why})")
        return 2
    b = Bridge(mc, EventType)
    mc.subscribe(EventType.CONTACT_MSG_RECV, b.on_msg)
    mc.subscribe(EventType.CHANNEL_MSG_RECV, b.on_msg)
    mc.subscribe(EventType.ADVERTISEMENT, b.on_advert)
    mc.subscribe(EventType.PATH_UPDATE, b.on_advert)
    mc.subscribe(EventType.ACK, b.on_ack)
    mc.subscribe(EventType.DISCONNECTED, b.on_disconnect)
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
    p.add_argument("port", help="serial device of the node, e.g. /dev/ttyUSB0, "
                                "or tcp:HOST:PORT for a companion on Wi-Fi, e.g. tcp:192.168.1.50:5000")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, stream=sys.stderr,
                        format="meshcore_bridge %(levelname)s %(message)s")
    code = asyncio.run(run(args.port))
    _out.flush()
    os._exit(code)


if __name__ == "__main__":
    main()
