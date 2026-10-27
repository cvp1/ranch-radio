#!/usr/bin/env python3
"""BTECH UV-PRO control link <-> Radio Remote, as JSON lines on stdin/stdout.

Runs as a child of the server (addons/uvpro.py) under the Python that has
`benlink` (its own venv). benlink speaks the radio's command protocol over
classic-Bluetooth RFCOMM channel 4 ("SPP Dev" by SDP) or Bluetooth LE (GATT
service 00001100-d102-11e1-9b23-00025b00a5a5). The radio answers either only
while nothing holds its KISS channel (RFCOMM 1, the packet hub's): the add-on
borrows the radio from the hub first (linux-station, 2026-10-16). Keeping the
library in its own process means a stuck link never takes the server with
it: this process exits and the server starts it again.

    uvpro_bridge.py ADDRESS [--transport tcp:HOST:PORT|rfcomm:4|ble] [--battery-every S]

tcp:HOST:PORT is the packet hub's control port (tnc_hub.py --control-port):
the hub carries the radio's command protocol over its own Bluetooth link,
next to packet, so nothing has to be borrowed.

ADDRESS: the radio's Bluetooth address on Linux (38:D2:...), or the
CoreBluetooth UUID macOS gives it (bleak's discover lists it; BLE only).

Out (stdout, one JSON object per line):
    {"ev": "status", "connected": bool, "error": str|None}
    {"ev": "device", "info": {...}}
    {"ev": "exact", "channels": [id, ...], "settings": bool, "beacon": bool}
                                                  (what benlink reads back byte for byte: only those are written)
    {"ev": "channels", "channels": [{...}, ...]}  /  {"ev": "channel", "channel": {...}}
    {"ev": "settings", "values": {...}}
    {"ev": "beacon", "values": {...}}
    {"ev": "vfo", "channels": [{...}, ...]}       (the VFO records, 251 and 252, as channels)
    {"ev": "extras", "values": {...}}             (volume, regions, FM radio, APRS path; any subset)
    {"ev": "state", "status": {...}}              (on every status change from the radio)
    {"ev": "battery", "percent": int|None, "volts": float|None}
    {"ev": "done", "ref", "ok", "error"}
In (stdin):
    {"cmd": "channel", "ref": n, "id": int, "values": {...}}
    {"cmd": "settings", "ref": n, "values": {...}}
    {"cmd": "beacon", "ref": n, "values": {...}}
    {"cmd": "volume", "ref": n, "values": {"level": 0..15}}
    {"cmd": "region", "ref": n, "values": {"region": n}}     (then channels and settings are read again)
    {"cmd": "fm", "ref": n, "values": {"on": bool} | {"freq": MHz} | {"seek": "up"|"down"}}
    {"cmd": "aprs_path", "ref": n, "values": {"path": "WIDE1-1,WIDE2-1"}}
    {"cmd": "power", "ref": n, "values": {"on": bool}}
    {"cmd": "refresh"}

Only the commands above exist. Nothing here can key the transmitter, send
packet data or audio, press the radio's programmable functions, reset the
radio or touch its firmware: benlink's send_tnc_data and send_bytes are
never called, and the commands benlink has no message for (volume, region,
FM radio, APRS path, power) go out through raw() for the opcodes in RAW_OK
only. The server checks every value first (addons/uvpro.py); this side
checks the field names again.

The commands benlink lacks follow HTCommander's layouts (github.com/Ylianst/
HTCommander, src/lib/radio/radio.dart, read 2026-10-16) and benlink PR #28
(regions). Tuning a VFO is a write of its record, 251 (A) or 252 (B), the
way tncd and BenshiCommander do it; FREQ_MODE_SET_PAR is not used (tncd:
UV-PRO firmware never makes it the operating frequency).

benlink writes a whole channel (or all settings, or the whole beacon) from
its copy of what it read, so a write is only as faithful as that copy. Every
message from the radio is encoded again as it arrives and compared with the
bytes the radio sent; a channel, the settings or the beacon is written only
if its last read came back byte for byte. A channel write is then read back:
what was not asked to change (frequencies, RX-only, modulation among them)
must be unchanged and the new values in place, or the write is reported as
failed (the radio rewrites a TX it can't send: linux-station, 2026-10-16).
"""

