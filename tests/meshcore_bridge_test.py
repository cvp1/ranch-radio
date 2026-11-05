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


def parse(spec, fn="parse_port"):
    """parse_port(spec) in a child: importing the bridge takes over its stdout."""
    conv = "list(b.parse_port(sys.argv[2]))" if fn == "parse_port" else "[list(l) for l in b.candidates(sys.argv[2])]"
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import meshcore_bridge as b\n"
            f"try:\n    r = {conv}\n"
            "except ValueError as e:\n    r = 'ValueError'\n"
            "b.emit(r=r)")
    out = subprocess.run([sys.executable, "-c", code, ROOT, spec], capture_output=True, text=True, timeout=30)
    return json.loads(out.stdout.strip().splitlines()[-1])["r"]


check("a serial device", parse("/dev/ttyUSB0"), ["serial", "/dev/ttyUSB0"])
check("tcp:HOST:PORT", parse("tcp:192.168.1.50:5000"), ["tcp", "192.168.1.50", 5000])
check("tcp: a host name", parse("tcp:node.lan:5000"), ["tcp", "node.lan", 5000])
check("tcp: no port is refused", parse("tcp:192.168.1.50"), "ValueError")
check("tcp: a port that isn't a number is refused", parse("tcp:192.168.1.50:http"), "ValueError")
check("tcp: port 0 is refused", parse("tcp:192.168.1.50:0"), "ValueError")
check("tcp: no host is refused", parse("tcp::5000"), "ValueError")
check("ble:NAME", parse("ble:NODE"), ["ble", "NODE"])
check("ble: no name is refused", parse("ble:"), "ValueError")
check("a list, in order; a missing serial device left out",
      parse("tcp:192.168.1.50:5000, /dev/no-such-node ,ble:NODE", "candidates"),
      [["tcp", "192.168.1.50", 5000], ["ble", "NODE"]])
check("a list with one bad entry is refused", parse("tcp:192.168.1.50,ble:NODE", "candidates"), "ValueError")

r = subprocess.run([sys.executable, BRIDGE, "tcp:192.168.1.50"], capture_output=True, text=True, timeout=30,
                   stdin=subprocess.DEVNULL)
line = json.loads(r.stdout.strip().splitlines()[0]) if r.stdout.strip() else {}
check("a bad spec exits 2", r.returncode, 2)
check("  with a not-connected status", (line.get("ev"), line.get("connected")), ("status", False))
check("  that says why", "tcp:HOST:PORT" in (line.get("error") or ""), True)

print("PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
