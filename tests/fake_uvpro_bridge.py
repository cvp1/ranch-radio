#!/usr/bin/env python3
"""A stand-in for addons/uvpro_bridge.py: the same JSON-lines protocol, no radio.

    fake_uvpro_bridge.py ADDRESS [--battery-every S]

ADDRESS "absent" -> the radio can't be reached (status false, exit 2).
On start: connected, device, four channels (146.520 simplex, a 146.940 -0.600
repeater with a 100.0 tone, 144.390 APRS, 162.550 NOAA RX only), settings,
VFO A/B (records 251, 252), beacon (N9ABC-7), state, battery 87 %, extras
(volume 8, regions HOME/TRAVEL/AIR, FM off on 91.5, path WIDE1-1,WIDE2-1).
volume / region / fm / aprs_path / power change those and send them back;
a region switch sends the other region's channels (names get the region's
name as a prefix). A channel or VFO write to 99.999 MHz TX is "rewritten by
the radio" (TX 0.0) and reported failed, as the real bridge's read-back does.
Writes update the values and send them back, then done; a channel named
"fail" is refused by the "radio"; a channel named "die" makes the radio go
away (exit 3). "refresh" re-sends everything; if $FAKE_UVPRO_TX names a file
that exists, the state then says the radio is transmitting.
Each start appends a line to $FAKE_UVPRO_STARTS and every command received
goes to $FAKE_UVPRO_LOG. Channels whose ids $FAKE_UVPRO_INEXACT lists (comma
separated) are reported as not read back byte for byte ("exact").
"""
import json
import os
import sys

if len(sys.argv) > 1 and sys.argv[1] == "absent":
    print(json.dumps({"ev": "status", "connected": False, "error": "BleakDeviceNotFoundError: not found"}), flush=True)
    sys.exit(2)

if os.environ.get("FAKE_UVPRO_STARTS"):
    with open(os.environ["FAKE_UVPRO_STARTS"], "a") as f:
        f.write("start\n")
    if os.environ.get("FAKE_UVPRO_ARGV"):
        with open(os.environ["FAKE_UVPRO_ARGV"], "a") as f:
            f.write(json.dumps(sys.argv[1:]) + "\n")


def emit(**d):
    print(json.dumps(d), flush=True)


def ch(i, name, rx, tx, tone="", tx_disable=False):
    return {"id": i, "name": name, "rx_freq": rx, "tx_freq": tx, "rx_mod": "FM", "tx_mod": "FM",
            "rx_sub_audio": "", "tx_sub_audio": tone, "power": "high", "bandwidth": "WIDE", "scan": True,
            "tx_disable": tx_disable, "talk_around": False, "mute": False,
            "fixed": {"freq": False, "bandwidth": False, "power": False}}


CHANNELS = [ch(0, "2M CALL", 146.52, 146.52), ch(1, "RPT", 146.94, 146.34, "100.0"),
            ch(2, "APRS", 144.39, 144.39), ch(3, "NOAA", 162.55, 162.55, tx_disable=True)]
SETTINGS = {"channel_a": 0, "channel_b": 2, "scan": False, "squelch_level": 3, "mic_gain": 4, "bt_mic_gain": 4,
            "tail_elim": True, "power_saving_mode": False, "dis_tone": False, "imperial_unit": True,
            "ptt_lock": False, "ch_data_lock": False, "double_channel": 1}
SETTINGS.update(json.loads(os.environ.get("FAKE_UVPRO_SETTINGS") or "{}"))   # e.g. {"auto_relay_en": true}
BEACON = {"aprs_callsign": "N9ABC", "aprs_ssid": 7, "aprs_symbol": "/[", "beacon_message": "UV-PRO",
          "location_share_interval": 0, "should_share_location": False, "packet_format": "APRS",
          "send_pwr_voltage": False, "ptt_release_send_location": False, "ptt_release_send_id_info": False}
DEVICE = {"vendor_id": 1, "product_id": 2, "hardware_version": 1, "firmware_version": 152, "channel_count": 4,
          "supports_dmr": False, "supports_vfo": True, "supports_radio": True, "region_count": 3,
          "supports_software_power_control": True}
VFOS = [ch(251, "", 146.52, 146.52), ch(252, "", 446.0, 446.0)]
REGIONS = ["HOME", "TRAVEL", "AIR"]
EXTRAS = {"volume": 8, "regions": REGIONS, "region": 0, "fm": {"on": False, "seeking": False, "freq": 91.5},
          "aprs_path": "WIDE1-1,WIDE2-1", "power_control": True}
POWER = {"on": True}