import argparse
import asyncio
import json
import logging
import os
import sys

log = logging.getLogger("uvpro_bridge")

CONNECT_TIMEOUT_S = 40.0      # hydrating reads every channel; 32 channels over BLE take a while
CONNECT_RETRY_S = 1.0         # the radio can take a moment to free its control channel after the hub lets go
ALIVE_EVERY_S = 2.0
RAW_TIMEOUT_S = 3.0
RAW_MAYBE_S = 1.0             # how long to wait for a reply the radio may not send
REGION_SETTLE_S = 6.0         # a region switch: the radio's status shows the new region within this
FM_SEEK_S = 10.0              # an FM seek stops within this
# A channel's TX side, as the radio holds it: a write that did not ask to
# change one of these must find it unchanged on the read-back.
CHANNEL_KEPT = ("rx_freq", "tx_freq", "tx_disable", "rx_mod", "tx_mod")
VFO_IDS = (251, 252)          # the VFO records: what VFO A / B tune (tncd, BenshiCommander)

# What may be written, per command (the server's schema is the real check).
CHANNEL_FIELDS = {"name", "rx_freq", "tx_freq", "tx_disable", "rx_mod", "tx_mod", "rx_sub_audio",
                  "tx_sub_audio", "power", "bandwidth", "scan", "talk_around", "mute"}
SETTINGS_FIELDS = {"channel_a", "channel_b", "scan", "squelch_level", "mic_gain", "bt_mic_gain",
                   "tail_elim", "power_saving_mode", "dis_tone", "imperial_unit", "ptt_lock", "ch_data_lock",
                   "double_channel", "tx_time_limit", "auto_power_off", "screen_timeout", "vox_en", "vox_level",
                   "vox_delay", "wx_mode", "noaa_ch", "alarm_volume", "ns_en", "auto_relay_en",
                   "gpwpl_upload_en", "dis_bt_mic"}
BEACON_FIELDS = {"aprs_callsign", "aprs_ssid", "aprs_symbol", "beacon_message", "location_share_interval",
                 "should_share_location", "packet_format", "send_pwr_voltage",
                 "ptt_release_send_location", "ptt_release_send_id_info"}

# The library prints debug lines to stdout/stderr; the protocol gets its own
# copy of fd 1 and everything else written to stdout goes to stderr (the log).
_out = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr


def emit(**d):
    _out.write(json.dumps(d) + "\n")


# --- byte-for-byte check of everything the radio sends

EXACT = {}          # ("channel", id) / ("settings",) / ("beacon",) -> did its last read encode back exactly


def _structs(body):
    """What a decoded message body carries that the bridge may write back:
    (key, kind, protocol struct)."""
    for b in (body, getattr(body, "event", None)):
        if b is None:
            continue
        rf = getattr(b, "rf_ch", None)
        if rf is not None and hasattr(rf, "channel_id"):
            yield ("channel", int(rf.channel_id)), "channel", rf
        if getattr(b, "settings", None) is not None:
            yield ("settings",), "settings", b.settings
        if getattr(b, "bss_settings", None) is not None:
            yield ("beacon",), "beacon", b.bss_settings


def exact(kind, x):
    """Does benlink's model of X (what a write is built from) encode back to
    X's bytes? A write of a model that doesn't would change bits nobody asked
    to change."""
    from benlink.command import BeaconSettings, Channel, Settings
    model = {"channel": Channel, "settings": Settings, "beacon": BeaconSettings}[kind]
    try:
        return model.from_protocol(x).to_protocol().to_bytes() == x.to_bytes()
    except Exception:            # noqa: BLE001 - can't round-trip it: not exact
        return False


def install_vfo_guard():
    """benlink keeps channels 0..n-1 and would fail on an event for a VFO
    record (251, 252): those are the bridge's to keep."""
    from benlink.command import ChannelChangedEvent
    from benlink.controller import RadioController
    handle = RadioController._on_event_message
    if getattr(handle, "_vfo_guard", False):
        return

    def on_event(self, ev):
        if isinstance(ev, ChannelChangedEvent) and ev.channel.channel_id >= len(self._state.channels):  # noqa: SLF001
            return None
        return handle(self, ev)
    on_event._vfo_guard = True
    RadioController._on_event_message = on_event


