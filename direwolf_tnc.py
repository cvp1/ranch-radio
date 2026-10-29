#!/usr/bin/env python3
"""Direwolf as the packet hub's modem, for a radio on a USB sound card cable
(an AIOC, a Digirig, a CM108 dongle). Rendered from the host file's
"packet": {"tnc": "direwolf", "direwolf": {...}} block and run by launchd or
systemd; the packet hub (tnc_hub.py --tnc /tmp/kisstnc) is its only client.

    python3 direwolf_tnc.py --usb 1209:7388 --audio "AIOC Audio" --mycall N9ABC
        [--ptt off|serial] [--igate-login N9ABC-10 --igate-passcode-file FILE
        [--igate-server noam.aprs2.net]] [--direwolf PATH] [--state DIR]

What it does, and why:
- Waits for the cable (by USB ID), so the service can be on before the cable
  is plugged in.
- Writes Direwolf's config, and runs Direwolf with its KISS TNC on a
  pseudo-terminal (/tmp/kisstnc) and NO network ports (AGWPORT 0, KISSPORT 0).
  Direwolf's KISS and AGWPE ports listen on every interface and have no
  authentication: anyone who reached them could transmit, past the hub's
  transmit gate. The hub is the transmit gate; it must be the only way in.
- PTT: "off" (default) gives Direwolf no way to key the radio. "serial" keys
  through the cable's serial port: DTR on, RTS off (the AIOC's default PTT).
  Transmit also needs the hub's own switch ("tx": true).
- iGate (optional): received packets go to APRS-IS. Receive only: nothing
  from APRS-IS is ever sent on the air (no IGTXVIA). The passcode is read
  from a file at start and written only into the 0600 config file.
- Watches the cable: Direwolf never recovers when its sound card goes away
  (2026-10-29: after a USB drop it kept running, deaf). When the cable
  disappears, Direwolf is stopped and this exits; the service manager starts
  it again, and it waits for the cable.
- Clears a stale /tmp/kisstnc first: Direwolf won't replace an existing link,
  and an old one can point at a terminal another program now owns.

DIREWOLF_TNC_DEVICE_FILE: a stand-in for the USB lookup (tests): the file's
content is the serial port, empty or missing when the cable is unplugged.
DIREWOLF_TNC_PTY_LINK, DIREWOLF_TNC_POLL: the link to clear and the poll
interval (tests).
"""

import argparse
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

log = logging.getLogger("direwolf_tnc")
PTY_LINK = os.environ.get("DIREWOLF_TNC_PTY_LINK", "/tmp/kisstnc")   # tests: never the live one
TOCALLS = ["/opt/homebrew/share/direwolf", "/usr/local/share/direwolf", "/usr/share/direwolf"]
POLL_S = float(os.environ.get("DIREWOLF_TNC_POLL", "10"))


def usb_id(text):
    m = re.fullmatch(r"([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})", text)
    if not m:
        raise argparse.ArgumentTypeError("USB ID as VID:PID in hex, e.g. 1209:7388")
    return int(m.group(1), 16), int(m.group(2), 16)


def find_cable(usb):
    """The cable's serial port, or None when it isn't plugged in."""
    stand_in = os.environ.get("DIREWOLF_TNC_DEVICE_FILE")
    if stand_in is not None:
        try:
            return Path(stand_in).read_text().strip() or None
        except OSError:
            return None
    from opengd77 import OpenGD77Error, find_port   # the USB-ID lookup, Linux and macOS
    try:
        return find_port(usb)
    except OpenGD77Error as e:
        log.warning("%s", e)
        return None


def read_passcode(path):
    try:
        code = Path(os.path.expanduser(path)).read_text().strip()
    except OSError as e:
        raise SystemExit(f"direwolf_tnc: iGate passcode file {path}: {e.strerror}")
    if not re.fullmatch(r"-?\d{1,5}", code):
        raise SystemExit(f"direwolf_tnc: iGate passcode file {path} doesn't hold a passcode (digits)")
    return code


