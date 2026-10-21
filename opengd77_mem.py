"""OpenGD77 MD-UV380/390 memory: the radio's settings, last-used channels and
VFOs, decoded and encoded byte for byte. Pure functions, no I/O.

OpenGD77 has no CAT, so the page changes the radio the way the radio itself
keeps its state across a restart: in its flash. On the MD-UV380 the
"EEPROM" is the bottom of the SPI flash (EEPROM.c,
MDUV380_EMULATED_EEPROM_ADDRESS_OFFSET 0), and three records there are
what the radio comes up on:

  LUCZ      0x6000, 74 bytes: "LUCZ", then one byte per zone (the channel's
            index in that zone, 0-based) and, at offset 68, a little-endian
            int16 for All Channels (the channel number - 1)
            (codeplug.c codeplugSetLastUsedChannelInZone; the All Channels
            entry lands at 68 because SAFE_MIN(-1, 68U) compares unsigned)
  settings  0x604B, 116 bytes: settingsStruct_t (settings.h), magic 0x4780
  VFO A, B  0x7590, 56 bytes each, in codeplug form (codeplug.c
            codeplugSetVFO_ChannelData: BCD frequencies, CSS tones)

The radio keeps all three in RAM and writes them back only at power off
or when asked over USB (usb_com.c 'C' 6 2: settingsSaveSettings(true);
settingsSaveIfNeeded is a no-op on this platform). So a change is:
save (C 6 2), read, patch, write the flash sector (X 1/2/3), restart
without saving (C 6 1). opengd77.Control does that; this module only
says which bytes.

Every offset and range here is from the R20260131 source (5797626,
the build on mac-shack since 2026-10-17) and was checked against that
radio's own bytes (2026-10-20: magic 0x4780, squelch 10/10/10, master
power level 6 = the 3W on its screen, zone 4 = All Channels with four
zones). Another build may move them: PINNED_REVISIONS refuses it.
"""

import struct

PINNED_REVISIONS = {"5797626"}     # OpenGD77 R20260131 (MD-UV380 10 W PLUS)
MAGIC = 0x4780                     # settings.c STORAGE_MAGIC_NUMBER, R20260131

LUCZ_ADDR = 0x6000
LUCZ_LEN = 4 + 70                  # "LUCZ" + lastUsedChannelInZoneData[CODEPLUG_ALL_ZONES_MAX + 1]
LUCZ_ALL = 68                      # offset of the All Channels int16 in the data
SETTINGS_ADDR = 0x604B             # settingsStorage.c STORAGE_BASE_ADDRESS
SETTINGS_LEN = 116                 # sizeof(settingsStruct_t), arm-none-eabi (short enums)
VFO_ADDR = 0x7590
CHANNEL_SIZE = 56
VFO_LEN = 2 * CHANNEL_SIZE

# What may ever be written, and nothing else: the three records above. The
# link refuses any flash write outside these spans (and so any other sector,
# calibration at 0x10000 above all).
WRITABLE = ((LUCZ_ADDR, LUCZ_LEN), (SETTINGS_ADDR, SETTINGS_LEN), (VFO_ADDR, VFO_LEN))
SECTOR = 4096

UI_VFO_MODE = 33                   # menuSystem.h MENU_SCREENS, R20260131 (stored 34 in channel mode)
UI_CHANNEL_MODE = 34

# Hardware bands the firmware accepts for a VFO (trx.c RADIO_HARDWARE_FREQUENCY_BANDS,
# non-datasheet ranges); outside them settingsVFOSanityCheck resets the VFO.
BANDS_HZ = ((127_000_000, 178_000_000), (190_000_000, 282_000_000), (380_000_000, 564_000_000))
STEPS_HZ = (2500, 5000, 6250, 10000, 12500, 25000, 30000, 50000)   # codeplug.c VFO_FREQ_STEP_TABLE
POWER_NAMES = ("50mW", "250mW", "500mW", "750mW", "1W", "2W", "3W", "5W", "10W", "+W-")  # master levels 0..9
CTCSS = (670, 693, 719, 744, 770, 797, 825, 854, 885, 915, 948, 974, 1000, 1035, 1072, 1109, 1148, 1188,
         1230, 1273, 1318, 1365, 1413, 1462, 1514, 1567, 1598, 1622, 1655, 1679, 1713, 1738, 1773, 1799,
         1835, 1862, 1899, 1928, 1966, 1995, 2035, 2065, 2107, 2181, 2257, 2291, 2336, 2418, 2503, 2541)