def install_exact_check():
    """Wrap benlink's message decoder (both transports go through it): each
    message, and each channel / settings / beacon in it, is encoded again and
    compared with the radio's bytes."""
    from benlink import protocol as p
    decode = p.Message.from_bytes.__func__
    if getattr(decode, "_exact_check", False):
        return

    def from_bytes(cls, data, *a, **k):
        m = decode(cls, data, *a, **k)
        try:
            whole = m.to_bytes() == bytes(data)
        except Exception:        # noqa: BLE001
            whole = False
        for key, kind, x in _structs(m.body):
            EXACT[key] = whole and exact(kind, x)
            if not EXACT[key]:
                log.warning("%s: benlink does not read it back byte for byte; it will not be written", key)
        return m
    from_bytes._exact_check = True
    p.Message.from_bytes = classmethod(from_bytes)


def exact_out(n_channels):
    return {"channels": [i for i in [*range(n_channels), *VFO_IDS] if EXACT.get(("channel", i))],
            "settings": bool(EXACT.get(("settings",))), "beacon": bool(EXACT.get(("beacon",)))}


# --- commands benlink has no message for: raw bodies, these opcodes only

def raw_ok():
    from benlink import protocol as p
    B = p.BasicCommand
    return {B.GET_VOLUME, B.SET_VOLUME, B.READ_REGION_NAME, B.SET_REGION, B.RADIO_GET_STATUS,
            B.RADIO_SET_MODE, B.RADIO_SET_FREQ, B.RADIO_SEEK_UP, B.RADIO_SEEK_DOWN, B.GET_APRS_PATH,
            B.SET_APRS_PATH, B.SET_HT_ON_OFF}


async def raw(radio, name, body=b"", reply=True, timeout=RAW_TIMEOUT_S):
    """Send opcode NAME (one of raw_ok()) with BODY; the reply's body after
    its status byte. reply=False: don't wait; reply=None: wait TIMEOUT for a
    reply the radio may not send (None if it didn't). A reply whose status
    isn't success raises."""
    from benlink import protocol as p
    from benlink.command import UnknownProtocolMessage
    cmd = p.BasicCommand[name]
    if cmd not in raw_ok():
        raise ValueError(f"{name} is not a command the bridge sends")
    conn = radio._conn                                       # noqa: SLF001 - benlink has no raw API
    got = asyncio.get_running_loop().create_future()

    def on(msg):
        m = msg.message if isinstance(msg, UnknownProtocolMessage) else None
        if m is not None and m.is_reply and m.command == cmd and not got.done():
            got.set_result(bytes(m.body) if isinstance(m.body, (bytes, bytearray)) else b"")
    remove = conn._add_message_handler(on)                   # noqa: SLF001
    try:
        await conn._link.send(p.Message(command_group=p.CommandGroup.BASIC, is_reply=False,  # noqa: SLF001
                                        command=cmd, body=bytes(body)))
        if reply is False:
            return None
        try:
            data = await asyncio.wait_for(got, min(timeout, RAW_MAYBE_S) if reply is None else timeout)
        except TimeoutError:
            if reply is None:
                return None
            raise TimeoutError(f"no answer from the radio to {name}") from None
    finally:
        remove()
    if not data or data[0] != 0:
        status = data[0] if data else None
        try:
            status = p.ReplyStatus(status).name
        except (ValueError, TypeError):
            pass
        raise RuntimeError(f"the radio refused {name}: {status}")
    return data[1:]


def fm_out(d):
    """RADIO_GET_STATUS reply (after status): [flags, 0, freq hi, freq lo], freq in 10 kHz."""
    if len(d) < 4:
        return None
    return {"on": bool(d[0] & 0x80), "seeking": bool(d[0] & 0x10), "freq": round(((d[2] << 8) | d[3]) / 100, 2)}


