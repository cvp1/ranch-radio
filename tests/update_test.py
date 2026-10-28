"""`ranch-radio update`, end to end, against a real git origin.

A copy of this checkout is pushed to a bare repo and cloned; new commits are
pushed to the origin and the clone updates itself, with stand-in service
managers (launchctl/systemctl) and a stand-in probe: a server "serves" while
the MARK it was last (re)started with says good (servers keep running the
old code until restarted), and is "transmitting" while BUSY exists.

  - --check: exit 0 when current, 2 with the new commits listed
  - a dry run pulls nothing (the running servers import lazily from the checkout)
  - --apply pulls, restarts, and checks every server that was serving still is
  - a commit that leaves a server down is rolled back (exit 1), remembered,
    and not tried again until a newer commit lands
  - requirements the host's Python lacks: the pull is undone, nothing restarts
  - a busy radio is left on the old code and restarted at the next update
  - --scheduled: notifies once per new commit; with "apply": true, updates

    python3 tests/update_test.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="update-test-"))
MAC = os.uname().sysname == "Darwin"
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


def sh(*argv, cwd=None):
    r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"{' '.join(map(str, argv))}: {r.stderr or r.stdout}")
    return r.stdout.strip()


GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
os.environ.update(GIT_ENV)

# ---------------------------------------------------------------- an origin, a checkout, a dev clone
origin, co, dev, home = TMP / "origin.git", TMP / "checkout", TMP / "dev", TMP / "home"
home.mkdir()
sh("rsync", "-a", "--exclude", ".git", "--exclude", ".venv", "--exclude", "certs", "--exclude", "__pycache__",
   "--exclude", "server.log", "--exclude", "packet-logs", f"{ROOT}/", f"{co}/")
(co / "MARK").write_text("good\n")
sh("git", "init", "-q", "-b", "main", str(co))
sh("git", "-C", str(co), "add", "-A")
sh("git", "-C", str(co), "commit", "-q", "-m", "start")
sh("git", "init", "-q", "--bare", "-b", "main", str(origin))
sh("git", "-C", str(co), "remote", "add", "origin", str(origin))
sh("git", "-C", str(co), "push", "-q", "-u", "origin", "main")
sh("git", "clone", "-q", str(origin), str(dev))


def push(msg, mark=None, req=None):
    if mark is not None:
        (dev / "MARK").write_text(mark + "\n")
    if req is not None:
        (dev / "requirements.txt").write_text(req)
    (dev / "CHANGES.test").write_text(msg + "\n")
    sh("git", "-C", str(dev), "add", "-A")
    sh("git", "-C", str(dev), "commit", "-q", "-m", msg)
    sh("git", "-C", str(dev), "push", "-q")
    return sh("git", "-C", str(dev), "rev-parse", "HEAD")


def head():
    return sh("git", "-C", str(co), "rev-parse", "HEAD")


# ---------------------------------------------------------------- the host file and the stand-ins
host = "mac-shack" if MAC else "linux-laptop"
hosts = TMP / "hosts"
hosts.mkdir()
hf = json.loads((ROOT / f"examples/hosts/{host}.json").read_text())
hf["python"] = sys.executable          # has aiohttp; it is not the checkout's .venv
(hosts / f"{host}.json").write_text(json.dumps(hf))

LOG, LOADED, NOTES = TMP / "calls.log", TMP / "loaded", TMP / "notes.log"
LOADED.mkdir()
fake = TMP / "fakectl"
fake.write_text(f"""#!/bin/sh
echo "$(basename $0) $*" >> {LOG}
last=""; for a in "$@"; do last="$a"; done
label=$(basename "$last" .plist); label=${{label##*/}}
# A (re)started server runs what the checkout holds now: copy its MARK.
# try-restart, like systemd's, does nothing to a unit that has failed (one
# whose MARK went bad); restart starts it whatever its state.
case " $* " in
  *" bootstrap "*|*" load "*|*" --user restart "*) cp {co / 'MARK'} {TMP / 'RUNNING'} ;;
  *" try-restart "*) grep -q good {TMP / 'RUNNING'} && cp {co / 'MARK'} {TMP / 'RUNNING'} ;;
esac
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
probe.write_text(f"""import json, os
mark = open({str(TMP / 'RUNNING')!r}).read().strip()
busy = os.path.exists({str(TMP / 'BUSY')!r})
print(json.dumps({{"up": mark == "good", "tx": busy}}))
""")
notify = TMP / "notify.sh"
notify.write_text(f'#!/bin/sh\necho "$1 | $2" >> {NOTES}\n')
notify.chmod(0o755)

ENV = dict(os.environ, HOME=str(home), RANCH_RADIO_HOSTS=str(hosts),
           RANCH_RADIO_LAUNCHCTL=str(TMP / "launchctl"), RANCH_RADIO_SYSTEMCTL=str(TMP / "systemctl"),
           RANCH_RADIO_PROBE=f"{sys.executable} {probe}", RANCH_RADIO_NOTIFY=str(notify),
           RANCH_RADIO_HEALTH_WAIT="2", RANCH_RADIO_LAUNCHD_WAIT="1", RANCH_RADIO_NO_VENV="1")
