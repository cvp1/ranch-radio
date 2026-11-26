"""webguard.host_allowed: local names in, internet names out (DNS rebinding).

Its own names are looked up on first need, never at import: getfqdn() is a
reverse DNS lookup a dead resolver holds ~35 s (macOS CI runner, 2026-11-26).

    python3 tests/webguard_test.py
"""
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} got {got!r}" + ("" if ok else f" want {want!r}"))


lookups = []
real_getfqdn = socket.getfqdn
socket.getfqdn = lambda *a: lookups.append(1) or "station.example.org"
socket.getfqdn()
check("control: the stand-in getfqdn counts a call", len(lookups), 1)
lookups.clear()
import webguard  # noqa: E402
from webguard import host_allowed  # noqa: E402
check("import: no reverse lookup", len(lookups), 0)
for h in ("127.0.0.1:7300", "[::1]", "linux-station", "linux-station.local", "radio.ts.net"):
    host_allowed(h)
check("IP and local names: still no lookup", len(lookups), 0)
check("own dotted name (getfqdn) allowed", host_allowed("station.example.org:7300"), True)
check("  looked up once", len(lookups), 1)
check("control: another dotted name refused", host_allowed("other.example.org"), False)
check("  and the lookup is kept, not repeated", len(lookups), 1)
socket.getfqdn = real_getfqdn
webguard._own = None                       # the real names for the cases below
cases = [
    ("192.168.1.20:7300", True), ("[::1]:7310", True), ("127.0.0.1", True), ("localhost:7300", True),
    ("linux-station:7300", True), ("linux-station.local", True), ("linux-station.lan:7300", True), ("linux-station.home", True),
    ("linux-station.home.arpa", True), ("radio.internal", True), ("linux-station.localdomain", True),
    ("linux-station.tail1234.ts.net", True), ("LINUX-STATION.LAN.", True), (socket.gethostname(), True),
    ("evil.example:7300", False), ("linux-station.lan.evil.example", False), ("lan", True), ("", False),
    ("example.com", False), ("192.168.1.20.nip.io", False),
]
for host, want in cases:
    got = host_allowed(host)
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {host!r:34} {got}")
ok = host_allowed("radio.example.org", ["Radio.Example.org"])
fails += not ok
print(f"{'ok  ' if ok else 'FAIL'}  --allow-host names                  {ok}")
print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