def text_out(d):
    return bytes(d).split(b"\0", 1)[0].decode("utf-8", "replace").strip()


# --- radio objects -> plain JSON

def sub_audio_out(x):
    """None -> "", CTCSS 100.0 -> "100.0", DCS(23) -> "D023"."""
    if x is None:
        return ""
    if isinstance(x, (int, float)):
        return f"{float(x):.1f}"
    return f"D{int(x.n):03d}"


def sub_audio_in(s):
    from benlink.command import DCS
    s = str(s or "").strip().upper()
    if not s:
        return None
    if s.startswith("D"):
        return DCS(n=int(s[1:]))
    return float(s)


def channel_out(c):
    power = "high" if c.tx_at_max_power else "med" if c.tx_at_med_power else "low"
    return {"id": c.channel_id, "name": c.name, "rx_freq": round(c.rx_freq, 6), "tx_freq": round(c.tx_freq, 6),
            "rx_mod": c.rx_mod, "tx_mod": c.tx_mod,
            "rx_sub_audio": sub_audio_out(c.rx_sub_audio), "tx_sub_audio": sub_audio_out(c.tx_sub_audio),
            "power": power, "bandwidth": c.bandwidth, "scan": c.scan, "tx_disable": c.tx_disable,
            "talk_around": c.talk_around, "mute": c.mute,
            "fixed": {"freq": c.fixed_freq, "bandwidth": c.fixed_bandwidth, "power": c.fixed_tx_power}}


def channel_in(values):
    out = {}
    for k, v in values.items():
        if k == "power":
            out["tx_at_max_power"], out["tx_at_med_power"] = v == "high", v == "med"
        elif k in ("rx_sub_audio", "tx_sub_audio"):
            out[k] = sub_audio_in(v)
        else:
            out[k] = v
    return out


def model_out(m, fields=None):
    d = m.model_dump() if hasattr(m, "model_dump") else dict(m)
    if fields is not None:
        d = {k: v for k, v in d.items() if k in fields}
    return {k: (v if isinstance(v, (bool, int, float, str)) or v is None else str(v)) for k, v in d.items()}


def status_out(s):
    return model_out(s)


