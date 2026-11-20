"""ranch-radio demo: try the station without a radio.

Starts a simulated IC-7300 (server.py --sim) and a station page in front of
it, on loopback only, with the README's made-up Roster and Map
(tools/readme_fixture.py: invented calls around FN31), prints the address and
runs until Ctrl-C. Everything lives in a scratch directory that is the
processes' HOME and is removed on exit: no host file, no service, nothing in
your own config or state. A simulated radio cannot transmit, and the demo's
logbook sync is a stand-in that answers nothing: a click on a Roster row says
so instead of reaching JTDX.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START_S = 60                      # the radio and the station each get this long to answer

# Run in the station's process: swap its logbook sync (loaded by name, see
# station._logsync_module) for a stand-in serving the fixture, then start it.
STATION = """
import sys, time, types
sys.path[:0] = [{root!r}, {tools!r}]
import station
import readme_fixture as F
from addons import map as M


class DemoSync:
    roster = None                                   # no JTDX: no grid of its own

    def __init__(self, block, **_):
        pass

    async def start(self):
        pass

    async def stop(self):
        pass

    def status(self):
        return {{"loaded": True, "running": False}}

    def roster_view(self):
        return F.ROSTER

    def map_view(self, band=None):
        return {{**F.MAP["map"], "showing": band or F.MAP["map"]["showing"]}}

    def rig_view(self):
        return {{"alive": False}}

    def reply(self, key, done=None):
        return "this is the demo: there is no JTDX to answer that station"

    async def correct(self, qso_id, dest):
        return "this is the demo: there is no logbook"


station._logsync_module = lambda: types.SimpleNamespace(LogSync=DemoSync, scrub=lambda s, cap=200: str(s)[:cap])
M.Map.hears_me = lambda self: ({{**F.MAP["hears_me"], "checked": int(time.time()) - 30}}, None)
sys.argv = ["station.py"] + sys.argv[1:]
station.main()
"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def answers(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


def _wait(procs, port, what, log):
    end = time.time() + START_S
    while time.time() < end:
        if answers(port):
            return
        for p in procs:
            if p.poll() is not None:
                raise SystemExit(f"ranch-radio demo: the {what} stopped (exit {p.returncode}):\n{_tail(log)}")
        time.sleep(0.1)
    raise SystemExit(f"ranch-radio demo: the {what} did not answer in {START_S} s:\n{_tail(log)}")


def _tail(path, n=15):
    try:
        return "".join(Path(path).read_text(errors="replace").splitlines(True)[-n:])
    except OSError:
        return ""


def _stop(p):
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def _interrupt(*_):
    raise KeyboardInterrupt      # a SIGTERM stops the demo the way Ctrl-C does


def run(port=0):
    tmp = tempfile.mkdtemp(prefix="ranch-radio-demo-")
    env = {**os.environ, "HOME": tmp, "XDG_CONFIG_HOME": os.path.join(tmp, ".config"),
           "XDG_DATA_HOME": os.path.join(tmp, ".local", "share"), "XDG_STATE_HOME": os.path.join(tmp, ".local", "state"),
           "RANCH_RADIO_HOSTS": "", "RANCH_RADIO_OFFLINE": "1", "RANCH_RADIO_DEFAULT_HOST": "127.0.0.1"}
    st_port = port or free_port()
    rp = {"web": free_port(), "rigctl": free_port(), "lane": free_port()}
    cfg = {"name": "Demo Station",
           "radios": [{"id": "ic7300", "name": "IC-7300 (simulated)", **rp}],
           "addons": {"roster": {"enabled": True}, "map": {"enabled": True, "pota": False}},
           "addons_state": os.path.join(tmp, "addons.json"),
           "logsync": {"demo": True}}
    with open(os.path.join(tmp, "station.json"), "w") as f:
        json.dump(cfg, f)
    radio_log, station_log = os.path.join(tmp, "radio.log"), os.path.join(tmp, "station.log")
    procs = []
    # Ctrl-C reaches the whole process group; this process stops the children itself.
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        procs.append(subprocess.Popen(
            [sys.executable, "server.py", "--sim", "--rig", "ic7300", "--no-audio", "--host", "127.0.0.1",
             "--listen", str(rp["web"]), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(rp["rigctl"]),
             "--winlink-rigctl-port", str(rp["lane"]), "--addons-state", ""],
            cwd=ROOT, env=env, stdout=open(radio_log, "w"), stderr=subprocess.STDOUT))
        _wait(procs, rp["web"], "simulated radio", radio_log)
        code = STATION.format(root=str(ROOT), tools=str(ROOT / "tools"))
        procs.append(subprocess.Popen(
            [sys.executable, "-c", code, "--config", os.path.join(tmp, "station.json"), "--host", "127.0.0.1",
             "--listen", str(st_port), "--rigctl-host", "127.0.0.1", "--rigctl-port", "0"],
            cwd=ROOT, env=env, stdout=open(station_log, "w"), stderr=subprocess.STDOUT))
        _wait(procs, st_port, "station", station_log)
        print(f"Ranch Radio demo: http://127.0.0.1:{st_port}/", flush=True)
        print(f"  a simulated IC-7300 (its own page: http://127.0.0.1:{rp['web']}/), made-up stations "
              "on the Roster and Map; nothing transmits. Ctrl-C to stop.", flush=True)
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
        dead = next(p for p in procs if p.poll() is not None)
        log = radio_log if dead is procs[0] else station_log
        print(f"ranch-radio demo: a process stopped (exit {dead.returncode}):\n{_tail(log)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nRanch Radio demo: stopped.", flush=True)
        return 0
    finally:
        for p in reversed(procs):
            _stop(p)
        shutil.rmtree(tmp, ignore_errors=True)       # the demo's own scratch directory, nothing else
