"""Watch a running server during a supervised transmit test.

Logs every key/unkey with duration, the highest SWR meter reading while
keyed, ATU tuning state, frequency changes, and server notices (time-limit,
keepalive and auto-tune). Read-only: it never sends anything to the radio.

    python3 tests/txmon.py [seconds] [url]
"""
import asyncio
import json
import ssl
import sys
import time

import aiohttp

TOTAL = float(sys.argv[1]) if len(sys.argv) > 1 else 1200
URL = sys.argv[2] if len(sys.argv) > 2 else "https://localhost:7300/ws"


def stamp():
    return time.strftime("%H:%M:%S")


async def main():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    t0 = time.time()
    keyed_at, swr_max = None, 0
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(URL, ssl=ctx) as ws:
            st = json.loads((await ws.receive()).data)["state"]
            print(stamp(), "monitor up:", {k: st.get(k) for k in ("freq", "mode", "data", "tx", "swr", "tuner")}, flush=True)
            while time.time() - t0 < TOTAL:
                try:
                    m = await asyncio.wait_for(ws.receive(), 2)
                except asyncio.TimeoutError:
                    continue
                if m.type != aiohttp.WSMsgType.TEXT:
                    if m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        print(stamp(), "server connection closed", flush=True)
                        return
                    continue
                d = json.loads(m.data)
                if d.get("t") == "notice":
                    print(stamp(), "NOTICE:", d.get("msg"), flush=True)
                if d.get("t") != "state":
                    continue
                diff = d["d"]
                st.update(diff)
                if "swr" in diff and st.get("tx"):
                    swr_max = max(swr_max, diff["swr"] or 0)
                if "tx" in diff:
                    if diff["tx"]:
                        keyed_at, swr_max = time.time(), 0
                        mode = st["mode"] + ("-D" if st.get("data") else "")
                        print(stamp(), f"TX ON   {st['freq'] / 1e6:.6f} MHz {mode}", flush=True)
                    elif keyed_at:
                        print(stamp(), f"TX OFF  after {time.time() - keyed_at:.1f} s, max SWR meter (raw) {swr_max}", flush=True)
                        keyed_at = None
                if "tuner" in diff:
                    label = {0: "off", 1: "on (idle)", 2: "TUNING"}.get(diff["tuner"], diff["tuner"])
                    print(stamp(), f"ATU     {label}", flush=True)
                if "freq" in diff and not st.get("tx"):
                    print(stamp(), f"freq    {st['freq'] / 1e6:.6f} MHz", flush=True)


asyncio.run(main())