class Bridge:
    def __init__(self, radio, battery_every):
        self.radio = radio
        self.battery_every = battery_every
        self.done = asyncio.Event()
        self.vfo = {}             # 251 / 252 -> Channel, as last read
        self.region = None        # the region the channels were read in
        self.lock = asyncio.Lock()

    def on_event(self, ev):
        from benlink.command import (
            ChannelChangedEvent,
            SettingsChangedEvent,
            StatusChangedEvent,
        )
        try:
            if isinstance(ev, ChannelChangedEvent):
                if ev.channel.channel_id in VFO_IDS:
                    self.vfo[ev.channel.channel_id] = ev.channel
                    emit(ev="vfo", channels=[channel_out(c) for _, c in sorted(self.vfo.items())])
                else:
                    emit(ev="channel", channel=channel_out(ev.channel))
                emit(ev="exact", **exact_out(len(self.radio.channels)))
            elif isinstance(ev, SettingsChangedEvent):
                emit(ev="settings", values=model_out(ev.settings, SETTINGS_FIELDS))
                emit(ev="exact", **exact_out(len(self.radio.channels)))
            elif isinstance(ev, StatusChangedEvent):
                emit(ev="state", status=status_out(ev.status))
                if self.region is not None and ev.status.curr_region != self.region and not self.lock.locked():
                    # switched on the radio itself: its channels are another region's now
                    asyncio.ensure_future(self.reread())
        except Exception:
            log.exception("event %r", type(ev).__name__)

    async def everything(self):
        r = self.radio
        await self.read_vfos()
        self.region = r.status.curr_region
        emit(ev="device", info=model_out(r.device_info))
        emit(ev="exact", **exact_out(len(r.channels)))
        emit(ev="channels", channels=[channel_out(c) for c in r.channels])
        emit(ev="vfo", channels=[channel_out(c) for _, c in sorted(self.vfo.items())])
        emit(ev="settings", values=model_out(r.settings, SETTINGS_FIELDS))
        emit(ev="beacon", values=model_out(r.beacon_settings, BEACON_FIELDS))
        emit(ev="state", status=status_out(r.status))
        emit(ev="extras", values=await self.extras())

    async def read_vfos(self):
        """The VFO records. A radio without them (or that refuses) has no VFO
        here. Read whatever the device info says: linux-station's UV-PRO reports
        supports_vfo false but has both records (read 2026-10-16)."""
        for cid in VFO_IDS:
            try:
                self.vfo[cid] = await asyncio.wait_for(self.radio._conn.get_channel(cid), RAW_TIMEOUT_S)  # noqa: SLF001
            except Exception as e:   # noqa: BLE001
                log.info("VFO record %d not read: %s", cid, e)
                self.vfo.pop(cid, None)

    async def extras(self, which=("volume", "regions", "fm", "aprs_path")):
        """What benlink doesn't read: each on its own, None if the radio didn't answer."""
        info, out = self.radio.device_info, {}
        if "volume" in which:
            out["volume"] = await self._try(self._volume)
        if "regions" in which:
            out["regions"] = await self._try(self._regions) if info.region_count else []
            out["region"] = self.radio.status.curr_region
        if "fm" in which:
            out["fm"] = await self._try(self._fm) if info.supports_radio else None
        if "aprs_path" in which:
            out["aprs_path"] = await self._try(self._aprs_path)
        out["power_control"] = bool(info.supports_software_power_control)
        return out

    async def _try(self, f):
        try:
            return await f()
        except Exception as e:     # noqa: BLE001 - one read failing leaves the others
            log.info("%s: %s", f.__name__, e)
            return None

    async def _volume(self):
        d = await raw(self.radio, "GET_VOLUME")
        return d[0] if d else None

    async def _regions(self):
        names = []
        for i in range(self.radio.device_info.region_count):
            d = await raw(self.radio, "READ_REGION_NAME", bytes([i]))
            names.append(text_out(d[1:11]) if len(d) > 1 else "")
        return names

    async def _fm(self):
        return fm_out(await raw(self.radio, "RADIO_GET_STATUS"))

    async def _aprs_path(self):
        return text_out(await raw(self.radio, "GET_APRS_PATH"))

    async def reread(self):
        async with self.lock:
            await self._reread()

    async def _reread(self):
        """Channels and settings again (another region), then the status."""
        r, conn = self.radio, self.radio._conn            # noqa: SLF001
        st = await conn.get_status()
        r._state.status = st                              # noqa: SLF001
        r._state.channels[:] = [await conn.get_channel(i) for i in range(len(r.channels))]   # noqa: SLF001
        r._state.settings = await conn.get_settings()     # noqa: SLF001
        await self.read_vfos()
        self.region = st.curr_region
        emit(ev="exact", **exact_out(len(r.channels)))
        emit(ev="channels", channels=[channel_out(c) for c in r.channels])
        emit(ev="vfo", channels=[channel_out(c) for _, c in sorted(self.vfo.items())])
        emit(ev="settings", values=model_out(r.settings, SETTINGS_FIELDS))
        emit(ev="state", status=status_out(st))
        emit(ev="extras", values={"region": st.curr_region})

    async def battery(self):
        pct = volts = None
        try:
            pct = await asyncio.wait_for(self.radio.battery_level_as_percentage(), 5)
        except Exception as e:
            log.info("battery percent: %s", e)
        try:
            volts = round(await asyncio.wait_for(self.radio.battery_voltage(), 5), 2)
        except Exception as e:
            log.info("battery voltage: %s", e)
        emit(ev="battery", percent=pct, volts=volts)

    async def battery_loop(self):
        while not self.done.is_set():
            await self.battery()
            try:
                await asyncio.wait_for(self.done.wait(), self.battery_every)
            except TimeoutError:
                pass

    async def alive_loop(self):
        while not self.done.is_set():
            if not self.radio.is_connected():
                emit(ev="status", connected=False, error="Bluetooth link to the radio lost")
                self.done.set()
                return
            await asyncio.sleep(ALIVE_EVERY_S)

    async def write(self, m):
        async with self.lock:
            await self._write(m)

    async def _write(self, m):
        ref, cmd, values = m.get("ref"), m.get("cmd"), m.get("values") or {}
        try:
            if cmd == "channel":
                if not set(values) <= CHANNEL_FIELDS:
                    raise ValueError(f"not a channel field: {sorted(set(values) - CHANNEL_FIELDS)}")
                cid = int(m["id"])
                what = f"VFO {'AB'[VFO_IDS.index(cid)]}" if cid in VFO_IDS else f"channel {cid + 1}"
                if cid in VFO_IDS:
                    if cid not in self.vfo:
                        raise ValueError(f"{what} was not read from the radio")
                    if self.vfo[cid].name.strip() or "name" in values:
                        raise ValueError(f"{what}: record {cid} has a name, so it is not a VFO record; not written")
                elif not 0 <= cid < len(self.radio.channels):
                    raise ValueError(f"the radio has no channel {cid + 1}")
                if not EXACT.get(("channel", cid)):
                    raise ValueError(f"{what} was not read back byte for byte, so it is not written")
                await self.write_channel(cid, channel_in(values), what)
            elif cmd == "settings":
                if not set(values) <= SETTINGS_FIELDS:
                    raise ValueError(f"not a setting: {sorted(set(values) - SETTINGS_FIELDS)}")
                if not EXACT.get(("settings",)):
                    raise ValueError("the settings were not read back byte for byte, so they are not written")
                await self.radio.set_settings(**values)
                emit(ev="settings", values=model_out(self.radio.settings, SETTINGS_FIELDS))
            elif cmd == "beacon":
                if not set(values) <= BEACON_FIELDS:
                    raise ValueError(f"not a beacon setting: {sorted(set(values) - BEACON_FIELDS)}")
                if not EXACT.get(("beacon",)):
                    raise ValueError("the beacon settings were not read back byte for byte, so they are not written")
                await self.radio.set_beacon_settings(**values)
                emit(ev="beacon", values=model_out(self.radio.beacon_settings, BEACON_FIELDS))
            elif cmd == "volume":
                level = int(values["level"])
                if not 0 <= level <= 15:
                    raise ValueError(f"volume {level} is outside 0..15")
                await raw(self.radio, "SET_VOLUME", bytes([level]), reply=None)
                got = await self._volume()
                emit(ev="extras", values={"volume": got})
                if got != level:
                    raise RuntimeError(f"read back, the volume is {got}, not {level}")
            elif cmd == "region":
                await self.set_region(int(values["region"]))
            elif cmd == "fm":
                await self.fm(values)
            elif cmd == "aprs_path":
                path = str(values["path"]).strip().upper()
                await raw(self.radio, "SET_APRS_PATH", path.encode(), reply=None)
                got = await self._aprs_path()
                emit(ev="extras", values={"aprs_path": got})
                if got != path:
                    raise RuntimeError(f"read back, the APRS path is {got!r}, not {path!r}")
            elif cmd == "power":
                on = values["on"]
                if not isinstance(on, bool):
                    raise ValueError("power: on is true or false")
                if not self.radio.device_info.supports_software_power_control:
                    raise ValueError("this radio has no power control over Bluetooth")
                await raw(self.radio, "SET_HT_ON_OFF", bytes([1 if on else 0]), reply=None)
                await asyncio.sleep(1.0)
                try:
                    st = await asyncio.wait_for(self.radio._conn.get_status(), RAW_TIMEOUT_S)  # noqa: SLF001
                    emit(ev="state", status=status_out(st))
                except Exception as e:   # noqa: BLE001 - off: it may not answer
                    log.info("status after power %s: %s", on, e)
            else:
                raise ValueError(f"unknown command {cmd!r}")
        except Exception as e:
            emit(ev="exact", **exact_out(len(self.radio.channels)))
            emit(ev="done", ref=ref, ok=False, error=f"{type(e).__name__}: {e}")
            return
        emit(ev="exact", **exact_out(len(self.radio.channels)))
        emit(ev="done", ref=ref, ok=True, error=None)

    async def set_region(self, region):
        n = self.radio.device_info.region_count
        if not 0 <= region < n:
            raise ValueError(f"the radio has regions 1 to {n}")
        # HTCommander sends it without waiting; benlink PR #28 saw a reply on a VR-N76
        await raw(self.radio, "SET_REGION", bytes([region]), reply=None)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + REGION_SETTLE_S
        while True:
            st = await asyncio.wait_for(self.radio._conn.get_status(), RAW_TIMEOUT_S)   # noqa: SLF001
            if st.curr_region == region:
                break
            if loop.time() > deadline:
                raise RuntimeError(f"the radio is still in region {st.curr_region + 1}")
            await asyncio.sleep(0.5)
        await self._reread()

    async def fm(self, values):
        if not self.radio.device_info.supports_radio:
            raise ValueError("this radio has no FM broadcast receiver")
        if "on" in values:
            await raw(self.radio, "RADIO_SET_MODE", bytes([2 if values["on"] else 0]), reply=None)
        elif "freq" in values:
            u = int(round(float(values["freq"]) * 100))
            await raw(self.radio, "RADIO_SET_FREQ", bytes([(u >> 8) & 0xFF, u & 0xFF]), reply=None)
        elif values.get("seek") in ("up", "down"):
            await raw(self.radio, "RADIO_SEEK_UP" if values["seek"] == "up" else "RADIO_SEEK_DOWN", reply=None)
        else:
            raise ValueError("fm: on, freq or seek")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (FM_SEEK_S if "seek" in values else 2.0)
        while True:
            st = await self._fm()
            emit(ev="extras", values={"fm": st})
            if st is None or not st["seeking"] or loop.time() > deadline:
                break
            await asyncio.sleep(0.5)
        if "on" in values and (st or {}).get("on") != values["on"]:
            raise RuntimeError(f"read back, the FM radio is {'on' if (st or {}).get('on') else 'off'}")
        if "freq" in values and st and abs(st["freq"] - float(values["freq"])) > 0.005:
            raise RuntimeError(f"read back, the FM radio is on {st['freq']} MHz")

    def _held(self, cid):
        return self.vfo[cid] if cid in VFO_IDS else self.radio.channels[cid]

    async def write_channel(self, cid, values, what):
        """Write, then read the channel back from the radio and check it:
        the new values in place, and nothing else of its TX side moved."""
        before = self._held(cid)
        new = before.model_copy(update=dict(values))
        await self.radio._conn.set_channel(new)                                 # noqa: SLF001
        after = await asyncio.wait_for(self.radio._conn.get_channel(cid), 10)   # noqa: SLF001 - no public re-read
        if cid in VFO_IDS:
            self.vfo[cid] = after
            emit(ev="vfo", channels=[channel_out(c) for _, c in sorted(self.vfo.items())])
        else:
            self.radio._state.channels[cid] = after                             # noqa: SLF001
            emit(ev="channel", channel=channel_out(after))
        moved = [k for k in CHANNEL_KEPT if k not in values and getattr(after, k) != getattr(before, k)]
        if moved:
            log.error("%s: %s changed on the radio after a write: %s", what, moved,
                      {k: (getattr(before, k), getattr(after, k)) for k in moved})
            raise RuntimeError(f"read back, {what}'s {', '.join(moved)} changed; check it on the radio")

        def same(k, v):
            a = getattr(after, k)
            if k == "name":
                return a.rstrip() == v.rstrip()
            if k in ("rx_freq", "tx_freq"):
                return abs(a - v) < 5e-7
            return a == v
        missed = [k for k, v in values.items() if not same(k, v)]
        if missed:
            raise RuntimeError(f"read back, {what} does not hold the new {', '.join(missed)}"
                               + (" (the radio rewrote it: it may not transmit there)"
                                  if "tx_freq" in missed else ""))

    async def stdin_loop(self):
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        while not self.done.is_set():
            line = await reader.readline()
            if not line:                # server closed our stdin: exit
                break
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("cmd") == "refresh":
                async with self.lock:
                    await self.everything()
                await self.battery()
            else:
                await self.write(m)
        self.done.set()


