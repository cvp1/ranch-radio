"""tests/ci/sentinel.py: an untouched HOME passes; a changed, deleted or new
file fails; planting over existing station state is refused."""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tests", "ci", "sentinel.py")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} got {got!r}" + ("" if ok else f" want {want!r}"))


def run(cmd, home, snap):
    return subprocess.run([sys.executable, TOOL, cmd, home, snap], capture_output=True, text=True).returncode


def fresh():
    home = tempfile.mkdtemp(prefix="sentinel-home-")
    snap = os.path.join(tempfile.mkdtemp(prefix="sentinel-snap-"), "s.json")
    return home, snap, run("plant", home, snap)


home, snap, rc = fresh()
check("plant into an empty HOME", rc, 0)
check("untouched: check passes", run("check", home, snap), 0)

listen = os.path.join(home, ".local/state/radio-remote/winlink-listen.json")
os.remove(listen)
check("the listen record consumed: check fails", run("check", home, snap), 1)

home, snap, _ = fresh()
with open(os.path.join(home, ".local/state/radio-remote/addons.json"), "w") as f:
    f.write('{"winlink": {"on": true}}')
check("addons.json rewritten: check fails", run("check", home, snap), 1)

home, snap, _ = fresh()
os.makedirs(os.path.join(home, ".config/pat"))
open(os.path.join(home, ".config/pat/config.json"), "w").close()
check("a Pat config created: check fails", run("check", home, snap), 1)

check("planting over existing state is refused", run("plant", home, snap + ".2"), 1)

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
