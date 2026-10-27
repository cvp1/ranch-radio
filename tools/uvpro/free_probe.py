import asyncio
import socket
import sys
import time

addr = sys.argv[1]
SVC = "00001100-d102-11e1-9b23-00025b00a5a5"
def probe(ch):
    try:
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        s.settimeout(8); s.connect((addr, ch)); s.send(bytes.fromhex("ff0100010002000403"))
        data, end = b"", time.time() + 3
        while time.time() < end:
            try: b = s.recv(1024)
            except TimeoutError: break
            if not b: break
            data += b
            if len(data) > 64: break
        s.close()
        return f"{len(data)} bytes {data[:24].hex()}"
    except OSError as e:
        return f"{type(e).__name__}: {e}"
for ch in (1, 4):
    print(f"rfcomm {ch}:", probe(ch), flush=True)
async def scan():
    from bleak import BleakScanner
    ds = await BleakScanner.discover(timeout=12, return_adv=True)
    hits = [(d.address, d.name, a.rssi) for d, a in ds.values() if SVC in [u.lower() for u in a.service_uuids] or "UV" in (d.name or "").upper()]
    print("BLE radio adverts:", hits, flush=True)
asyncio.run(scan())