class TcpCommandLink:
    """benlink's command link (Gaia frames) over TCP: the packet hub's control
    port, which passes them to and from the radio on the hub's own link."""

    def __init__(self, host, port):
        self.host, self.port = host, int(port)
        self._w = None
        self._task = None

    def is_connected(self):
        return self._w is not None and not self._w.is_closing()

    async def send(self, msg):
        from benlink import protocol as p
        b = msg.to_bytes()
        await self.send_bytes(p.GaiaFrame(flags=p.GaiaFlags.NONE, n_bytes_payload=len(b) - 4, data=b).to_bytes())

    async def send_bytes(self, data):
        if not self.is_connected():
            raise ConnectionError("the packet hub's control port closed")
        self._w.write(data)
        await self._w.drain()

    async def connect(self, callback):
        from benlink import protocol as p
        from benlink.protocol.command.bitfield import BitStream
        reader, self._w = await asyncio.open_connection(self.host, self.port)

        async def read():
            buf = BitStream()
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    buf = buf.extend_bytes(data)
                    frames, buf = p.GaiaFrame.from_bitstream_batch(buf)
                    for f in frames:
                        try:
                            m = p.Message.from_bytes(f.data)
                        except Exception as e:   # noqa: BLE001 - one odd frame isn't the link
                            log.info("control frame not decoded (%s): %s", e, f.data.hex())
                            continue
                        callback(m)
            except (OSError, asyncio.IncompleteReadError) as e:
                log.info("control port: %s", e)
            finally:
                self._w.close()
        self._task = asyncio.create_task(read())

    async def disconnect(self):
        if self._w is not None:
            self._w.close()
        if self._task:
            self._task.cancel()


