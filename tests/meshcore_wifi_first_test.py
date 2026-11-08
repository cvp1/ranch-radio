"""meshcore_bridge: Wi-Fi before Bluetooth, and the node's last Wi-Fi address
before the configured one (DHCP moves it)."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import meshcore_bridge as mb  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} got {got!r}" + ("" if ok else f" want {want!r}"))


TCP, BLE = ("tcp", "192.168.1.50", 5000), ("ble", "NODE")
check("configured order kept when Wi-Fi is first", mb.wifi_first([TCP, BLE], {}), [TCP, BLE])
check("Wi-Fi tried before Bluetooth even if listed after", mb.wifi_first([BLE, TCP], {}), [TCP, BLE])
check("the learned address goes before the configured one",
      mb.wifi_first([TCP, BLE], {"tcp:192.168.1.50:5000": "192.168.1.77"}),
      [("tcp", "192.168.1.77", 5000), TCP, BLE])
check("a learned address equal to the configured one adds nothing",
      mb.wifi_first([TCP, BLE], {"tcp:192.168.1.50:5000": "192.168.1.50"}), [TCP, BLE])
check("Bluetooth alone is left alone", mb.wifi_first([BLE], {"tcp:192.168.1.50:5000": "192.168.1.77"}), [BLE])

d = tempfile.mkdtemp(prefix="mc-wifi-")
path = os.path.join(d, "state", "meshcore-wifi.json")
check("nothing learned yet: empty", mb.load_learned(path), {})
mb.save_learned("tcp:192.168.1.50:5000", "192.168.1.77", path)
check("saved, then read back", mb.load_learned(path), {"tcp:192.168.1.50:5000": "192.168.1.77"})
mb.save_learned("tcp:192.168.1.50:5000", "192.168.1.78", path)
check("a newer address replaces it", mb.load_learned(path), {"tcp:192.168.1.50:5000": "192.168.1.78"})
with open(path, "w") as f:
    f.write("not json")
check("a corrupt file reads as nothing learned", mb.load_learned(path), {})

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
