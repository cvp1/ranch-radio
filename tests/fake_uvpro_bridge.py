#!/usr/bin/env python3
"""A stand-in for addons/uvpro_bridge.py: the same JSON-lines protocol, no radio.

    fake_uvpro_bridge.py ADDRESS        ("absent": unreachable, exit 2)

Starts connected with four channels (simplex, a -0.600 repeater with tone,
APRS, RX-only NOAA), VFOs 251/252, settings, beacon N9ABC-7, battery 87 %,
extras (volume, regions HOME/TRAVEL/AIR, FM, APRS path). Writes are applied
and sent back, then done; a region switch sends that region's channels. A
channel/VFO TX of 99.999 MHz is "rewritten by the radio" and reported failed;
a channel named "fail" is refused, "die" exits 3.

Environment:
  FAKE_UVPRO_LOG        file: every command received (FAKE_UVPRO_LOG_T adds
                        "t"; FAKE_UVPRO_PROBE=path adds "probe": path existed)
  FAKE_UVPRO_STARTS     file: a line per start (FAKE_UVPRO_ARGV: also argv)
  FAKE_UVPRO_TX         file: while it exists, the state says transmitting
  FAKE_UVPRO_CH3        MHz: channel 3 is simplex there instead of APRS
  FAKE_UVPRO_INEXACT    ids (comma separated) reported not exact
  FAKE_UVPRO_NO_VFO=1   a UV-50PRO: supports_vfo false, VFOs read 0.0
  FAKE_UVPRO_FAIL_NEXT  "settings" or "channel": next one answers ok:false
                        (a channel write is still applied, as the real one may be)
  FAKE_UVPRO_SETTINGS   JSON merged into the starting settings
  FAKE_UVPRO_STATE      file: settings, channels and VFOs survive restarts
"""
import json
import os
import sys
import time

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
if os.environ.get("FAKE_UVPRO_CH3"):
    # Winlink never tunes to the APRS channel (winlink.check_url)
    _f = float(os.environ["FAKE_UVPRO_CH3"])
    CHANNELS[2] = ch(2, "WL2K", _f, _f)
SETTINGS = {"channel_a": 0, "channel_b": 2, "scan": False, "squelch_level": 3, "mic_gain": 4, "bt_mic_gain": 4,
            "tail_elim": True, "power_saving_mode": False, "dis_tone": False, "imperial_unit": True,
            "ptt_lock": False, "ch_data_lock": False, "double_channel": 1}
SETTINGS.update(json.loads(os.environ.get("FAKE_UVPRO_SETTINGS") or "{}"))
BEACON = {"aprs_callsign": "N9ABC", "aprs_ssid": 7, "aprs_symbol": "/[", "beacon_message": "UV-PRO",
          "location_share_interval": 0, "should_share_location": False, "packet_format": "APRS",
          "send_pwr_voltage": False, "ptt_release_send_location": False, "ptt_release_send_id_info": False}
DEVICE = {"vendor_id": 1, "product_id": 2, "hardware_version": 1, "firmware_version": 152, "channel_count": 4,
          "supports_dmr": False, "supports_vfo": True, "supports_radio": True, "region_count": 3,
          "supports_software_power_control": True}
VFOS = [ch(251, "", 146.52, 146.52), ch(252, "", 446.0, 446.0)]
if os.environ.get("FAKE_UVPRO_NO_VFO") == "1":
    DEVICE["supports_vfo"] = False
    VFOS = [ch(251, "", 0.0, 0.0), ch(252, "", 0.0, 0.0)]
FAIL_NEXT = {os.environ.get("FAKE_UVPRO_FAIL_NEXT") or None} - {None}
REGIONS = ["HOME", "TRAVEL", "AIR"]
EXTRAS = {"volume": 8, "regions": REGIONS, "region": 0, "fm": {"on": False, "seeking": False, "freq": 91.5},
          "aprs_path": "WIDE1-1,WIDE2-1", "power_control": True}
POWER = {"on": True}
STATE_FILE = os.environ.get("FAKE_UVPRO_STATE")
if STATE_FILE and os.path.exists(STATE_FILE):
    with open(STATE_FILE) as _f:
        _saved = json.load(_f)
    SETTINGS.clear()
    SETTINGS.update(_saved["settings"])
    CHANNELS[:] = _saved["channels"]
    VFOS[:] = _saved["vfos"]


def save():
    if STATE_FILE:
        with open(STATE_FILE + ".tmp", "w") as f:
            json.dump({"settings": SETTINGS, "channels": CHANNELS, "vfos": VFOS}, f)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)


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
            probe = os.environ.get("FAKE_UVPRO_PROBE")
            extra = {"t": time.time()} if os.environ.get("FAKE_UVPRO_LOG_T") else {}
            if probe:
                extra["probe"] = os.path.exists(probe)
            f.write(json.dumps({**m, **extra}) + "\n")
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
        save()
        if "channel" in FAIL_NEXT:
            FAIL_NEXT.discard("channel")
            if vfo:
                emit(ev="vfo", channels=VFOS)
            else:
                emit(ev="channel", channel=c)
            emit(ev="done", ref=ref, ok=False, error="MessageReplyError: FAILURE (FAKE_UVPRO_FAIL_NEXT)")
            continue
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
        if "settings" in FAIL_NEXT:
            FAIL_NEXT.discard("settings")
            emit(ev="done", ref=ref, ok=False, error="MessageReplyError: FAILURE (FAKE_UVPRO_FAIL_NEXT)")
            continue
        SETTINGS.update(values)
        save()
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