def controller(address, transport):
    from benlink.command import CommandConnection
    from benlink.controller import RadioController
    if transport.startswith("tcp:"):
        host, _, port = transport[4:].rpartition(":")
        return RadioController(CommandConnection(TcpCommandLink(host or "127.0.0.1", port)))
    if transport == "ble":
        return RadioController.new_ble(address)
    if transport.startswith("rfcomm:"):
        return RadioController.new_rfcomm(address, int(transport.split(":", 1)[1]))
    raise ValueError(f"unknown transport {transport!r} (rfcomm:N or ble)")


async def connect(address, transport):
    """Connect and read everything, trying again while the radio is still
    freeing its control channel (a refused or reset connection)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CONNECT_TIMEOUT_S
    while True:
        radio = controller(address, transport)
        try:
            await asyncio.wait_for(radio.connect(), max(1.0, deadline - loop.time()))
            return radio
        except (TimeoutError, OSError, EOFError) as e:
            try:
                await asyncio.wait_for(radio._conn.disconnect(), 3)   # noqa: SLF001 - a half-open link
            except Exception:   # noqa: BLE001
                pass
            if deadline - loop.time() < CONNECT_RETRY_S + 2:
                raise
            log.info("connect: %s; trying again", e)
            await asyncio.sleep(CONNECT_RETRY_S)


async def run(args):
    install_exact_check()
    install_vfo_guard()
    try:
        radio = await connect(args.address, args.transport)
    except Exception as e:
        emit(ev="status", connected=False, error=f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
        return 2
    b = Bridge(radio, args.battery_every)
    radio.add_event_handler(b.on_event)
    emit(ev="status", connected=True, error=None)
    async with b.lock:
        await b.everything()
    tasks = [asyncio.create_task(t) for t in (b.battery_loop(), b.alive_loop(), b.stdin_loop())]
    await b.done.wait()
    for t in tasks:
        t.cancel()
    try:
        await asyncio.wait_for(radio.disconnect(), 5)
    except Exception:
        pass
    return 1


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("address", help="the radio's Bluetooth address (Linux) or CoreBluetooth UUID (macOS)")
    p.add_argument("--transport", default="ble",
                   help="tcp:HOST:PORT (the packet hub's control port), rfcomm:N (classic Bluetooth, Linux) "
                        "or ble (default %(default)s)")
    p.add_argument("--battery-every", type=float, default=60.0, help="seconds between battery reads")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, stream=sys.stderr,
                        format="uvpro_bridge %(levelname)s %(message)s")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