DCS = (0x023, 0x025, 0x026, 0x031, 0x032, 0x043, 0x047, 0x051, 0x054, 0x065, 0x071, 0x072, 0x073, 0x074,
       0x114, 0x115, 0x116, 0x125, 0x131, 0x132, 0x134, 0x143, 0x152, 0x155, 0x156, 0x162, 0x165, 0x172,
       0x174, 0x205, 0x223, 0x226, 0x243, 0x244, 0x245, 0x251, 0x261, 0x263, 0x265, 0x271, 0x306, 0x311,
       0x315, 0x331, 0x343, 0x345, 0x351, 0x364, 0x365, 0x371, 0x411, 0x412, 0x413, 0x423, 0x431, 0x432,
       0x445, 0x464, 0x465, 0x466, 0x503, 0x506, 0x516, 0x532, 0x546, 0x565, 0x606, 0x612, 0x624, 0x627,
       0x631, 0x632, 0x654, 0x662, 0x664, 0x703, 0x712, 0x723, 0x731, 0x732, 0x734, 0x743, 0x754)


class MemError(ValueError):
    pass


def _choices(*names):
    return [[i, n] for i, n in enumerate(names)]


def _pct(v):
    return f"{(v - 1) * 5}%"


# The settings the page offers, as the radio's own Options menus do (ranges
# from menuRadioOptions.c, menuSoundOptions.c, menuDisplayOptions.c,
# menuGeneralOptions.c). (key, offset, group, label, values). values is
# [min, max] or [min, max, step], or a list of [value, label]. Anything not
# listed is never written (hotspot mode, calibration trims, GPS NMEA output
# that blocks the USB link, ...).
SETTINGS = [
    ("power", 70, "Radio", "Master power", _choices(*POWER_NAMES)),
    ("squelch_vhf", 102, "Radio", "Squelch VHF", [1, 21]),
    ("squelch_220", 103, "Radio", "Squelch 220", [1, 21]),
    ("squelch_uhf", 104, "Radio", "Squelch UHF", [1, 21]),
    ("band_limits", 83, "Radio", "TX band limits", _choices("Off", "Amateur bands", "From CPS")),
    ("tot_beep", 71, "Radio", "TX timeout warning", _choices("Off", "5 s", "10 s", "15 s", "20 s")),
    ("eco", 105, "Radio", "Power saving (eco)", [0, 5]),
    ("apo", 106, "Radio", "Auto power off", [[i, "Off" if not i else f"{i * 30 // 60}h{i * 30 % 60:02d}"
                                               if i * 30 >= 60 else f"{i * 30} min"] for i in range(25)]),
    ("autolock", 109, "Radio", "Keypad auto-lock", [[i, "Off" if not i else f"{i} min"] for i in range(31)]),
    ("beep_volume", 72, "Sound", "Beep volume", [[i, f"{(2 - i) * 3} dB"] for i in range(11)]),
    ("mic_dmr", 73, "Sound", "DMR mic gain", [[i, f"{(i - 5) * 3} dB"] for i in range(16)]),
    ("mic_fm", 74, "Sound", "FM mic gain", [[i, f"{(i - 4) * 3} dB"] for i in range(16)]),
    ("prompts", 99, "Sound", "Audio prompts", _choices("Silent", "Beep", "No key beep", "Voice 1", "Voice 2",
                                                       "Voice 3")),
    ("backlight_mode", 75, "Display", "Backlight", _choices("Auto", "Squelch", "Manual", "Buttons", "None")),
    ("backlight_timeout", 76, "Display", "Backlight timeout", [[i, "Never" if not i else f"{i} s"]
                                                               for i in range(0, 31, 5)]),
    ("brightness_day", 78, "Display", "Brightness (day)", [0, 100]),
    ("brightness_night", 79, "Display", "Brightness (night)", [0, 100]),
    ("brightness_off", 80, "Display", "Brightness when off", [0, 100]),
    ("info_on_screen", 82, "Display", "Show TS / power", _choices("Off", "Timeslot", "Power", "Both")),
    ("contact_priority", 95, "Display", "Contact display", _choices("CC/DB/TA", "DB/CC/TA", "TA/CC/DB", "TA/DB/CC")),
    ("split_contact", 96, "Display", "Contact lines", _choices("One line", "Two lines", "Auto")),
    ("cc_ts_filter", 92, "DMR", "CC / TS filter", _choices("None", "CC", "TS", "CC + TS")),
    ("dest_filter", 90, "DMR", "Destination filter", _choices("None", "TG", "DC", "RxG")),
    ("capture_timeout", 91, "DMR", "Capture timeout (s)", [2, 90]),
    ("private_calls", 94, "DMR", "Private calls", _choices("Off", "On", "PTT")),
    ("dmr_agc", 86, "DMR", "DMR RX AGC", [[i, "Off" if not i else str(i)] for i in range(9)]),
    ("scan_delay", 85, "Scan", "Scan delay (s)", [1, 30]),
    ("scan_mode", 84, "Scan", "Scan on signal", _choices("Hold", "Pause", "Stop")),
    ("scan_step", 88, "Scan", "Scan step time", [[i, f"{30 + i * 30} ms"] for i in range(16)]),
]
_BY_KEY = {s[0]: s for s in SETTINGS}


