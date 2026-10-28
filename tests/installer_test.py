"""The installer: init, start, update's restart rules, doctor --fix, install.sh.

  - init finds the radio: Linux by /dev/serial/by-id (the QMX by its product),
    macOS by the USB tree (the IC-7300 by the model in its serial number; one
    CP210x seen through two drivers counts once)
  - its host file validates, renders, and is receive-only (--no-tx) until a
    callsign is given
  - start (Linux) enables the radio units, so a radio plugged in later gets
    its server (linux-station's QMX, 2026-10-21), and enables + starts the station;
    start (macOS) falls back to launchctl load when bootstrap fails
    (mac-shack's IC-7300, 2026-10-21)
  - restart never touches a radio in use or a manual_restart unit
  - doctor --fix runs a recipe, never one with sudo
  - install.sh, end to end, in a scratch home with stand-in service managers:
    venv, PATH link, init, install, start, doctor (macOS or Linux, whichever
    runs it; needs the network for pip)

    python3 tests/installer_test.py            (INSTALLER_E2E=0 skips install.sh)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["RANCH_RADIO_HOSTS"] = ""

from ranch_radio import doctor, init, render, service  # noqa: E402
from ranch_radio import cli as _cli  # noqa: E402
_cli.HOSTS = ROOT / "examples/hosts"            # the examples stand in for a host's files

fails = 0
TMP = Path(tempfile.mkdtemp(prefix="installer-"))
MAC = os.uname().sysname == "Darwin"


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


# ---------------------------------------------------------------- init: finding the radio
byid = TMP / "by-id"
byid.mkdir()
for n in ("usb-Espressif_USB_JTAG_serial_debug_unit_80:F1:B2:A6:FD:78-if00", "usb-QRP_Labs_QMX_Transceiver-if00"):
    (byid / n).touch()
os.environ["RANCH_RADIO_SERIAL_BYID"] = str(byid)
found = init.find_radios("linux")
check("linux: both serial ports listed", len(found), 2)
check("linux: the QMX recognized by its USB product", [f["rig"] for f in found if f["rig"]], ["qmx"])

IOREG = """+-o Root  <class IORegistryEntry>
  +-o AppleT8132USBXHCI@01000000
  | +-o CP2102 USB to UART Bridge Controller@01210000
  | | {
  | |   "USB Product Name" = "CP2102 USB to UART Bridge Controller"
  | |   "USB Serial Number" = "IC-7300 02062664"
  | | }
  | | +-o AppleUSBSLCOM
  | |   +-o IOSerialBSDClient
  | |       "IOCalloutDevice" = "/dev/cu.SLAB_USBtoUART"
  | |   +-o IOSerialBSDClient
  | |       "IOCalloutDevice" = "/dev/cu.usbserial-21210"
  +-o Bluetooth
    +-o IOSerialBSDClient
        "IOCalloutDevice" = "/dev/cu.UV-PRO"
"""
(TMP / "ioreg.txt").write_text(IOREG)
os.environ["RANCH_RADIO_IOREG_FILE"] = str(TMP / "ioreg.txt")
found = init.find_radios("macos")
check("mac: one CP210x through two drivers counts once", [f["device"] for f in found], ["/dev/cu.usbserial-21210"])
check("mac: the IC-7300 recognized by the model in its serial number", found[0]["rig"], "ic7300")
check("mac: Bluetooth ports are not radios", any("UV-PRO" in f["device"] for f in found), False)

# ---------------------------------------------------------------- init: the host file
for os_name, rig, dev in (("macos", "ic7300", "/dev/cu.usbserial-21210"),
                          ("linux", "qmx", str(byid / "usb-QRP_Labs_QMX_Transceiver-if00"))):
    h = init.host_file(os_name, "newhost", "/home/x/ranch-radio", rig, dev)
    arts = render.artifacts(h, home="/home/x")
    text = "".join(a.text() for a in arts)
    check(f"{os_name}: init's host file validates and renders", bool(arts), True)
    check(f"{os_name}: receive-only until a callsign (--no-tx)", "--no-tx" in text, True)
    h = init.host_file(os_name, "newhost", "/home/x/ranch-radio", rig, dev, call="n0call")
    text = "".join(a.text() for a in render.artifacts(h, home="/home/x"))
    check(f"{os_name}: with a callsign, transmit allowed", ("--no-tx" in text, h.get("call")), (False, "N0CALL"))
check("linux: the station renders with the radio", any(a.path.endswith("station.service")
      for a in render.artifacts(init.host_file("linux", "n", "/r", "qmx", "/dev/qmx"))), True)
try:
    render.validate({**init.host_file("linux", "n", "/r", "qmx", "/dev/qmx"), "call": "N0CALL --tx"})
    check("a bad callsign is refused", "accepted", "refused")
except render.HostError:
    check("a bad callsign is refused", "refused", "refused")

# ---------------------------------------------------------------- start / restart, stand-in managers
LOG = TMP / "calls.jsonl"
fake = TMP / "fakectl"
STATE_DIR = TMP / "launchd"
STATE_DIR.mkdir()
fake.write_text(f"""#!/bin/sh
# A stand-in launchctl/systemctl that remembers what is loaded (by label).
echo "$(basename $0) $*" >> {LOG}
last=""; for a in "$@"; do last="$a"; done
label=$(basename "$last" .plist); label=${{label##*/}}
case "$1" in
  bootstrap) [ -n "$FAIL_BOOTSTRAP" ] && exit 5; touch "{STATE_DIR}/$label" ;;
  load) [ -n "$NEVER_LOAD" ] && exit 0
        if [ -n "$LOAD_NOOP_ONCE" ] && [ ! -e "{STATE_DIR}/.noop-done" ]; then touch "{STATE_DIR}/.noop-done"; exit 0; fi
        touch "{STATE_DIR}/$label" ;;
  bootout) rm -f "{STATE_DIR}/$label" ;;
  print) [ -e "{STATE_DIR}/$label" ] || exit 1 ;;
