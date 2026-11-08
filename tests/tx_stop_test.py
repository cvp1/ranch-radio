"""STOP TX and the auto-tune loop, against a simulated rig (--sim, never hardware).

Regression: an IC-7300 re-tuned 20 m 15 times in 34 s, each tune's frequency
report reading as a band change, and nothing on the page could stop it.
  1  loop: the frequency flaps off-band and back during every (fast) tune:
     one tune only
  2  repeat cap: the same flap after each tune: at most two tunes, then
     auto-tune stops for the run, saying so
  3  STOP TX: releases an app's key and refuses every way to transmit
     (rigctl T 1 / G TUNE, page PTT and tune, auto-tune); latched across a
     reload; Resume lets keying back in, re-arming nothing
  4  STOP TX during an ATU tune ends the tune at once

    .venv/bin/python tests/tx_stop_test.py
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import testkit  # noqa: E402

fails = 0
OFF_BAND, BACK = 10_000_000, 14_098_600      # WWV, then the 20 m VARA gateway


def check(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} {detail}")


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


class Page:
    """A page's control socket: every message kept, with when it came."""

    def __init__(self, ws):
        self.ws, self.msgs, self.hello = ws, [], None
        self.task = asyncio.ensure_future(self._read())

    async def _read(self):
        async for m in self.ws:
            if m.type != aiohttp.WSMsgType.TEXT:
                continue
            d = json.loads(m.data)
            if d.get("t") == "hello":
                self.hello = d
            self.msgs.append((time.monotonic(), d))

    async def send(self, m):
        await self.ws.send_str(json.dumps(m))

    def tunes(self):
        return [(t, d["msg"]) for t, d in self.msgs if d.get("t") == "notice" and "ATU tuning" in d.get("msg", "")]

    def notices(self, text):
        return [d["msg"] for _, d in self.msgs if d.get("t") == "notice" and text in d.get("msg", "")]

    def state(self, key):
        """The last value of `key` the server sent this page."""
        v = None
        for _, d in self.msgs:
            if d.get("t") == "hello":
                v = d["state"].get(key, v)
            elif d.get("t") == "state" and key in d["d"]:
                v = d["d"][key]
        return v

    async def wait(self, pred, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            await asyncio.sleep(0.05)
        return False


async def run(scenario, extra=("--auto-tune",)):
    port, web = testkit.free_port(), testkit.free_port()
    env = dict(os.environ)
    env.pop("CC_OBS_TOKENS_FILE", None)
    proc = subprocess.Popen([sys.executable, "server.py", "--sim", "--rig", "ic7300", "--no-audio",
                             "--listen", str(web), "--rigctl-port", str(port), *extra],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(80):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                await asyncio.sleep(0.1)
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(f"http://127.0.0.1:{web}/ws") as ws:
                page = Page(ws)
                rig = Rig(port)
                await asyncio.sleep(3.5)              # past the post-connect arming window
                await scenario(rig, page, lambda: s.ws_connect(f"http://127.0.0.1:{web}/ws"))
                rig.close()
                page.task.cancel()
    finally:
        proc.terminate()
        out = proc.communicate(timeout=10)[0]
    check("no server traceback", "Traceback" not in out, out[-600:] if "Traceback" in out else "")
    return out


async def flap(page):
    """The radio's frequency reads off-band and back: a band change and a return."""
    await page.send({"t": "sim_panel", "d": {"freq": OFF_BAND}})
    await asyncio.sleep(0.15)
    await page.send({"t": "sim_panel", "d": {"freq": BACK}})


async def main():
    # 1 -- the loop: a flap during every tune
    async def loop(rig, page, _new):
        rig.cmd(f"F {BACK}")                          # 20 m -> 20 m: no tune yet
        await page.send({"t": "sim_panel", "d": {"freq": 7_074_000}})
        await asyncio.sleep(0.2)
        await page.send({"t": "sim_panel", "d": {"freq": BACK}})   # a real band change back to 20 m
        seen, end = 0, time.monotonic() + 14
        while time.monotonic() < end:
            n = len(page.tunes())
            if n > seen:                              # a tune just started: the radio's report flaps
                seen = n
                await asyncio.sleep(0.2)
                await flap(page)
                await asyncio.sleep(0.3)              # an ATU that already matches ends fast
                await page.send({"t": "sim_panel", "d": {"tuner": 1}})
            await asyncio.sleep(0.05)
        msgs = [m for _, m in page.tunes()]
        check("1 loop: a flap during each tune starts no new tune", len(msgs) == 1, str(msgs))
    out = await run(loop)
    check("1 loop: the server logged exactly one auto-tune", out.count("auto-tune: ATU tuning") == 1,
          str(out.count("auto-tune: ATU tuning")))

    # 2 -- the repeat cap: the flap after each tune is over
    async def cap(rig, page, _new):
        await page.send({"t": "sim_panel", "d": {"freq": 7_074_000}})
        await asyncio.sleep(0.2)
        await page.send({"t": "sim_panel", "d": {"freq": BACK}})
        seen, end = 0, time.monotonic() + 24
        while time.monotonic() < end:
            n = len(page.tunes())
            if n > seen:
                seen = n
                await page.wait(lambda: page.state("tuner") != 2, 5)
                await asyncio.sleep(1.6)              # past the quiet window
                await flap(page)
            await asyncio.sleep(0.05)
        msgs = [m for _, m in page.tunes()]
        check("2 cap: 20 m auto-tuned at most twice", 1 <= len(msgs) <= 2, str(msgs))
        check("2 cap: the page is told auto-tune stopped", bool(page.notices("Auto-tune stopped")),
              str(page.notices("Auto-tune")))
        cfg = [d["config"] for _, d in page.msgs if d.get("t") == "config"]
        check("2 cap: config now says auto_tune off", bool(cfg) and cfg[-1]["auto_tune"] is False)
    out = await run(cap)
    check("2 cap: a WARNING in the log", "auto-tune OFF for this run" in out)

    # 3 -- STOP TX latches every way to transmit
    async def stop(rig, page, new):
        check("3 before: rigctl T 1 keys", rig.cmd("T 1") == "RPRT 0")
        await page.wait(lambda: page.state("tx") is True, 2)
        await page.send({"t": "tx_stop"})
        ok = await page.wait(lambda: any(d.get("t") == "tx_stop" and d.get("stopped") for _, d in page.msgs), 3)
        check("3 STOP: broadcast to the page, latched", ok)
        check("3 STOP: the radio is unkeyed", await page.wait(lambda: page.state("tx") is False, 2))
        check("3 STOP: rigctl reads RX", rig.cmd("t") == "0")
        check("3 STOP: rigctl T 1 refused", rig.cmd("T 1") == "RPRT -11")
        check("3 STOP: rigctl G TUNE refused", rig.cmd("G TUNE") == "RPRT -11")
        page.msgs.clear()
        await page.send({"t": "set", "k": "tx", "v": True, "mic": False})
        await page.send({"t": "set", "k": "tuner", "v": 2})
        await asyncio.sleep(0.6)
        check("3 STOP: page PTT refused, saying why", len(page.notices("STOP TX")) >= 2, str(page.notices("")))
        check("3 STOP: radio never keyed or tuned", page.state("tx") in (None, False) and page.state("tuner") != 2)
        check("3 STOP: a band change is accepted (receive)", rig.cmd("F 7074000") == "RPRT 0")
        await asyncio.sleep(3.0)
        check("3 STOP: ...but auto-tune does not tune it", not page.tunes(), str(page.tunes()))
        async with new() as ws2:                      # a reload, or another page
            p2 = Page(ws2)
            await p2.wait(lambda: p2.hello is not None, 3)
            st = (p2.hello or {}).get("config", {}).get("tx_stopped")
            check("3 STOP: a new page sees it latched (survives reload)", bool(st and st.get("at")), str(st))
            p2.task.cancel()
        await page.send({"t": "tx_resume"})
        ok = await page.wait(lambda: any(d.get("t") == "tx_stop" and d.get("stopped") is None
                                         for _, d in page.msgs), 3)
        check("3 Resume: broadcast, unlatched", ok)
        check("3 Resume: rigctl T 1 keys again", rig.cmd("T 1") == "RPRT 0")
        rig.cmd("T 0")
    out = await run(stop)
    check("3 STOP: WARNINGs name STOP and Resume", "STOP TX by" in out and "STOP TX cleared" in out)

    # 4 -- STOP during a tune ends it
    async def stop_tune(rig, page, _new):
        check("4 G TUNE accepted", rig.cmd("G TUNE") == "RPRT 0")
        await page.wait(lambda: page.state("tuner") == 2, 2)
        t0 = time.monotonic()
        await page.send({"t": "tx_stop"})
        ended = await page.wait(lambda: page.state("tuner") != 2, 2)
        check("4 STOP during a tune: the tune ends at once", ended and time.monotonic() - t0 < 1.0,
              f"{time.monotonic() - t0:.2f}s")
    await run(stop_tune, extra=())

    print("\nPASS" if not fails else f"\n{fails} FAILED")
    sys.exit(1 if fails else 0)


asyncio.run(main())
