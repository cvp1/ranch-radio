"""The station's side of the JS8 engines (js8_engine.py -> jf8call): receive only.

One client per radio with a js8_port, on that engine's loopback WebSocket.
It keeps the frames heard lately and who sent them, and pushes both to the
station's pages:

    {"t": "js8", "radio": ID, "frame": {...}}        each decoded frame
    {"t": "js8_status", "radio": ID, "status": {...}} engine up/down, rig, TX state

A page may ask for one radio's state ({"t": "js8", "do": "state", "radio": ID});
nothing else. This release sends the engine status.get and nothing more: no
tx.*, no radio.*, no config.set. Sending comes later, through the radio
server's gates, with its own tests.

The engine's own TX state is reported, never trusted: js8_engine.py disarms
it at every start, so an engine that says TX, auto-reply or heartbeat is on
is shown as unsafe on the page. What went out on air is the radio server's
state (jf8call says "tx.finished" even when its PTT was refused).
"""

import asyncio
import collections
import json
import logging
import re
import time

import aiohttp

log = logging.getLogger("js8")

MAX_FRAMES = 300            # per radio: about an hour of a busy band
MAX_HEARD = 200
MAX_TEXT = 200              # characters kept of one frame or message
BACKOFF_S = (1, 2, 5, 10, 30)
STATUS_EVERY_S = 30
FRAME_CALL = re.compile(r"^\s*([A-Z0-9]{1,3}[0-9][A-Z0-9/]{0,8}):")
STATUS_KEYS = ("radio_connected", "radio_freq_khz", "radio_mode", "submode_name", "callsign", "grid",
               "transmitting", "tx_enabled", "auto_reply", "heartbeat_enabled", "heartbeat_ack_enabled")
UNSAFE_KEYS = ("tx_enabled", "auto_reply", "heartbeat_enabled", "heartbeat_ack_enabled")


def check_block(block):
    """station.json's js8 block: {"radios": {ID: PORT}}."""
    if not isinstance(block, dict) or not isinstance(block.get("radios"), dict) or not block["radios"]:
        raise ValueError('js8: {"radios": {"ID": PORT}} with at least one radio')
    for rid, port in block["radios"].items():
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port < 65536:
            raise ValueError(f"js8.radios.{rid} = {port!r}: a port")


def _text(v):
    return str(v or "")[:MAX_TEXT]


def frame_of(event, data):
    """One engine event as the page sees it: an allowlist, text capped."""
    decoded = event == "message.decoded"
    text = data.get("raw") or data.get("body") if decoded else data.get("frame_text")
    f = {"kind": "message" if decoded else "frame", "utc": _text(data.get("utc_iso"))[:20],
         "submode": _text(data.get("submode_name"))[:12], "snr": data.get("snr_db"),
         "freq_hz": data.get("freq_hz"), "dial_khz": data.get("dial_freq_khz"), "text": _text(text)}
    if decoded:
        f.update({"from": _text(data.get("from"))[:12], "to": _text(data.get("to"))[:12],
                  "type": _text(data.get("type_name"))[:24], "grid": _text(data.get("grid"))[:8]})
    for k in ("snr", "freq_hz", "dial_khz"):
        if not isinstance(f[k], (int, float)) or isinstance(f[k], bool):
            f[k] = None
    return f


