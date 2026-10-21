#!/usr/bin/env python3
"""A fake qdmr dmrconf for tests: the radio's codeplug is a file.

FAKE_DMRCONF_DIR holds radio.yaml (what the radio holds) and calls.log (one
line per call: start and end time, then the arguments). Flag files in it:
A write also leaves layout.json: the radio's memory after it, channels in
slots 1..N and the zones by those numbers, as dmrconf's encode does (a fake
radio follows it). "reboot" holds a number: after a write, that many reads fail the way they
do while the radio restarts. "old" plays dmrconf 0.11.3 (no openuv380 driver); "drop" makes a write lose
its last channel, so the read-back differs; "warn" makes read print qdmr's
zone-linking warning. encode writes the file's meaning as its "binary" (so two
files that say the same encode the same). verify and encode refuse a file containing
"BAD" (so does write); a "crash" flag file makes verify die on SIGSEGV
like dmrconf 0.15.1 with openuv380. Each read and write takes FAKE_DMRCONF_SLOW seconds (default 0.5)."""
import atexit
import os
import signal
import sys
import time

d = os.environ["FAKE_DMRCONF_DIR"]
args = sys.argv[1:]
t0 = time.time()


@atexit.register
def _log():
    with open(os.path.join(d, "calls.log"), "a") as f:
        f.write(f"{t0:.3f} {time.time():.3f} " + " ".join(args) + "\n")


old = os.path.exists(os.path.join(d, "old"))
slow = float(os.environ.get("FAKE_DMRCONF_SLOW", "0.5"))
radio = os.path.join(d, "radio.yaml")

if args == ["--version"]:
    print("dmrconf 0.11.3" if old else "dmrconf 0.15.1")
elif args == ["--list-radios"]:
    print(" Key              | Name              | Manufacturer")
    print("------------------+-------------------+-------------")
    print(" opengd77         | OpenGD77          | OpenGD77 Project")
    if not old:
        print(" openuv380        | OpenMDUV380       | OpenGD77 Project")
    print(" uv390            | MD-UV390          | TyT")
elif args and args[0] == "read":
    time.sleep(slow)
    left = os.path.join(d, "rebooting")
    if os.path.exists(left):
        n = int(open(left).read() or 0)
        if n > 0:
            open(left, "w").write(str(n - 1))
            print("ERROR in cli/readcodeplug.cc@42: Codeplug download error: Device not configured", file=sys.stderr)
            sys.exit(255)
    with open(radio) as f, open(args[-1], "w") as g:
        g.write(f.read())
    if os.path.exists(os.path.join(d, "warn")):        # qdmr 0.15.1 on sparse channel slots
        print("\x1b[33mWarning in lib/opengd77base_codeplug.cc@1694: While linking zone 'GMRS': "
              "5-th channel index 32 out of bounds.\x1b[39m", file=sys.stderr)
elif args and args[0] == "verify":
    if os.path.exists(os.path.join(d, "crash")):           # qdmr 0.15.1 with openuv380
        _log()
        os.kill(os.getpid(), signal.SIGSEGV)
    with open(args[-1]) as f:
        if "BAD" in f.read():
            print("Error: channel 'BAD' frequency outside the radio's bands", file=sys.stderr)
            sys.exit(255)
elif args and args[0] == "encode":                      # the "binary": the file's meaning, canonical
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import json
    import qdmr_yaml
    with open(args[-2]) as f:
        text = f.read()
    if "BAD" in text:
        print("Error: channel 'BAD' frequency outside the radio's bands", file=sys.stderr)
        sys.exit(255)
    try:
        model = qdmr_yaml.load(text)
    except qdmr_yaml.YamlError as e:
        print(f"Error: Cannot parse codeplug: {e}", file=sys.stderr)
        sys.exit(255)
    with open(args[-1], "wb") as g:
        g.write(json.dumps(qdmr_yaml.to_json(model), sort_keys=True).encode())
elif args and args[0] == "write":
    time.sleep(slow)
    with open(args[-1]) as f:
        text = f.read()
    if "BAD" in text:                                      # write checks the limits itself
        print("Error: Cannot upload codeplug to device: Codeplug cannot be verified with radio.", file=sys.stderr)
        sys.exit(255)
    if os.path.exists(os.path.join(d, "drop")):
        text = text[:text.rindex("  - ")] if "  - " in text else text
    with open(radio, "w") as f:
        f.write(text)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import json
    import qdmr
    chans = qdmr.channels(text)
    pos = {c["id"]: i + 1 for i, c in enumerate(chans)}
    layout = {"slots": list(range(1, len(chans) + 1)), "names": {i + 1: c["name"] for i, c in enumerate(chans)},
              "zones": [{"name": z["name"], "slots": [pos[c] for c in z["A"] + z["B"] if c in pos]}
                        for z in qdmr.zones(text)]}
    with open(os.path.join(d, "layout.json"), "w") as f:
        json.dump(layout, f)
    if os.path.exists(os.path.join(d, "reboot")):
        with open(os.path.join(d, "reboot")) as f, open(os.path.join(d, "rebooting"), "w") as g:
            g.write(f.read().strip() or "1")
else:
    print("usage", file=sys.stderr)
    sys.exit(1)
