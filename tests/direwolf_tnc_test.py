"""Direwolf as the packet hub's modem ("packet": {"tnc": "direwolf"}).

  - the host file renders Direwolf and the hub, on macOS (launchd agents) and
    Linux (systemd units); the hub reads Direwolf's pseudo-terminal, never a
    network port
  - a malformed block is refused before anything renders
  - direwolf_tnc.py, against a stand-in Direwolf and a stand-in USB lookup:
    waits for the cable, writes a config with no network ports and no PTT
    unless asked, keeps the iGate passcode in a 0600 file, clears a stale
    pty link, and stops Direwolf when the cable goes away or on SIGTERM
  - doctor asks for Direwolf, not BlueZ, for this TNC

    python3 tests/direwolf_tnc_test.py
"""
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ranch_radio import doctor, render  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="direwolf-tnc-"))
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


def until(pred, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


# ---------------------------------------------------------------- rendering
mac = json.loads((ROOT / "examples/hosts/mac-aioc.json").read_text())
arts = {a.path: a for a in render.artifacts(mac, home="/H")}
hub = arts.get("Library/LaunchAgents/org.ranch-radio.packet-hub.plist")
dw = arts.get("Library/LaunchAgents/org.ranch-radio.direwolf.plist")
check("mac: renders the hub and Direwolf agents", (hub is not None, dw is not None), (True, True))
hargs = hub.content["ProgramArguments"] if hub else []
check("mac: the hub reads Direwolf's pseudo-terminal", hargs[hargs.index("--tnc") + 1] if "--tnc" in hargs else None,
      "/tmp/kisstnc")
check("  receive only (no --tx)", "--tx" in hargs, False)
dargs = dw.content["ProgramArguments"] if dw else []
check("mac: Direwolf runs through direwolf_tnc.py with the cable's ID and sound card",
      (dargs[1].endswith("/direwolf_tnc.py"), dargs[dargs.index("--usb") + 1], dargs[dargs.index("--audio") + 1]),
      (True, "1209:7388", "AIOC Audio"))
check("  the iGate passcode is a file path, not the passcode",
      dargs[dargs.index("--igate-passcode-file") + 1], "/H/.config/ranch-radio/aprs-is-passcode")

lin = dict(mac, host="linux-aioc", os="linux", python="/usr/bin/python3", station={"https": False})
larts = {a.path: a for a in render.artifacts(lin)}
du = larts.get(".config/systemd/user/direwolf.service")
hu = larts.get(".config/systemd/user/packet-hub.service")
check("linux: renders direwolf.service and packet-hub.service", (du is not None, hu is not None), (True, True))
check("  the hub starts after Direwolf", "After=direwolf.service" in (hu.content if hu else ""), True)
check("  Direwolf restarts whenever it exits (it exits when the cable goes)",
      "Restart=always" in (du.content if du else ""), True)
check("  no uvpro-hub.service for a Direwolf TNC", ".config/systemd/user/uvpro-hub.service" in larts, False)


def refused(change):
    h = json.loads(json.dumps(mac))
    change(h)
    try:
        render.validate(h)
        return False
    except render.HostError:
        return True


check("refused: tnc direwolf with no direwolf block", refused(lambda h: h["packet"].pop("direwolf")), True)
check("refused: a direwolf block on another TNC", refused(lambda h: h["packet"].update(tnc="tcp:dw.local:8001")), True)
check("refused: an iGate with no passcode file", refused(lambda h: h["packet"]["direwolf"]["igate"].pop("passcode")), True)
check("refused: a malformed USB ID", refused(lambda h: h["packet"]["direwolf"].update(usb="AIOC")), True)
check("refused: PTT other than off or serial", refused(lambda h: h["packet"]["direwolf"].update(ptt="vox")), True)
check("refused: a txdelay that isn't 10-2000 ms", refused(lambda h: h["packet"]["direwolf"].update(txdelay=5000)), True)
td = json.loads(json.dumps(mac)); td["packet"]["direwolf"]["txdelay"] = 600
targs = next(a for a in render.artifacts(td, home="/H") if a.path.endswith("org.ranch-radio.direwolf.plist")).content["ProgramArguments"]
check("txdelay reaches Direwolf's launcher", targs[targs.index("--txdelay") + 1] if "--txdelay" in targs else None, "600")

names = {c.what.split(":")[0].split(" ")[0] for c in doctor.static(mac) + [
    c for n in [m for m in doctor.MANIFESTS["packet"]["needs"]]
    for c in doctor._need("packet", n, mac["packet"], sys.executable, os.environ.get("PATH"), "macos")]}
check("doctor: asks for Direwolf, not BlueZ, for this TNC", ("direwolf" in names, "bluetoothctl" in names), (True, False))

# ---------------------------------------------------------------- the launcher
fake = TMP / "direwolf"
argv_log, started = TMP / "argv.txt", TMP / "started"
fake.write_text(f"""#!/bin/sh
echo "$@" > {argv_log}
cp "$2" {TMP / 'conf-seen.txt'}
touch {started}
trap 'echo stopped > {TMP / "stopped"}; exit 0' TERM
while :; do sleep 0.1; done
""")
fake.chmod(0o755)
dev = TMP / "device"
pty = TMP / "kisstnc"
pty.symlink_to("/dev/null")                       # a stale link from an earlier Direwolf
passfile = TMP / "passcode"
passfile.write_text("12345\n")
env = dict(os.environ, DIREWOLF_TNC_DEVICE_FILE=str(dev), DIREWOLF_TNC_PTY_LINK=str(pty), DIREWOLF_TNC_POLL="0.2")
state = TMP / "state"


def launch(*extra):
    return subprocess.Popen([sys.executable, str(ROOT / "direwolf_tnc.py"), "--usb", "1209:7388",
                             "--audio", "AIOC Audio", "--mycall", "N9ABC", "--direwolf", str(fake),
                             "--state", str(state), *extra],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


p = launch("--igate-login", "N9ABC-10", "--igate-passcode-file", str(passfile))
time.sleep(1)
check("waits for the cable: no Direwolf while it's unplugged", started.exists(), False)
dev.write_text("/dev/cu.usbmodemTEST\n")
check("starts Direwolf once the cable appears", until(started.exists), True)
conf = (TMP / "conf-seen.txt").read_text() if (TMP / "conf-seen.txt").exists() else ""
check("  Direwolf gets a pty and no network ports",
      ("-p" in argv_log.read_text().split(), "AGWPORT 0" in conf, "KISSPORT 0" in conf), (True, True, True))
check("  no PTT unless asked", ("PTT off" in conf, "DTR" in conf), (True, False))
check("  the sound card and callsign", ('ADEVICE "AIOC Audio"' in conf, "MYCALL N9ABC" in conf), (True, True))
check("  iGate: login and passcode, receive only (no IGTXVIA)",
      ("IGLOGIN N9ABC-10 12345" in conf, any(l.startswith("IGTXVIA") for l in conf.splitlines())), (True, False))
mode = stat.S_IMODE(os.stat(state / "direwolf.conf").st_mode) if (state / "direwolf.conf").exists() else None
check("  the config (it holds the passcode) is 0600", mode, 0o600)
check("  the stale pty link is gone", pty.is_symlink() or pty.exists(), False)
dev.write_text("")
check("the cable goes: Direwolf is stopped", until(lambda: (TMP / "stopped").exists()), True)
check("  and the launcher exits non-zero (the service manager restarts it)", p.wait(10) != 0, True)
out = p.stdout.read()
check("  the passcode never reaches the log", "12345" in out, False)

for f in (started, TMP / "stopped", TMP / "conf-seen.txt"):
    f.unlink(missing_ok=True)
dev.write_text("/dev/ttyACM0\n")
p = launch("--ptt", "serial", "--txdelay", "600")
until(started.exists)
conf = (TMP / "conf-seen.txt").read_text() if (TMP / "conf-seen.txt").exists() else ""
check("PTT serial: keys on the cable's port, DTR on and RTS off", "PTT /dev/ttyACM0 DTR -RTS" in conf, True)
check("  --txdelay 600 ms is TXDELAY 60 (Direwolf counts 10 ms)", "TXDELAY 60" in conf.splitlines(), True)
check("  no iGate unless asked", "IGLOGIN" in conf, False)
p.send_signal(signal.SIGTERM)
check("SIGTERM stops Direwolf too", until(lambda: (TMP / "stopped").exists()), True)
p.wait(10)

bad = TMP / "bad-passcode"
bad.write_text("hunter2\n")
r = subprocess.run([sys.executable, str(ROOT / "direwolf_tnc.py"), "--usb", "1209:7388", "--audio", "A",
                    "--mycall", "N9ABC", "--igate-login", "N9ABC-10", "--igate-passcode-file", str(bad),
                    "--direwolf", str(fake)], env=env, capture_output=True, text=True, timeout=10)
check("a passcode file that isn't digits: refused, and not echoed",
      (r.returncode != 0, "hunter2" in r.stdout + r.stderr), (True, False))

subprocess.run(["rm", "-rf", str(TMP)])
print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
