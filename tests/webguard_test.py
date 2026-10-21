"""webguard.host_allowed: local names in, internet names out (DNS rebinding).

    python3 tests/webguard_test.py
"""
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from webguard import host_allowed  # noqa: E402

fails = 0
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