def _allowed(values, v):
    if isinstance(values[0], list):
        return v in {x for x, _ in values}
    lo, hi = values[0], values[1]
    step = values[2] if len(values) > 2 else 1
    return lo <= v <= hi and (v - lo) % step == 0


# ------------------------------------------------------------------ settings

def check_settings(b):
    """Raise unless b is a settings record of the build this module knows."""
    if len(b) != SETTINGS_LEN:
        raise MemError(f"settings are {len(b)} bytes, not {SETTINGS_LEN}")
    magic = struct.unpack_from("<I", b, 0)[0]
    if magic != MAGIC:
        raise MemError(f"settings magic {magic:#x}, not {MAGIC:#x}: another firmware build")


def settings(b):
    """The page's view of a settings record: {key: value} for SETTINGS, plus
    zone, mode ("channel" | "vfo" | None), active VFO (0 = A), timezone and
    the GPS mode (read only)."""
    check_settings(b)
    out = {k: b[off] for k, off, *_ in SETTINGS}
    out["zone"] = struct.unpack_from("<h", b, 58)[0]
    menu = b[81]
    out["mode"] = "channel" if menu == UI_CHANNEL_MODE else "vfo" if menu == UI_VFO_MODE else None
    out["vfo"] = b[89]
    tz = b[12]
    out["tz_minutes"] = ((tz & 0x7F) - 64) * 15      # offset from UTC
    out["tz_local"] = bool(tz & 0x80)                # the radio shows local time (else UTC)
    out["gps"] = {0: "not detected", 1: "off", 2: "on", 3: "on (NMEA)", 4: "on (logging)"}.get(b[111] & 0x0F, "?")
    out["user_power_mw"] = struct.unpack_from("<H", b, 60)[0]
    return out


