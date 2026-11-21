"""The station's JS8 bridge (js8_bridge.py) against a fake jf8call engine.

  - check_block: no radios, a non-port refused; a radio the station lacks refused
  - connect: a js8_status push, connected, nothing unsafe while disarmed
  - frames: each frame pushed to pages; the sender lands in heard (from a
    message's "from", or a frame's "CALL:" prefix), a message's grid kept
  - bounds: at most MAX_FRAMES frames, text capped at MAX_TEXT; keys the
    engine adds that are not on the allowlist never reach a page
  - receive only: across everything here the bridge sent status.get and nothing else
  - unsafe: an engine reporting TX or auto-reply on is shown unsafe, pushed once
  - pages: "state" answers that page only; send, arm or an unknown radio refused
  - malformed engine messages (not JSON, a list, data not an object) don't stop it
  - the engine restarts: the bridge says disconnected, then reconnects by itself

    python3 tests/js8_bridge_test.py
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from aiohttp import web  # noqa: E402

import js8_bridge  # noqa: E402
import testkit  # noqa: E402

js8_bridge.BACKOFF_S = (0.2,)
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


async def until(pred, timeout=4.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return bool(pred())


class FakeEngine:
    """jf8call's WebSocket, as much of it as the bridge uses."""

    def __init__(self, port):
        self.port, self.cmds, self.clients, self.runner = port, [], set(), None
        self.status = {"radio_connected": True, "radio_freq_khz": 14078.0, "radio_mode": "USB",
                       "submode_name": "Normal", "callsign": "N0CALL", "grid": "FN31", "transmitting": False,
                       "tx_enabled": False, "auto_reply": False, "heartbeat_enabled": False,
                       "heartbeat_ack_enabled": False, "ws_port": 2102}

    async def ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.clients.add(ws)
        await ws.send_json({"type": "event", "event": "status", "data": self.status})
        await ws.send_json({"type": "hello", "data": {"message": "JF8Call WebSocket API"}})
        async for msg in ws:
            m = json.loads(msg.data)
            self.cmds.append(m.get("cmd"))
            if m.get("cmd") == "status.get":
                await ws.send_json({"type": "reply", "id": m.get("id"), "ok": True, "data": self.status})
        self.clients.discard(ws)
        return ws

    async def push(self, obj):
        for ws in list(self.clients):
            await (ws.send_str(obj) if isinstance(obj, str) else ws.send_json(obj))

    async def start(self):
        app = web.Application()
        app.router.add_get("/", self.ws)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()

    async def stop(self):
        for ws in list(self.clients):
            await ws.close()
        await self.runner.cleanup()


class Radio:
    def __init__(self, rid):
        self.id = rid


class FakeStation:
    def __init__(self):
        self.radios = [Radio("ic7300")]
        self.pushed, self.sent = [], []

    def _broadcast(self, msg):
        self.pushed.append(json.loads(msg))

    async def _send(self, ws, msg):
        self.sent.append((ws, json.loads(msg)))


