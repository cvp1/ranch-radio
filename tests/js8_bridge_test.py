"""The station's JS8 bridge (js8_bridge.py) against a fake jf8call engine.

  - check_block: no radios, a non-port refused; a radio the station lacks refused
  - connect: a js8_status push, connected, nothing unsafe while disarmed
  - frames: each frame pushed to pages; the sender lands in heard (from a
    message's "from", or a frame's "CALL:" prefix), a message's grid kept
  - bounds: at most MAX_FRAMES frames, text capped at MAX_TEXT; keys the
    engine adds that are not on the allowlist never reach a page
  - unsafe: an engine reporting TX or auto-reply on without an arm is shown
    unsafe, pushed once, and disarmed (tx.halt, then config.set DISARM)
  - pages: "state" answers that page only; an unknown verb or radio refused
  - arm gates: refused, and why, with the radio absent, --no-tx, STOP TX on,
    not connected, or the engine without a callsign; nothing sent then
  - arm: config.set ARM exactly, confirmed by status.get; an engine that
    doesn't confirm is disarmed and the arm refused
  - send: only while armed and only from the arming page; text upper-cased,
    capped, printable ASCII; tx.send carries the text, and an offset
    (OFFSET_HZ) and a named speed (SPEEDS) only when asked and in range
  - hb: tx.hb, only while armed and only from the arming page
  - disarm: by the page, the time running out, the page closing, STOP TX at
    the radio (at once, through radio_changed), the engine going away, the
    station stopping
  - the allowlist: across everything here the bridge sent only status.get,
    config.set (ARM or DISARM exactly), tx.send {text, tx_freq_hz?, submode?},
    tx.hb and tx.halt
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
        self.log, self.obey, self.mute, self.stale_on_halt = [], True, False, False
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
            cmd, data = m.get("cmd"), m.get("data") or {}
            self.cmds.append(cmd)
            self.log.append((cmd, data))
            ok, out, err = True, {}, None
            if cmd == "tx.halt" and self.stale_on_halt:
                await ws.send_json({"type": "event", "event": "status", "data": dict(self.status)})
                await asyncio.sleep(js8_bridge.SAFETY_EVERY_S * 1.5)     # a safety tick lands in the gap
            if cmd == "status.get":
                out = self.status
            elif cmd == "config.set" and self.obey:
                for k, v in data.items():
                    if k in self.KEYS:
                        self.status[self.KEYS[k]] = v
            elif cmd == "tx.hb":
                if not self.status.get("tx_enabled"):
                    ok, err = False, "TX is disarmed — arm TX first"
                else:
                    out = {"queue_size": 1, "freq_hz": 730.0, "all_clear": True}
            elif cmd == "tx.send":
                if not self.status.get("tx_enabled"):
                    ok, err = False, "TX is disabled (txEnabled=false)"
                else:
                    out = {"queue_size": 2}
            if self.mute:
                continue
            await ws.send_json({"type": "reply", "id": m.get("id"), "ok": ok,
                                **({"data": out} if ok else {"error": err})})
        self.clients.discard(ws)
        return ws

    KEYS = {"txEnabled": "tx_enabled", "autoReply": "auto_reply", "heartbeatEnabled": "heartbeat_enabled",
            "heartbeatAckEnabled": "heartbeat_ack_enabled"}

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


class Page:
    closed = False


class Radio:
    def __init__(self, rid):
        self.id = rid


class FakeStation:
    def __init__(self):
        self.radios = [Radio("ic7300")]
        self.pushed, self.sent = [], []
        self.tx = {"present": True, "connected": True, "tx": False, "tx_allowed": True, "tx_stopped": None}

    def radio_tx_state(self, rid):
        return dict(self.tx)

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
    check("on without an arm: the bridge disarms it", await until(
        lambda: ("config.set", js8_bridge.DISARM) in eng.log and "tx.halt" in eng.cmds), True)
    check("halt goes before the disarm", eng.cmds.index("tx.halt") < eng.log.index(("config.set", js8_bridge.DISARM)),
          True)
    await eng.push({"type": "event", "event": "status", "data": eng.status})
    check("then it reads safe", await until(lambda: not e.view()["unsafe"]), True)

    # ------------------------------------------------------------ pages
    page = object()
    await br.handle(page, {"t": "js8", "do": "state", "radio": "ic7300"})
    check("state answers the asking page", (st.sent[-1][0] is page, st.sent[-1][1]["t"],
                                            len(st.sent[-1][1]["frames"])), (True, "js8_state", js8_bridge.MAX_FRAMES))
    for bad in ({"do": "key", "radio": "ic7300"}, {"do": "radio.ptt.set", "radio": "ic7300"},
                {"do": "state", "radio": "g90"}, {"radio": "ic7300"}, {"do": "arm", "radio": "ic7300", "minutes": "10"},
                {"do": "send", "radio": "ic7300", "text": ["CQ"]}):
        try:
            await br.handle(page, dict(bad, t="js8"))
            check(f"page {bad} refused", "taken", "refused")
        except ValueError:
            check(f"page {bad} refused", "refused", "refused")

    # ------------------------------------------------------------ arm gates
    a, other = Page(), Page()

    async def ask(page, **m):
        return await br.handle(page, dict(m, t="js8", radio="ic7300"))

    n_cmds = len(eng.cmds)
    for name, tx in (("radio absent", {"present": False}), ("--no-tx", {"tx_allowed": False}),
                     ("STOP TX on", {"tx_stopped": {"at": 1, "by": "x"}}), ("radio not connected", {"connected": False})):
        st.tx = dict(st.tx, **tx) if tx.get("present") is not False else {"present": False}
        why = await ask(a, do="arm", minutes=10)
        check(f"arm refused: {name}", bool(why) and why.startswith("Can't arm JS8"), True)
        st.tx = {"present": True, "connected": True, "tx": False, "tx_allowed": True, "tx_stopped": None}
    call = e.status.pop("callsign")
    check("arm refused: no callsign", "callsign" in (await ask(a, do="arm") or ""), True)
    e.status["callsign"] = call
    check("refused arms sent the engine nothing", eng.cmds[n_cmds:], [])
    check("send while disarmed refused", await ask(a, do="send", text="CQ"), "JS8 isn't armed on this radio.")
    check("hb while disarmed refused", await ask(a, do="hb"), "JS8 isn't armed on this radio.")
    check("refused hb sent the engine nothing", "tx.hb" in eng.cmds, False)

    # ------------------------------------------------------------ an engine that won't arm
    eng.obey = False
    why = await ask(a, do="arm", minutes=10)
    check("unconfirmed arm refused", bool(why) and "did not confirm" in why, True)
    check("unconfirmed arm: disarmed again, then read back", (e.armed, eng.log[-2:]),
          (None, [("config.set", js8_bridge.DISARM), ("status.get", {})]))
    eng.obey = True

    # ------------------------------------------------------------ arm, send
    check("arm", await ask(a, do="arm", minutes=10), None)
    check("arm: config.set ARM exactly", ("config.set", js8_bridge.ARM) in eng.log, True)
    check("armed: tx_enabled is not unsafe", (e.view()["armed"], e.view()["unsafe"]), (True, []))
    left = e.view()["armed_until"] - time.time()
    check("armed for the minutes asked", 590 < left <= 600, True)
    check("another page can't arm over it", "another page" in (await ask(other, do="arm") or ""), True)
    check("another page can't send", "another page" in (await ask(other, do="send", text="CQ") or ""), True)
    check("send", await ask(a, do="send", text="  n9abc   snr?  "), None)
    check("send: upper-cased, spaces folded, text only", eng.log[-1], ("tx.send", {"text": "N9ABC SNR?"}))
    check("send: too long refused", "Too long" in (await ask(a, do="send", text="X" * 161) or ""), True)
    check("send: non-ASCII refused", "can't send" in (await ask(a, do="send", text="CQ ünïcode") or ""), True)
    check("send: control characters refused", "can't send" in (await ask(a, do="send", text="CQ\x07") or ""), True)
    check("send: empty refused", await ask(a, do="send", text="   "), "Nothing to send.")
    check("send with offset and speed", await ask(a, do="send", text="cq", offset_hz=1234.4, speed="fast"), None)
    check("send: offset rounded, speed named", eng.log[-1], ("tx.send", {"text": "CQ", "tx_freq_hz": 1234, "submode": "fast"}))
    for bad, want in (({"offset_hz": 100}, "Offset"), ({"offset_hz": 9000}, "Offset"), ({"offset_hz": True}, "Offset"),
                      ({"offset_hz": "1500"}, "Offset"), ({"speed": "ultra"}, "Speed"), ({"speed": "radio.ptt.set"}, "Speed")):
        n = len(eng.log)
        why = await ask(a, do="send", text="CQ", **bad)
        check(f"send {bad} refused, nothing sent", (want in (why or ""), len(eng.log) == n), (True, True))
    check("hb", await ask(a, do="hb"), None)
    check("hb: tx.hb, no data", eng.log[-1], ("tx.hb", {}))
    check("another page can't hb", "another page" in (await ask(other, do="hb") or ""), True)
    await eng.push({"type": "event", "event": "tx.queued", "data": {"queue_size": 3}})
    check("the queue is shown", await until(lambda: e.view()["queue"] == 3), True)
    check("arm is clamped to ARM_MAX_S", (await ask(a, do="arm", minutes=999),
                                          e.view()["armed_until"] - time.time() <= js8_bridge.ARM_MAX_S), (None, True))

    # ------------------------------------------------------------ disarms
    async def disarmed_by(name, trigger):
        await ask(a, do="arm", minutes=10)
        mark = len(eng.log)
        await trigger()
        ok = await until(lambda: e.armed is None and ("config.set", js8_bridge.DISARM) in eng.log[mark:]
                         and ("tx.halt", {}) in eng.log[mark:], 3)
        check(f"disarmed: {name}", ok, True)
        a.closed = False
        st.tx = {"present": True, "connected": True, "tx": False, "tx_allowed": True, "tx_stopped": None}

    async def by_page():
        await ask(a, do="disarm")

    async def time_out():
        e.armed["until"] = time.monotonic() - 1

    async def page_closes():
        a.closed = True

    async def stop_tx():
        st.tx["tx_stopped"] = {"at": time.time(), "by": "a page"}
        t0 = time.monotonic()
        await br.radio_changed("ic7300")
        check("STOP TX disarms at once, not on the next tick", e.armed is None and time.monotonic() - t0 < 0.5, True)

    async def radio_gone():
        st.tx = {"present": False}

    async def no_tx_now():
        st.tx["tx_allowed"] = False

    async def auto_reply_on():
        eng.status["auto_reply"] = True
        await eng.push({"type": "event", "event": "status", "data": eng.status})

    for name, trig in (("from the page", by_page), ("the engine turned auto-reply on", auto_reply_on), ("the time ran out", time_out), ("the page closed", page_closes),
                       ("STOP TX at the radio", stop_tx), ("the radio went away", radio_gone),
                       ("TX turned off at the radio", no_tx_now)):
        await disarmed_by(name, trig)
    # a disarm racing the engine's own status: no "on without an arm" error
    import logging

    class Grab(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines = []

        def emit(self, rec):
            self.lines.append(rec.getMessage())
    grab = Grab()
    js8_bridge.log.addHandler(grab)
    eng.stale_on_halt = True
    await ask(a, do="arm", minutes=10)
    await ask(a, do="disarm")
    await asyncio.sleep(js8_bridge.SAFETY_EVERY_S * 2)
    eng.stale_on_halt = False
    js8_bridge.log.removeHandler(grab)
    check("a disarm with a stale status in flight: no false 'without an arm' error",
          [x for x in grab.lines if "while not armed" in x], [])
    check("...and it ends disarmed", (e.armed, e.status.get("tx_enabled")), (None, False))

    notices = [m for m in st.pushed if m["t"] == "notice"]
    check("each disarm tells the pages why", any("time ran out" in m["msg"] for m in notices)
          and any("STOP TX" in m["msg"] for m in notices), True)

    # an engine that stops answering: the arm fails, it doesn't hang
    eng.mute = True
    js8_bridge.REPLY_S = 0.3
    t0 = time.monotonic()
    why = await ask(a, do="arm", minutes=10)
    check("an engine that doesn't answer: arm refused, bounded", (bool(why), time.monotonic() - t0 < 2), (True, True))
    eng.mute = False
    js8_bridge.REPLY_S = 5
    await until(lambda: e.armed is None)

    # ------------------------------------------------------------ armed when the engine goes away
    await ask(a, do="arm", minutes=10)
    check("armed before the engine goes", e.armed is not None, True)

    # ------------------------------------------------------------ reconnect
    await eng.stop()
    check("engine gone: disconnected, pushed", await until(lambda: not e.connected and any(
        m["t"] == "js8_status" and not m["status"]["connected"] for m in st.pushed)), True)
    check("engine gone: says why", bool(e.error), True)
    check("engine gone: the arm is gone too", e.armed, None)
    eng2 = FakeEngine(port)
    await eng2.start()
    check("engine back: reconnects by itself", await until(lambda: e.connected, 6), True)

    # ------------------------------------------------------------ station stops while armed
    check("re-armed on the new engine", await ask(a, do="arm", minutes=10), None)
    mark = len(eng2.log)
    await br.stop()
    check("station stop disarms first", [c for c, _ in eng2.log[mark:]], ["tx.halt", "config.set", "status.get"])

    # ------------------------------------------------------------ the allowlist
    log_all = eng.log + eng2.log
    check("only allowlisted commands", {c for c, _ in log_all} <= {"status.get", "config.set", "tx.send", "tx.hb", "tx.halt"},
          True)
    check("config.set only ever ARM or DISARM", all(d in (js8_bridge.ARM, js8_bridge.DISARM)
                                                    for c, d in log_all if c == "config.set"), True)
    check("tx.send only ever {text, tx_freq_hz?, submode?}",
          all("text" in d and set(d) <= {"text", "tx_freq_hz", "submode"} for c, d in log_all if c == "tx.send"), True)
    check("tx.hb never carries data", all(d == {} for c, d in log_all if c == "tx.hb"), True)
    check("never radio.*", any(c.startswith("radio.") for c, _ in log_all), False)
    check("its own count agrees", set(e.sent) <= {"status.get", "config.set", "tx.send", "tx.hb", "tx.halt"}, True)

    await eng2.stop()


asyncio.run(main())
print(f"\n{fails} failure(s)" if fails else "\nall ok")
sys.exit(1 if fails else 0)
