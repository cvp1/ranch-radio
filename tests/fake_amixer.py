"""A stand-in for ALSA's `amixer`, enough for codec_level_test.py.

    amixer -c CARD sget CONTROL
    amixer -q -c CARD -- sset CONTROL -38.00dB

Environment:
  FAKE_AMIXER_STATE  file holding the control's level in dB (default 0.00)
  FAKE_AMIXER_LOG    file this fake appends one JSON line per call to
  FAKE_AMIXER_FAIL   "set": every sset fails; "get": every sget fails
  FAKE_AMIXER_CARD   the only card that exists (default CODEC)
  FAKE_AMIXER_SLEEP  seconds every sset takes (a slow mixer: opens the race
                     between preparing a transmission and keying it)
"""
import json
import os
import re
import sys

STATE = os.environ.get("FAKE_AMIXER_STATE", "/dev/null")
FAIL = os.environ.get("FAKE_AMIXER_FAIL", "")
CARD = os.environ.get("FAKE_AMIXER_CARD", "CODEC")
argv = sys.argv[1:]
if os.environ.get("FAKE_AMIXER_LOG"):
    with open(os.environ["FAKE_AMIXER_LOG"], "a") as f:
        f.write(json.dumps(argv) + "\n")

card = argv[argv.index("-c") + 1] if "-c" in argv else "0"
args = [a for i, a in enumerate(argv) if a not in ("-q", "-c", "--") and (i == 0 or argv[i - 1] != "-c")]
if card != CARD:
    sys.exit(f"amixer: Invalid card number '{card}'")


def level():
    try:
        with open(STATE) as f:
            return float(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0.0


cmd, control = args[0], args[1]
if control != "PCM":
    sys.exit(f"amixer: Unable to find simple control '{control}',0")
if cmd == "sget":
    if FAIL == "get":
        sys.exit("amixer: Mixer attach hw:CODEC error: No such device")
    db = level()
    print(f"Simple mixer control 'PCM',0\n  Capabilities: pvolume pswitch pswitch-joined\n"
          f"  Playback channels: Front Left - Front Right\n  Limits: Playback 0 - 128\n  Mono:\n"
          f"  Front Left: Playback 90 [70%] [{db:.2f}dB] [on]\n"
          f"  Front Right: Playback 90 [70%] [{db:.2f}dB] [on]")
elif cmd == "sset":
    if os.environ.get("FAKE_AMIXER_SLEEP"):
        import time
        time.sleep(float(os.environ["FAKE_AMIXER_SLEEP"]))
    if FAIL == "set":
        sys.exit("amixer: Mixer attach hw:CODEC error: No such device")
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)dB", args[2])
    if not m:
        sys.exit(f"amixer: Invalid command! ({args[2]})")
    with open(STATE, "w") as f:
        f.write(m.group(1))
else:
    sys.exit(f"amixer: Unknown command '{cmd}'")
