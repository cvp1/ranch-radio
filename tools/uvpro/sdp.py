"""Read-only SDP query (Linux, BlueZ L2CAP socket): list a device's service
records with their class UUIDs, names and RFCOMM channels."""
import socket
import struct
import sys
import uuid

BASE = "-0000-1000-8000-00805f9b34fb"


def de(buf, i):
    """Parse one SDP data element at i -> (value, next_i)."""
    h = buf[i]; t, sz = h >> 3, h & 7; i += 1
    if t == 0:
        return None, i
    if sz < 5:
        n = [1, 2, 4, 8, 16][sz]
    else:
        k = [1, 2, 4][sz - 5]
        n = int.from_bytes(buf[i:i + k], "big"); i += k
    raw = buf[i:i + n]; j = i + n
    if t == 1:
        return ("uint", int.from_bytes(raw, "big")), j
    if t == 2:
        return ("int", int.from_bytes(raw, "big", signed=True)), j
    if t == 3:
        if n == 2:
            return ("uuid", f"0000{raw.hex()}{BASE}"), j
        if n == 4:
            return ("uuid", f"{raw.hex()}{BASE}"), j
        return ("uuid", str(uuid.UUID(bytes=bytes(raw)))), j
    if t in (4, 8):
        return ("str", bytes(raw).decode(errors="replace")), j
    if t == 5:
        return ("bool", bool(raw[0])), j
    if t in (6, 7):
        items, k = [], i
        while k < j:
            v, k = de(buf, k)
            items.append(v)
        return ("seq", items), j
    return ("?", raw.hex()), j


def flat_uuids(v, out):
    if v and v[0] == "uuid":
        out.append(v[1])
    elif v and v[0] == "seq":
        for x in v[1]:
            flat_uuids(x, out)


def rfcomm_channel(pdl):
    for proto in (pdl or ("seq", []))[1]:
        if proto[0] == "seq" and proto[1] and proto[1][0] == ("uuid", f"00000003{BASE}") and len(proto[1]) > 1:
            return proto[1][1][1]
    return None


def query(addr):
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
    s.settimeout(10)
    s.connect((addr, 1))
    pattern = bytes([0x35, 0x03, 0x19, 0x01, 0x00])                 # UUID 0x0100 (L2CAP): every record
    attrs = bytes([0x35, 0x05, 0x0A, 0x00, 0x00, 0xFF, 0xFF])        # attribute range 0x0000-0xFFFF
    cont, data, tid = b"\x00", b"", 1
    while True:
        params = pattern + struct.pack(">H", 0xFFFF) + attrs + cont
        s.send(struct.pack(">BHH", 0x06, tid, len(params)) + params)
        r = s.recv(4096)
        if r[0] != 0x07:
            raise SystemExit(f"SDP error PDU {r[0]:#x}: {r.hex()}")
        n = struct.unpack(">H", r[5:7])[0]
        data += r[7:7 + n]
        cl = r[7 + n]
        cont = r[7 + n:8 + n + cl]
        tid += 1
        if cl == 0:
            break
    s.close()
    lists, _ = de(data, 0)
    for rec in lists[1]:
        items = rec[1]
        a = {items[k][1]: items[k + 1] for k in range(0, len(items) - 1, 2)}
        classes = []
        flat_uuids(a.get(0x0001), classes)
        name = a.get(0x0100, ("str", ""))[1]
        print(f"name={name!r:20} rfcomm={rfcomm_channel(a.get(0x0004))!s:5} classes={classes}")


query(sys.argv[1])
