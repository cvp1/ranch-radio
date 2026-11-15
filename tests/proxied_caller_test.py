"""webguard.from_this_machine: a caller through a proxy on this machine
(the station, Tailscale Serve) is not this machine; forwarded_for chains.

    python3 tests/proxied_caller_test.py
"""
import os
import sys

from multidict import CIMultiDict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from webguard import forwarded_for, from_this_machine  # noqa: E402


class Req:
    def __init__(self, remote, **headers):
        self.remote = remote
        self.headers = CIMultiDict()
        for k, v in headers.items():
            for one in (v if isinstance(v, list) else [v]):
                self.headers.add(k.replace("_", "-"), one)


fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} {got!r}")


TS = {"Tailscale_Headers_Info": "https://tailscale.com/s/serve-headers"}
check("loopback, no proxy (the UV-PRO add-on)", from_this_machine(Req("127.0.0.1")), True)
check("::1, no proxy", from_this_machine(Req("::1")), True)
check("LAN caller direct", from_this_machine(Req("192.168.1.50")), False)
check("LAN caller claiming XFF loopback", from_this_machine(Req("192.168.1.50", X_Forwarded_For="127.0.0.1")), False)
check("local caller through the station", from_this_machine(Req("127.0.0.1", X_Forwarded_For="127.0.0.1")), True)
check("LAN caller through the station", from_this_machine(Req("127.0.0.1", X_Forwarded_For="192.168.1.50")), False)
check("tailnet peer through Tailscale Serve",
      from_this_machine(Req("127.0.0.1", X_Forwarded_For="100.64.0.7", **TS)), False)
check("Tailscale Serve with no XFF at all", from_this_machine(Req("127.0.0.1", **TS)), False)
check("forged loopback hop ahead of the real one",
      from_this_machine(Req("127.0.0.1", X_Forwarded_For="127.0.0.1, 100.64.0.7")), False)
check("two XFF headers, one remote",
      from_this_machine(Req("127.0.0.1", X_Forwarded_For=["127.0.0.1", "100.64.0.7"])), False)

check("forwarded_for: none given", forwarded_for(Req("192.168.1.50")), "192.168.1.50")
check("forwarded_for: chains", forwarded_for(Req("127.0.0.1", X_Forwarded_For="100.64.0.7")), "100.64.0.7, 127.0.0.1")
# Tailscale -> station -> radio server: the radio server must still refuse.
hop2 = Req("127.0.0.1", X_Forwarded_For=forwarded_for(Req("127.0.0.1", X_Forwarded_For="100.64.0.7", **TS)))
check("tailnet peer two proxies deep", from_this_machine(hop2), False)
hop2 = Req("127.0.0.1", X_Forwarded_For=forwarded_for(Req("192.168.1.50", X_Forwarded_For="127.0.0.1")))
check("LAN forger two proxies deep", from_this_machine(hop2), False)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
