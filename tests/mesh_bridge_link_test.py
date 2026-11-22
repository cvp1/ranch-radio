"""mesh_bridge.py's links to the node, without a node or the meshtastic package.

  - tcp:HOST and tcp:HOST:PORT parse (4403 when no port is given), and so
    does a bracketed IPv6 address; no host, a bad port or a bare IPv6 is not
    a tcp target
  - a comma list keeps its order (serial, tcp, ble), a serial device that
    isn't plugged in is left out, and a malformed tcp: entry is refused
  - usb:SERIAL is the device of the USB board with that serial number,
    whatever its case, the /dev/cu.* one where a board shows twice; a board
    that isn't plugged in is left out of a list like a missing device, and
    usb: with no number is refused
  - earlier_cable: the cable that is there now and comes before the port in
    use; nothing when the port in use is first, when the cable is out, or
    when only Bluetooth or Wi-Fi come before it
  - cable_loop, on Bluetooth with the cable out: nothing while it stays out,
    nothing when it shows once and goes, and after two looks running the
    bridge says the cable is back and lets go of its link; it ends quietly
    when the link is lost first
  - connect() opens the right interface for each kind and names its link
  - the bridge says which link it used (info's "link")
  - power saving (which turns Bluetooth and Wi-Fi off on an ESP32 node) is
    refused over Bluetooth and Wi-Fi, allowed over USB
  - a malformed port exits 2 with a status line saying why, before the
    bridge imports the meshtastic package (so it is checked on any Python)

    python3 tests/mesh_bridge_link_test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import types
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} got {got!r}" + ("" if ok else f" want {want!r}"),
          file=sys.__stderr__)


# Stand-ins for the meshtastic interfaces: record what they were opened with.
opened = []


def fake_iface(kind):
    class Iface:
        def __init__(self, *a, **k):
            opened.append((kind, a, k))
    return Iface


pkg = types.ModuleType("meshtastic")
pkg.__path__ = []
sys.modules["meshtastic"] = pkg
for mod, cls, kind in (("serial_interface", "SerialInterface", "serial"),
                       ("ble_interface", "BLEInterface", "ble"),
                       ("tcp_interface", "TCPInterface", "tcp")):
    m = types.ModuleType(f"meshtastic.{mod}")
    setattr(m, cls, fake_iface(kind))
    sys.modules[f"meshtastic.{mod}"] = m

import mesh_bridge  # noqa: E402  (after the stubs; it redirects stdout to stderr)

# --- tcp_target
for spec, want in (("tcp:192.168.1.60", ("192.168.1.60", 4403)),
                   ("tcp:192.168.1.60:4404", ("192.168.1.60", 4404)),
                   ("tcp:meshnode.local", ("meshnode.local", 4403)),
                   ("tcp:[fd00::60]:4403", ("fd00::60", 4403)),
                   ("tcp:[fd00::60]", ("fd00::60", 4403)),
                   ("tcp:", None), ("tcp::4403", None), ("tcp:192.168.1.60:0", None),
                   ("tcp:192.168.1.60:65536", None), ("tcp:192.168.1.60:api", None),
                   ("tcp:fd00::60", None), ("tcp:[fd00::60", None), ("tcp:[fd00::60]4403", None)):
    check(f"tcp_target {spec}", mesh_bridge.tcp_target(spec), want)

# --- candidates
with tempfile.NamedTemporaryFile() as dev:
    got = mesh_bridge.candidates(f"{dev.name}, tcp:192.168.1.60 ,/dev/no-such-node,ble:NODEA_fd78")
    check("a list keeps its order; a missing serial device left out", got,
          [dev.name, "tcp:192.168.1.60", "ble:NODEA_fd78"])
try:
    mesh_bridge.candidates("tcp:192.168.1.60:http,ble:NODEA_fd78")
    check("a malformed tcp: entry is refused", "accepted", "ValueError")
except ValueError as e:
    check("a malformed tcp: entry is refused", "tcp:192.168.1.60:http" in str(e), True)

# --- usb:SERIAL
P = SimpleNamespace
plugged = [P(device="/dev/tty.usbmodem1101", serial_number="1A2B3C4D5E6F7A8B"),
           P(device="/dev/cu.usbmodem1101", serial_number="1A2B3C4D5E6F7A8B"),
           P(device="/dev/cu.SLAB_USBtoUART", serial_number="RIG 0001"), P(device="/dev/cu.debug", serial_number=None)]
check("usb:SERIAL is that board's device, the cu one", mesh_bridge.usb_device("1A2B3C4D5E6F7A8B", plugged), "/dev/cu.usbmodem1101")
check("  whatever the case", mesh_bridge.usb_device(" 1a2b3c4d5e6f7a8b ", plugged), "/dev/cu.usbmodem1101")
check("  a board that is not plugged in: none", mesh_bridge.usb_device("FFFF", plugged), None)
check("  no number matches no board (a port without one is not it)", mesh_bridge.usb_device("", plugged), None)
there = []                                  # what pyserial lists: the cable is out to begin with
mesh_bridge.usb_ports = lambda: there
LIST = "usb:1A2B3C4D5E6F7A8B,ble:NODEA_fd78"
check("in a list, a board that is out is left out", mesh_bridge.candidates(LIST), ["ble:NODEA_fd78"])
there[:] = plugged
check("  and plugged in it is first, as its device", mesh_bridge.candidates(LIST), ["/dev/cu.usbmodem1101", "ble:NODEA_fd78"])
try:
    mesh_bridge.candidates("usb:,ble:NODEA_fd78")
    check("usb: with no number is refused", "accepted", "ValueError")
except ValueError as e:
    check("usb: with no number is refused", "usb:SERIAL" in str(e), True)

# --- earlier_cable
check("on Bluetooth with the cable in: the cable", mesh_bridge.earlier_cable(LIST, "ble:NODEA_fd78"), "/dev/cu.usbmodem1101")
check("  on the cable already: nothing", mesh_bridge.earlier_cable(LIST, "/dev/cu.usbmodem1101"), None)
there[:] = []
check("  with the cable out: nothing", mesh_bridge.earlier_cable(LIST, "ble:NODEA_fd78"), None)
there[:] = plugged
check("only Bluetooth and Wi-Fi before the port in use: nothing (they would have to be tried)",
      mesh_bridge.earlier_cable("tcp:192.168.1.60,ble:NODEA_fd78,usb:1A2B3C4D5E6F7A8B", "/dev/cu.usbmodem1101"), None)
with tempfile.NamedTemporaryFile() as dev:
    check("a device path counts as a cable too", mesh_bridge.earlier_cable(f"{dev.name},ble:NODEA_fd78", "ble:NODEA_fd78"), dev.name)

# --- cable_loop
import threading  # noqa: E402
import time  # noqa: E402
said = []
mesh_bridge.emit = lambda **d: said.append(d)
mesh_bridge.CABLE_AGAIN_S = 0.05
iface0 = SimpleNamespace(myInfo=SimpleNamespace(my_node_num=1), nodesByNum={}, nodes={})


def loop():
    b = mesh_bridge.Bridge(iface0, {}, "ble")
    t = threading.Thread(target=b.cable_loop, args=(LIST, "ble:NODEA_fd78"), daemon=True)
    t.start()
    return b, t


there[:] = []
b, t = loop()
time.sleep(0.4)                              # several looks with the cable out
check("cable out: the bridge stays on Bluetooth", (b.lost.is_set(), said), (False, []))
looks = {"n": 0}


def once():
    looks["n"] += 1
    return plugged if looks["n"] == 1 else []


mesh_bridge.usb_ports = once                 # there for one look, gone at the next
time.sleep(0.4)
check("  the cable shows once and goes: still on Bluetooth", (b.lost.is_set(), said, looks["n"] >= 3), (False, [], True))
mesh_bridge.usb_ports = lambda: plugged
t.join(5)
check("  there two looks running: the bridge lets go of its link", (b.lost.is_set(), t.is_alive()), (True, False))
check("  saying the cable is back", [(d["ev"], d["connected"], "/dev/cu.usbmodem1101 is plugged in again" in d["error"]) for d in said],
      [("status", False, True)])
said.clear()
mesh_bridge.usb_ports = lambda: []
b, t = loop()
b.lost.set()                                 # the link went first
t.join(5)
check("the link lost first: the loop ends, saying nothing", (t.is_alive(), said), (False, []))
mesh_bridge.usb_ports = lambda: there

# --- connect
for spec, kind, args, kw in (("tcp:192.168.1.60", "tcp", ("192.168.1.60",), {"portNumber": 4403}),
                             ("tcp:192.168.1.60:4404", "tcp", ("192.168.1.60",), {"portNumber": 4404}),
                             ("ble:NODEA_fd78", "ble", ("NODEA_fd78",), {}),
                             ("/dev/meshtastic", "serial", ("/dev/meshtastic",), {})):
    opened.clear()
    _, link = mesh_bridge.connect(spec)
    check(f"connect {spec}: link", link, kind)
    check("  opened with", opened and opened[0], (kind, args, kw))

# --- the bridge's guard on its own link
emitted = []
mesh_bridge.emit = lambda **d: emitted.append(d)
iface = SimpleNamespace(myInfo=SimpleNamespace(my_node_num=1), nodesByNum={}, nodes={})
for link, refused in (("tcp", True), ("ble", True), ("serial", False)):
    b = mesh_bridge.Bridge(iface, {}, link)
    emitted.clear()
    try:
        b.set_config({"ref": 7, "section": "power", "values": {"is_power_saving": True}})
    except AttributeError:
        pass                          # not refused: it went on to the (absent) node
    done = next((e for e in emitted if e.get("ev") == "done"), None)
    said = bool(done and not done["ok"] and "power saving" in (done.get("error") or ""))
    check(f"power saving over {link}: {'refused' if refused else 'not refused'}", said, refused)
b = mesh_bridge.Bridge(iface, {}, "tcp")
emitted.clear()
try:
    b.set_config({"ref": 8, "section": "power", "values": {"is_power_saving": False}})
except AttributeError:
    pass
check("power saving off over tcp: not refused", any(e.get("ok") is False and "power saving"
                                                    in (e.get("error") or "") for e in emitted), False)

# --- the link in info
node = SimpleNamespace(localConfig=SimpleNamespace(lora=SimpleNamespace(modem_preset=0)), channels=[])
cfg = types.ModuleType("meshtastic.protobuf.config_pb2")
cfg.Config = SimpleNamespace(LoRaConfig=SimpleNamespace(ModemPreset=SimpleNamespace(Name=lambda n: "LONG_FAST")))
sys.modules["meshtastic.protobuf"] = types.ModuleType("meshtastic.protobuf")
sys.modules["meshtastic.protobuf"].config_pb2 = cfg
sys.modules["meshtastic.protobuf.config_pb2"] = cfg
iface.localNode = node
emitted.clear()
mesh_bridge.Bridge(iface, {}, "tcp").info()
check("info names the link", emitted and emitted[-1].get("link"), "tcp")

# --- a malformed port: exit 2 before any import (this Python has no meshtastic)
env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
r = subprocess.run([sys.executable, os.path.join(ROOT, "mesh_bridge.py"), "tcp:192.168.1.60:0"],
                   capture_output=True, text=True, timeout=30, env=env)
check("malformed port: exit code 2", r.returncode, 2)
lines = [json.loads(x) for x in r.stdout.splitlines() if x.strip()]
check("  with a status line saying why", bool(lines and lines[0].get("ev") == "status"
                                              and "tcp:192.168.1.60:0" in lines[0].get("error", "")), True)

# --- what the Modules page may set as the node's port
from ranch_radio import manifest  # noqa: E402
for v, ok in (("tcp:192.168.1.60", True), ("tcp:192.168.1.60:4403", True),
              ("tcp:meshnode.local,/dev/meshtastic,ble:NODEA_fd78", True), ("/dev/meshtastic", True),
              ("usb:1A2B3C4D5E6F7A8B", True), ("usb:1A2B3C4D5E6F7A8B,ble:NODEA_fd78", True),
              ("usb:", False), ("usb:1A2B;reboot", False),
              ("tcp:192.168.1.60;reboot", False), ("tcp:", False), ("tcp:a b", False)):
    check(f"page rule node_link {v!r}", manifest.check_ask("node_link", v) is None, ok)
check("the mesh module asks for its port with node_link", manifest.MANIFESTS["mesh"]["rules"]["port"],
      "node_link")

print("\nPASS" if not fails else f"\n{fails} FAILED", file=sys.__stderr__)
sys.exit(1 if fails else 0)
