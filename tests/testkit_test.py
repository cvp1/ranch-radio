"""tests/testkit.py's own guarantees. Nothing goes on air.

  free_port never hands one process the same port twice (two servers on one
  port, CI 2026-11-15), skips a port something already listens on, and picks
  below the OS's ephemeral range, where no outbound socket can take a port
  between the pick and the server's bind (2026-11-26). Each has a negative
  control proving the check can fail.

    .venv/bin/python tests/testkit_test.py
"""
import os
import socket
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testkit  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:<60} got {got!r}" + ("" if ok else f" want {want!r}"))


class RepeatingRng:
    """testkit._rng stand-in whose randrange returns seq in order."""
    def __init__(self, seq):
        self.seq = list(seq)

    def randrange(self, *a):
        return self.seq.pop(0)


def ephemeral_first():
    """The lowest port the OS assigns unasked, or None when unreadable."""
    try:
        with open("/proc/sys/net/ipv4/ip_local_port_range") as f:
            return int(f.read().split()[0])
    except OSError:
        pass
    try:
        out = subprocess.run(["sysctl", "-n", "net.inet.ip.portrange.first"],
                             capture_output=True, text=True, timeout=5).stdout
        return int(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


real_rng = testkit._rng
try:
    # Three ports that were free a moment ago (free_port binds each), not three
    # random ones: a random port busy on the runner made free_port draw again
    # and run the stand-in's list dry (IndexError, CI shard 5, 2026-11-27).
    a, b, c = (testkit.free_port() for _ in range(3))
    # negative control: the stand-in really repeats
    stub = RepeatingRng([a, a])
    check("control: the stand-in offers the same port twice", stub.randrange() == stub.randrange(), True)
    testkit._handed_out.clear()
    testkit._rng = RepeatingRng([a, a, b, b, c])
    got = [testkit.free_port() for _ in range(3)]
    check("repeats are skipped", got, [a, b, c])
    testkit._rng = RepeatingRng([a] * 1000)
    try:
        testkit.free_port()
        check("only repeats: raises, never hangs", "returned", "raised")
    except RuntimeError:
        check("only repeats: raises, never hangs", "raised", "raised")

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        q = busy.getsockname()[1]
        with socket.socket() as probe:     # negative control: binding q really fails
            try:
                probe.bind(("127.0.0.1", q))
                refused = False
            except OSError:
                refused = True
        check("control: a listened-on port refuses a second bind", refused, True)
        testkit._handed_out.discard(q)
        r = next(p for p in range(testkit.PORT_LO, testkit.PORT_HI) if p not in testkit._handed_out)
        testkit._rng = RepeatingRng([q, r])
        check("a port something listens on is skipped", testkit.free_port(), r)
finally:
    testkit._rng = real_rng

ports = [testkit.free_port() for _ in range(200)]
check("200 real ports, all distinct", len(set(ports)), 200)
check("all within PORT_LO..PORT_HI-2 (pair_port's +1 stays inside)",
      all(testkit.PORT_LO <= p < testkit.PORT_HI - 1 for p in ports), True)
first = ephemeral_first()
if first is None:
    print("note  the OS's ephemeral range is unreadable here; range check skipped")
else:
    with socket.socket() as s:            # negative control: the OS's own pick lands in its range
        s.bind(("127.0.0.1", 0))
        os_pick = s.getsockname()[1]
    check(f"control: bind(0) lands at or above {first}", os_pick >= first, True)
    check(f"every pick is below the ephemeral range ({first})", max(ports) < first, True)

print("PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
