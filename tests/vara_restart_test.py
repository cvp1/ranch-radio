"""The radio server restarts xara once xara's lane listens (--winlink-vara-label).

xara keeps one rigctld connection to its lane and does not reconnect it: a
server restart broke it, the first VARA call after it failed in ~6 s (xara's
key hit a dead socket, xara exited, launchd brought it back). Now the server
restarts xara's service as soon as the lane listens again.

server.py --sim with stand-in launchctl / systemctl first on PATH; each records
its argv and whether xara's lane was listening when it ran. Nothing transmits.

  - label set: one restart per server start, after the lane listens, through
    the platform's service manager (launchd on macOS, systemd --user on Linux)
  - a second server start: restarted again (a crash restart is covered too)
  - no label: no restart
  - the restart fails: the server keeps running, the lane still listens, the
    failure is logged

    .venv/bin/python tests/vara_restart_test.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import testkit  # noqa: E402

fails = 0
TMP = tempfile.mkdtemp(prefix="vararestart-")
CALLS = os.path.join(TMP, "calls.jsonl")
LABEL = "test.ranch-radio.xara"


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:<62} got {got!r}" + ("" if ok else f" want {want!r}"))


# Stand-in service managers: record argv and whether the lane listened then.
BIN = os.path.join(TMP, "bin")
os.makedirs(BIN)
for name in ("launchctl", "systemctl"):
    path = os.path.join(BIN, name)
    with open(path, "w") as f:
        f.write(f"""#!{sys.executable}
import json, os, socket, sys
try:
    socket.create_connection(("127.0.0.1", int(os.environ["VLANE"])), timeout=1).close()
    up = True
except OSError:
    up = False
with open({CALLS!r}, "a") as f:
    f.write(json.dumps({{"argv": [os.path.basename(sys.argv[0])] + sys.argv[1:], "lane_up": up}}) + "\\n")
sys.exit(int(os.environ.get("FAKE_RESTART_RC", "0")))
""")
    os.chmod(path, 0o755)


def calls():
    try:
        with open(CALLS) as f:
            return [json.loads(ln) for ln in f]
    except FileNotFoundError:
        return []


def listening(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


def start(label=True, rc=0):
    web, lane, vlane, vport = (testkit.free_port() for _ in range(4))
    env = dict(os.environ, PATH=BIN + os.pathsep + os.environ["PATH"], VLANE=str(vlane),
               FAKE_RESTART_RC=str(rc))
    log = open(os.path.join(TMP, f"radio-{web}.log"), "w")
    argv = [sys.executable, "server.py", "--sim", "--rig", "ic7300", "--no-audio", "--host", "127.0.0.1",
            "--listen", str(web), "--rigctl-host", "127.0.0.1", "--rigctl-port", "0",
            "--winlink-rigctl-port", str(lane), "--winlink-vara-log", os.path.join(TMP, "xara.log"),
            "--winlink-vara-rigctl-port", str(vlane), "--winlink-vara-port", str(vport), "--addons-state", ""]
    if label:
        argv += ["--winlink-vara-label", LABEL]
    p = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    if not testkit.wait_for(lambda: listening(vlane), 10):
        p.kill()
        raise SystemExit(f"server did not start; see {log.name}")
    return p, vlane, log.name


def stop(p):
    p.terminate()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


want = (["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"] if sys.platform == "darwin"
        else ["systemctl", "--user", "restart", LABEL])

p, _, _ = start()
try:
    check("label: xara restarted once", testkit.wait_for(lambda: len(calls()) == 1, 5), True)
    check("  ...through the platform's service manager", calls()[0]["argv"] if calls() else None, want)
    check("  ...after its lane listened", calls()[0]["lane_up"] if calls() else None, True)
finally:
    stop(p)

p, _, _ = start()
try:
    check("a second server start: restarted again", testkit.wait_for(lambda: len(calls()) == 2, 5), True)
finally:
    stop(p)

p, _, _ = start(label=False)
try:
    testkit.wait_for(lambda: len(calls()) > 2, 2)
    check("no label: no restart", len(calls()), 2)
finally:
    stop(p)

p, vlane, logf = start(rc=1)
try:
    check("restart fails: it was tried", testkit.wait_for(lambda: len(calls()) == 3, 5), True)
    testkit.wait_for(lambda: "restarting xara" in open(logf).read(), 5)
    check("  ...the server still runs, the lane still listens", (p.poll(), listening(vlane)), (None, True))
    check("  ...logged", f"restarting xara ({LABEL}) failed (rc 1)" in open(logf).read(), True)
finally:
    stop(p)

print("PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