def config(a, port, passcode=None):
    """Direwolf's config text."""
    lines = [
        "# Written by direwolf_tnc.py at %s; edit the host file, not this." % time.strftime("%Y-%m-%d %H:%M"),
        f'ADEVICE "{a.audio}"',
        "ARATE 48000",
        "ACHANNELS 1",
        "CHANNEL 0",
        f"MYCALL {a.mycall}",
        "MODEM 1200",
        f"PTT {port} DTR -RTS" if a.ptt == "serial" else "# PTT off: receive only",
        "AGWPORT 0",
        "KISSPORT 0",
    ]
    if a.igate_login:
        lines += [
            "# iGate, receive only: RF to APRS-IS; nothing from APRS-IS goes on the air (no IGTXVIA).",
            f"IGSERVER {a.igate_server}",
            f"IGLOGIN {a.igate_login} {passcode}",
        ]
    return "\n".join(lines) + "\n"


def write_config(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # it can hold the iGate passcode
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def tocalls_dir():
    for d in TOCALLS:
        if os.path.exists(os.path.join(d, "tocalls.yaml")):
            return d
    return None


def run(a):
    passcode = read_passcode(a.igate_passcode_file) if a.igate_login else None
    said = False
    while True:
        port = find_cable(a.usb)
        if port:
            break
        if not said:
            log.info("waiting for the cable (USB %04x:%04x)", *a.usb)
            said = True
        time.sleep(POLL_S)
    conf = Path(os.path.expanduser(a.state)) / "direwolf.conf"
    write_config(conf, config(a, port, passcode))
    if os.path.islink(PTY_LINK) or os.path.exists(PTY_LINK):
        os.unlink(PTY_LINK)
    log.info("cable on %s; PTT %s; iGate %s", port, a.ptt, a.igate_login or "off")
    # Direwolf finds tocalls.yaml (which radio or app sent a packet) in its working directory.
    dw = subprocess.Popen([a.direwolf, "-c", str(conf), "-p", "-t", "0"], cwd=tocalls_dir())

    def stop(*_):
        """Stop Direwolf, then exit non-zero: the service manager starts this
        again, and it waits for the cable."""
        if dw.poll() is None:
            dw.terminate()
            try:
                dw.wait(10)
            except subprocess.TimeoutExpired:
                dw.kill()
        sys.exit(1)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while dw.poll() is None:
        time.sleep(POLL_S)
        if dw.poll() is None and not find_cable(a.usb):
            log.warning("the cable is gone from USB; stopping Direwolf (it can't recover its sound card)")
            stop()
    log.warning("Direwolf exited (%s)", dw.returncode)
    return 1


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Direwolf as the packet hub's modem (see the module docstring)")
    p.add_argument("--usb", type=usb_id, required=True, help="the cable's USB ID, VID:PID (an AIOC is 1209:7388)")
    p.add_argument("--audio", required=True, help="the cable's sound card, as Direwolf names it (ADEVICE)")
    p.add_argument("--mycall", required=True)
    p.add_argument("--ptt", choices=["off", "serial"], default="off")
    p.add_argument("--igate-login", help="APRS-IS login (callsign-SSID); turns the receive-only iGate on")
    p.add_argument("--igate-passcode-file", help="a file holding the APRS-IS passcode")
    p.add_argument("--igate-server", default="noam.aprs2.net")
    p.add_argument("--direwolf", default="direwolf")
    p.add_argument("--state", default="~/.local/state/ranch-radio/direwolf")
    a = p.parse_args(argv)
    if a.igate_login and not a.igate_passcode_file:
        p.error("--igate-login needs --igate-passcode-file")
    if any(c in a.audio for c in '"\n\r'):
        p.error("--audio can't hold quotes or line breaks")
    return a


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
