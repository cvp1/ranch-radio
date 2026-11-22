"""`ranch-radio status | stop | restart`, never cutting off a transmission.

Stand-ins for systemctl, the probe and the STOP TX call (Linux host file):
  - stop refuses a radio that is transmitting, in a Winlink session, or
    running but not answering; and the service manager is never asked to stop it
  - rigctl clients refuse unless --drop-rigctl
  - an idle radio: STOP TX first, then its socket and service stop
  - STOP TX not confirmed: left alone
  - restart refuses a radio with STOP TX latched in memory only (stop does
    not); a latch the radio saves (--stop-state) restarts
  - a stopped radio: stop says so, restart starts it
Then the real thing: a simulated radio (server.py --sim, never hardware), the
real probe and STOP TX call, systemctl a stand-in. PTT on over rigctl: stop
refuses and systemctl is never called. PTT off: stop latches STOP TX on the
radio (its /healthz says so) and only then stops the units; restart now
refuses (latched, not saved). `status --json` reports it all.
Then with --stop-state: the stop's own STOP TX isn't saved (killed and
started again, it keys); a page's STOP TX is, a restart goes ahead, and the
radio comes back stopped; update's idle() lets it restart too.

    python3 tests/cli_lifecycle_test.py
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import testkit  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:64} got {got!r}" + ("" if ok else f" want {want!r}"))


TMP = pathlib.Path(tempfile.mkdtemp(prefix="lifecycle-"))
LOG, ACTIVE = TMP / "calls.log", TMP / "active"
(TMP / "systemctl").write_text(f"""#!/bin/sh
echo "systemctl $*" >> {LOG}
case "$2" in
  is-active) grep -qx "$4" {ACTIVE} 2>/dev/null; exit $? ;;
  stop) shift 2; for u in "$@"; do grep -vx "$u" {ACTIVE} > {ACTIVE}.n; mv {ACTIVE}.n {ACTIVE}; done ;;
  start) shift 2; for u in "$@"; do echo "$u" >> {ACTIVE}; done ;;
