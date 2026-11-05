#!/usr/bin/env python3
"""Put a MeshCore companion on Wi-Fi, from its USB port, without echoing the password.

    python3 tools/meshcore_wifi.py /dev/ttyUSB0

For a companion build with the Wi-Fi interface (companion_radio_ble on ESP32
boards has Bluetooth and Wi-Fi; companion_radio_wifi). Its Wi-Fi settings
are text commands on the firmware's rescue CLI, which a USB port only
reaches during the first seconds after boot, with a long press on the
board's button. This:

  1. resets the board (the USB-serial chip's RTS line) and asks you to hold
     the button until the screen says it's in rescue mode
  2. asks for the network name, and the password with no echo (getpass),
     and sends set wifi.ssid / wifi.pwd / wifi.enabled 1
  3. reads the name and the enabled flag back, then reboots the node
  4. looks for the node on the LAN: a port 5000 that wasn't open before

The password goes only to the node: never to the screen, a log, argv or a
file. The rescue CLI echoes what it is sent; this reads the echo and drops
it. The companion's TCP port has no login, and anything that can reach it
can also read the password back (get wifi.pwd), so put the node on a
network you trust.

Needs pyserial. The node's own settings (name, keys, radio) are kept.
"""

import argparse
import concurrent.futures
import getpass
import ipaddress
import socket
import sys
import time

BANNER = b"CLI Rescue"
RESCUE_WAIT_S = 25.0
TCP_PORT = 5000


def reset(ser):
    ser.dtr = False                  # GPIO0 high: boot the app, not the ROM loader
    ser.rts = True
    time.sleep(0.1)
    ser.rts = False


def read_until(ser, marker, timeout):
    end, buf = time.time() + timeout, b""
    while time.time() < end:
        buf += ser.read(256)
        if marker in buf:
            return buf
    return None


def command(ser, line, secret=False):
    """Send one rescue-CLI line; return its reply, never the echo.

    The CLI echoes each character it reads (the line ends in "\\r"), then
    prints "  <reply>\\r\\n": the reply is the last piece that starts with
    two spaces. For a secret line only "(sent)" or "(no reply)" comes back."""
    ser.reset_input_buffer()
    ser.write(line.encode() + b"\r")
    buf = read_until(ser, b"\r\n", 3.0) or b""
    pieces = [x for x in buf.decode(errors="replace").replace("\n", "\r").split("\r") if x.startswith("  ")]
    reply = pieces[-1].strip() if pieces else ""
    if secret:
        return "(sent)" if reply else "(no reply)"
    return reply


def lan():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return ipaddress.ip_network(f"{s.getsockname()[0]}/24", strict=False)
    except OSError:
        return None
    finally:
        s.close()


def open_5000(net):
    def probe(ip):
        try:
            socket.create_connection((str(ip), TCP_PORT), timeout=0.6).close()
            return str(ip)
        except OSError:
            return None
    with concurrent.futures.ThreadPoolExecutor(64) as ex:
        return {ip for ip in ex.map(probe, net.hosts()) if ip}


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("port", help="the node's USB serial device")
    p.add_argument("--ssid", help="network name (asked for when not given)")
    p.add_argument("--net", help="LAN to look for the node on, e.g. 192.168.1.0/24 (default: this machine's /24)")
    args = p.parse_args()
    import serial

    net = ipaddress.ip_network(args.net) if args.net else lan()
    before = open_5000(net) if net else set()

    ssid = args.ssid or input("Wi-Fi network name: ").strip()
    pwd = getpass.getpass("Wi-Fi password (not shown): ")
    if not ssid or len(ssid.encode()) > 32 or len(pwd.encode()) > 63:
        sys.exit("The name must be 1-32 bytes and the password at most 63.")

    with serial.Serial(args.port, 115200, timeout=0.2) as ser:
        reset(ser)
        print("Resetting the node. Hold its button (PRG) now, until the screen goes to rescue mode...")
        if read_until(ser, BANNER, RESCUE_WAIT_S) is None:
            sys.exit("The node didn't enter rescue mode. Run this again, and hold the button "
                     "within 8 seconds of the reset.")
        print("In rescue mode.")
        print(" ", command(ser, f"set wifi.ssid {ssid}"))
        print(" ", command(ser, f"set wifi.pwd {pwd}", secret=True))
        del pwd
        print(" ", command(ser, "set wifi.enabled 1"))
        got_ssid, got_on = command(ser, "get wifi.ssid"), command(ser, "get wifi.enabled")
        print(f"  read back: network {got_ssid[2:]!r}, Wi-Fi enabled {got_on[2:]}")
        if got_ssid[2:].strip() != ssid or got_on[2:].strip() != "1":
            sys.exit("The node didn't keep the settings; nothing else was changed.")
        reply = command(ser, "reboot")
        if not reply:
            reset(ser)
        print("Rebooting the node onto Wi-Fi.")

    if not net:
        print("No LAN address here to look for it on: check your router for the new client.")
        return 0
    print(f"Looking for it on {net} (port {TCP_PORT})...")
    for _ in range(12):
        time.sleep(5)
        new = open_5000(net) - before
        if new:
            for ip in sorted(new):
                print(f"Found: {ip}:{TCP_PORT}  ->  add-on port \"tcp:{ip}:{TCP_PORT}\"")
            print("Reserve that address for the node in your router, so it stays put.")
            return 0
    print("Not seen on the LAN within a minute. Check the network name and password, and that the "
          "network is 2.4 GHz (the ESP32 has no 5 GHz).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
