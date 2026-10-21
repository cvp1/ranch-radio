"""winlink.check_url's band gate: HF for every radio transport, VHF/UHF for
packet only. Pure; no servers. Run: python3 tests/winlink_bands_test.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from server import band_of          # noqa: E402
from winlink import check_url, without_freq       # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} got {got!r}" + ("" if ok else f" want {want!r}"))


def refused(url):
    return check_url(url, band_of) is not None


check("ARDOP on 80 m: allowed", refused("ardop:///N0DAJ?freq=3588.5"), False)
check("ARDOP off-band (14.400 MHz): refused", refused("ardop:///N0DAJ?freq=14400"), True)
check("packet on 2 m (N7RCC-10, 145.010): allowed", refused("ax25:///N7RCC-10?freq=145010"), False)
check("packet on 2 m via agwpe: allowed", refused("ax25+agwpe:///K7RDG-10?freq=144930"), False)
check("packet on 70 cm (441.300): allowed", refused("ax25:///K7ARC-10?freq=441300"), False)
check("packet on 1.25 m (223.400): allowed", refused("ax25:///W7XXX-10?freq=223400"), False)
check("packet on HF 30 m (10.147): allowed", refused("ax25:///W7XXX-10?freq=10147"), False)
check("packet off-band (150.000, business): refused", refused("ax25:///W7XXX-10?freq=150000"), True)
check("packet off-band (462.5625, GMRS): refused", refused("ax25:///W7XXX-10?freq=462562.5"), True)
check("ARDOP on 2 m: refused (HF rigs only)", refused("ardop:///W7XXX?freq=145010"), True)
check("packet with no freq: allowed", refused("ax25:///N7RCC-10"), False)

check("without_freq: drops freq only",
      without_freq("ardop:///N0DAJ?bw=2000MAX&freq=3588.5"), "ardop:///N0DAJ?bw=2000MAX")
check("without_freq: packet", without_freq("ax25:///N7RCC-10?freq=145010"), "ax25:///N7RCC-10")
check("without_freq: none to drop", without_freq("ax25:///N7RCC-10"), "ax25:///N7RCC-10")

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