class Engine:
    """One radio's engine, seen from the station."""

    def __init__(self, rid, port, push):
        self.rid, self.port, self.push = rid, port, push
        self.frames = collections.deque(maxlen=MAX_FRAMES)
        self.heard = collections.OrderedDict()   # call -> {"utc", "snr", "freq_hz", "grid", "submode"}
        self.status = {}
        self.connected = False
        self.error = None
        self.sent = collections.Counter()       # what this client sent the engine, by command (tests read it)
        self._task = None

    def snapshot(self):
        return {"radio": self.rid, "status": self.view(), "frames": list(self.frames),
                "heard": [dict(call=c, **h) for c, h in reversed(self.heard.items())]}

    def view(self):
        unsafe = [k for k in UNSAFE_KEYS if self.status.get(k)]
        return {"connected": self.connected, "error": self.error, "unsafe": unsafe,
                **{k: self.status.get(k) for k in STATUS_KEYS}}

    def _heard(self, f):
        call = f.get("from") or (FRAME_CALL.match(f["text"]) or [None, None])[1]
        if not call:
            return
        h = self.heard.pop(call, {})
        h.update({"utc": f["utc"], "snr": f["snr"], "freq_hz": f["freq_hz"], "submode": f["submode"]})
        if f.get("grid"):
            h["grid"] = f["grid"]
        self.heard[call] = h
        while len(self.heard) > MAX_HEARD:
            self.heard.popitem(last=False)

    def _status(self, data):
        old = self.view()
        self.status = {k: data.get(k) for k in STATUS_KEYS if k in data}
        new = self.view()
        if new["unsafe"] and new["unsafe"] != old["unsafe"]:
            log.error("js8 %s: the engine reports %s on; it should start disarmed (js8_engine.py)",
                      self.rid, ", ".join(new["unsafe"]))
        if new != old:
            self.push({"t": "js8_status", "radio": self.rid, "status": new})

    async def _send(self, ws, cmd):
        self.sent[cmd] += 1
        await ws.send_json({"type": "cmd", "id": cmd, "cmd": cmd, "data": {}})

    async def _session(self, session):
        async with session.ws_connect(f"ws://127.0.0.1:{self.port}", heartbeat=20, max_msg_size=1 << 20) as ws:
            self.connected, self.error = True, None
            self.push({"t": "js8_status", "radio": self.rid, "status": self.view()})
            await self._send(ws, "status.get")
            last = time.monotonic()
            while True:
                try:
                    msg = await ws.receive(timeout=STATUS_EVERY_S)
                except asyncio.TimeoutError:
                    msg = None
                if time.monotonic() - last >= STATUS_EVERY_S:
                    await self._send(ws, "status.get")
                    last = time.monotonic()
                if msg is None:
                    continue
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                    return
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    m = json.loads(msg.data)
                except ValueError:
                    continue
                if not isinstance(m, dict):
                    continue
                data = m.get("data") if isinstance(m.get("data"), dict) else {}
                if m.get("type") == "reply" and m.get("id") == "status.get" and m.get("ok"):
                    self._status(data)
                elif m.get("type") == "event" and m.get("event") == "status":
                    self._status(data)
                elif m.get("type") == "event" and m.get("event") in ("message.frame", "message.decoded"):
                    f = frame_of(m["event"], data)
                    self.frames.append(f)
                    self._heard(f)
                    self.push({"t": "js8", "radio": self.rid, "frame": f})

    async def run(self):
        tries = 0
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=5)) as session:
            while True:
                try:
                    await self._session(session)
                    tries = 0
                    self.error = "the engine closed its WebSocket"
                except asyncio.CancelledError:
                    raise
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
                    self.error = f"engine not reachable on 127.0.0.1:{self.port} ({type(e).__name__})"
                if self.connected:
                    self.connected = False
                    self.push({"t": "js8_status", "radio": self.rid, "status": self.view()})
                await asyncio.sleep(BACKOFF_S[min(tries, len(BACKOFF_S) - 1)])
                tries += 1

    def start(self):
        if self._task is None:
            self._task = asyncio.ensure_future(self.run())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


class Js8Bridge:
    def __init__(self, block, station):
        check_block(block)
        known = {r.id for r in station.radios}
        unknown = sorted(set(block["radios"]) - known)
        if unknown:
            raise ValueError(f"js8.radios names no radio on this station: {unknown}")
        self.station = station
        self.engines = {rid: Engine(rid, port, self._push) for rid, port in block["radios"].items()}

    def _push(self, msg):
        self.station._broadcast(json.dumps(msg))

    async def start(self):
        for e in self.engines.values():
            e.start()

    async def stop(self):
        for e in self.engines.values():
            await e.stop()

    def status(self):
        """For the page's hello: an allowlist."""
        return {"loaded": True, "radios": {rid: e.view() for rid, e in self.engines.items()}}

    async def handle(self, ws, m):
        if m.get("do") != "state":
            raise ValueError("JS8 is receive-only on this station: the page may ask for state only")
        e = self.engines.get(m.get("radio"))
        if e is None:
            raise ValueError(f"no JS8 engine for radio {m.get('radio')!r}")
        await self.station._send(ws, json.dumps({"t": "js8_state", **e.snapshot()}))