esac
exit 0
""")
(TMP / "systemctl").chmod(0o755)
(TMP / "probe.py").write_text(f"""import json, os, sys
port = sys.argv[-1].rsplit(":", 1)[1].split("/")[0]
f = os.path.join({str(TMP)!r}, "state-" + port + ".json")
print(open(f).read() if os.path.exists(f) else json.dumps({{"up": True}}))
""")
(TMP / "txstop.py").write_text(f"""import json, os, sys
open({str(TMP / 'txstop.log')!r}, "a").write(sys.argv[-2] + " " + sys.argv[-1] + "\\n")
print(json.dumps({{"stopped": not os.path.exists({str(TMP / 'NOLATCH')!r})}}))
""")

h = json.loads((ROOT / "examples/hosts/linux-station.json").read_text())
h["python"] = sys.executable
G90 = next(r for r in h["radios"] if r["id"] == "g90")
for r in h["radios"]:
    r["web"] = testkit.free_port()        # nothing real listens there
hosts = TMP / "hosts"
hosts.mkdir()


def write_host():
    (hosts / "linux-station.json").write_text(json.dumps(h))


write_host()
BASE = dict(os.environ, RANCH_RADIO_HOSTS=str(hosts), RANCH_RADIO_SYSTEMCTL=str(TMP / "systemctl"))
STANDIN = dict(BASE, RANCH_RADIO_PROBE=f"{sys.executable} {TMP / 'probe.py'}",
               RANCH_RADIO_TXSTOP=f"{sys.executable} {TMP / 'txstop.py'}")


def rr(*args, env=STANDIN):
    LOG.unlink(missing_ok=True)
    p = subprocess.run([sys.executable, str(ROOT / "ranch-radio"), *args, "--host", "linux-station",
                        *(["--home", str(TMP)] if args[0] in ("stop", "restart") else [])],
                       capture_output=True, text=True, env=env, timeout=120)
    return p.returncode, p.stdout + p.stderr


def calls():
    return LOG.read_text().splitlines() if LOG.exists() else []


def stops():
    return [c for c in calls() if " stop " in c + " "]


def state(st):
    (TMP / f"state-{G90['web']}.json").write_text(json.dumps(st))


def running():
    ACTIVE.write_text("g90-remote.socket\ng90-remote.service\n")


# ---------------------------------------------------------------- stand-ins
for what, st, word in (("transmitting", {"up": True, "tx": True}, "transmitting"),
                       ("in a Winlink session", {"up": True, "armed": True}, "Winlink"),
                       ("running but not answering", {"up": False, "why": "not answering"}, "not answering")):
    running()
    state(st)
    (TMP / "txstop.log").unlink(missing_ok=True)
    rc, out = rr("stop", "g90")
    check(f"stop refuses a radio {what}", (rc, word in out), (1, True))
    check("  the service manager is never asked to stop it", stops(), [])
    check("  and no STOP TX is sent", (TMP / "txstop.log").exists(), False)

running()
state({"up": True, "rigctl": 1})
check("rigctl clients: refused", rr("stop", "g90")[0], 1)
check("  --drop-rigctl goes ahead", rr("stop", "g90", "--drop-rigctl")[0], 0)

running()
state({"up": True})
(TMP / "txstop.log").unlink(missing_ok=True)
rc, out = rr("stop", "g90")
check("an idle radio stops", rc, 0)
check("  STOP TX went to that radio first", (TMP / "txstop.log").read_text().split()[0],
      f"ws://127.0.0.1:{G90['web']}/ws")
check("  then its socket and its service", stops(), ["systemctl --user stop g90-remote.socket g90-remote.service"])
check("stop again: already stopped, nothing asked", (rr("stop", "g90")[0], stops()), (0, []))

running()
(TMP / "NOLATCH").write_text("")
rc, out = rr("stop", "g90")
check("STOP TX not confirmed: left alone", (rc, "not confirmed" in out, stops()), (1, True, []))
(TMP / "NOLATCH").unlink()

running()
state({"up": True, "stopped": True})
rc, out = rr("restart", "g90")
check("restart refuses a radio with STOP TX latched", (rc, "latched" in out), (1, True))
check("  nothing stopped or started", [c for c in calls() if " is-active " not in c], [])
check("  stop is still allowed (it can't transmit stopped)", rr("stop", "g90")[0], 0)

running()
state({"up": True, "stopped": True, "stop_saved": True})
rc, out = rr("restart", "g90")
check("a latch the radio saves: restart goes ahead", rc, 0)
check("  stopped, then started", [c.split()[2] for c in calls() if c.split()[2] in ("stop", "start")],
      ["stop", "start"])

ACTIVE.write_text("")
rc, out = rr("restart", "g90")
check("restart a stopped radio: started, socket and service",
      (rc, [c for c in calls() if " start " in c + " "]),
      (0, ["systemctl --user start g90-remote.socket g90-remote.service"]))
check("an unknown name is refused", rr("stop", "nope")[0], 1)

# ---------------------------------------------------------------- the real thing
web, rig = G90["web"], testkit.free_port()
srv = subprocess.Popen([sys.executable, "server.py", "--sim", "--rig", "g90", "--no-audio", "--host", "127.0.0.1",
                        "--listen", str(web), "--rigctl-port", str(rig), "--tx-timeout", "30"], cwd=ROOT,
                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def health():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{web}/healthz", timeout=3) as r:
            return json.load(r)
    except OSError:
        return {}


class Rigctl:
    """One rigctl connection, held open: the server drops a client's PTT when
    its connection closes (as JTDX's would)."""
    def __init__(self):
        self.s = socket.create_connection(("127.0.0.1", rig), timeout=5)

    def __call__(self, cmd):
        self.s.sendall((cmd + "\n").encode())
        return self.s.recv(256).decode().strip()

    def close(self):
        self.s.close()


REAL = dict(BASE)
try:
    check("the simulated radio is up", testkit.wait_for(lambda: health().get("rig_connected"), 60, 0.2), True)
    running()
    rigctl = Rigctl()
    check("PTT on over rigctl (the simulator: no RF)", rigctl("T 1"), "RPRT 0")
    check("  the radio says it is keyed", testkit.wait_for(lambda: health().get("tx"), 10, 0.1), True)
    rows = json.loads(rr("status", "--json", env=REAL)[1])
    g = next(x for x in rows if x["key"] == "g90")
    check("status: running, answering, transmitting", (g["active"], g["up"], g["tx"]), (True, True, True))
    rc, out = rr("stop", "g90", env=REAL)
    check("stop refuses the keyed radio", (rc, "transmitting" in out), (1, True))
    check("  systemctl was never asked to stop it", stops(), [])
    check("  it is still keyed: the CLI did not touch it", health().get("tx"), True)
    rigctl("T 0")
    rigctl.close()
    testkit.wait_for(lambda: health().get("tx") is False and not health().get("rigctl_clients"), 10, 0.1)
    rc, out = rr("stop", "g90", "--drop-rigctl", env=REAL)
    check("PTT off: stop goes ahead", rc, 0)
    check("  STOP TX latched on the radio before the stop", health().get("tx_stopped"), True)
    check("  then the units stopped", stops(), ["systemctl --user stop g90-remote.socket g90-remote.service"])
    running()                                       # the stand-in stopped nothing real; say it runs again
    rc, out = rr("restart", "g90", "--drop-rigctl", env=REAL)
    check("restart now refuses: STOP TX is latched", (rc, "latched" in out), (1, True))
    g = next(x for x in json.loads(rr("status", "--json", env=REAL)[1]) if x["key"] == "g90")
    check("status says latched", g.get("stopped"), True)
finally:
    srv.terminate()
    try:
        srv.wait(10)
    except subprocess.TimeoutExpired:
        srv.kill()

# ---------------------------------------------------------------- the real thing, STOP TX saved
SAVED = TMP / "stop-tx" / "g90.json"
PAGE_STOP = """import asyncio, json, sys, aiohttp
async def main():
    async with aiohttp.ClientSession() as s, s.ws_connect(sys.argv[1]) as ws:
        await ws.receive(timeout=5)
        await ws.send_str(json.dumps({"t": "tx_stop"}))
        while not json.loads((await ws.receive(timeout=5)).data).get("stopped"):
            pass
asyncio.run(main())
"""


def start_saved():
    return subprocess.Popen([sys.executable, "server.py", "--sim", "--rig", "g90", "--no-audio", "--host",
                             "127.0.0.1", "--listen", str(web), "--rigctl-port", str(rig), "--stop-state",
                             str(SAVED)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def kill(p):
    p.kill()
    p.wait(10)
    testkit.wait_for(lambda: not health(), 10, 0.1)


def saved():
    return json.loads(SAVED.read_text()).get("stopped") if SAVED.exists() else None


srv = start_saved()
try:
    check("saved: the simulated radio is up", testkit.wait_for(lambda: health().get("rig_connected"), 60, 0.2), True)
    running()
    rc, out = rr("stop", "g90", "--drop-rigctl", env=REAL)
    check("stop: STOP TX latched before the units stopped", (rc, health().get("tx_stopped")), (0, True))
    check("  but not saved: it was the stop's, not the operator's", saved(), None)
    kill(srv)
    srv = start_saved()
    testkit.wait_for(lambda: health().get("rig_connected"), 60, 0.2)
    check("  started again: not stopped (a routine restart keys)", health().get("tx_stopped"), False)
    subprocess.run([sys.executable, "-c", PAGE_STOP, f"ws://127.0.0.1:{web}/ws"], timeout=30, check=True)
    check("a page's STOP TX is saved", bool(saved()), True)
    running()                                       # the stand-in stopped nothing real
    g = next(x for x in json.loads(rr("status", "--json", env=REAL)[1]) if x["key"] == "g90")
    check("  status: latched, and saved", (g.get("stopped"), g.get("stop_saved")), (True, True))
    sys.path.insert(0, str(ROOT))
    from ranch_radio import service
    check("  update's idle() lets it restart", service.idle(h, G90, drop_rigctl=True)[0], True)
    before = saved()
    running()
    rc, out = rr("restart", "g90", "--drop-rigctl", env=REAL)
    check("  restart goes ahead", (rc, [c.split()[2] for c in calls() if c.split()[2] in ("stop", "start")]),
          (0, ["stop", "start"]))
    check("  the operator's latch on disk is untouched", saved(), before)
    kill(srv)
    srv = start_saved()
    testkit.wait_for(lambda: health().get("rig_connected"), 60, 0.2)
    check("  and the radio comes back stopped", health().get("tx_stopped"), True)
finally:
    srv.kill()
    srv.wait(10)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
