"""Radio profiles: what each supported rig can do and how to talk to it.

The UI reads `caps` to decide which controls to show, so a profile should only
claim what the radio really does. CI-V details for the Xiegu rigs follow
Hamlib's rigs/icom/xiegu.c.

`tested` marks profiles verified against real hardware; the rest are built from
the manufacturers' CI-V references and Hamlib, and should be treated as
"expected to work".
"""

ICOM_HF_MODES = ["LSB", "USB", "AM", "CW", "RTTY", "FM", "CW-R", "RTTY-R"]
ICOM_AGC = [[1, "FAST"], [2, "MID"], [3, "SLOW"]]

# Raw S-meter -> dB relative to S9 (Hamlib X108G_STR_CAL, shared by Xiegu rigs).
XIEGU_STR_CAL = [(0, -54), (5, -29), (15, -27), (43, -22), (68, -17), (92, -12),
                 (120, -6), (141, 2), (162, 13), (182, 25), (202, 38), (222, 47),
                 (241, 57), (255, 63)]

_ICOM_FULL = {
    "backend": "civ", "baud": 115200, "modes": ICOM_HF_MODES,
    "data_modes": ["LSB", "USB", "AM", "FM"], "filters": 3, "agc": ICOM_AGC,
    "preamp": ["OFF", "AMP 1", "AMP 2"], "att_on": 0x20, "att_label": "20 dB",
    "levels": ["af", "rf", "sql", "power"], "meters": ["smeter", "po", "swr", "alc"],
    "features": ["nb", "nr", "tuner", "split", "vfo_b", "power_on"],
    "scope": True, "smeter_cal": None,
}

