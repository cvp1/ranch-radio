"""`install --apply` then the `update --apply` it points to (2026-11-26): an
add-on was added to the host file and installed; install said the update
would load it, but update restarts by what DIFFERS, install had just made the
files match, and the station ran on with its old station.json until it was
restarted by hand. Against this checkout, with stand-in launchctl/systemctl
and probe (as update_test), no git origin (update --no-pull).

  - install --apply records the station as pending, and says the update
    restarts it
  - update --apply then restarts the running station (bootout + bootstrap, or
    systemctl restart) and clears the pending list
  - control: an update with nothing pending restarts nothing
  - install --apply's printed commands wait for launchd to let go between
    bootout and bootstrap (macOS)

    python3 tests/install_restart_test.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="install-restart-"))
MAC = os.uname().sysname == "Darwin"
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


home, hosts = TMP / "home", TMP / "hosts"
home.mkdir()
hosts.mkdir()
host = "mac-shack" if MAC else "linux-laptop"
hf = json.loads((ROOT / f"examples/hosts/{host}.json").read_text())
hf["python"] = sys.executable
(hosts / f"{host}.json").write_text(json.dumps(hf))

LOG, LOADED = TMP / "calls.log", TMP / "loaded"
LOADED.mkdir()
fake = TMP / "fakectl"
fake.write_text(f"""#!/bin/sh
echo "$(basename $0) $*" >> {LOG}
last=""; for a in "$@"; do last="$a"; done
label=$(basename "$last" .plist); label=${{label##*/}}
case "$1" in
  bootstrap|load) touch "{LOADED}/$label" ;;
  bootout) rm -f "{LOADED}/$label" ;;
  print) [ -e "{LOADED}/$label" ] || exit 1 ;;
esac
exit 0
""")
fake.chmod(0o755)
for n in ("launchctl", "systemctl"):
    (TMP / n).symlink_to(fake)
probe = TMP / "probe.py"
probe.write_text('import json; print(json.dumps({"up": True, "tx": False, "tuning": False, "rigctl": 0}))\n')
ENV = dict(os.environ, HOME=str(home), RANCH_RADIO_HOSTS=str(hosts),
           RANCH_RADIO_LAUNCHCTL=str(TMP / "launchctl"), RANCH_RADIO_SYSTEMCTL=str(TMP / "systemctl"),
           RANCH_RADIO_PROBE=f"{sys.executable} {probe}", RANCH_RADIO_HEALTH_WAIT="2",
           RANCH_RADIO_LAUNCHD_WAIT="1", RANCH_RADIO_NO_VENV="1")


def rr(*args):
    LOG.unlink(missing_ok=True)
    r = subprocess.run([sys.executable, str(ROOT / "ranch-radio"), *args, "--home", str(home)],
                       env=ENV, capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout + r.stderr


def calls():
    return LOG.read_text().splitlines() if LOG.exists() else []


def pending():
    p = home / ".local/state/ranch-radio/update.json"
    return json.loads(p.read_text()).get("pending", []) if p.exists() else []


def station_restarted(cs):
    if MAC:
        return any(" bootout " in f" {c} " and "station" in c for c in cs) and \
            any((" bootstrap " in f" {c} " or " load " in f" {c} ") and "station" in c for c in cs)
    return any("restart" in c and "station" in c for c in cs)


# A first install and the update after it: everything running, nothing pending.
rc, out = rr("install", host, "--apply")
check("first install writes the host's files", rc, 0)
rc, out = rr("update", host, "--apply", "--no-pull")
check("  and the update after it runs clean", (rc, pending()), (0, []))
# Running, as on a live host (the fake only knows what it bootstrapped; an
# agent it never saw running is left stopped on purpose: the widget's Off).
for p in (home / "Library/LaunchAgents").glob("*.plist"):
    (LOADED / p.stem).touch()

# The control: nothing changed, nothing pending, nothing restarted.
rc, out = rr("update", host, "--apply", "--no-pull")
check("control: an update with nothing pending restarts nothing", station_restarted(calls()), False)

# The 2026-11-26 case: an add-on added to the host file by hand.
hf.setdefault("addons", {})["grids"] = {"enabled": True}
(hosts / f"{host}.json").write_text(json.dumps(hf))
rc, out = rr("install", host, "--apply")
check("install --apply writes station.json", (rc, "station.json" in out), (0, True))
check("  records the station as pending", "station" in pending(), True)
check("  and says the update restarts it", f"ranch-radio update {host} --apply restarts" in out, True)
if MAC:
    lines = [ln.strip() for ln in out.splitlines()]
    i = next((k for k, ln in enumerate(lines) if ln.startswith("launchctl bootout") and "station" in ln), None)
    check("  its printed commands wait for launchd to let go before bootstrap",
          i is not None and lines[i + 1].startswith("for i in") and "launchctl print" in lines[i + 1]
          and lines[i + 2].startswith("launchctl bootstrap"), True)
check("  and install itself restarted nothing", station_restarted(calls()), False)
rc, out = rr("update", host, "--apply", "--no-pull")
check("the update it points to restarts the station", (rc, station_restarted(calls())), (0, True))
check("  and clears the pending list", pending(), [])

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
