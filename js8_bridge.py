"""The station's side of the JS8 engines (js8_engine.py -> jf8call).

One client per radio with a js8_port, on that engine's loopback WebSocket.
It keeps the frames heard lately and who sent them, and pushes both to the
station's pages:

    {"t": "js8", "radio": ID, "frame": {...}}        each decoded frame
    {"t": "js8_status", "radio": ID, "status": {...}} engine up/down, rig, TX state

Pages ({"t": "js8", "do": VERB, "radio": ID}, or the JS8 tab through its
add-on):

    state                  that radio's frames, heard and status, to the asking page
    arm     minutes        let the engine send for a while (ARM_S default, ARM_MAX_S most)
    send    text [offset_hz speed]
                           queue one message (only the page that armed it); the page
                           composes CQ and the directed queries (N9ABC SNR?) as text
    hb                     one heartbeat now (jf8call picks a clear offset in the
                           heartbeat sub-band); the periodic heartbeat stays off
    disarm                 halt the engine's transmission and disarm it

Sending is armed per radio, time-boxed, and owned by the page that armed it.
Arming needs the engine connected with a callsign and the radio's own
server present, connected, with TX allowed (no --no-tx) and not stopped.
It ends, with tx.halt then txEnabled false, when the time runs out, the
owning page closes, the radio's STOP TX is pressed, the radio or engine goes
away, or the station stops. An engine that reports TX, auto-reply or
heartbeat on when it isn't armed is disarmed at once and shown unsafe.

What the bridge ever sends an engine is an allowlist: status.get,
config.set with ARM or DISARM exactly, tx.send {"text", "tx_freq_hz"?,
"submode"?}, tx.hb, tx.halt. Never radio.* (PTT, tune, connect), and of what
a page writes only the text, a bounded offset and a named speed.

Every key-up still goes through the radio's own rigctl port, so the radio
server's gates (STOP TX latch, --no-tx, --tx-timeout, keepalive unkey) hold
whatever the engine does. What went out on air is the radio server's state
(radio_tx here): jf8call says "tx.finished" even when its PTT was refused.
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
ARM_S = 10 * 60               # an arm lasts this long unless the page asks otherwise
ARM_MAX_S = 30 * 60
REPLY_S = 5                  # an engine command not answered by then has failed
SAFETY_EVERY_S = 1.0
REDISARM_S = 5               # how often an engine found armed without us is disarmed again
MAX_SEND = 160               # characters of one message a page may send
OFFSET_HZ = (400, 2600)      # audio offsets a page may pick to transmit on
SPEEDS = ("normal", "fast", "turbo", "slow")   # not ultra: stock JS8Call may not decode it
SEND_OK = re.compile(r"^[\x20-\x7e]+$")   # printable ASCII; jf8call packs it (varicode/JSC)
ARM = {"txEnabled": True, "autoReply": False, "heartbeatEnabled": False, "heartbeatAckEnabled": False}
DISARM = {"txEnabled": False, "autoReply": False, "heartbeatEnabled": False, "heartbeatAckEnabled": False}
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

    def __init__(self, rid, port, push, radio=None):
        self.rid, self.port, self.push = rid, port, push
        self.radio = radio or (lambda: {})   # the radio server's word: {"present", "tx_allowed", "tx_stopped", "tx"}
        self.armed = None                    # {"by": page, "until": monotonic, "until_utc": epoch} while it may send
        self.queue = 0                       # frames the engine says it has queued
        self._ws = None
        self._lock = asyncio.Lock()
        self._pending = {}                   # command id -> future of its reply
        self._ids = 0
        self._redisarmed = 0.0
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
        unsafe = [k for k in UNSAFE_KEYS if self.status.get(k) and not (k == "tx_enabled" and self.armed)]
        r = self.radio() or {}
        return {"connected": self.connected, "error": self.error, "unsafe": unsafe,
                "armed": bool(self.armed), "armed_until": self.armed and self.armed["until_utc"],
                "queue": self.queue, "radio_tx": bool(r.get("tx")), "why_not": self.why_not(),
                **{k: self.status.get(k) for k in STATUS_KEYS}}

    def why_not(self):
        """None, or why this radio can't be armed now."""
        r = self.radio() or {}
        if not self.connected:
            return "the JS8 engine isn't connected"
        if not self.status.get("callsign"):
            return "the JS8 engine has no callsign"
        if not self.status.get("radio_connected"):
            return "the JS8 engine isn't connected to the radio"
        if not r.get("present"):
            return "the radio's server isn't reachable"
        if not r.get("connected"):
            return "the radio isn't connected to its server"
        if not r.get("tx_allowed"):
            return "transmit is off on this radio (--no-tx)"
        if r.get("tx_stopped"):
            return "STOP TX is on at the radio; Resume there first"
        return None

    def pushview(self):
        self.push({"t": "js8_status", "radio": self.rid, "status": self.view()})

    # --- commands to the engine: the allowlist, nothing else

    async def _call(self, cmd, data=None):
        """Send one command and wait for its reply: (ok, data or error)."""
        ws = self._ws
        if ws is None or ws.closed:
            return False, "the JS8 engine isn't connected"
        self._ids += 1
        cid = f"rr-{self._ids}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[cid] = fut
        try:
            async with self._lock:
                self.sent[cmd] += 1
                await ws.send_json({"type": "cmd", "id": cid, "cmd": cmd, "data": data or {}})
            return await asyncio.wait_for(fut, REPLY_S)
        except asyncio.TimeoutError:
            return False, f"the JS8 engine didn't answer {cmd}"
        except (aiohttp.ClientError, ConnectionError, RuntimeError) as e:
            return False, f"the JS8 engine: {type(e).__name__}"
        finally:
            self._pending.pop(cid, None)

    async def arm(self, page, seconds):
        why = self.why_not()
        if why:
            return f"Can't arm JS8: {why}."
        if self.armed and self.armed["by"] is not page:
            return "JS8 is armed from another page; disarm it there first."
        seconds = max(60, min(int(seconds), ARM_MAX_S))
        rec = {"by": page, "until": time.monotonic() + seconds, "until_utc": time.time() + seconds}
        self.armed = rec
        ok, got = await self._call("config.set", ARM)
        if ok:
            ok, got = await self._call("status.get")
            if ok and isinstance(got, dict):
                self._status(got)
        st = self.status
        if self.armed is not rec:            # disarmed while arming: that disarm went out after this
            return "JS8 was disarmed while arming."
        if not ok or not st.get("tx_enabled") or any(st.get(k) for k in UNSAFE_KEYS if k != "tx_enabled"):
            await self.disarm("the engine didn't confirm TX on with auto-reply and heartbeat off")
            return f"Can't arm JS8: {got if not ok else 'the engine did not confirm the arm'}."
        log.warning("js8 %s: armed for %d s", self.rid, seconds)
        self.pushview()
        return None

    async def disarm(self, why):
        """Halt and disarm, whatever the state: every step gets its turn."""
        was = self.armed is not None
        self.armed = None
        if self._ws is not None and not self._ws.closed:
            for cmd, data in (("tx.halt", None), ("config.set", DISARM)):
                ok, err = await self._call(cmd, data)
                if not ok:
                    log.error("js8 %s: disarm: %s: %s", self.rid, cmd, err)
        if was:
            log.warning("js8 %s: disarmed: %s", self.rid, why)
            self.push({"t": "notice", "level": "warn", "msg": f"JS8 {self.rid} disarmed: {why}."})
        self.pushview()

    async def _may_send(self, page):
        if not self.armed:
            return "JS8 isn't armed on this radio."
        if self.armed["by"] is not page:
            return "JS8 is armed from another page; send from there."
        why = self.why_not()
        if why:
            await self.disarm(why)
            return f"Not sent: {why}."
        return None

    async def heartbeat(self, page):
        why = await self._may_send(page)
        if why:
            return why
        ok, got = await self._call("tx.hb")
        if not ok:
            return f"Heartbeat not sent: {got}."
        log.info("js8 %s: heartbeat queued at %s Hz", self.rid, got.get("freq_hz"))
        return None

    async def send(self, page, text, offset_hz=None, speed=None):
        why = await self._may_send(page)
        if why:
            return why
        text = " ".join(str(text or "").upper().split())
        if not text:
            return "Nothing to send."
        if len(text) > MAX_SEND:
            return f"Too long: {len(text)} characters (at most {MAX_SEND})."
        if not SEND_OK.match(text):
            return "JS8 can't send some of those characters."
        data = {"text": text}
        if offset_hz is not None:
            if not isinstance(offset_hz, (int, float)) or isinstance(offset_hz, bool) \
                    or not OFFSET_HZ[0] <= offset_hz <= OFFSET_HZ[1]:
                return f"Offset must be {OFFSET_HZ[0]}-{OFFSET_HZ[1]} Hz."
            data["tx_freq_hz"] = round(float(offset_hz))
        if speed is not None:
            if speed not in SPEEDS:
                return f"Speed must be one of {', '.join(SPEEDS)}."
            data["submode"] = speed
        ok, got = await self._call("tx.send", data)
        if not ok:
            return f"Not sent: {got}."
        log.info("js8 %s: queued %d characters", self.rid, len(text))
        return None

    async def safety(self):
        """Once a second, and whenever the radio's state changes: end an arm
        that has lost its reason, and disarm an engine armed without one."""
        if self.armed:
            page = self.armed["by"]
            why = None
            if any(self.status.get(k) for k in UNSAFE_KEYS if k != "tx_enabled"):
                why = "the engine reported auto-reply or heartbeat on"
            elif time.monotonic() >= self.armed["until"]:
                why = "the arm time ran out"
            elif getattr(page, "closed", False):
                why = "the page that armed it closed"
            else:
                why = self.why_not()
            if why:
                await self.disarm(why)
        elif self.connected and any(self.status.get(k) for k in UNSAFE_KEYS):
            now = time.monotonic()
            if now - self._redisarmed >= REDISARM_S:
                self._redisarmed = now
                log.error("js8 %s: the engine reports %s on while not armed; disarming it", self.rid,
                          ", ".join(k for k in UNSAFE_KEYS if self.status.get(k)))
                await self.disarm("it was on without an arm")

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
        async with self._lock:
            self.sent[cmd] += 1
            await ws.send_json({"type": "cmd", "id": cmd, "cmd": cmd, "data": {}})

    async def _session(self, session):
        async with session.ws_connect(f"ws://127.0.0.1:{self.port}", heartbeat=20, max_msg_size=1 << 20) as ws:
            self._ws = ws
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
                fut = self._pending.get(m.get("id")) if m.get("type") == "reply" else None
                if fut is not None:
                    if not fut.done():
                        fut.set_result((True, data) if m.get("ok") else (False, _text(m.get("error")) or "refused"))
                elif m.get("type") == "reply" and m.get("id") == "status.get" and m.get("ok"):
                    self._status(data)
                elif m.get("type") == "event" and m.get("event") == "status":
                    self._status(data)
                elif m.get("type") == "event" and m.get("event") == "tx.queued":
                    q = data.get("queue_size")
                    self.queue = q if isinstance(q, int) and not isinstance(q, bool) and q >= 0 else 0
                    self.pushview()
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
                self._ws = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_result((False, "the JS8 engine went away"))
                if self.armed:
                    # A restarted engine comes up disarmed (js8_engine.py); one that
                    # didn't restart is disarmed by safety() when it is back.
                    self.armed = None
                    log.warning("js8 %s: disarmed: the engine went away", self.rid)
                    self.push({"t": "notice", "msg": f"JS8 {self.rid} disarmed: the engine went away.",
                               "level": "warn"})
                if self.connected:
                    self.connected = False
                    self.push({"t": "js8_status", "radio": self.rid, "status": self.view()})
                await asyncio.sleep(BACKOFF_S[min(tries, len(BACKOFF_S) - 1)])
                tries += 1

    def start(self):
        if self._task is None:
            self._task = asyncio.ensure_future(self.run())

    async def stop(self):
        if self.armed or any(self.status.get(k) for k in UNSAFE_KEYS):
            await self.disarm("the station is stopping")
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
        self.engines = {rid: Engine(rid, port, self._push, lambda rid=rid: self._radio(rid))
                        for rid, port in block["radios"].items()}
        self.listeners = []          # callables given each push (the JS8 tab's add-on); one failing is logged
        self._safety = None

    def _radio(self, rid):
        """What the radio's own server says (station.RadioWatch); {} when unknown."""
        fn = getattr(self.station, "radio_tx_state", None)
        return fn(rid) if fn else {}

    def _push(self, msg):
        self.station._broadcast(json.dumps(msg))
        for fn in list(self.listeners):
            try:
                fn(msg)
            except Exception as e:
                log.error("js8 listener %s: %s: %s", getattr(fn, "__qualname__", fn), type(e).__name__, e)

    async def start(self):
        for e in self.engines.values():
            e.start()
        if self._safety is None:
            self._safety = asyncio.ensure_future(self._safety_loop())

    async def stop(self):
        if self._safety is not None:
            self._safety.cancel()
            await asyncio.gather(self._safety, return_exceptions=True)
            self._safety = None
        for e in self.engines.values():
            await e.stop()

    async def _safety_loop(self):
        while True:
            await asyncio.sleep(SAFETY_EVERY_S)
            for e in self.engines.values():
                try:
                    await e.safety()
                except Exception:              # noqa: BLE001 - one engine must not stop the others' checks
                    log.exception("js8 %s: safety check", e.rid)

    async def radio_changed(self, rid):
        """The radio's server changed what it says (STOP TX, TX allowed,
        keyed, gone): end an arm that lost its reason now, not in a second."""
        e = self.engines.get(rid)
        if e is None:
            return
        await e.safety()
        e.pushview()

    async def page_closed(self, page):
        for e in self.engines.values():
            if e.armed and e.armed["by"] is page:
                await e.disarm("the page that armed it closed")

    def status(self):
        """For the page's hello: an allowlist."""
        return {"loaded": True, "radios": {rid: e.view() for rid, e in self.engines.items()}}

    async def handle(self, ws, m):
        """A page's request. Returns None, or why it was refused (the caller
        tells that page); a malformed request raises ValueError."""
        do = m.get("do")
        if do not in ("state", "arm", "disarm", "send", "hb"):
            raise ValueError(f"JS8: unknown request {do!r}")
        e = self.engines.get(m.get("radio"))
        if e is None:
            raise ValueError(f"no JS8 engine for radio {m.get('radio')!r}")
        if do == "state":
            await self.station._send(ws, json.dumps({"t": "js8_state", **e.snapshot()}))
            return None
        if do == "arm":
            mins = m.get("minutes", ARM_S / 60)
            if not isinstance(mins, (int, float)) or isinstance(mins, bool):
                raise ValueError("JS8 arm: minutes must be a number")
            return await e.arm(ws, mins * 60)
        if do == "disarm":
            await e.disarm("disarmed from the page")
            return None
        if do == "hb":
            return await e.heartbeat(ws)
        if not isinstance(m.get("text"), str):
            raise ValueError("JS8 send: text must be a string")
        return await e.send(ws, m["text"], m.get("offset_hz"), m.get("speed"))