PROFILES = {
    "ic7300": {**_ICOM_FULL, "name": "IC-7300", "addr": 0x94, "max_watts": 100,
               "freq": [30_000, 74_800_000], "tested": True,
               # 1A 05 00 66 "MOD input connector during DATA OFF" (IC-7300
               # manual: 00=MIC, 01=ACC, 02=MIC/ACC, 03=USB; the MK2 moved it
               # to 00 84). The page's voice in plain USB/LSB needs USB there;
               # it is set for the transmission and put back (mac-shack, 2026-10-18).
               "data_off_mod": [0x00, 0x66], "data_off_mod_usb": 0x03},
    "ic705": {**_ICOM_FULL, "name": "IC-705", "addr": 0xA4, "max_watts": 10,
              "freq": [30_000, 470_000_000]},
    "ic7610": {**_ICOM_FULL, "name": "IC-7610", "addr": 0x98, "max_watts": 100,
               "freq": [30_000, 60_000_000]},
    "ic9700": {**_ICOM_FULL, "name": "IC-9700", "addr": 0xA2, "max_watts": 100,
               "freq": [144_000_000, 1_300_000_000],
               "features": ["nb", "nr", "split", "vfo_b", "power_on"]},
    "ic7100": {**_ICOM_FULL, "name": "IC-7100", "addr": 0x88, "max_watts": 100,
               "freq": [30_000, 470_000_000], "scope": False},
    "g90": {
        "name": "Xiegu G90", "backend": "civ", "addr": 0x88, "baud": 19200,
        "max_watts": 20, "freq": [500_000, 30_000_000],
        "modes": ["LSB", "USB", "AM", "CW", "CW-R", "FM"],
        # Verified on a real G90 (2026-10-13): 1A 06 reads back "01 00" in
        # U-D. Its line audio to a Digirig only flows in U-D/L-D, and a plain
        # mode set (06) leaves data mode, so data is kept across LSB<->USB.
        "data_modes": ["LSB", "USB"], "data_sticky": True,
        # So audio from the computer keys at 0 W in plain USB/LSB (linux-station,
        # 2026-10-17): the page refuses voice there and asks for DATA.
        "tx_audio_needs_data": True,
        # F13 (linux-station 2026-10-14, fw 1.81): the G90 acks 1A 06 01 but stays in
        # plain USB, and a 1A 06 read echoes the last set ("01 00" while the
        # display says USB). Mode, data and filter go through 26 00 instead
        # (CI-V 0x26 added in G90 fw 1.80).
        "mode_cmd_26": True,
        # F16 (linux-station 2026-10-13/14, fw 1.81): 15 12 is SWR as binary 8.8 fixed
        # point, not BCD on Icom's scale (01 4c = 1.30; 00 00 at a match). Read
        # as BCD it shows ~3x high, as Hamlib #1915 and flrig users report.
        "swr_fixed88": True,
        # Log each change of the raw PO/SWR/ALC replies (calibration aid; only
        # here: an IC-7300's PO changes on nearly every poll while keyed).
        "log_meter_raw": True,
        "filters": 3, "agc": [[0, "OFF"], [1, "FAST"], [2, "SLOW"], [3, "AUTO"]],
        "preamp": ["OFF", "ON"], "att_on": 0x01, "att_label": "ON",
        # No "power": the G90 reads it back as 0 whatever it is, and a CAT
        # set from the browser slider left it at 0 W out (linux-station, 2026-10-13).
        # RF power is set by hand on the radio.
        # PO (15 11) and ALC (15 13) are 0-255 meters in Xiegu's CI-V
        # reference v1.0, like SWR; the first profile left them out unexplained.
        "levels": ["af", "rf", "sql"], "meters": ["smeter", "po", "swr", "alc"],
        "features": ["nb", "tuner", "split"],
        "scope": False, "smeter_cal": XIEGU_STR_CAL,
        "tested": True,
    },
    "x6100": {
        "name": "Xiegu X6100", "backend": "civ", "addr": 0xA4, "baud": 19200,
        "max_watts": 10, "freq": [30_000, 54_000_000],
        "modes": ["LSB", "USB", "AM", "CW", "CW-R", "FM"], "data_modes": ["LSB", "USB"],
        "filters": 3, "agc": [[0, "OFF"], [1, "FAST"], [2, "SLOW"], [3, "AUTO"]],
        "preamp": ["OFF", "ON"], "att_on": 0x01, "att_label": "ON",
        "levels": ["af", "rf", "sql", "power"], "meters": ["smeter", "swr", "alc"],
        "features": ["nb", "nr", "tuner", "split"],
        "scope": False, "smeter_cal": XIEGU_STR_CAL,
    },
    # QRP Labs QMX / QMX+: native CAT (qmx.py), not Hamlib model 2057, whose
    # beta driver offers no USB/LSB. Built from the QMX CAT manual (firmware
    # 1_04_003) and Hamlib's encoding. Run on a real QMX on linux-station (firmware
    # 1_04_010) since 2026-10-15: CAT, receive audio, and FT8 transmit keyed
    # by JTDX over rigctl (2026-10-19 and 10-21). The DiGi sideband (MD6, not
    # MD9) and Winlink's USB mode (DiGi keyed 0 W for ARDOP) were found there.
    "qmx": {
        "name": "QRP Labs QMX", "backend": "qmx", "addr": None,
        # USB virtual COM port: the speed setting is ignored by the radio.
        "baud": 115200,
        # Nominal 5 W; RF power follows the supply voltage, no CAT control.
        "max_watts": 5,
        # QMX+ covers 160-6 m; a QMX kit covers a subset and the radio
        # itself refuses bands it isn't built for (read back corrects us).
        "freq": [1_800_000, 54_000_000],
        # USB/LSB are SSB; USB/LSB + data are the QMX's DiGi mode (MD6/MD9),
        # which WSJT-X/JTDX FSK modes need. AM left out until checked.
        "modes": ["LSB", "USB", "CW", "CW-R"], "data_modes": ["LSB", "USB"],
        "filters": 0, "agc": [], "preamp": ["OFF"], "att_on": None, "att_label": "ON",
        # AF/RF gain exist (AG/RG); left out until their effect on the USB
        # audio a remote operator hears is checked on a real radio.
        "levels": [], "meters": ["smeter", "po", "swr"],
        "features": ["split", "vfo_b"],
        "scope": False, "smeter_cal": None,
        "tested": True,
    },
    # Anything Hamlib supports (Yaesu, Kenwood, Elecraft, ...), via rigctld.
    # Capabilities are discovered from the radio when it connects.
    "hamlib": {
        "name": "Hamlib rig", "backend": "hamlib", "baud": None, "addr": None,
        "max_watts": 100, "freq": [30_000, 470_000_000],
        "modes": ["LSB", "USB", "AM", "CW", "CW-R", "RTTY", "RTTY-R", "FM"],
        "data_modes": ["LSB", "USB", "FM"], "filters": 0,
        "agc": [], "preamp": ["OFF", "ON"], "att_on": None, "att_label": "ON",
        "levels": ["af", "rf", "sql", "power"], "meters": ["smeter", "po", "swr", "alc"],
        "features": ["nb", "nr", "tuner", "split"],
        "scope": False, "smeter_cal": None,
    },
}


def get(name):
    try:
        prof = dict(PROFILES[name])
    except KeyError:
        raise SystemExit(f"unknown rig {name!r}; choose from: {', '.join(PROFILES)}")
    prof["id"] = name
    prof.setdefault("tested", False)
    prof.setdefault("unreadable", [])   # settable but not reported by the radio
    prof.setdefault("data_sticky", False)  # re-assert DATA after a mode change
    prof.setdefault("tx_audio_needs_data", False)  # line audio only reaches TX with DATA on
    prof.setdefault("data_off_mod", None)  # CI-V 1A 05 sub-command: DATA OFF modulation source
    return prof


def describe():
    rows = []
    for key, p in PROFILES.items():
        bits = [f"{p['max_watts']} W"]
        if p["backend"] == "civ":
            bits.append(f"CI-V {p['addr']:02X}h @ {p['baud']}")
        elif p["backend"] == "qmx":
            bits.append("Kenwood-style CAT (USB)")
        else:
            bits.append("via rigctld")
        if p["scope"]:
            bits.append("scope")
        if p.get("tested"):
            bits.append("tested")
        rows.append(f"  {key:8} {p['name']:14} {', '.join(bits)}")
    return "\n".join(rows)


def caps_for_client(p):
    """The subset of a profile the browser needs."""
    keys = ["id", "name", "max_watts", "freq", "modes", "data_modes", "filters", "agc",
            "preamp", "att_label", "levels", "meters", "features", "scope", "tested",
            "tx_audio_needs_data"]
    return {k: p[k] for k in keys}