esac
exit 0
""")
fake.chmod(0o755)
for n in ("launchctl", "systemctl"):
    (TMP / n).symlink_to(fake)
os.environ["RANCH_RADIO_LAUNCHCTL"] = str(TMP / "launchctl")
os.environ["RANCH_RADIO_SYSTEMCTL"] = str(TMP / "systemctl")
os.environ["RANCH_RADIO_LAUNCHD_WAIT"] = "1"


def calls():
    out = LOG.read_text().splitlines() if LOG.exists() else []
    LOG.unlink(missing_ok=True)
    return out


lin = json.loads((ROOT / "examples/hosts/linux-station.json").read_text())
service.start(lin)
c = calls()
check("linux start: enables the radio units (a radio plugged in later gets its server)",
      any(x.startswith("systemctl --user enable g90-remote.") and "qmx-remote.service" in x for x in c), True)
check("linux start: enables and starts the station", any("enable --now" in x and "station.service" in x for x in c), True)

mac = json.loads((ROOT / "examples/hosts/mac-shack.json").read_text())
os.environ["FAIL_BOOTSTRAP"] = "1"
out = service.start(mac)
c = calls()
check("mac start: bootstrap fails, then launchctl load starts it",
      [x.split()[1] for x in c if "org.ranch-radio.ic7300-remote" in x and "print" not in x], ["bootstrap", "load"])
# mac-shack, 2026-10-21: bootout returned, load ran while the old instance was
# still going, did nothing and exited 0; the radio stayed down.
for f in STATE_DIR.iterdir():
    f.unlink()
os.environ["LOAD_NOOP_ONCE"] = "1"
radio_arts = [a for a in render.artifacts(mac) if a.module == "ic7300"]
(STATE_DIR / "org.ranch-radio.ic7300-remote").touch()          # running: a restart is for a running agent
done, deferred = service.restart(mac, radio_arts, check_idle=lambda h, r: (True, "idle"))
c = calls()
check("mac restart: a load that silently does nothing is caught and retried",
      (STATE_DIR / "org.ranch-radio.ic7300-remote").exists(), True)
check("  (two loads: the no-op, then the one that took)",
      sum(1 for x in c if " load " in x and "ic7300-remote" in x), 2)
del os.environ["LOAD_NOOP_ONCE"]
(STATE_DIR / ".noop-done").unlink(missing_ok=True)
os.environ["NEVER_LOAD"] = "1"
for f in STATE_DIR.iterdir():
    f.unlink()
(STATE_DIR / "org.ranch-radio.ic7300-remote").touch()
done, deferred = service.restart(mac, radio_arts, check_idle=lambda h, r: (True, "idle"))
calls()
check("mac restart: an agent that never comes up is reported, not passed off as done",
      any("NOT RUNNING" in line and rc for line, rc, _ in done), True)
del os.environ["NEVER_LOAD"]
del os.environ["FAIL_BOOTSTRAP"]
# Switched off from the widget: a restart writes it and leaves it off. A
# newly installed agent (the update check) is loaded.
for f in STATE_DIR.iterdir():
    f.unlink()
upd = [a for a in render.artifacts(mac) if a.module == "updates"]
done, deferred = service.restart(mac, radio_arts + upd, check_idle=lambda h, r: (True, "idle"),
                                 new={a.path for a in upd})
c = calls()
check("mac restart: a radio agent that was off stays off", (STATE_DIR / "org.ranch-radio.ic7300-remote").exists(), False)
check("  and says so", any("left stopped" in line for line, rc, _ in done), True)
check("mac restart: a newly installed update agent is loaded", (STATE_DIR / "org.ranch-radio.update").exists(), True)
done, deferred = service.restart(mac, upd, check_idle=lambda h, r: (True, "idle"), skip={"updates"})
check("mac restart: the update job never restarts itself", calls(), [])

arts = render.artifacts(lin)
changed = [a for a in arts if a.module in ("g90", "qmx", "station", "packet")]
busy = {"g90": (False, "G90 is transmitting")}
done, deferred = service.restart(lin, changed, check_idle=lambda h, r: busy.get(r["id"] if r else "-", (True, "idle")))
c = calls()
check("restart: a transmitting radio is not restarted", any("g90-remote.service" in x for x in c), False)
check("  and says so", any("G90 is transmitting" in d for d in deferred), True)
check("restart: an idle radio is", any("try-restart qmx-remote.service" in x for x in c), True)
check("restart: manual_restart units never", any("uvpro-hub.service" in x for x in c), False)
held = []
service.restart(lin, changed, check_idle=lambda h, r: busy.get(r["id"] if r else "-", (True, "idle")), held=held)
calls()
check("restart: a busy radio is held, to restart at the next update", held, ["g90"])
service.restart(lin, [a for a in arts if a.module == "updates"], check_idle=lambda h, r: (True, "idle"))
c = calls()
check("linux restart: the update timer is enabled and (re)started",
      (any("enable ranch-radio-update.timer" in x for x in c), any("restart ranch-radio-update.timer" in x for x in c)),
      (True, True))
check("  its oneshot service is never started by a restart",
      any("ranch-radio-update.service" in x and "daemon-reload" not in x for x in c), False)
service.start(lin)
check("linux start: the update service is not enabled (its timer starts it)",
      any("ranch-radio-update.service" in x for x in calls()), False)
check("  with the reason", any("uvpro-hub.service" in d and "power-cycled" in d for d in deferred), True)
done, deferred = service.restart(mac, [a for a in render.artifacts(mac) if a.module == "ic7300"],
                                 check_idle=lambda h, r: (False, "IC-7300 is in a Winlink session"))
check("mac restart: a radio in a Winlink session is left alone", (calls(), len(deferred)), ([], 1))

# ---------------------------------------------------------------- idle, against a real (simulated) radio server
import random  # noqa: E402
import socket  # noqa: E402
import time  # noqa: E402

port = 19000 + random.randrange(900)
srv = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(port), "--rigctl-port", "0",
                        "--addons-state", ""], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    hh, rr = {"os": "linux", "python": sys.executable}, {"name": "Sim", "web": port}
    check("idle: a quiet radio is idle", service.idle(hh, rr), (True, "idle"))
    key = subprocess.Popen([sys.executable, "-c", (
        "import asyncio, json, aiohttp, sys\n"
        "async def m():\n"
        "    async with aiohttp.ClientSession() as s:\n"
        f"        async with s.ws_connect('http://127.0.0.1:{port}/ws') as ws:\n"
        "            await ws.receive()\n"
        "            await ws.send_str(json.dumps({'t': 'sim_panel', 'd': {'tx': True}}))\n"
        "            print('keyed', flush=True); await asyncio.sleep(8)\n"
        "asyncio.run(m())")], stdout=subprocess.PIPE, text=True)
    key.stdout.readline()
    time.sleep(0.5)
    check("idle: a keyed radio is not (restart refused)", service.idle(hh, rr), (False, "Sim is transmitting"))
    key.kill()
finally:
    srv.terminate()
    srv.wait(10)
hh["python"] = sys.executable
check("idle: a server that isn't running is idle (nothing to cut off)",
      service.idle(hh, {"name": "Gone", "web": port})[0], True)

# ---------------------------------------------------------------- update after a code change
from ranch_radio import cli  # noqa: E402

restarted = []
h1 = TMP / "h1"
hmac = render.load(ROOT / "examples/hosts/mac-shack.json")
cli.write_installed(hmac, h1, list(render.installed_states(hmac, h1)))   # every file already current
(TMP / "probe_up.py").write_text('print(\'{"up": true}\')\n')
os.environ["RANCH_RADIO_PROBE"] = f"{sys.executable} {TMP / 'probe_up.py'}"   # never the real :7300
os.environ["RANCH_RADIO_HEALTH_WAIT"] = "1"
real_restart, real_hosts = service.restart, cli.HOSTS
(TMP / "uhosts").mkdir()
(TMP / "uhosts/mac-shack.json").write_text(json.dumps(dict(json.loads((ROOT / "examples/hosts/mac-shack.json").read_text()),
                                                         python=sys.executable)))   # a Python that exists here
cli.HOSTS = TMP / "uhosts"
service.restart = lambda h, changed, check_idle=None, **kw: (restarted.extend(a.path for a in changed), ([], []))[1]
try:
    import contextlib, io  # noqa: E401,E402
    with contextlib.redirect_stdout(io.StringIO()):
        cli.cmd_update(SimpleNamespace(host="mac-shack", no_pull=True, code_changed=True, apply=True, home=str(TMP / "h1")))
finally:
    service.restart, cli.HOSTS = real_restart, real_hosts
names = {p.rsplit("/", 1)[-1] for p in restarted}
check("update after a code change restarts the radio server", "org.ranch-radio.ic7300-remote.plist" in names, True)
check("  but not ardopcf or the menu-bar widget (they don't run this code)",
      {"org.ranch-radio.ardopcf.plist", "org.ranch-radio.ic7300-widget.plist"} & names, set())

# ---------------------------------------------------------------- doctor --fix
marker = TMP / "fixed"
cs = [doctor.Check("x", doctor.MISSING, "a", recipe=f"touch {marker}"),
      doctor.Check("x", doctor.MISSING, "b", recipe=f"sudo touch {marker}.root"),
      doctor.Check("x", doctor.OK, "c", recipe=f"touch {marker}.ok")]
doctor.fix(cs, log=lambda *_: None)
check("doctor --fix runs a missing check's recipe", marker.exists(), True)
check("  never one with sudo, never an ok check's", (Path(f"{marker}.root").exists(), Path(f"{marker}.ok").exists()),
      (False, False))
h = dict(json.loads((ROOT / "examples/hosts/linux-laptop.json").read_text()), python=sys.executable)
h["mesh"]["python"] = str(TMP / "nomesh/bin/python")
mc = [c for c in doctor.machine(h) if c.module == "mesh" and "meshtastic" in c.what][0]
check("mesh's missing venv gets a recipe that makes it",
      ("-m venv" in mc.recipe, "pip install -q meshtastic" in mc.recipe), (True, True))

# ---------------------------------------------------------------- install.sh, end to end
if os.environ.get("INSTALLER_E2E", "1") != "0":
    co = TMP / "checkout"
    subprocess.run(["rsync", "-a", "--exclude", ".venv", "--exclude", "certs", "--exclude", "__pycache__",
                    "--exclude", "server.log", f"{ROOT}/", f"{co}/"], check=True)
    home = TMP / "home"
    home.mkdir()
    env = dict(os.environ, HOME=str(home), PATH=f"{TMP}:{os.environ['PATH']}",
               RANCH_RADIO_HOSTS=str(home / ".config/ranch-radio/hosts"))
    for k in ("RANCH_RADIO_IOREG_FILE",):
        env.pop(k, None)
    args = ["--port", "/dev/null", "--rig", "ic7300" if MAC else "qmx", "--call", "N0CALL"]
    r = subprocess.run(["bash", str(co / "install.sh"), *args], env=env, capture_output=True, text=True, timeout=900)
    out = r.stdout + r.stderr
    host = init.this_host()
    hf = home / ".config/ranch-radio/hosts" / f"{host}.json"
    check("install.sh: made the venv with aiohttp",
          subprocess.run([str(co / ".venv/bin/python"), "-c", "import aiohttp"]).returncode, 0)
    check("install.sh: ranch-radio on the PATH", (home / ".local/bin/ranch-radio").resolve() == (co / "ranch-radio").resolve(), True)
    check("install.sh: init wrote the host file outside the checkout", hf.exists(), True)
    if MAC:
        plist = home / "Library/LaunchAgents/org.ranch-radio.ic7300.plist"
        check("install.sh: the radio's agent installed", plist.exists(), True)
        check("install.sh: and started", any("bootstrap" in x and "org.ranch-radio.ic7300" in x for x in calls()), True)
    else:
        check("install.sh: the radio's unit installed", (home / ".config/systemd/user/qmx-remote.service").exists(), True)
        c = calls()
        check("install.sh: radios enabled, station started",
              (any("enable qmx-remote" in x for x in c), any("enable --now" in x for x in c)), (True, True))
    check("install.sh: ends with doctor", "== Check" in out and "core" in out, True)
    r2 = subprocess.run(["bash", str(co / "install.sh")], env=env, capture_output=True, text=True, timeout=600)
    check("install.sh again: keeps the host file (no second init)", "using " in r2.stdout, True)
    if fails:
        print(out[-3000:])

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