ENV.pop("RANCH_RADIO_PIP", None)


def rr(*args, env=None):
    LOG.unlink(missing_ok=True)
    r = subprocess.run([sys.executable, str(co / "ranch-radio"), *args, "--home", str(home)],
                       env=env or ENV, capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout + r.stderr


def calls():
    return LOG.read_text().splitlines() if LOG.exists() else []


def notes():
    out = NOTES.read_text().splitlines() if NOTES.exists() else []
    NOTES.unlink(missing_ok=True)
    return out


def state():
    p = home / ".local/state/ranch-radio/update.json"
    return json.loads(p.read_text()) if p.exists() else {}


def restarted():
    c = calls()
    return any(("bootout" in x or "try-restart" in x) and "update" not in x for x in c)


# first install, and the radio agent running
(TMP / "RUNNING").write_text("good\n")
rc, out = rr("install", host, "--apply")
check("setup: install", rc, 0)
if MAC:
    (LOADED / hf["radios"][0]["label"]).touch()
    (LOADED / "org.ranch-radio.update").touch()
start = head()

# ---------------------------------------------------------------- check, dry run, apply
rc, out = rr("update", host, "--check")
check("--check: current is exit 0", (rc, "current" in out), (0, True))
c1 = push("a good change")
rc, out = rr("update", host, "--check")
check("--check: behind is exit 2, with the commit", (rc, "a good change" in out), (2, True))
rc, out = rr("update", host)
check("dry run: says what it would pull", "would pull 1 commit" in out, True)
check("  and pulls nothing", head(), start)
rc, out = rr("update", host, "--apply")
check("--apply: pulls", head(), c1)
check("  restarts the radio server", restarted(), True)
check("  and confirms it serves", (rc, "every server that was serving is serving" in out), (0, True))
if rc:
    print(out)

# ---------------------------------------------------------------- a commit that breaks a server
bad = push("a change that crashes the server", mark="bad")
rc, out = rr("update", host, "--apply")
check("broken commit: exit 1", rc, 1)
check("  rolled back to the commit before", head(), c1)
check("  says so, and that the servers serve again", "Rolling back" in out and "serving again" in out, True)
check("  the rollback restarted the old code, which serves", "rollback's restart also" in out, False)
check("  remembered as failed here", bad in state().get("failed", {}), True)
rc, out = rr("update", host, "--apply")
check("  not tried again while it is the newest commit", (rc, head(), "failed here" in out), (0, c1, True))
fixed = push("the fix", mark="good")
rc, out = rr("update", host, "--apply")
check("  a newer commit is tried, and lands", (rc, head()), (0, fixed))

# ---------------------------------------------------------------- requirements the Python lacks
req = (dev / "requirements.txt").read_text()
needs = push("needs a package nobody has", req=req + "ranch-radio-no-such-package>=1.0\n")
rc, out = rr("update", host, "--apply")
check("unmet requirement: exit 1", rc, 1)
check("  the pull is undone", head(), fixed)
check("  nothing restarted", restarted(), False)
check("  says what's missing and why it won't pip", "ranch-radio-no-such-package" in out and "won't pip" in out, True)
check("  remembered as failed here", needs in state().get("failed", {}), True)
rc, out = rr("update", host, "--apply", "--force", env=dict(ENV, RANCH_RADIO_PIP=f"{sys.executable} -c pass"))
check("  --force retries it; pip that installs nothing is caught", (rc, head(), "still missing" in out), (1, fixed, True))
unneeded = push("drop it again", req=req)
rc, out = rr("update", host, "--apply")
check("  the next commit lands", (rc, head()), (0, unneeded))

# ---------------------------------------------------------------- a busy radio
(TMP / "BUSY").touch()
busy_rev = push("while the radio transmits")
rc, out = rr("update", host, "--apply")
check("busy radio: the code updates", (rc, head()), (0, busy_rev))
check("  the radio is left alone", restarted(), False)
check("  and held for later", bool(state().get("pending")), True)
rc, out = rr("update", host, "--check")
check("  --check reports it (exit 2)", (rc, "still on the old code" in out), (2, True))
(TMP / "BUSY").unlink()
rc, out = rr("update", host, "--apply")
check("  the next update restarts it", (rc, restarted(), state().get("pending")), (0, True, []))

# ---------------------------------------------------------------- the first update, pulled by an older one
# The deployed update pulls, then runs `update HOST --no-pull --code-changed
# --apply` under the new code, with no --from: the new code finds where the
# pull came from (ORIG_HEAD) so it can still roll back.
base = head()
old_way = push("pulled by the old update, crashes", mark="bad")
sh("git", "-C", str(co), "pull", "-q", "--ff-only")
rc, out = rr("update", host, "--no-pull", "--code-changed", "--apply")
check("old-style hand-off, broken commit: rolled back to where the pull came from", (rc, head()), (1, base))
fix2 = push("fixed again", mark="good")
rc, out = rr("update", host, "--apply")
check("  next update lands", (rc, head()), (0, fix2))
needs2 = push("pulled by the old update, needs a package", req=req + "ranch-radio-no-such-package>=1.0\n")
sh("git", "-C", str(co), "pull", "-q", "--ff-only")
rc, out = rr("update", host, "--no-pull", "--code-changed", "--apply")
check("old-style hand-off, unmet requirement: moved back, nothing restarted", (rc, head(), restarted()), (1, fix2, False))
check("  the pulled commit is the one remembered as failed", (needs2 in state()["failed"], fix2 in state()["failed"]),
      (True, False))
rc, out = rr("update", host, "--no-pull", "--code-changed", "--apply")
check("a manual --code-changed when HEAD did not just move by a pull never rewinds it", head(), fix2)
unneeded = push("drop it again, again", req=req)
rc, out = rr("update", host, "--apply")
check("  next update lands", (rc, head()), (0, unneeded))

# ---------------------------------------------------------------- new code that dies before it can check itself
crash_base = head()
(dev / "ranch_radio/cli.py").write_text('raise RuntimeError("broken on purpose")\n' + (dev / "ranch_radio/cli.py").read_text())
crash = push("new code that can't even start")
rc, out = rr("update", host, "--apply")
check("new code crashes: exit 1, rolled back by the old code", (rc, head()), (1, crash_base))
check("  says so, and remembers it", ("failed (exit" in out, crash in state()["failed"]), (True, True))
check("  the servers serve on the old code", "every server that was serving is serving again" in out, True)
sh("git", "-C", str(dev), "checkout", "-q", "HEAD~1", "--", "ranch_radio/cli.py")
uncrash = push("cli fixed")
rc, out = rr("update", host, "--apply")
check("  the fix lands", (rc, head()), (0, uncrash))

# ---------------------------------------------------------------- the checkout moved without an update
manual = push("pulled by hand")
sh("git", "-C", str(co), "pull", "-q", "--ff-only")
rc, out = rr("update", host, "--check")
check("a hand pull: --check says the servers run older code (exit 2)", (rc, "the servers run" in out), (2, True))
rc, out = rr("update", host, "--apply")
check("  update --apply restarts them on it", (rc, restarted(), "every server that was serving is serving" in out),
      (0, True, True))
manual_bad = push("pulled by hand, broken", mark="bad")
sh("git", "-C", str(co), "pull", "-q", "--ff-only")
rc, out = rr("update", host, "--apply")
check("  broken: rolled back to what the servers ran", (rc, head()), (1, manual))
check("  and they serve again", "every server that was serving is serving again" in out, True)
manual_fix = push("pulled by hand, fixed", mark="good")
rc, out = rr("update", host, "--apply")
check("  next update lands", (rc, head()), (0, manual_fix))

# ---------------------------------------------------------------- one update at a time
import fcntl  # noqa: E402
lk = home / ".local/state/ranch-radio/update.lock"
lk.parent.mkdir(parents=True, exist_ok=True)
with open(lk, "w") as f:
    fcntl.flock(f, fcntl.LOCK_EX)
    other = push("while another update runs")
    rc, out = rr("update", host, "--apply")
    check("a second update while one runs: refused, nothing pulled", (rc, head() != other, "another update" in out),
          (1, True, True))
rc, out = rr("update", host, "--apply")
check("  and runs once the first is done", (rc, head()), (0, other))

# ---------------------------------------------------------------- the scheduled job
notes()
sched = push("found by the daily check")
rc, out = rr("update", host, "--scheduled")
check("scheduled, check only: pulls nothing", (rc, head()), (0, other))
n = notes()
check("  notifies once", (len(n), "update available" in (n[0] if n else "")), (1, True))
rc, out = rr("update", host, "--scheduled")
check("  not twice for the same commit", notes(), [])
hf["updates"] = {"apply": True, "at": "11:30"}
(hosts / f"{host}.json").write_text(json.dumps(hf))
rc, out = rr("update", host, "--scheduled")
check("scheduled with apply: updates", (rc, head()), (0, sched))
check("  never restarts its own job", any("update" in x and ("bootout" in x or "restart ranch-radio-update" in x)
                                          for x in calls()), False)
check("  says it updated", any("updated" in x for x in notes()), True)
installed = (home / ("Library/LaunchAgents/org.ranch-radio.update.plist" if MAC
                     else ".config/systemd/user/ranch-radio-update.timer")).read_text()
check("  a code update installs the host file's new time too", "11:30" in installed or "<integer>11</integer>" in installed,
      True)

(TMP / "RUNNING").write_text("bad\n")         # the radio is off: nothing to check an update against
idle_rev = push("found while nothing serves")
rc, out = rr("update", host, "--scheduled")
check("scheduled apply, nothing serving: leaves it for later", (rc, head() != idle_rev, "no server is serving" in out),
      (0, True, True))
(TMP / "RUNNING").write_text("good\n")
hf["updates"] = False
(hosts / f"{host}.json").write_text(json.dumps(hf))
rc, out = rr("update", host, "--scheduled")
check('"updates": false: an installed job does nothing', (rc, "updates are off" in out, notes()), (0, True, []))

if fails:
    print(out[-3000:])
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