def refuses(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


async def main():
    # ------------------------------------------------------------ the block
    st = FakeStation()
    check("no radios refused", refuses(js8_bridge.check_block, {"radios": {}}), True)
    check("a non-port refused", refuses(js8_bridge.check_block, {"radios": {"ic7300": "2102"}}), True)
    check("a radio the station lacks refused", refuses(js8_bridge.Js8Bridge, {"radios": {"g90": 4600}}, st), True)

    port = testkit.free_port()
    eng = FakeEngine(port)
    await eng.start()
    br = js8_bridge.Js8Bridge({"radios": {"ic7300": port}}, st)
    await br.start()
    e = br.engines["ic7300"]

    # ------------------------------------------------------------ connect
    check("connects", await until(lambda: e.connected and e.status.get("radio_freq_khz") == 14078.0), True)
    statuses = [m for m in st.pushed if m["t"] == "js8_status"]
    check("pushes a js8_status", bool(statuses) and statuses[-1]["status"]["connected"], True)
    check("disarmed: nothing unsafe", e.view()["unsafe"], [])
    check("status keys are the allowlist", "ws_port" in e.view(), False)

    # ------------------------------------------------------------ frames
    await eng.push({"type": "event", "event": "message.frame",
                    "data": {"utc_iso": "2026-11-21T16:34:30Z", "submode_name": "Normal", "snr_db": 7,
                             "freq_hz": 800.0, "dial_freq_khz": 14078.0, "frame_text": "W0DRH: AF7NX  ",
                             "secret": "never to a page"}})
    await eng.push({"type": "event", "event": "message.decoded",
                    "data": {"utc_iso": "2026-11-21T16:35:00Z", "submode_name": "Normal", "snr_db": -9,
                             "freq_hz": 1993.0, "from": "KF0LSL", "to": "@HB", "raw": "KF0LSL: @HB HEARTBEAT EM12",
                             "type_name": "Heartbeat", "grid": "EM12"}})
    check("frames reach the pages", await until(lambda: len([m for m in st.pushed if m["t"] == "js8"]) == 2), True)
    f0 = [m for m in st.pushed if m["t"] == "js8"][0]["frame"]
    check("a frame's sender from its CALL: prefix", "W0DRH" in e.heard, True)
    check("a message's sender and grid", (e.heard.get("KF0LSL") or {}).get("grid"), "EM12")
    check("keys off the allowlist never reach a page", "secret" in json.dumps(st.pushed), False)
    check("a frame as the page sees it", (f0["kind"], f0["snr"], f0["freq_hz"], f0["text"]),
          ("frame", 7, 800.0, "W0DRH: AF7NX  "))

    # ------------------------------------------------------------ bounds
    for i in range(js8_bridge.MAX_FRAMES + 10):
        await eng.push({"type": "event", "event": "message.frame",
                        "data": {"frame_text": f"N{i % 10}ABC: " + "X" * 500, "snr_db": "loud", "freq_hz": i}})
    check("frames capped", await until(lambda: len(e.frames) == js8_bridge.MAX_FRAMES
                                       and e.frames[-1]["freq_hz"] == js8_bridge.MAX_FRAMES + 9), True)
    check("text capped", len(e.frames[-1]["text"]), js8_bridge.MAX_TEXT)
    check("a non-number SNR becomes null", e.frames[-1]["snr"], None)

    # ------------------------------------------------------------ malformed
    for junk in ("not json", "[1, 2]", json.dumps({"type": "event", "event": "message.frame", "data": [1]})):
        await eng.push(junk)
    await eng.push({"type": "event", "event": "message.frame", "data": {"frame_text": "K1ABC: AFTER JUNK"}})
    check("malformed messages don't stop it", await until(lambda: "K1ABC" in e.heard), True)

    # ------------------------------------------------------------ unsafe
    n0 = len([m for m in st.pushed if m["t"] == "js8_status"])
    eng.status = dict(eng.status, tx_enabled=True, auto_reply=True)
    await eng.push({"type": "event", "event": "status", "data": eng.status})
    check("TX or auto-reply on: shown unsafe", await until(lambda: e.view()["unsafe"] == ["tx_enabled", "auto_reply"]),
          True)
    unsafe_pushes = [m for m in st.pushed[n0:] if m["t"] == "js8_status" and m["status"]["unsafe"]]
    check("unsafe pushed to pages once", len(unsafe_pushes), 1)
    eng.status = dict(eng.status, tx_enabled=False, auto_reply=False)
    await eng.push({"type": "event", "event": "status", "data": eng.status})
    await until(lambda: not e.view()["unsafe"])

    # ------------------------------------------------------------ pages
    page = object()
    await br.handle(page, {"t": "js8", "do": "state", "radio": "ic7300"})
    check("state answers the asking page", (st.sent[-1][0] is page, st.sent[-1][1]["t"],
                                            len(st.sent[-1][1]["frames"])), (True, "js8_state", js8_bridge.MAX_FRAMES))
    for bad in ({"do": "send", "radio": "ic7300", "text": "CQ"}, {"do": "arm", "radio": "ic7300"},
                {"do": "state", "radio": "g90"}, {"radio": "ic7300"}):
        try:
            await br.handle(page, dict(bad, t="js8"))
            check(f"page {bad} refused", "taken", "refused")
        except ValueError:
            check(f"page {bad} refused", "refused", "refused")

    # ------------------------------------------------------------ reconnect
    await eng.stop()
    check("engine gone: disconnected, pushed", await until(lambda: not e.connected and any(
        m["t"] == "js8_status" and not m["status"]["connected"] for m in st.pushed)), True)
    check("engine gone: says why", bool(e.error), True)
    eng2 = FakeEngine(port)
    await eng2.start()
    check("engine back: reconnects by itself", await until(lambda: e.connected, 6), True)

    # ------------------------------------------------------------ receive only
    sent = set(eng.cmds) | set(eng2.cmds)
    check("the bridge sent status.get and nothing else", sent, {"status.get"})
    check("its own count agrees", set(e.sent), {"status.get"})

    await br.stop()
    await eng2.stop()


asyncio.run(main())
print(f"\n{fails} failure(s)" if fails else "\nall ok")
sys.exit(1 if fails else 0)
