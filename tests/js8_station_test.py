"""JS8 on a running station: station.py with a js8 block, a simulated radio
and a fake jf8call engine; the test is a page.

  - hello carries js8: loaded, and the radio's engine connected
  - a frame the engine decodes reaches the page as {"t": "js8"}
  - "state" answers the page with js8_state (frames and heard)
  - a page asking to send is refused with a notice; the station stays up
  - the station sent the engine status.get and nothing else
  - a js8 block naming a radio the station lacks: the station runs on, and
    hello says js8 is not loaded, and why

    python3 tests/js8_station_test.py
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

import testkit  # noqa: E402

fails = 0
LOGS = []


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


class FakeEngine:
    def __init__(self):
        self.cmds, self.clients = [], set()
        self.status = {"radio_connected": True, "radio_freq_khz": 14078.0, "radio_mode": "USB",
                       "tx_enabled": False, "auto_reply": False, "heartbeat_enabled": False}

    async def ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.clients.add(ws)
        async for msg in ws:
            m = json.loads(msg.data)
            self.cmds.append(m.get("cmd"))
            if m.get("cmd") == "status.get":
                await ws.send_json({"type": "reply", "id": m.get("id"), "ok": True, "data": self.status})
        self.clients.discard(ws)
        return ws

    async def push(self, obj):
        for ws in list(self.clients):
            await ws.send_json(obj)


async def serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    port = testkit.free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, port


def start(js8_block):
    tmp = Path(tempfile.mkdtemp(prefix="js8-station-"))
    web_p, radio_web, rig = testkit.free_port(), testkit.free_port(), testkit.free_port()
    radio = subprocess.Popen([sys.executable, "server.py", "--sim", "--rig", "ic7300", "--no-audio", "--no-tx",
                              "--host", "127.0.0.1", "--listen", str(radio_web),
                              "--rigctl-host", "127.0.0.1", "--rigctl-port", str(rig)],
                             cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cfg = {"name": "test", "radios": [{"id": "ic7300", "name": "IC-7300", "web": radio_web, "rigctl": rig}],
           "addons_state": str(tmp / "addons.json"), "js8": js8_block}
    (tmp / "station.json").write_text(json.dumps(cfg))
    st = subprocess.Popen([sys.executable, "station.py", "--config", str(tmp / "station.json"), "--host", "127.0.0.1",
                           "--listen", str(web_p), "--rigctl-port", "0"],
                          cwd=ROOT, stdout=open(tmp / "station.log", "w"), stderr=subprocess.STDOUT)
    LOGS.append(tmp / "station.log")
    return web_p, radio, st


def stop(*procs):
    for p in procs:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


async def page_until(ws, pred, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            m = json.loads((await ws.receive(timeout=max(0.1, end - time.monotonic()))).data)
        except (asyncio.TimeoutError, TypeError, ValueError):
            continue
        if pred(m):
            return m
    return None


async def up(web_p):
    end = time.monotonic() + 20
    while time.monotonic() < end:
        try:
            async with aiohttp.ClientSession() as s, s.get(f"http://127.0.0.1:{web_p}/") as r:
                if r.status == 200:
                    return True
        except aiohttp.ClientError:
            pass
        await asyncio.sleep(0.2)
    return False


async def main():
    eng = FakeEngine()
    app = web.Application()
    app.router.add_get("/", eng.ws)
    runner, eport = await serve(app)

    web_p, radio, st = start({"radios": {"ic7300": eport}})
    try:
        check("station up", await up(web_p), True)
        ok = await asyncio.get_running_loop().run_in_executor(None, lambda: testkit.wait_for(lambda: eng.clients, 10))
        check("the station connects to the engine", bool(ok), True)
        async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{web_p}/ws") as ws:
            hello = json.loads((await ws.receive(timeout=10)).data)
            j = hello.get("js8") or {}
            check("hello: js8 loaded", j.get("loaded"), True)
            check("hello: the engine connected", ((j.get("radios") or {}).get("ic7300") or {}).get("connected"), True)

            await eng.push({"type": "event", "event": "message.decoded",
                            "data": {"utc_iso": "2026-11-21T16:35:00Z", "snr_db": -9, "freq_hz": 1993.0,
                                     "from": "N9ABC", "to": "@HB", "raw": "N9ABC: @HB HEARTBEAT FN31"}})
            m = await page_until(ws, lambda m: m.get("t") == "js8")
            check("a decode reaches the page", (m or {}).get("frame", {}).get("from"), "N9ABC")

            await ws.send_str(json.dumps({"t": "js8", "do": "state", "radio": "ic7300"}))
            m = await page_until(ws, lambda m: m.get("t") == "js8_state")
            check("state: frames and heard", ((m or {}).get("frames", [{}])[-1].get("from"),
                                              [h["call"] for h in (m or {}).get("heard", [])]), ("N9ABC", ["N9ABC"]))

            await ws.send_str(json.dumps({"t": "js8", "do": "send", "radio": "ic7300", "text": "CQ CQ"}))
            m = await page_until(ws, lambda m: m.get("t") == "notice")
            check("send refused with a notice", "receive-only" in (m or {}).get("msg", ""), True)
        check("the station is still up", await up(web_p), True)
        check("the station sent the engine status.get only", set(eng.cmds), {"status.get"})
    finally:
        stop(st, radio)

    web_p, radio, st = start({"radios": {"g90": eport}})
    try:
        check("a broken js8 block: the station runs on", await up(web_p), True)
        async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{web_p}/ws") as ws:
            j = json.loads((await ws.receive(timeout=10)).data).get("js8") or {}
            check("a broken js8 block: not loaded, says why", (j.get("loaded"), "g90" in (j.get("error") or "")),
                  (False, True))
    finally:
        stop(st, radio)
    await runner.cleanup()


asyncio.run(main())
if fails:
    for p in LOGS:
        print(f"--- {p}\n" + p.read_text()[-2000:])
print(f"\n{fails} failure(s)" if fails else "\nall ok")
sys.exit(1 if fails else 0)
