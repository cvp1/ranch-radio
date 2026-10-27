"""`ranch-radio init`: the first host file for this machine, from the radio
that's plugged in.

    find_radios(os)            -> [{"device", "desc", "rig"}]   what's plugged in
    host_file(...)             -> the host file (a dict), ready to write

It finds the radio's serial port (Linux: /dev/serial/by-id, a name that
survives replugs; macOS: the USB device tree), guesses the rig from the USB
names (Icom puts the model in the serial number; the QMX in the product), and
on Linux pairs the radio's PipeWire sound card when its name gives it away.
Transmit is off until a callsign is given (--call): the radio's "tx": false
renders server.py --no-tx.

The file goes to ~/.config/ranch-radio/hosts/<host>.json, outside the checkout.
"""

import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

import rigs

from . import paths, render

# Text in a USB product name or serial number -> rig profile.
USB_RIGS = [("IC-7300", "ic7300"), ("IC-705", "ic705"), ("IC-7610", "ic7610"), ("IC-9700", "ic9700"),
            ("IC-7100", "ic7100"), ("QMX", "qmx"), ("X6100", "x6100")]
SERIAL_HINT = re.compile(r"usb|serial|uart|acm|cp210|ftdi|qmx|ic-", re.IGNORECASE)
MAC_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


def guess_rig(desc):
    for text, rig in USB_RIGS:
        if text.lower() in desc.lower():
            return rig
    return None


def _linux_ports():
    d = Path(os.environ.get("RANCH_RADIO_SERIAL_BYID", "/dev/serial/by-id"))
    if not d.is_dir():
        return []
    return [{"device": str(p), "desc": p.name} for p in sorted(d.iterdir())]


def _mac_ports():
    """Serial ports with the nearest USB product name and serial number above
    them in the IOService tree."""
    src = os.environ.get("RANCH_RADIO_IOREG_FILE")
    if src:
        text = Path(src).read_text()
    else:
        try:
            text = subprocess.run(["ioreg", "-l", "-w0", "-p", "IOService"], capture_output=True,
                                  text=True, timeout=20).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
    out, stack = [], []
    for line in text.splitlines():
        m = re.match(r"^([ |]*)\+-o ", line)
        if m:
            d = len(m.group(1))
            stack = [s for s in stack if s["d"] < d] + [{"d": d, "name": None, "serial": None}]
            continue
        if not stack:
            continue
        p = re.search(r'"USB Product Name" = "(.*)"', line)
        if p:
            stack[-1]["name"] = p.group(1)
        s = re.search(r'"USB Serial Number" = "(.*)"', line)
        if s:
            stack[-1]["serial"] = s.group(1)
        c = re.search(r'"IOCalloutDevice" = "(.*)"', line)
        if c:
            names = [x["name"] for x in stack if x["name"]]
            serials = [x["serial"] for x in stack if x["serial"]]
            if names:                         # USB only: not Bluetooth, not the debug console
                out.append({"device": c.group(1), "serial": serials[-1] if serials else "",
                            "desc": " ".join(filter(None, [names[-1], serials[-1] if serials else ""]))})
    # One CP210x shows twice on a Mac, once per driver (Apple's usbserial-*
    # and Silicon Labs' SLAB_USBtoUART): keep one per USB serial number,
    # Apple's (mac-shack's IC-7300 runs on usbserial-21210).
    by_serial, keep = {}, []
    for p in out:
        if not p["serial"]:
            keep.append(p)
            continue
        prev = by_serial.get(p["serial"])
        if prev is None or ("usbserial-" in p["device"] and "usbserial-" not in prev["device"]):
            by_serial[p["serial"]] = p
    return keep + list(by_serial.values())


def find_radios(os_name):
    ports = _mac_ports() if os_name == "macos" else _linux_ports()
    found = []
    for p in ports:
        if not SERIAL_HINT.search(p["desc"] + p["device"]):
            continue
        found.append({**p, "rig": guess_rig(p["desc"])})
    return found


def _pipewire_pair(token):
    """(source, sink) PipeWire names containing token, if exactly one of each."""
    def names(kind):
        try:
            r = subprocess.run(["pactl", "list", "short", kind], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return []
        return [ln.split("\t")[1] for ln in r.stdout.splitlines() if "\t" in ln]
    src = [n for n in names("sources") if token in n and not n.endswith(".monitor")]
    snk = [n for n in names("sinks") if token in n]
    return (src[0], snk[0]) if len(src) == 1 and len(snk) == 1 else (None, None)


def this_host():
    name = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().split(".")[0].lower()).strip("-")
    return name or "radio"


