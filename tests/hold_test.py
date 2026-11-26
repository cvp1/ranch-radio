"""Hold + tune-on-first-use test, against a simulated rig (never hardware).

While a rigctl app (JTDX) is connected, a change made at the radio's front
panel (band button, front-panel tune, dial) is undone once the radio is
receiving; changes made by an app (rigctl or browser) move the hold instead. Only a
rigctl app that SET something pins the radio: one that only listens (the JS8
engine) does not, a browser change alone never seeds it, the page's HELD
badge releases it, and it goes when the app that set it leaves.
The first frequency an app sets on a band not tuned this run tunes the ATU.

    .venv/bin/python tests/hold_test.py [rig]
"""
import asyncio
import json
import os
import socket
import subprocess
import sys

import aiohttp
import testkit  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RIG = sys.argv[1] if len(sys.argv) > 1 else "g90"
fails = 0


def check(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} {detail}")


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


async def main():
    port, web = testkit.free_port(), testkit.free_port()
    proc = subprocess.Popen([sys.executable, "server.py", "--sim", "--rig", RIG, "--no-audio",
                             "--listen", str(web), "--rigctl-port", str(port),
                             "--auto-tune", "--tx-timeout", "30"],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    state, notices, holds = {}, [], []
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
                            continue
                        d = json.loads(m.data)
                        if d.get("t") == "hello":
                            state.update(d["state"])
                        elif d.get("t") == "state":
                            state.update(d["d"])
                        elif d.get("t") == "notice":
                            notices.append(d["msg"])
                        elif d.get("t") == "hold":
                            holds.append(d["on"])
                w = asyncio.ensure_future(watch())

                async def panel(**d):
                    await ws.send_str(json.dumps({"t": "sim_panel", "d": d}))

                async def browser(k, v):
                    await ws.send_str(json.dumps({"t": "set", "k": k, "v": v}))

                def held():
                    return holds[-1] if holds else None

                def tunes():
                    return [n for n in notices if n.startswith("ATU tuning")]

                await asyncio.sleep(3.5)                 # past the post-connect arming window
                check("simulator starts on 20 m", 14_000_000 <= state["freq"] <= 14_350_000, str(state["freq"]))
                await panel(mode="USB", data=True)
                await asyncio.sleep(0.3)

                # Front-panel change with no app connected: left alone.
                await panel(freq=14326000)
                await asyncio.sleep(2.5)
                check("no app connected: front-panel change left alone", state["freq"] == 14326000,
                      str(state["freq"]))
                check("no app connected: no tune", not tunes(), str(tunes()))

                rig = Rig(port)
                check("app sets 20 m", rig.cmd("F VFOA 14074000") == "RPRT 0")
                await asyncio.sleep(3.0)
                check("first app frequency on an untuned band tunes the ATU",
                      tunes() == ["ATU tuning for 20 m (first use this run)"], str(tunes()))
                await asyncio.sleep(3.0)                 # simulated tune runs 2.5 s
                rig.cmd("F VFOA 14075000")
                await asyncio.sleep(2.6)
                check("same band again: no second tune", len(tunes()) == 1, str(tunes()))
                rig.cmd("F VFOA 14074000")
                await asyncio.sleep(0.3)

                # G90 band button: band memory, plain USB.
                await panel(freq=14326000, data=False)
                await asyncio.sleep(0.5)
                check("band button: not undone before the settle", state["freq"] == 14326000)
                await asyncio.sleep(2.0)
                check("band button: frequency restored", state["freq"] == 14074000, str(state["freq"]))
                check("band button: DATA restored", state["data"] is True, str(state["data"]))
                check("browser told why", any("front panel" in n for n in notices), str(notices[-1:]))

                # Front-panel tune: transmitting, then dropped DATA and moved.
                await panel(tx=True, freq=14200000, data=False)
                await asyncio.sleep(2.5)
                check("front-panel tune: nothing restored while transmitting",
                      state["freq"] == 14200000 and state["tx"] is True, str(state["freq"]))
                await panel(tx=False)
                await asyncio.sleep(2.5)
                check("front-panel tune: restored after it ends",
                      (state["freq"], state["data"]) == (14074000, True), str((state["freq"], state["data"])))

                # An app move is the new hold, not a drift.
                await browser("freq", 14080000)
                await asyncio.sleep(2.5)
                check("browser move: kept (hold follows the apps)", state["freq"] == 14080000, str(state["freq"]))
                rig.cmd("F VFOA 14074000")
                await asyncio.sleep(0.3)

                # Split hop while keyed is ours: never fought.
                rig.cmd("S VFOA 1 VFOB")
                rig.cmd("I VFOA 14075000")
                check("split key", rig.cmd("T VFOA 1") == "RPRT 0")
                await asyncio.sleep(2.5)
                if RIG == "g90":     # emulated: the dial hops to TX while keyed
                    check("split hop held while keyed", state["freq"] == 14075000, str(state["freq"]))
                else:                # real split: VFO B holds TX, VFO A stays put
                    check("real split: VFO A held, B on TX", (state["freq"], state.get("freq_b")) == (14074000, 14075000),
                          str((state["freq"], state.get("freq_b"))))
                rig.cmd("T VFOA 0")
                await asyncio.sleep(2.5)
                check("split: back on RX, no restore fight", state["freq"] == 14074000, str(state["freq"]))
                rig.cmd("S VFOA 0 VFOA")

                # Bounded: a radio that keeps moving is left alone after a few tries.
                before = sum("restored" in n for n in notices)
                for f in (14100000, 14110000, 14120000, 14130000):
                    await panel(freq=f)
                    await asyncio.sleep(2.2)
                restored = sum("restored" in n for n in notices) - before
                gave_up = sum("keeps moving" in n for n in notices)
                check("restore fight is bounded, warned once", restored <= 2 and gave_up == 1,
                      f"restored {restored}, warnings {gave_up}")

                # App gone: hold released.
                rig.close()
                await asyncio.sleep(0.5)
                await panel(freq=7074000)
                await asyncio.sleep(2.5)
                check("app disconnected: hold released", state["freq"] == 7074000, str(state["freq"]))
                check("app disconnected: HELD cleared on the page", held() is False, str(holds[-3:]))
                n_tunes = len(tunes())
                await panel(freq=14074000, data=True)     # back on 20 m: its band-change tune
                for _ in range(100):                      # must end, or the browser set is refused
                    await asyncio.sleep(0.1)
                    if len(tunes()) > n_tunes and state.get("tuner") != 2:
                        break
                await asyncio.sleep(0.5)

                # A listening-only app (the JS8 engine: polls, never sets) plus a
                # browser change: nothing pins the radio (2026-11-26 regression:
                # the knob snapped back to the page's last frequency).
                listener = Rig(port)
                check("listener polls", listener.cmd("f").isdigit())
                await browser("freq", 14228000)
                await asyncio.sleep(0.5)
                check("browser set applied", state["freq"] == 14228000, str(state["freq"]))
                n = len(holds)
                await panel(freq=14255000, data=False)
                await asyncio.sleep(6.0)       # past any band-change tune (2.5 s) + the settle
                check("listener only + browser set: knob change left alone",
                      (state["freq"], state["data"]) == (14255000, False), str((state["freq"], state["data"])))
                check("listener only: page never shows HELD", True not in holds[n:], str(holds[n:]))

                # An app that sets: held. Release from the page: left alone.
                rig = Rig(port)
                rig.cmd("F VFOA 14074000")
                await asyncio.sleep(3.5)       # a band-change tune may be running: applied when it ends
                check("setting app: page shows HELD", held() is True, str(holds[-3:]))
                await ws.send_str(json.dumps({"t": "hold_release"}))
                await asyncio.sleep(0.3)
                check("release: HELD cleared on the page", held() is False, str(holds[-3:]))
                await panel(freq=14140000)
                await asyncio.sleep(6.0)       # past any band-change tune (2.5 s) + the settle
                check("released: knob change left alone", state["freq"] == 14140000, str(state["freq"]))

                # The app sets again: held again (a release is not forever).
                rig.cmd("F VFOA 14074000")
                await asyncio.sleep(0.3)
                check("app sets again: HELD again", held() is True, str(holds[-3:]))
                await panel(freq=14150000)
                await asyncio.sleep(2.5)
                check("held again: knob change restored", state["freq"] == 14074000, str(state["freq"]))

                # The setting app leaves; the listener stays: the hold goes with the app.
                rig.close()
                await asyncio.sleep(0.5)
                check("setting app left, listener on: HELD cleared", held() is False, str(holds[-3:]))
                await panel(freq=14255000)
                await asyncio.sleep(6.0)       # past any band-change tune (2.5 s) + the settle
                check("setting app left, listener on: knob left alone",
                      state["freq"] == 14255000, str(state["freq"]))
                listener.close()
                w.cancel()
    finally:
        proc.terminate()
        out = proc.communicate(timeout=5)[0]
    if "Traceback" in out:
        check("no server traceback", False, out[-600:])
    for line in out.splitlines():
        if "hold:" in line or "auto-tune" in line or "refused" in line:
            print("   log:", line.split(" ", 2)[-1])
    print("PASS" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)


asyncio.run(main())
