"""Auto-tune-on-band-change test, against a simulated rig (never hardware).

    .venv/bin/python tests/autotune_test.py [rig]
"""
import asyncio
import json
import os
import random
import socket
import ssl
import subprocess
import sys
import time

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RIG = sys.argv[1] if len(sys.argv) > 1 else "g90"
fails = 0


def check(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:52} {detail}")


class Rig:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.f = self.s.makefile("r")

    def cmd(self, line):
        self.s.sendall((line + "\n").encode())
        return self.f.readline().strip()

    def close(self):
        self.f.close()
        self.s.close()


async def run(extra, scenario, early=False):
    port, web = 15000 + random.randrange(1000), 18000 + random.randrange(1000)
    proc = subprocess.Popen([sys.executable, "server.py", "--sim", "--rig", RIG, "--no-audio",
                             "--listen", str(web), "--rigctl-port", str(port)] + extra,
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tunes = []          # (time, band notice) from the server's notices
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                await asyncio.sleep(0.1)
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(f"http://127.0.0.1:{web}/ws") as ws:
                async def watch():
                    async for m in ws:
                        if m.type != aiohttp.WSMsgType.TEXT:
                            continue            # IC-7300 scope frames are binary
                        d = json.loads(m.data)
                        if d.get("t") == "notice" and "ATU tuning" in d.get("msg", ""):
                            tunes.append((time.monotonic(), d["msg"]))
                w = asyncio.ensure_future(watch())
                rig = Rig(port)
                if not early:
                    await asyncio.sleep(3.5)          # past the post-connect arming window
                await scenario(rig, tunes)
                rig.close()
                w.cancel()
    finally:
        proc.terminate()
        out = proc.communicate(timeout=5)[0]
    if "Traceback" in out:
        check("no server traceback", False, out[-400:])
    return out


async def main():
    async def basic(rig, tunes):
        check("startup band is not tuned", not tunes, str(tunes))
        rig.cmd("F 7074000")
        await asyncio.sleep(1.0)
        check("no tune before the 2 s settle", not tunes)
        await asyncio.sleep(2.0)
        check("band change 20 -> 40 m tunes once", [m for _, m in tunes] == ["ATU tuning for 40 m (band change)"], str(tunes))
        rig.cmd("F 7200000")
        await asyncio.sleep(3)
        check("move within 40 m: no retune", len(tunes) == 1, str(len(tunes)))
        rig.cmd("F 21074000")
        await asyncio.sleep(0.8)
        rig.cmd("F 28074000")                 # quick hop: only the band it lands on
        await asyncio.sleep(5)                # settle 2 s + sim tune 2.5 s: a retune
                                              # during the tune is refused (F3)
        check("quick hop 15 -> 10 m tunes only 10 m", tunes[-1][1].startswith("ATU tuning for 10 m") and len(tunes) == 2, str([m for _, m in tunes]))
        rig.cmd("F 7074000")
        await asyncio.sleep(5)
        check("back to 40 m (tuned before): retunes", len(tunes) == 3 and tunes[-1][1].startswith("ATU tuning for 40 m"), str([m for _, m in tunes]))
        rig.cmd("F 10000000")                 # WWV: outside the ham bands
        await asyncio.sleep(3)
        check("off-band frequency: no tune", len(tunes) == 3, str(len(tunes)))

        rig.cmd("T 1")
        check("band change while keyed: refused (F3)", rig.cmd("F 18100000") == "RPRT -11")
        await asyncio.sleep(3)
        rig.cmd("t")                          # keepalive
        check("no tune while keyed", len(tunes) == 3, str(len(tunes)))
        rig.cmd("T 0")
        check("band change after unkey accepted", rig.cmd("F 18100000") == "RPRT 0")
        await asyncio.sleep(2.5)
        check("after unkey: 17 m tunes", tunes[-1][1].startswith("ATU tuning for 17 m"), str(tunes[-1][1]))

        await asyncio.sleep(2.5)              # let the 17 m tune finish
        rig.cmd("F 24900000")
        await asyncio.sleep(2.2)              # tune starts (~2.0 s); sim tunes for 2.5 s
        check("PTT refused while the ATU tunes", rig.cmd("T 1") == "RPRT -11")
        check("freq change during the tune is queued (F3)", rig.cmd("F 24915000") == "RPRT 0")
        await asyncio.sleep(3)
        check("G TUNE via rigctl accepted", rig.cmd("G TUNE") == "RPRT 0")

    out = await run(["--auto-tune"], basic)
    for line in out.splitlines():
        if "auto-tune" in line:
            print("   log:", line.split(" ", 2)[-1])

    # F11: JTDX reconnects ~0.4 s after a restart, inside the 3 s arming
    # window; its frequency must still get the first-use tune once armed.
    for hz, band in ((14074000, "20 m"), (7074000, "40 m")):
        async def early_app(rig, tunes, hz=hz, band=band):
            t0 = time.monotonic()
            rig.cmd(f"F {hz}")
            await asyncio.sleep(3.0 + 2.0 + 1.5)  # arm + settle + slack
            msgs = [m for _, m in tunes]
            check(f"F {hz} inside the arming window: first-use tune", msgs ==
                  [f"ATU tuning for {band} (first use this run)"], str(msgs))
            if tunes:
                check(f"  ...not before the window ends", tunes[0][0] - t0 >= 2.5,
                      f"{tunes[0][0] - t0:.2f}s")
            await asyncio.sleep(3.0)
            check(f"  ...and only once", len(tunes) == 1, str(len(tunes)))
        await run(["--auto-tune"], early_app, early=True)

    async def no_tx(rig, tunes):
        rig.cmd("F 7074000")
        await asyncio.sleep(3)
        check("--no-tx: band change does not tune", not tunes, str(tunes))
        check("--no-tx: G TUNE refused", rig.cmd("G TUNE") == "RPRT -11")
    out = await run(["--auto-tune", "--no-tx"], no_tx)
    check("--no-tx: --auto-tune reported as ignored", "--auto-tune ignored" in out)

    async def off(rig, tunes):
        rig.cmd("F 7074000")
        await asyncio.sleep(3)
        check("without --auto-tune: no tune", not tunes, str(tunes))
    await run([], off)

    print("\nPASS" if not fails else f"\n{fails} FAILED")
    sys.exit(1 if fails else 0)


asyncio.run(main())