def state():
    tx = bool(os.environ.get("FAKE_UVPRO_TX")) and os.path.exists(os.environ["FAKE_UVPRO_TX"])
    return {"is_power_on": POWER["on"], "is_in_tx": tx, "is_sq": False, "is_in_rx": False, "double_channel": "A",
            "is_scan": False, "is_radio": True, "curr_ch_id": SETTINGS["channel_a"], "is_gps_locked": True,
            "is_hfp_connected": False, "is_aoc_connected": False, "rssi": 0.0, "curr_region": EXTRAS["region"]}


INEXACT = {int(x) for x in os.environ.get("FAKE_UVPRO_INEXACT", "").split(",") if x.strip()}


def everything():
    emit(ev="device", info=DEVICE)
    exact()
    emit(ev="channels", channels=CHANNELS)
    emit(ev="vfo", channels=VFOS)
    emit(ev="settings", values=SETTINGS)
    emit(ev="beacon", values=BEACON)
    emit(ev="state", status=state())
    emit(ev="battery", percent=87, volts=8.12)
    emit(ev="extras", values=EXTRAS)


def exact():
    emit(ev="exact", channels=[c["id"] for c in CHANNELS + VFOS if c["id"] not in INEXACT], settings=True, beacon=True)


emit(ev="status", connected=True, error=None)
everything()

for line in sys.stdin:
    m = json.loads(line)
    if os.environ.get("FAKE_UVPRO_LOG"):
        with open(os.environ["FAKE_UVPRO_LOG"], "a") as f:
            f.write(json.dumps(m) + "\n")
    cmd, ref, values = m.get("cmd"), m.get("ref"), m.get("values") or {}
    if cmd == "refresh":
        everything()
    elif cmd == "channel":
        if values.get("name") == "die":
            emit(ev="status", connected=False, error="Bluetooth link to the radio lost")
            sys.exit(3)
        if values.get("name") == "fail":
            emit(ev="done", ref=ref, ok=False, error="MessageReplyError: INVALID_PARAMETER")
            continue
        vfo = m["id"] in (251, 252)
        c = next(x for x in (VFOS if vfo else CHANNELS) if x["id"] == m["id"])
        c.update(values)
        rewritten = abs(c["tx_freq"] - 99.999) < 1e-6
        if rewritten:
            c["tx_freq"] = 0.0
        if vfo:
            emit(ev="vfo", channels=VFOS)
        else:
            emit(ev="channel", channel=c)
        emit(ev="done", ref=ref, ok=not rewritten,
             error="read back, channel does not hold the new tx_freq (the radio rewrote it)" if rewritten else None)
    elif cmd == "settings":
        SETTINGS.update(values)
        emit(ev="settings", values=SETTINGS)
        emit(ev="state", status=state())
        emit(ev="done", ref=ref, ok=True, error=None)
    elif cmd == "beacon":
        BEACON.update(values)
        emit(ev="beacon", values=BEACON)
        emit(ev="done", ref=ref, ok=True, error=None)
    elif cmd == "volume":
        EXTRAS["volume"] = values["level"]
        emit(ev="extras", values={"volume": EXTRAS["volume"]})
        emit(ev="done", ref=ref, ok=True, error=None)
    elif cmd == "region":
        EXTRAS["region"] = r = values["region"]
        BASE = globals().setdefault("BASE", {c["id"]: c["name"] for c in CHANNELS})
        for c in CHANNELS:
            c["name"] = f"{REGIONS[r][:3]} {BASE[c['id']]}"[:10] if r else BASE[c["id"]]
        exact()
        emit(ev="channels", channels=CHANNELS)
        emit(ev="settings", values=SETTINGS)
        emit(ev="state", status=state())
        emit(ev="extras", values={"region": r})
        emit(ev="done", ref=ref, ok=True, error=None)
    elif cmd == "fm":
        fm = EXTRAS["fm"]
        if "on" in values:
            fm["on"] = values["on"]
        elif "freq" in values:
            fm["freq"] = values["freq"]
        else:
            fm["freq"] = round(fm["freq"] + (0.4 if values["seek"] == "up" else -0.4), 2)
        emit(ev="extras", values={"fm": fm})
        emit(ev="done", ref=ref, ok=True, error=None)
    elif cmd == "aprs_path":
        EXTRAS["aprs_path"] = values["path"]
        emit(ev="extras", values={"aprs_path": values["path"]})
        emit(ev="done", ref=ref, ok=True, error=None)
    elif cmd == "power":
        POWER["on"] = values["on"]
        emit(ev="state", status=state())
        emit(ev="done", ref=ref, ok=True, error=None)