def _tilde(p):
    home = os.path.expanduser("~")
    p = str(p)
    return "~" + p[len(home):] if p.startswith(home + os.sep) else p


def host_file(os_name, host, repo, rig, device, call=None, name=None, audio=(None, None), python=None):
    prof = rigs.PROFILES[rig]
    rid = rig if rig != "hamlib" else "radio"
    short = name or prof["name"].replace("ICOM ", "").replace("QRP Labs ", "")
    radio = {"id": rid, "name": short, "rig": rig, "device": device, "tx_timeout": 60}
    h = {"host": host, "os": os_name, "repo": _tilde(repo),
         "python": python or _tilde(Path(repo) / ".venv/bin/python"),
         "note": f"Written by ranch-radio init, {time.strftime('%Y-%m-%d')}."}
    if call:
        h["call"] = call.upper()
    else:
        radio["tx"] = False                    # receive only until there's a callsign
    if os_name == "macos":
        # The certificate names this machine as browsers reach it, whatever the host file is called.
        radio.update(label=f"org.ranch-radio.{rid}", https=True, tls_names=[this_host()], path=MAC_PATH)
        h["radios"] = [radio]
    else:
        radio.update(description=f"Ranch Radio: {short}", web=7301, rigctl=4533)
        if audio[0] and audio[1]:
            radio.update(audio_in=audio[0], audio_out=audio[1])
        h["radios"] = [radio]
        h["station"] = {"https": True}
    render.validate(h)
    return h


def run(a, repo):
    """The init command. Returns an exit code."""
    os_name = "macos" if os.uname().sysname == "Darwin" else "linux"
    host = a.host or this_host()
    existing = paths.find(Path(repo) / "hosts", host)
    target_dir = paths.user_dir() or Path(os.path.expanduser(paths.USER_HOSTS))
    target = target_dir / f"{host}.json"
    if existing and not a.force:
        print(f"ranch-radio: {host} already has a host file: {paths.shown(existing)}\n"
              f"  Change it there, or pass --force to write a new one in {paths.shown(target_dir)}.")
        return 1
    found = find_radios(os_name)
    device, rig = a.port, a.rig
    if device is None:
        pick = [f for f in found if f["rig"]] if not rig else found
        if len(pick) == 1:
            device = pick[0]["device"]
            rig = rig or pick[0]["rig"]
        else:
            print("Serial ports that could be a radio:" if found else "No radio's serial port found.")
            for f in found:
                print(f"  {f['device']}  {f['desc']}" + (f"  (looks like {f['rig']})" if f["rig"] else ""))
            print("\nRun again with --port DEVICE and --rig RIG"
                  f" (rigs: {', '.join(rigs.PROFILES)}).")
            return 2
    if rig is None:
        rig = guess_rig(next((f["desc"] for f in found if f["device"] == device), ""))
    if rig is None:
        print(f"ranch-radio: which radio is on {device}? Pass --rig (one of: {', '.join(rigs.PROFILES)}).")
        return 2
    if rig not in rigs.PROFILES:
        print(f"ranch-radio: no rig {rig!r} (one of: {', '.join(rigs.PROFILES)}).")
        return 2
    if a.call and not render.CALL.fullmatch(a.call):
        print(f"ranch-radio: {a.call!r} doesn't look like a callsign.")
        return 2
    audio = (None, None)
    if os_name == "linux" and "/by-id/" in device:
        token = Path(device).name.removeprefix("usb-").split("-if")[0]
        audio = _pipewire_pair(token)
    h = host_file(os_name, host, repo, rig, device, a.call, a.name, audio)
    text = json.dumps(h, indent=2) + "\n"
    print(f"{paths.shown(target)}:\n{text}")
    if not rigs.PROFILES[rig].get("tested"):
        print(f"Note: the {rigs.PROFILES[rig]['name']} profile is built from the references and hasn't run "
              "on a real radio yet.")
    if os_name == "linux" and not (audio[0] and audio[1]):
        print("Note: no sound card paired. Add audio_in/audio_out (pactl list short sources/sinks) to "
              "hear the radio in the page.")
    if not a.call:
        print("Transmit is off (receive only). Give your callsign to turn it on: "
              "ranch-radio init --force --call YOURCALL ...")
    if a.dry_run:
        print("Dry run: nothing written.")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(target)
    print(f"Written. Next: ranch-radio install {host} --apply, then ranch-radio start {host}.")
    return 0
