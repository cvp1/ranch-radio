import socket
import sys
import time

addr, ch = sys.argv[1], int(sys.argv[2])
s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
s.settimeout(8)
s.connect((addr, ch))
s.send(bytes.fromhex("ff0100010002000403"))      # GAIA: BASIC GET_DEV_INFO (read-only)
data, end = b"", time.time() + 4
while time.time() < end:
    try:
        b = s.recv(1024)
    except TimeoutError:
        break
    if not b:
        break
    data += b
    if data.startswith(b"\xff\x01") and len(data) >= 4 + data[3] + 4:
        break
s.close()
print(f"channel {ch}: {len(data)} bytes: {data[:64].hex()}")
