"""addons/uvpro_bridge.py against a fake UV-PRO at benlink's protocol level.

The radio here keeps each channel, the settings and the beacon as raw bytes,
answers benlink's real request messages with real reply messages (through
benlink's own encoder and decoder), and records every write. So this runs the
bridge as it runs on the radio, minus the Bluetooth socket, and checks:
  - connect reads device, channels, settings, beacon and status; everything
    it read is reported exact (encodes back byte for byte)
  - a channel write changes only what was asked: the stored bytes differ
    from before only in the edited fields; frequencies, RX-only and
    modulation are read back unchanged unless asked; a frequency write
    lands; a TX the radio rewrites is reported as a failed write
  - the VFO records (251, 252) are read and written like channels, never
    named; benlink's own state doesn't break on their events
  - volume, region (channels and settings read again, also when switched on
    the radio), FM radio, APRS path and power go out as the raw bodies
    HTCommander uses and are read back; raw() sends nothing outside its list
  - a channel whose bytes benlink's model can't reproduce is not exact and
    is never written (nothing sent to the radio)
  - a radio that changes the TX side on a write (it shouldn't) is caught by
    the read-back and reported as a failed write
  - a radio that doesn't take the new value is reported too
  - settings and beacon writes: only with an exact read; a beacon that
    doesn't round-trip is refused
  - fields outside the bridge's lists are refused before anything is sent
  - connect tries again while the radio still refuses (the hub just let go)
  - no request the bridge sends is TNC data (HT_SEND_DATA), a programmable
    function (DO_PROG_FUNC) or FREQ_MODE_SET_PAR

    ~/.local/share/venvs/benlink/bin/python tests/uvpro_bridge_test.py
"""
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "addons"))

import uvpro_bridge as ub  # noqa: E402  (moves print() to stderr; results go to fd 1 via OUT)
from benlink import protocol as p  # noqa: E402
from benlink.command import DCS, BeaconSettings, Channel, Settings, Status  # noqa: E402
from benlink.command import CommandConnection  # noqa: E402
from benlink.controller import RadioController  # noqa: E402

OUT = ub._out
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    OUT.write(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}") + "\n")


EMITTED = []
ub._out = type("Collect", (), {"write": lambda self, s: EMITTED.append(json.loads(s))})()


def ch(i, name, rx, tx, tx_tone=None, rx_tone=None, tx_disable=False):
    return Channel(channel_id=i, tx_mod="FM", tx_freq=tx, rx_mod="FM", rx_freq=rx, tx_sub_audio=tx_tone,
                   rx_sub_audio=rx_tone, scan=True, tx_at_max_power=True, talk_around=False, bandwidth="WIDE",
                   pre_de_emph_bypass=False, sign=False, tx_at_med_power=False, tx_disable=tx_disable,
                   fixed_freq=False, fixed_bandwidth=False, fixed_tx_power=False, mute=False, name=name)


def settings_bytes():
    # Today's linux-station read, through benlink's own model.
    return Settings(channel_a=1, channel_b=2, scan=False, aghfp_call_mode=0, double_channel=2, squelch_level=1,
                    tail_elim=False, auto_relay_en=True, auto_power_on=True, keep_aghfp_link=False, mic_gain=3,
                    tx_hold_time=0, tx_time_limit=6, local_speaker=0, bt_mic_gain=3, adaptive_response=False,
                    dis_tone=False, power_saving_mode=False, auto_power_off=0, auto_share_loc_ch=2, hm_speaker=0,
                    positioning_system=15, time_offset=10, use_freq_range_2=False, ptt_lock=False,
                    leading_sync_bit_en=False, pairing_at_power_on=False, screen_timeout=20,
                    kiss_upload_tx_msg=False, kiss_en=True, imperial_unit=False, wx_mode=0, noaa_ch=0,
                    vfol_tx_power_x=0, vfo2_tx_power_x=0, dis_digital_mute=True, signaling_ecc_en=False,
                    ch_data_lock=False, kiss_tx_delay=0, kiss_tx_tail=0, vox_en=False, vox_level=0,
                    dis_bt_mic=False, vox_delay=0, ns_en=False, alarm_volume=15, use_custom_location=False,
                    gpwpl_upload_en=False, vfo1_mod_freq_x=0, custom_location_lat=0,
                    custom_location_lon=0).to_protocol().to_bytes()


