"""/healthz says when a Meshtastic node has been away (addons/mesh.py
degraded, addons Addons.degraded, station.py).

Seen live 2026-11-22: a node's USB link was out for 11 hours overnight; the
page showed it, and nothing that could have told anyone knew.

Without a server:
  - a node not connected for less than DOWN_GRACE_S is not degraded (a start,
    a reboot); for that long or more it is a line naming it, how long, and why
  - a connected node and a switched-off add-on (no engine) say nothing
  - a connected node that has passed on no packet for SILENT_S (since it
    connected or since its last packet) is a line too; a packet or a
    reconnect starts the count again; a node away is only ever the one line
  - Addons.degraded asks only switched-on add-ons, and one that raises is a
    line of its own
Then station.py with two tests/fake_mesh_bridge.py nodes, one of which finds
no node:
  - /healthz turns not ok, naming that node and not the other
  - the node back: ok again, nothing degraded

    python3 tests/mesh_healthz_test.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import testkit  # noqa: E402
import addons  # noqa: E402
from addons import mesh as mesh_addon  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:64} got {got!r}" + ("" if ok else f" want {want!r}"))


# --- the add-on's own answer
a = mesh_addon.MeshAddon(SimpleNamespace(), {"port": "a", "label": "MF18", "radios": [{"label": "LF", "port": "b"}]})
check("the grace is two minutes", mesh_addon.DOWN_GRACE_S, 120.0)
check("switched off (no engines): nothing", a.degraded(now=1000), [])
a.engine = SimpleNamespace(connected=True, changed=0, error=None, last_rx=None)
a.extra[0]["engine"] = SimpleNamespace(connected=False, changed=900, error="none of b is present")
check("away for less than the grace: not degraded yet", a.degraded(now=1000), [])
check("away for the grace: a line with who, how long and why", a.degraded(now=1020),
      ["mesh LF not connected for 2 min: none of b is present"])
a.extra[0]["engine"].error = None
a.engine.last_rx = 900 + 3500
check("  no reason known: who and how long", a.degraded(now=900 + 3600), ["mesh LF not connected for 60 min"])

# --- connected, but silent (seen live 2026-11-22: a Bluetooth link delivered nothing for five hours)
check("the silence limit is half an hour", mesh_addon.SILENT_S, 1800.0)
s = mesh_addon.MeshAddon(SimpleNamespace(), {"port": "a", "label": "MF18"})
s.engine = SimpleNamespace(connected=True, changed=0, error=None, last_rx=None)
check("connected, no packet yet, under the limit: nothing", s.degraded(now=1799), [])
check("connected, no packet since connecting for the limit: a line", s.degraded(now=1800),
      ["mesh MF18 connected but no packets for 30 min"])
s.engine.last_rx = 1000
check("a packet resets the count", s.degraded(now=2700), [])
check("  and the limit after the last packet: a line", s.degraded(now=1000 + 3600),
      ["mesh MF18 connected but no packets for 60 min"])
s.engine.changed, s.engine.last_rx = 5000, 1000
check("a reconnect resets it too (an old packet does not count)", s.degraded(now=6000), [])
s.engine.connected = False
check("not connected: only the not-connected line, never both",
      s.degraded(now=5000 + 7200), ["mesh MF18 not connected for 120 min"])

a.engine.connected, a.engine.changed = False, 0
check("both away: both, the first node first", [x.split(" not")[0] for x in a.degraded(now=5000)], ["mesh MF18", "mesh LF"])


# --- the registry
class Bad:
    def degraded(self):
        raise RuntimeError("no")


reg = addons.Addons.__new__(addons.Addons)
reg.items = {"mesh": {"addon": a, "on": True}, "off": {"addon": a, "on": False}, "bad": {"addon": Bad(), "on": True},
             "missing": {"addon": None, "on": True}}
a.engine.connected, a.engine.last_rx = True, time.time()
got = reg.degraded()
check("the registry asks switched-on add-ons only; one that raises is its own line",
      [g.split(" for ")[0] for g in got], ["mesh LF not connected", "bad: degraded() failed: RuntimeError"])

# --- a station with one of two nodes away
WEB = testkit.free_port()
TMP = tempfile.mkdtemp(prefix="meshhealthz-")
ABSENT = os.path.join(TMP, "absent")
MARK_A, MARK_B = os.path.join(TMP, "port-mf18"), os.path.join(TMP, "port-lf")
cfg = {"name": "Test Station", "radios": [],
       "mesh": {"port": MARK_A, "label": "MF18", "python": sys.executable, "bridge": "tests/fake_mesh_bridge.py",
                "log": "", "radios": [{"label": "LF", "port": MARK_B, "log": ""}]},
       "addons_state": os.path.join(TMP, "addons.json")}
with open(os.path.join(TMP, "station.json"), "w") as f:
    json.dump(cfg, f)
with open(ABSENT, "w") as f:
    f.write("port-lf")


def health():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{WEB}/healthz", timeout=2) as r:
            return json.load(r)
    except (OSError, ValueError):
        return {}


st = subprocess.Popen([sys.executable, "station.py", "--config", os.path.join(TMP, "station.json"), "--listen", str(WEB),
                       "--rigctl-port", "0"], cwd=ROOT,
                      env=dict(os.environ, FAKE_MESH_ABSENT=ABSENT, MESH_RESTART_S="0.3,0.5,0.5", MESH_DOWN_GRACE_S="2"),
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    check("the station answers /healthz", testkit.wait_for(lambda: "ok" in health(), 30, 0.2), True)
    check("LF away past the grace: not ok", testkit.wait_for(lambda: health().get("ok") is False, 30, 0.2), True)
    h = health()
    check("  naming LF, how long and why; MF18 is not there",
          h.get("degraded"), [f"mesh LF not connected for 0 min: none of {MARK_B} is present"])
    with open(ABSENT, "w") as f:
        f.write("")
    check("LF back: ok again", testkit.wait_for(lambda: health().get("ok") is True, 30, 0.2), True)
    check("  nothing degraded", health().get("degraded"), [])
finally:
    st.terminate()
    try:
        st.wait(10)
    except subprocess.TimeoutExpired:
        st.kill()
    shutil.rmtree(TMP, ignore_errors=True)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
