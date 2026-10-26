"""A station with a network KISS TNC (Direwolf) and no shared rigctl port.

The packet hub must render without any Bluetooth/UV-PRO plumbing when the
host file's packet "tnc" is tcp:host:port; the station may switch its shared
rigctl port off (station.rigctl_port 0), and then must not claim a primary
radio; port validation must refuse conflicts. Built from examples/hosts, never
a real host file.

    python3 tests/packet_hub_station_test.py
"""
import copy
import os as _os
import sys
from pathlib import Path

_os.environ["RANCH_RADIO_HOSTS"] = ""
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ranch_radio import render  # noqa: E402
from ranch_radio.render import HostError, validate  # noqa: E402
from station import Station  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


def raises(name, fn, match):
    global fails
    try:
        fn()
    except HostError as e:
        ok = match in str(e)
        fails += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got: {e}"))
    else:
        fails += 1
        print(f"FAIL {name}: no HostError")


base = render.load(str(ROOT / "examples/hosts/linux-station.json"))
host = copy.deepcopy(base)
host["packet"] = {"name": "Direwolf", "url": "http://127.0.0.1:7310", "agwpe": "127.0.0.1:8000",
                  "tnc": "tcp:127.0.0.1:8001", "mycall": "N9ABC", "allow_calls": ["N9ABC"], "tx": False}
host["addons"] = {}
host["station"] = {"https": True, "rigctl_port": 0}
for r in host["radios"]:
    r["rigctl"] = 0
    r.pop("lane", None)
host.pop("winlink", None)
host.pop("integrations", None)
host.pop("manual_restart", None)
validate(host)

arts = render.artifacts(host)
paths = [a.path for a in arts]
by = {a.path.rsplit("/", 1)[-1]: a.text() for a in arts}
hub = by.get("packet-hub.service", "")

check("packet-hub.service renders", "packet-hub.service" in by, True)
check("no uvpro-hub unit for a network TNC", any("uvpro-hub.service" in p for p in paths), False)
check("hub names the TCP TNC", "tcp:127.0.0.1:8001" in hub, True)
check("hub has no Bluetooth ordering", "bluetooth.target" not in hub, True)
check("hub has no fd store", "FileDescriptorStoreMax" not in hub, True)
check("station unit disables the shared rigctl port", "--rigctl-port 0" in by.get("station.service", ""), True)


class Args:
    token = ""
    rigctl_port = 0
    rigctl_host = "127.0.0.1"


st = Station(render.station_json(host), Args())
info = st.radios_info()
check("shared port off", (st.shared_port, st.shared_port_enabled), (0, False))
check("no primary radio when shared port is off", st.primary(), None)
check("radios_info reports shared_port 0", info["shared_port"], 0)
check("radios_info carries the packet hub's name", info["packet"].get("name"), "Direwolf")

clash = copy.deepcopy(host)
clash["station"]["rigctl_port"] = clash["radios"][0]["web"]
raises("station.rigctl_port may not collide with a radio's web port", lambda: validate(clash),
       f"station.rigctl_port {clash['radios'][0]['web']} conflicts with {clash['radios'][0]['id']}.web")

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