def patch_settings(b, changes):
    """b with changes applied: {key: value} for SETTINGS keys, and zone,
    mode, vfo, tz_minutes, tz_local. Every value is range-checked against the
    radio's own menus; anything else raises before a byte changes."""
    check_settings(b)
    out = bytearray(b)
    for k, v in changes.items():
        if k in _BY_KEY:
            _, off, _, label, values = _BY_KEY[k]
            if not isinstance(v, int) or isinstance(v, bool) or not _allowed(values, v):
                raise MemError(f"{label}: {v!r} is not a value the radio offers")
            out[off] = v
        elif k == "zone":
            if not isinstance(v, int) or not 0 <= v <= 68:
                raise MemError(f"zone {v!r}")
            struct.pack_into("<h", out, 58, v)
        elif k == "mode":
            if v not in ("channel", "vfo"):
                raise MemError(f"mode {v!r}")
            out[81] = UI_CHANNEL_MODE if v == "channel" else UI_VFO_MODE
        elif k == "vfo":
            if v not in (0, 1):
                raise MemError(f"VFO {v!r}")
            out[89] = v
        elif k == "tz_minutes":
            if not isinstance(v, int) or v % 15 or not -12 * 60 <= v <= 14 * 60:
                raise MemError(f"timezone {v!r} min")
            out[12] = (out[12] & 0x80) | (64 + v // 15)
        elif k == "tz_local":
            out[12] = (out[12] & 0x7F) | (0x80 if v else 0)
        else:
            raise MemError(f"no setting {k!r}")
    return bytes(out)


def settings_schema():
    """What the page draws: [{key, group, label, values}]."""
    return [{"key": k, "group": g, "label": label, "values": values} for k, _, g, label, values in SETTINGS]


# ------------------------------------------------------------------ last used channel in zone

def lucz(b):
    """{"zones": [index in zone, 0-based, for zones 0..67], "all": channel number} or
    None when the record is unset (the radio then starts each zone at its first channel)."""
    if len(b) != LUCZ_LEN:
        raise MemError(f"LUCZ is {len(b)} bytes, not {LUCZ_LEN}")
    if b[:4] != b"LUCZ":
        return None
    return {"zones": list(b[4:4 + 68]), "all": struct.unpack_from("<h", b, 4 + LUCZ_ALL)[0] + 1}


def patch_lucz(b, zone=None, index=None, all_channel=None):
    """b with zone's channel index (0-based) and/or All Channels' channel
    number set. An unset record is started from zeros, as the radio does."""
    if len(b) != LUCZ_LEN:
        raise MemError(f"LUCZ is {len(b)} bytes, not {LUCZ_LEN}")
    out = bytearray(b) if b[:4] == b"LUCZ" else bytearray(b"LUCZ" + bytes(70))
    if zone is not None:
        if not 0 <= zone < 68 or not 0 <= index < 80:
            raise MemError(f"zone {zone} channel index {index}")
        out[4 + zone] = index
    if all_channel is not None:
        if not 1 <= all_channel <= 1024:
            raise MemError(f"channel {all_channel}")
        struct.pack_into("<h", out, 4 + LUCZ_ALL, all_channel - 1)
    return bytes(out)


# ------------------------------------------------------------------ VFO channels

def _bcd_out(n):
    return int(str(n), 16)


def _bcd_in(v):
    s = "%x" % v
    if not s.isdigit():
        raise MemError(f"not BCD: {v:#x}")
    return int(s)


def tone_code(t):
    """"" | "100.0" | "D023N" | "D023I" -> the codeplug CSS value."""
    t = (t or "").strip().upper()
    if not t:
        return 0xFFFF
    if t.startswith("D"):
        inv = t.endswith("I")
        code = t[1:].rstrip("NI")
        try:
            v = int(code, 16)
        except ValueError:
            raise MemError(f"DCS {t!r}")
        if v not in DCS or any(c not in "01234567" for c in code):
            raise MemError(f"DCS {t!r} is not one the radio has")
        return 0x8000 | v | (0x4000 if inv else 0)
    try:
        tenths = round(float(t) * 10)
    except ValueError:
        raise MemError(f"tone {t!r}")
    if tenths not in CTCSS:
        raise MemError(f"CTCSS {t} Hz is not one the radio has")
    return _bcd_out(tenths)


def _in_band(hz):
    return any(lo <= hz <= hi for lo, hi in BANDS_HZ)


def vfo(b):
    """A 56-byte codeplug-form VFO: the fields the page edits."""
    if len(b) != CHANNEL_SIZE:
        raise MemError(f"VFO is {len(b)} bytes")
    rx, tx = struct.unpack_from("<II", b, 16)
    rxt, txt = struct.unpack_from("<HH", b, 32)
    from opengd77 import tone
    return {"rx_hz": _bcd_in(rx) * 10, "tx_hz": _bcd_in(tx) * 10, "mode": "DMR" if b[24] == 1 else "FM",
            "power": b[25], "rx_tone": tone(rxt), "tx_tone": tone(txt), "colour_code": b[44] & 0x0F,
            "timeslot": 2 if b[49] & 0x40 else 1, "wide": bool(b[51] & 0x02), "rx_only": bool(b[51] & 0x04),
            "step": (b[54] >> 4) & 0x07, "sql": b[55]}


def patch_vfo(b, ch):
    """b (56 bytes) with ch's fields; bytes the page doesn't edit stay as the radio had them."""
    if len(b) != CHANNEL_SIZE:
        raise MemError(f"VFO is {len(b)} bytes")
    out = bytearray(b)
    for k, v in ch.items():
        if k in ("rx_hz", "tx_hz"):
            if not isinstance(v, int) or v % 10 or not _in_band(v):
                raise MemError(f"{k[:2].upper()} {v!r} Hz is outside the radio's bands "
                               f"(127-178, 190-282, 380-564 MHz)")
            struct.pack_into("<I", out, 16 if k == "rx_hz" else 20, _bcd_out(v // 10))
        elif k == "mode":
            if v not in ("FM", "DMR"):
                raise MemError(f"mode {v!r}")
            out[24] = 1 if v == "DMR" else 0
        elif k == "power":
            if not isinstance(v, int) or not 0 <= v <= 10:
                raise MemError(f"power level {v!r}")
            out[25] = v
        elif k in ("rx_tone", "tx_tone"):
            struct.pack_into("<H", out, 32 if k == "rx_tone" else 34, tone_code(v))
        elif k == "colour_code":
            if not isinstance(v, int) or not 0 <= v <= 15:
                raise MemError(f"colour code {v!r}")
            out[44] = v
        elif k == "timeslot":
            if v not in (1, 2):
                raise MemError(f"timeslot {v!r}")
            out[49] = (out[49] & ~0x40) | (0x40 if v == 2 else 0)
        elif k == "wide":
            out[51] = (out[51] & ~0x02) | (0x02 if v else 0)
        elif k == "rx_only":
            out[51] = (out[51] & ~0x04) | (0x04 if v else 0)
        elif k == "step":
            if not isinstance(v, int) or not 0 <= v < len(STEPS_HZ):
                raise MemError(f"step {v!r}")
            out[54] = (out[54] & 0x8F) | (v << 4)
        elif k == "sql":
            if not isinstance(v, int) or not 0 <= v <= 21:
                raise MemError(f"squelch {v!r}")
            out[55] = v
        else:
            raise MemError(f"no VFO field {k!r}")
    return bytes(out)


# ------------------------------------------------------------------ writes

def spans(addr, old, new):
    """[(address, bytes)] of the runs where new differs from old."""
    if len(old) != len(new):
        raise MemError("length changed")
    out, i = [], 0
    while i < len(new):
        if old[i] == new[i]:
            i += 1
            continue
        j = i
        while j < len(new) and old[j] != new[j]:
            j += 1
        out.append((addr + i, bytes(new[i:j])))
        i = j
    return out


def writable(addr, length):
    """Is [addr, addr+length) inside one of the records the page may write?"""
    return length > 0 and any(a <= addr and addr + length <= a + n for a, n in WRITABLE)