def beacon_bytes():
    return BeaconSettings(max_fwd_times=0, time_to_live=0, ptt_release_send_location=False,
                          ptt_release_send_id_info=False, ptt_release_send_bss_user_id=False,
                          should_share_location=False, send_pwr_voltage=False, packet_format="APRS",
                          allow_position_check=False, aprs_ssid=7, location_share_interval=0, bss_user_id=0,
                          ptt_release_id_info="", beacon_message="APMAIL WINLINK", aprs_symbol="/$",
                          aprs_callsign="N9ABC").to_protocol().to_bytes()


def flip(b, bit):
    b = bytearray(b)
    b[bit // 8] ^= 0x80 >> (bit % 8)
    return bytes(b)


class FakeRadio:
    """The radio's side of the command link, at benlink's message level."""

    def __init__(self, refuse_connects=0):
        chans = [ch(0, "2M CALL", 146.52, 146.52), ch(1, "RPT", 146.94, 146.34, tx_tone=100.0),
                 ch(2, "APRS", 144.39, 144.39), ch(3, "NOAA", 162.55, 162.55, tx_disable=True),
                 ch(4, "CROSS", 462.65, 467.65, tx_tone=DCS(n=365), rx_tone=156.7)]
        self.ch = {c.channel_id: c.to_protocol().to_bytes() for c in chans}
        self.vfo = {251: ch(251, "", 146.52, 146.52).to_protocol().to_bytes(),
                    252: ch(252, "", 446.0, 446.0).to_protocol().to_bytes()}
        self.volume, self.region, self.on = 6, 0, True
        self.regions = ["HOME", "TRAVEL", "AIR", "", "", ""]
        self.fm = {"on": False, "seeking": False, "freq10k": 9150}
        self.path = "WIDE1-1,WIDE2-1"
        self.settings = settings_bytes()
        self.beacon = beacon_bytes()
        self.sent = []                  # every command the bridge sent, by name
        self.writes = []
        self.refuse_connects = refuse_connects
        self.on_write_rf = None         # a misbehaving radio: bytes -> bytes stored
        self.cb = None

    def reply(self, cmd, body):
        m = p.Message(command_group=p.CommandGroup.BASIC, is_reply=True, command=cmd, body=body)
        # through the bytes and benlink's decoder, as from the air
        asyncio.get_running_loop().call_soon(self.cb, p.Message.from_bytes(m.to_bytes()))

    def raw_reply(self, cmd, body):
        m = p.Message(command_group=p.CommandGroup.BASIC, is_reply=True, command=cmd, body=bytes(body))
        asyncio.get_running_loop().call_soon(self.cb, p.Message.from_bytes(m.to_bytes()))

    def store(self, cid):
        return self.vfo if cid in self.vfo else self.ch

    def status(self):
        return Status(is_power_on=self.on, is_in_tx=False, is_sq=True, is_in_rx=True, double_channel="B",
                      is_scan=False, is_radio=self.fm["on"], curr_ch_id=2, is_gps_locked=False,
                      is_hfp_connected=False, is_aoc_connected=False, rssi=46.0, curr_region=self.region)

    def handle(self, msg):
        c, b, S = msg.command, msg.body, p.ReplyStatus.SUCCESS
        self.sent.append(c.name)
        B = p.BasicCommand
        if c == B.GET_VOLUME:
            return self.raw_reply(c, [0, self.volume])
        if c == B.SET_VOLUME:
            self.volume = b[0]
            self.writes.append(("volume",))
            return                                       # HTCommander: its reply is ignored; none here
        if c == B.READ_REGION_NAME:
            i = b[0]
            if i >= len(self.regions):
                return self.raw_reply(c, [5])
            return self.raw_reply(c, [0, i] + list(self.regions[i].encode().ljust(10, b"\0")))
        if c == B.SET_REGION:
            self.region = b[0]
            self.writes.append(("region",))
            self.base = getattr(self, "base", None) or {
                cid: Channel.from_protocol(p.RfCh.from_bytes(raw)).name for cid, raw in self.ch.items()}
            for cid, raw in self.ch.items():               # the other region's channels
                x = Channel.from_protocol(p.RfCh.from_bytes(raw))
                base = self.base[cid]
                name = f"{self.regions[self.region][:3]} {base}"[:10] if self.region else base
                self.ch[cid] = x.model_copy(update={"name": name}).to_protocol().to_bytes()
            return                                       # no reply (HTCommander)
        if c == B.RADIO_GET_STATUS:
            f = self.fm
            return self.raw_reply(c, [0, (0x80 if f["on"] else 0) | (0x10 if f["seeking"] else 0), 0,
                                      f["freq10k"] >> 8, f["freq10k"] & 0xFF])
        if c == B.RADIO_SET_MODE:
            self.fm["on"] = b[0] == 2
            return self.raw_reply(c, [0])
        if c == B.RADIO_SET_FREQ:
            self.fm["freq10k"] = (b[0] << 8) | b[1]
            return self.raw_reply(c, [0])
        if c in (B.RADIO_SEEK_UP, B.RADIO_SEEK_DOWN):
            self.fm["freq10k"] += 40 if c == B.RADIO_SEEK_UP else -40
            return self.raw_reply(c, [0])
        if c == B.GET_APRS_PATH:
            return self.raw_reply(c, [0] + list(self.path.encode().ljust(32, b"\0")))
        if c == B.SET_APRS_PATH:
            self.path = bytes(b).decode()
            self.writes.append(("aprs_path",))
            return self.raw_reply(c, [0])
        if c == B.SET_HT_ON_OFF:
            self.on = b[0] == 1
            self.writes.append(("power",))
            return self.raw_reply(c, [0])
        if c == p.BasicCommand.GET_DEV_INFO:
            d = p.DevInfo(vendor_id=6, product_id=260, hw_ver=1, soft_ver=144, support_radio=True,
                          support_medium_power=True, fixed_loc_speaker_vol=False, not_support_soft_power_ctrl=False,
                          have_no_speaker=False, have_hm_speaker=False, region_count=6, support_noaa=True,
                          gmrs=False, support_vfo=True, support_dmr=False, channel_count=len(self.ch),
                          freq_range_count=5, support_noise_reduction=False, support_smart_beacon=True)
            self.reply(c, p.GetDevInfoReplyBody(reply_status=S, dev_info=d))
        elif c == p.BasicCommand.READ_RF_CH:
            self.reply(c, p.ReadRFChReplyBody(reply_status=S,
                                              rf_ch=p.RfCh.from_bytes(self.store(b.channel_id)[b.channel_id])))
        elif c == p.BasicCommand.WRITE_RF_CH:
            raw = b.rf_ch.to_bytes()
            if self.on_write_rf:
                raw = self.on_write_rf(raw)
            self.writes.append(("channel", b.rf_ch.channel_id))
            self.store(b.rf_ch.channel_id)[b.rf_ch.channel_id] = raw
            self.reply(c, p.WriteRFChReplyBody(reply_status=S, channel_id=b.rf_ch.channel_id))
        elif c == p.BasicCommand.READ_SETTINGS:
            self.reply(c, p.ReadSettingsReplyBody(reply_status=S, settings=p.Settings.from_bytes(self.settings)))
        elif c == p.BasicCommand.WRITE_SETTINGS:
            self.writes.append(("settings",))
            self.settings = b.settings.to_bytes()
            self.reply(c, p.WriteSettingsReplyBody(reply_status=S))
        elif c == p.BasicCommand.READ_BSS_SETTINGS:
            self.reply(c, p.ReadBSSSettingsReplyBody(reply_status=S,
                                                     bss_settings=p.BSSSettingsV2.from_bytes(self.beacon)))
        elif c == p.BasicCommand.WRITE_BSS_SETTINGS:
            self.writes.append(("beacon",))
            self.beacon = b.bss_settings.to_bytes()
            self.reply(c, p.WriteBSSSettingsReplyBody(reply_status=S))
        elif c == p.BasicCommand.GET_HT_STATUS:
            self.reply(c, p.GetHtStatusReplyBody(reply_status=S, status=self.status().to_protocol()))
        elif c == p.BasicCommand.READ_STATUS:
            t = b.status_type
            value = (p.BatteryVoltageStatus(battery_voltage=7.3) if t == p.PowerStatusType.BATTERY_VOLTAGE
                     else p.BatteryLevelPercentageStatus(battery_level_as_percentage=57))
            self.reply(c, p.ReadPowerStatusReplyBody(reply_status=S,
                                                     status=p.PowerStatus(power_status_type=t, value=value)))
        # REGISTER_NOTIFICATION: no reply, as on the radio


class FakeLink:
    def __init__(self, radio):
        self.radio, self.up = radio, False

    def is_connected(self):
        return self.up

    async def connect(self, callback):
        if self.radio.refuse_connects:
            self.radio.refuse_connects -= 1
            raise ConnectionRefusedError(111, "Connection refused")
        self.radio.cb, self.up = callback, True

    async def send(self, msg):
        self.radio.handle(msg)

    async def send_bytes(self, data):
        raise AssertionError("the bridge never sends raw bytes")

    async def disconnect(self):
        self.up = False


REAL_CONTROLLER = ub.controller


def controller_for(radio):
    return lambda address, transport: RadioController(CommandConnection(FakeLink(radio)))


def last(ev):
    return next((e for e in reversed(EMITTED) if e["ev"] == ev), None)


async def write(bridge, cmd, **m):
    EMITTED.clear()
    await bridge.write({"cmd": cmd, "ref": 1, **m})
    return last("done")


def ub_status_event(radio):
    from benlink.command import StatusChangedEvent
    return StatusChangedEvent(radio.status())


def rf(raw):
    return Channel.from_protocol(p.RfCh.from_bytes(raw))


async def main():
    ub.install_vfo_guard()
    ub.RAW_TIMEOUT_S = 0.3
    ub.install_exact_check()
    ub.install_exact_check()                     # twice: still one wrapper
    check("exact check wraps benlink's decoder once",
          getattr(p.Message.from_bytes.__func__, "_exact_check", False), True)

    # --- connect, with the radio still refusing twice
    radio = FakeRadio(refuse_connects=2)
    ub.controller = controller_for(radio)
    ub.CONNECT_RETRY_S = 0.05
    r = await ub.connect("00:11:22:33:44:55", "rfcomm:4")
    check("connect: tried again while the radio refused", radio.refuse_connects, 0)
    b = ub.Bridge(r, 60)
    r.add_event_handler(b.on_event)
    EMITTED.clear()
    await b.everything()
    ex = last("exact")
    check("connect: every channel and VFO exact", ex["channels"], [0, 1, 2, 3, 4, 251, 252])
    check("connect: VFO A and B read", [(c["id"], c["rx_freq"]) for c in last("vfo")["channels"]],
          [(251, 146.52), (252, 446.0)])
    x = last("extras")["values"]
    check("connect: volume, regions, region, FM, APRS path, power control",
          (x["volume"], x["regions"][:3], x["region"], x["fm"], x["aprs_path"], x["power_control"]),
          (6, ["HOME", "TRAVEL", "AIR"], 0, {"on": False, "seeking": False, "freq": 91.5}, "WIDE1-1,WIDE2-1", True))
    check("connect: settings and beacon exact", (ex["settings"], ex["beacon"]), (True, True))
    check("connect: five channels read", len(last("channels")["channels"]), 5)

    # --- a channel write changes only the edited fields
    before = radio.ch[1]
    d = await write(b, "channel", id=1, values={"name": "RPT2", "power": "low", "scan": False})
    check("channel write: ok", (d["ok"], d["error"]), (True, None))
    a = rf(radio.ch[1])
    check("channel write: new values on the radio", (a.name, a.tx_at_max_power, a.scan), ("RPT2", False, False))
    old = rf(before)
    check("channel write: TX side untouched",
          [getattr(a, k) == getattr(old, k) for k in ub.CHANNEL_KEPT], [True] * len(ub.CHANNEL_KEPT))
    rest = {k for k in Channel.model_fields if getattr(a, k) != getattr(old, k)}
    check("channel write: nothing else changed", rest, {"name", "tx_at_max_power", "scan"})
    check("channel write: read back after (READ_RF_CH follows WRITE_RF_CH)",
          radio.sent[-2:], ["WRITE_RF_CH", "READ_RF_CH"])
    check("channel write: the page gets the read-back channel", last("channel")["channel"]["name"], "RPT2")

    d = await write(b, "channel", id=4, values={"tx_sub_audio": "D023", "rx_sub_audio": "100.0"})
    a = rf(radio.ch[4])
    check("tone write (DCS and CTCSS): ok, on the radio",
          (d["ok"], a.tx_sub_audio, a.rx_sub_audio), (True, DCS(n=23), 100.0))
    check("tone write: TX frequency still 467.65", a.tx_freq, 467.65)

    # --- frequencies
    d = await write(b, "channel", id=1, values={"rx_freq": 147.0, "tx_freq": 147.6})
    a = rf(radio.ch[1])
    check("frequency write: ok, on the radio", (d["ok"], a.rx_freq, a.tx_freq), (True, 147.0, 147.6))
    d = await write(b, "channel", id=3, values={"tx_disable": False})
    check("RX-only off: ok, on the radio", (d["ok"], rf(radio.ch[3]).tx_disable), (True, False))
    radio.on_write_rf = lambda raw: Channel.from_protocol(p.RfCh.from_bytes(raw)).model_copy(
        update={"tx_freq": 0.0}).to_protocol().to_bytes()          # as linux-station's radio did with airband
    d = await write(b, "channel", id=0, values={"tx_freq": 118.0})
    check("a TX the radio rewrites: reported as failed",
          (d["ok"], "tx_freq" in (d["error"] or ""), "rewrote" in (d["error"] or "")), (False, True, True))
    radio.on_write_rf = None
    await write(b, "channel", id=0, values={"tx_freq": 146.52})
    await write(b, "channel", id=1, values={"rx_freq": 146.94, "tx_freq": 146.34})

    # --- the VFOs
    d = await write(b, "channel", id=251, values={"rx_freq": 147.42, "tx_freq": 147.42})
    check("VFO A tuned: ok, record 251 on the radio", (d["ok"], rf(radio.vfo[251]).rx_freq), (True, 147.42))
    check("  the page gets the VFOs", [c["rx_freq"] for c in last("vfo")["channels"]], [147.42, 446.0])
    d = await write(b, "channel", id=252, values={"name": "X"})
    check("VFO: a name refused", (d["ok"], "has a name" in (d["error"] or "")), (False, True))
    radio.vfo[252] = ch(252, "MEMO", 446.0, 446.0).to_protocol().to_bytes()
    await r._conn.get_channel(252)                                  # noqa: SLF001
    await b.read_vfos()
    n = len(radio.writes)
    d = await write(b, "channel", id=252, values={"rx_freq": 446.1})
    check("named record 252: not a VFO, not written", (d["ok"], len(radio.writes) - n), (False, 0))
    radio.vfo[252] = ch(252, "", 446.0, 446.0).to_protocol().to_bytes()
    await b.read_vfos()
    d = await write(b, "channel", id=9, values={"name": "X"})
    check("no such channel: refused", (d["ok"], "no channel 10" in (d["error"] or "")), (False, True))
    from benlink.command import ChannelChangedEvent
    try:
        r._on_event_message(ChannelChangedEvent(Channel.from_protocol(p.RfCh.from_bytes(radio.vfo[251]))))  # noqa: SLF001
        check("benlink's state survives a VFO channel event", True, True)
    except Exception as e:     # noqa: BLE001
        check("benlink's state survives a VFO channel event", repr(e), True)

    # --- volume, region, FM, APRS path, power
    d = await write(b, "volume", values={"level": 12})
    check("volume 12: ok, on the radio, read back", (d["ok"], radio.volume, last("extras")["values"]),
          (True, 12, {"volume": 12}))
    check("  sent as SET_VOLUME then GET_VOLUME", radio.sent[-2:], ["SET_VOLUME", "GET_VOLUME"])
    d = await write(b, "volume", values={"level": 16})
    check("volume 16: refused", d["ok"], False)
    EMITTED.clear()
    d = await write(b, "region", values={"region": 1})
    check("region 2: ok, the radio is in it", (d["ok"], d["error"], radio.region), (True, None, 1))
    check("  the region's channels read again", last("channels")["channels"][0]["name"], "TRA 2M CAL")  # 10 characters
    check("  settings and the region told", (last("settings") is not None, last("extras")["values"]),
          (True, {"region": 1}))
    d = await write(b, "region", values={"region": 6})
    check("region 7 of 6: refused", d["ok"], False)
    EMITTED.clear()
    radio.region = 0                                                # switched on the radio itself
    radio.handle(p.Message(command_group=p.CommandGroup.BASIC, is_reply=False, command=p.BasicCommand.SET_REGION,
                           body=bytes([0])))
    b.on_event(ub_status_event(radio))
    await asyncio.sleep(0.2)
    check("region switched on the radio: channels read again",
          (last("channels") or {}).get("channels", [{}])[0].get("name"), "2M CALL")
    d = await write(b, "fm", values={"on": True})
    check("FM on: ok", (d["ok"], radio.fm["on"]), (True, True))
    d = await write(b, "fm", values={"freq": 101.1})
    check("FM 101.1: ok, sent in 10 kHz units", (d["ok"], radio.fm["freq10k"]), (True, 10110))
    d = await write(b, "fm", values={"seek": "down"})
    check("FM seek down: ok, read back", (d["ok"], last("extras")["values"]["fm"]["freq"]), (True, 100.7))
    d = await write(b, "aprs_path", values={"path": "WIDE2-1"})
    check("APRS path: ok, on the radio, read back", (d["ok"], radio.path, last("extras")["values"]),
          (True, "WIDE2-1", {"aprs_path": "WIDE2-1"}))
    d = await write(b, "power", values={"on": False})
    check("power off: ok, status says off", (d["ok"], radio.on, last("state")["status"]["is_power_on"]),
          (True, False, False))
    d = await write(b, "power", values={"on": True})
    check("power on: ok", (d["ok"], radio.on), (True, True))
    n = len(radio.sent)
    try:
        await ub.raw(r, "DO_PROG_FUNC", bytes([13]))
        check("raw(): DO_PROG_FUNC refused", "sent", "refused")
    except ValueError:
        check("raw(): DO_PROG_FUNC refused", "refused", "refused")
    check("  nothing sent", len(radio.sent) - n, 0)

    # --- a radio that moves the TX frequency on a write is caught
    radio.on_write_rf = lambda raw: Channel.from_protocol(p.RfCh.from_bytes(raw)).model_copy(
        update={"tx_freq": 146.94}).to_protocol().to_bytes()
    d = await write(b, "channel", id=1, values={"name": "RPT3"})
    check("radio moved TX on a write: reported as failed",
          (d["ok"], "tx_freq changed" in (d["error"] or "")), (False, True))
    radio.on_write_rf = lambda raw: Channel.from_protocol(p.RfCh.from_bytes(raw)).model_copy(
        update={"name": "OLD"}).to_protocol().to_bytes()
    d = await write(b, "channel", id=0, values={"name": "NEW"})
    check("radio didn't take the value: reported as failed",
          (d["ok"], "does not hold the new name" in (d["error"] or "")), (False, True))
    radio.on_write_rf = None

    # --- a channel benlink can't reproduce is never written
    lossy = None
    base = radio.ch[2]
    for bit in range(len(base) * 8):
        raw = flip(base, bit)
        try:
            x = p.RfCh.from_bytes(raw)
            x.to_bytes()              # the radio can send it
        except Exception:      # noqa: BLE001
            continue
        if not ub.exact("channel", x):
            lossy = raw
            break
    if lossy is None:
        # benlink reproduces every one-bit change of a channel: make one it can't, by
        # having the model drop a bit (as 0.1.1 dropped settings bits).
        real = Channel.to_protocol
        Channel.to_protocol = lambda self: p.RfCh.from_bytes(flip(real(self).to_bytes(), 7))
        try:
            await r._conn.get_channel(2)          # noqa: SLF001 - a re-read, as on connect
        finally:
            Channel.to_protocol = real
    else:
        radio.ch[2] = lossy
        await r._conn.get_channel(2)              # noqa: SLF001
    check("lossy channel: not exact", ub.EXACT.get(("channel", 2)), False)
    n = len(radio.writes)
    d = await write(b, "channel", id=2, values={"name": "X"})
    check("lossy channel: write refused", (d["ok"], "byte for byte" in d["error"]), (False, True))
    check("lossy channel: nothing sent to the radio", len(radio.writes), n)
    check("lossy channel: the page is told", 2 in last("exact")["channels"], False)

    # --- settings
    d = await write(b, "settings", values={"squelch_level": 3})
    s = Settings.from_protocol(p.Settings.from_bytes(radio.settings))
    check("settings write: ok, on the radio", (d["ok"], s.squelch_level), (True, 3))
    check("settings write: KISS left on", s.kiss_en, True)
    d = await write(b, "settings", values={"kiss_en": False})
    check("settings: KISS is not a field the bridge writes", (d["ok"], "not a setting" in d["error"]), (False, True))

    # --- beacon
    d = await write(b, "beacon", values={"beacon_message": "HELLO"})
    bs = BeaconSettings.from_protocol(p.BSSSettingsV2.from_bytes(radio.beacon))
    check("beacon write: ok, on the radio", (d["ok"], bs.beacon_message), (True, "HELLO"))
    lossy_b = None
    for bit in range(len(radio.beacon) * 8):
        raw = flip(radio.beacon, bit)
        try:
            x = p.BSSSettingsV2.from_bytes(raw)
            x.to_bytes()
        except Exception:      # noqa: BLE001
            continue
        if not ub.exact("beacon", x):
            lossy_b = raw
            break
    check("a beacon benlink can't reproduce exists (bit flip)", lossy_b is not None, True)
    radio.beacon = lossy_b
    await r._conn.get_beacon_settings()                   # noqa: SLF001
    n = len(radio.writes)
    d = await write(b, "beacon", values={"beacon_message": "AGAIN"})
    check("lossy beacon: write refused, nothing sent", (d["ok"], len(radio.writes) - n), (False, 0))

    # --- the bridge's own field lists
    d = await write(b, "channel", id=0, values={"tx_power_watts": 5})
    check("unknown channel field: refused", (d["ok"], "not a channel field" in d["error"]), (False, True))
    d = await write(b, "reset", values={})
    check("unknown command refused", d["ok"], False)

    await b.battery()
    check("battery read", (last("battery")["percent"], last("battery")["volts"]), (57, 7.3))
    check("never sent: TNC data, programmable functions, FREQ_MODE",
          sorted({"HT_SEND_DATA", "DO_PROG_FUNC", "FREQ_MODE_SET_PAR", "SET_PF"} & set(radio.sent)), [])
    check("writes the radio saw", sorted({w[0] for w in radio.writes}),
          ["aprs_path", "beacon", "channel", "power", "region", "settings", "volume"])

    # --- through the packet hub's control port (tcp:HOST:PORT): Gaia frames over TCP
    from benlink.protocol.command.bitfield import BitStream
    radio2 = FakeRadio()

    async def on_client(reader, writer):
        def to_client(m):
            b = m.to_bytes()
            writer.write(p.GaiaFrame(flags=p.GaiaFlags.NONE, n_bytes_payload=len(b) - 4, data=b).to_bytes())
        radio2.cb = to_client
        buf = BitStream()
        while True:
            data = await reader.read(4096)
            if not data:
                break
            buf = buf.extend_bytes(data)
            frames, buf = p.GaiaFrame.from_bitstream_batch(buf)
            for f in frames:
                radio2.handle(p.Message.from_bytes(f.data))
        writer.close()
    srv = await asyncio.start_server(on_client, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    r2 = REAL_CONTROLLER("00:11:22:33:44:55", f"tcp:127.0.0.1:{port}")
    await asyncio.wait_for(r2.connect(), 10)
    b2 = ub.Bridge(r2, 60)
    EMITTED.clear()
    await b2.everything()
    check("control port: read everything over TCP", (len(last("channels")["channels"]), last("extras")["values"]["volume"]),
          (5, 6))
    d = await write(b2, "volume", values={"level": 9})
    check("control port: a write goes through", (d["ok"], radio2.volume), (True, 9))
    await r2.disconnect()
    srv.close()

    # --- a radio that never stops refusing: connect gives up with the reason
    ub.CONNECT_TIMEOUT_S = 0.5
    ub.controller = controller_for(FakeRadio(refuse_connects=10 ** 6))
    try:
        await ub.connect("00:11:22:33:44:55", "rfcomm:4")
        check("radio refuses for good: connect gives up", "connected", "raised")
    except ConnectionRefusedError:
        check("radio refuses for good: connect gives up", "raised", "raised")


asyncio.run(main())
OUT.write("\nall passed\n" if not fails else f"\n{fails} FAILED\n")
OUT.flush()
sys.exit(1 if fails else 0)
