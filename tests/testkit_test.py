"""tests/testkit.py's own guarantees. Nothing goes on air.

  free_port never hands one process the same port twice, even when the OS
  offers it again (two servers on one port, CI 2026-11-15); a negative
  control proves the stub really repeats.

    .venv/bin/python tests/testkit_test.py
"""
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testkit  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:<60} got {got!r}" + ("" if ok else f" want {want!r}"))


class RepeatingSocket:
    """socket.socket stand-in whose bind(0) offers 40000, 40000, 40001, 40001, ..."""
    seq = []

    def __init__(self, *a, **k):
        self.p = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def bind(self, addr):
        self.p = RepeatingSocket.seq.pop(0)

    def getsockname(self):
        return ("127.0.0.1", self.p)


real = socket.socket
try:
    socket.socket = RepeatingSocket
    RepeatingSocket.seq = [40000, 40000, 40001, 40001, 40002]
    # negative control: the OS stand-in really offers a repeat
    with RepeatingSocket() as s:
        s.bind(("127.0.0.1", 0))
        a = s.getsockname()[1]
    with RepeatingSocket() as s:
        s.bind(("127.0.0.1", 0))
        b = s.getsockname()[1]
    check("control: the stand-in offers the same port twice", a == b, True)
    RepeatingSocket.seq = [40000, 40000, 40001, 40001, 40002]
    got = [testkit.free_port() for _ in range(3)]
    check("repeats offered by the OS are skipped", got, [40000, 40001, 40002])
    RepeatingSocket.seq = [40000] * 1000
    try:
        testkit.free_port()
        check("an OS that only repeats: raises, never hangs", "returned", "raised")
    except RuntimeError:
        check("an OS that only repeats: raises, never hangs", "raised", "raised")
finally:
    socket.socket = real

ports = [testkit.free_port() for _ in range(200)]
check("200 real ports, all distinct", len(set(ports)), 200)

print("PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
