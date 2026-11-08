"""meshcore_bridge.py's port argument, without a node or the meshcore package.

  - a serial device, tcp:HOST:PORT and ble:NAME parse to the right link
  - tcp: with no port, a bad port or no host is refused; ble: needs a name
  - a comma list is tried in order, and a serial device that isn't
    plugged in is left out
  - a bad port spec exits 2 with a status line saying why, before the
    bridge imports the meshcore package (so it is checked on any Python)

    python3 tests/meshcore_bridge_test.py
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE = os.path.join(ROOT, "meshcore_bridge.py")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} got {got!r}" + ("" if ok else f" want {want!r}"))


CASES = [
    ("a serial device", "parse_port", "/dev/ttyUSB0", ["serial", "/dev/ttyUSB0"]),
    ("tcp:HOST:PORT", "parse_port", "tcp:192.168.1.50:5000", ["tcp", "192.168.1.50", 5000]),
    ("tcp: no port is refused", "parse_port", "tcp:192.168.1.50", "ValueError"),
    ("tcp: a port that isn't a number is refused", "parse_port", "tcp:192.168.1.50:http", "ValueError"),
    ("tcp: port 0 is refused", "parse_port", "tcp:192.168.1.50:0", "ValueError"),
    ("tcp: no host is refused", "parse_port", "tcp::5000", "ValueError"),
    ("ble:NAME", "parse_port", "ble:NODE", ["ble", "NODE"]),
    ("ble: no name is refused", "parse_port", "ble:", "ValueError"),
    ("a list, in order; a missing serial device left out", "candidates",
     "tcp:192.168.1.50:5000, /dev/no-such-node ,ble:NODE", [["tcp", "192.168.1.50", 5000], ["ble", "NODE"]]),
    ("a list with one bad entry is refused", "candidates", "tcp:192.168.1.50,ble:NODE", "ValueError"),
]
# one child for every case: importing the bridge takes over its stdout
CHILD = """import json, sys
sys.path.insert(0, sys.argv[1])
import meshcore_bridge as b
out = []
for fn, spec in json.loads(sys.argv[2]):
    try:
        r = getattr(b, fn)(spec)
        out.append(list(r) if fn == "parse_port" else [list(l) for l in r])
    except ValueError:
        out.append("ValueError")
b.emit(r=out)
"""
p = subprocess.run([sys.executable, "-c", CHILD, ROOT, json.dumps([(fn, spec) for _, fn, spec, _ in CASES])],
                   capture_output=True, text=True, timeout=30)
got = json.loads(p.stdout.strip().splitlines()[-1])["r"]
check("every case answered", len(got), len(CASES))
for (name, _, _, want), g in zip(CASES, got):
    check(name, g, want)

r = subprocess.run([sys.executable, BRIDGE, "tcp:192.168.1.50"], capture_output=True, text=True, timeout=30,
                   stdin=subprocess.DEVNULL)
line = json.loads(r.stdout.strip().splitlines()[0]) if r.stdout.strip() else {}
check("a bad spec exits 2", r.returncode, 2)
check("  with a not-connected status", (line.get("ev"), line.get("connected")), ("status", False))
check("  that says why", "tcp:HOST:PORT" in (line.get("error") or ""), True)

print("PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
