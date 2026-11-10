"""Winlink packet QSY: the UV-PRO add-on tunes the hub's radio to a packet
session's freq= before Pat dials and puts it back after, so APRS and the
iGate resume untouched. Against server.py --sim, tests/fake_pat.py as Pat,
tests/fake_uvpro_bridge.py as the radio and a stand-in hub whose
/api/status lists the links the test sets.

  in process: a second connect while the first's lane is arming is refused;
     Stop while arming undoes it; an arm that raises is refused
  1  a channel already on the frequency: selected, Pat gets the URL without
     freq=, the radio back on its channel (and dual watch) after
  2  already on it, dual watch off: nothing written either way
  3  no channel on it, a radio with a VFO: VFO A tuned and put back
  4  no VFO, qsy_channel set: that channel written, used, written back whole
  5  no VFO, no qsy_channel: refused, nothing written, Pat never asked
  6  the radio transmitting: refused, nothing written
  7  a link still up when Pat returns: the radio stays until it drops
  8  the link held past restore_wait_s: warned, record kept, restored first next time
  9  the select failing after a scratch write: written back at once, refused
  10 a page edit while tuned is refused
  11 the add-on can't be switched off while tuned
  12 the add-on off: the session runs untuned, no bridge contact
  13 a session that succeeds still puts the radio back
  14 the server stopping while the link is held: bounded, warns, exits
  station.py: the same, by the hub's name; borrow mode (no "control") refuses
"""
import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import winlink_test_lib as L  # noqa: E402
import testkit  # noqa: E402
from testkit import wait_for  # noqa: E402

TMP = tempfile.mkdtemp(prefix="wlqsy-")
fails = 0
HUB = {"links": []}            # the links the stand-in hub's /api/status lists
HOLDS = []                     # its APRS hold calls: (path, by), in order
LINK = {"local": "N9ABC", "remote": "N0DAJ", "state": "connected"}


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:70} got {got!r}" + ("" if ok else f" want {want!r}"))


def free_ports(n):
    """n distinct ports from the OS, held until all are picked."""
    socks = []
    for _ in range(n):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        socks.append(s)
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


L.APPS, L.LANE, L.PATP, L.WEB, HUB_PORT, CONTROL = free_ports(6)


async def hub_main(port, ready):
    async def status(request):
        return web.json_response({"radio": {"up": True, "last_rx": time.time()}, "tx": {"enabled": True},
                                  "links": list(HUB["links"])})
    async def hold(request):
        HOLDS.append((request.path, (await request.json()).get("by")))
        return web.json_response({"holds": []})
    app = web.Application()
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/hold", hold)
    app.router.add_post("/api/hold/release", hold)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    ready.set()
    await asyncio.Event().wait()


ready = threading.Event()
threading.Thread(target=lambda: asyncio.new_event_loop().run_until_complete(hub_main(HUB_PORT, ready)),
                 daemon=True).start()
ready.wait(10)


def jsonl(path):
    try:
        with open(path) as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


class Run:
    """One server.py --sim with the Packet and UV-PRO add-ons and fake Pat."""

    n = 0

    def __init__(self, env=None, **uvpro):
        Run.n += 1
        self.blog = os.path.join(TMP, f"bridge-{Run.n}.jsonl")
        cfg = {"packet": {"enabled": True, "url": f"http://127.0.0.1:{HUB_PORT}", "name": "Direwolf"},
               "uvpro": {"enabled": True, "address": "00:11:22:33:44:55", "python": sys.executable,
                         "bridge": os.path.join(HERE, "fake_uvpro_bridge.py"),
                         "control": f"tcp:127.0.0.1:{CONTROL}",
                         "packet_status": f"http://127.0.0.1:{HUB_PORT}/api/status",
                         "calls": ["N9ABC"], "restore_wait_s": 3, **uvpro}}
        path = os.path.join(TMP, f"addons-{Run.n}.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        HUB["links"] = []
        self.srv, self.flog = L.start({"FAKE_UVPRO_LOG": self.blog, "FAKE_UVPRO_CH3": "145.09", **(env or {})},
                                      "--addons", path, "--addons-state", "")
        ok = wait_for(self.ready, 20, 0.3)
        if not ok:
            print(L.stop(self.srv))
            raise SystemExit("the UV-PRO add-on did not read the radio")
        self.page = L.Page()

    @staticmethod
    def ready():
        p = L.Page()
        try:
            a = p.hello.get("addons") or {}
            uv = a.get("uvpro") or {}
            hub = p.hello.get("packet") or {}           # the station's own packet tab: the hub's presence
            return bool((uv.get("status") or {}).get("connected") and uv.get("settings")
                        and uv.get("memories") and hub.get("present"))
        finally:
            p.close()

    def writes(self, since=0):
        """The bridge's settings / channel commands, from command number `since`."""
        return [(c["cmd"], c.get("id"), c.get("values")) for c in jsonl(self.blog)[since:]
                if c.get("cmd") in ("settings", "channel")]

    def mark(self):
        return len(jsonl(self.blog)), len(L.fake_log(self.flog))

    def pat_urls(self, since):
        return [r.get("url") for r in L.fake_log(self.flog)[since:] if r.get("route") == "connect"]

    def ptt(self, since):
        return [r.get("reply") for r in L.fake_log(self.flog)[since:] if r.get("route") == "ptt_on"]

    def connect(self, url, wait_end=True, timeout=30):
        """Send a connect; wait until it is armed or refused (and, wait_end,
        for the session's end). Returns the armed event or None."""
        pg = self.page
        pg.events.clear()
        pg.send({"t": "winlink_connect", "url": url, "via": "packet"})
        e = pg.wait(lambda e: (e.get("t") == "winlink" and e.get("armed") is True)
                    or (e.get("t") == "notice" and e.get("level") == "warn"), 15)
        armed = e if e and e.get("t") == "winlink" else None
        if armed and wait_end:
            pg.wait(L.ended, timeout)
        return armed

    def notices(self, level=None):
        return [e.get("msg", "") for e in list(self.page.events)
                if e.get("t") == "notice" and (level is None or e.get("level") == level)]

    def notify_titles(self):
        return [e["notify"].get("title", "") for e in list(self.page.events)
                if e.get("t") == "winlink" and isinstance(e.get("notify"), dict)]

    def ended(self):
        return next((e for e in list(self.page.events) if L.ended(e)), None)

    def uvpro(self):
        """The add-on's snapshot as a fresh page sees it."""
        p = L.Page()
        try:
            return (p.hello.get("addons") or {}).get("uvpro") or {}
        finally:
            p.close()

    def addon(self, verb, **m):
        self.page.send({"t": "addon", "addon": "uvpro", "do": verb, **m})

    def stop(self):
        try:
            self.page.close()
        except Exception:   # noqa: BLE001 - the server may be gone already
            pass
        return L.stop(self.srv)


def has(msgs, *parts):
    return any(all(p in m for p in parts) for m in msgs)


def memory(r, cid):
    return next((m for m in r.uvpro().get("memories") or [] if m["id"] == cid), None)


def pat_called(r, p0):
    return lambda: bool(r.pat_urls(p0))


def pat_returned(r):
    """Pat's /api/connect has returned (its last log line of a session)."""
    return lambda: any(e.get("t") == "winlink" and "Disconnected." in str(e.get("log", ""))
                       for e in list(r.page.events))


def radio_tx(r, want):
    return lambda: bool((r.uvpro().get("state") or {}).get("is_in_tx")) is want


# --- in process: PatEngine admission while a lane's arm is awaited ------------
sys.path.insert(0, L.ROOT)
import winlink  # noqa: E402

logging.getLogger().addHandler(logging.NullHandler())   # the arm that raises below logs


async def admission():
    notes, started = [], []

    class SlowLane(winlink.NoLane):
        """A TunedLane stand-in whose arm() waits (the tuner writing the radio)."""

        def __init__(self, fail=False):
            super().__init__("Slow")
            self.gate, self.ends, self.fail = asyncio.Event(), [], fail

        async def arm(self, ws, url):
            await self.gate.wait()
            if self.fail:
                raise RuntimeError("tuner broke")
            return True

        async def end(self, started, ok, reason, closing, notify):
            self.ends.append(reason)

    st = SimpleNamespace(notice=lambda msg, level="info", ws=None: notes.append((level, msg)),
                         band_of=lambda hz: "2 m", _broadcast=lambda payload: None)
    args = SimpleNamespace(winlink_pat_port=1, winlink_pat="pat", winlink_session_max=60)
    lanes = []

    def lane_for(url, via):
        return lanes[-1], None
    eng = winlink.PatEngine(st, args, lane_for=lane_for)

    async def yes():
        return True

    async def none_queued(box):
        return []

    async def session(url, queued, pat_url, peer=False):
        started.append(pat_url)
    eng._ready, eng._listing, eng._session = yes, none_queued, session
    # Pat idle per its Status (unknown counts as busy and refuses every connect)
    eng.pat_ws, eng._status_live = object(), True
    eng.status = {"connected": False, "dialing": False, "active_listeners": [], "remote_addr": ""}
    url = "ax25:///N0DAJ?freq=145090"
    out = {}

    async def bounded(coro):
        try:
            await asyncio.wait_for(coro, 5)
        except TimeoutError:
            notes.append(("test", "timed out"))
        except Exception as e:               # noqa: BLE001 - the arm's error let out
            notes.append(("test", f"raised {e!r}"))

    slow = SlowLane()
    lanes.append(slow)
    first = asyncio.ensure_future(eng.connect(None, url))
    await asyncio.sleep(0.05)
    lanes.append(SlowLane())                 # a second connect, its lane ready at once
    lanes[-1].gate.set()
    await bounded(eng.connect(None, url))    # while the first is still arming
    out["second refused"] = ("warn", "A Winlink session is already running.") in notes
    slow.gate.set()
    await bounded(first)
    await asyncio.sleep(0)
    out["only the first started, freq dropped"] = started == ["ax25:///N0DAJ"]

    eng.session = None
    started.clear()
    notes.clear()
    slow = SlowLane()
    lanes.append(slow)
    t = asyncio.ensure_future(eng.connect(None, url))
    await asyncio.sleep(0.05)
    await bounded(eng.stop_session())        # Stop pressed while arming
    slow.gate.set()
    await bounded(t)
    out["stopped while arming: not started, undone"] = (started, slow.ends) == ([], ["stopped before it started"])

    eng.session = None
    notes.clear()
    lanes.append(SlowLane(fail=True))
    lanes[-1].gate.set()
    await bounded(eng.connect(None, url))
    out["arm raising: refused, undone, reservation released"] = (
        any(lvl == "warn" and "arming Slow failed" in m for lvl, m in notes), lanes[-1].ends, started,
        eng._admitting) == (True, ["arming failed: tuner broke"], [], False)
    return out


for name, ok in asyncio.run(admission()).items():
    check(f"admission: {name}", ok, True)


# --- 1 and 12: a radio with the gateway's frequency on a channel ---------------
TX_FLAG = os.path.join(TMP, "tx")
r = Run({"FAKE_UVPRO_TX": TX_FLAG})
try:
    b0, p0 = r.mark()
    h0 = len(HOLDS)
    armed = r.connect("ax25:///N0DAJ?freq=145090")
    check("1 existing channel: the session runs via the hub", armed and armed.get("via"), "Direwolf")
    check("  the hub was asked to hold APRS before the retune", HOLDS[h0:h0 + 1], [("/api/hold", "winlink-qsy")])
    check("  Pat got the URL without freq= (Pat does not QSY)", r.pat_urls(p0), ["ax25:///N0DAJ"])
    check("  the page is told the radio was tuned (channel 3)",
          has(r.notices("info"), "tuned to 145.090 MHz", "channel 3"), True)
    check("  this radio's own lane refused Pat's PTT", r.ptt(p0), ["RPRT -11"])
    check("  one select, one restore: no channel written",
          r.writes(b0), [("settings", None, {"channel_a": 2, "double_channel": 0}),
                         ("settings", None, {"channel_a": 0, "double_channel": 1})])
    check("  the page is told the radio is back on channel 1",
          any("channel 1" in t for t in r.notify_titles()), True)
    check("  the session ended", r.ended() is not None, True)
    check("  and the hold released once the radio was back",
          wait_for(lambda: HOLDS[h0:] == [("/api/hold", "winlink-qsy"), ("/api/hold/release", "winlink-qsy")], 10), True)

    # 3: no channel on 145.050, the radio has a VFO: VFO A is tuned and put back
    b0, p0 = r.mark()
    armed = r.connect("ax25:///N0DAJ?freq=145050")
    check("3 VFO: the session runs via the hub", armed and armed.get("via"), "Direwolf")
    check("  the page is told: VFO A", has(r.notices("info"), "tuned to 145.050 MHz", "VFO A"), True)
    check("  VFO A tuned, selected; afterwards the channel, then VFO A back",
          r.writes(b0), [("channel", 251, {"rx_freq": 145.05, "tx_freq": 145.05}),
                         ("settings", None, {"channel_a": 251, "double_channel": 0}),
                         ("settings", None, {"channel_a": 0, "double_channel": 1}),
                         ("channel", 251, {"rx_freq": 146.52, "tx_freq": 146.52})])
    check("  back on channel 1", any("channel 1" in t for t in r.notify_titles()), True)

    # 6: the radio transmitting: nothing is changed under it
    open(TX_FLAG, "w").close()
    r.addon("refresh")
    check("6 the radio reports transmitting", wait_for(radio_tx(r, True), 10), True)
    b0, p0 = r.mark()
    armed = r.connect("ax25:///N0DAJ?freq=145090")
    check("6 transmitting: refused, saying why", (armed, has(r.notices("warn"), "Not connecting", "transmitting")),
          (None, True))
    check("  nothing written, Pat not asked", (r.writes(b0), r.pat_urls(p0)), ([], []))
    os.unlink(TX_FLAG)
    r.addon("refresh")
    check("  the radio done transmitting", wait_for(radio_tx(r, False), 10), True)

    # 12: the add-on off: the operator tunes the radio
    r.addon("power", on=False)
    check("12 add-on off", wait_for(lambda: not (r.uvpro().get("on")), 10), True)
    b0, p0 = r.mark()
    armed = r.connect("ax25:///N0DAJ?freq=145050")
    check("12 add-on off: the session runs via the hub", armed and armed.get("via"), "Direwolf")
    check("  the page is told the radio must already be on 145.050 MHz",
          has(r.notices("info"), "must already be on", "145.050"), True)
    check("  Pat got the URL without freq=", r.pat_urls(p0), ["ax25:///N0DAJ"])
    check("  no bridge contact", jsonl(r.blog)[b0:], [])
finally:
    r.stop()

# --- 5: no VFO, no channel on the frequency, no qsy_channel: refused ------------
r = Run({"FAKE_UVPRO_NO_VFO": "1"})
try:
    b0, p0 = r.mark()
    armed = r.connect("ax25:///N0DAJ?freq=145050")
    time.sleep(1.0)                       # negative check: nothing may follow the refusal
    check("5 nothing to tune with: not armed", armed, None)
    check("  refused, saying why", has(r.notices("warn"), "Not connecting", "could not be tuned",
                                       "no FM simplex channel on 145.050 MHz"), True)
    check("  Pat was not asked", r.pat_urls(p0), [])
    check("  nothing written to the radio", r.writes(b0), [])
finally:
    r.stop()

# --- 2: already on the channel, dual watch off: nothing to write either way -----
r = Run({"FAKE_UVPRO_SETTINGS": json.dumps({"channel_a": 2, "double_channel": 0})})
try:
    b0, p0 = r.mark()
    armed = r.connect("ax25:///N0DAJ?freq=145090")
    check("2 already there: the session runs", armed and armed.get("via"), "Direwolf")
    check("  the page is told (channel 3)", has(r.notices("info"), "tuned to 145.090 MHz", "channel 3"), True)
    check("  nothing written, before or after", r.writes(b0), [])
    check("  and back on channel 3", any("channel 3" in t for t in r.notify_titles()), True)
finally:
    r.stop()

# --- 4: no VFO (the UV-50PRO), qsy_channel set: the spare channel is used -------
r = Run({"FAKE_UVPRO_NO_VFO": "1"}, qsy_channel=4)
try:
    before = memory(r, 3)
    b0, p0 = r.mark()
    armed = r.connect("ax25:///N0DAJ?freq=145050")
    check("4 scratch channel: the session runs", armed and armed.get("via"), "Direwolf")
    check("  the page is told (channel 4 written)", has(r.notices("info"), "145.050 MHz", "channel 4 written"), True)
    w = r.writes(b0)
    first = w[0] if w else (None, None, {})
    check("  channel 4 written first: 145.050 simplex, TX on, named WL QSY",
          (first[0], first[1], {k: first[2].get(k) for k in ("rx_freq", "tx_freq", "tx_disable", "name")}),
          ("channel", 3, {"rx_freq": 145.05, "tx_freq": 145.05, "tx_disable": False, "name": "WL QSY"}))
    check("  then selected; afterwards the channel, then channel 4 written back",
          [(c, i, v if c == "settings" else None) for c, i, v in w[1:]],
          [("settings", None, {"channel_a": 3, "double_channel": 0}),
           ("settings", None, {"channel_a": 0, "double_channel": 1}), ("channel", 3, None)])
    check("  channel 4 holds what it held before (NOAA, RX only)",
          ((before or {}).get("Name"), memory(r, 3) == before), ("NOAA", True))
finally:
    r.stop()

# --- 9: the select fails after the scratch write: written back, refused ---------
r = Run({"FAKE_UVPRO_NO_VFO": "1", "FAKE_UVPRO_FAIL_NEXT": "settings"}, qsy_channel=4)
try:
    before = memory(r, 3)
    b0, p0 = r.mark()
    armed = r.connect("ax25:///N0DAJ?freq=145050")
    check("9 select failed: not armed", armed, None)
    check("  refused, naming the error", has(r.notices("warn"), "Not connecting", "FAKE_UVPRO_FAIL_NEXT"), True)
    check("  written, select tried, written back at once",
          [(c, i) for c, i, _ in r.writes(b0)], [("channel", 3), ("settings", None), ("channel", 3)])
    check("  channel 4 as before", memory(r, 3) == before, True)
    check("  no recovery record left", r.uvpro().get("qsy"), None)
    check("  Pat was not asked", r.pat_urls(p0), [])
finally:
    r.stop()

# --- 13, 7, 10, 11, 8: sessions that succeed (Pat keys nothing for packet) -------
r = Run({"FAKE_PAT_NO_PTT": "1", "FAKE_PAT_TX_S": "3"}, restore_wait_s=6)
try:
    b0, p0 = r.mark()
    armed = r.connect("ax25:///N0DAJ?freq=145090")
    e = r.ended()
    check("13 a good session: ended ok, one message received",
          (e or {}).get("ok") is True and (e or {}).get("received") == 1, True)
    check("  the radio was tuned and put back",
          r.writes(b0), [("settings", None, {"channel_a": 2, "double_channel": 0}),
                         ("settings", None, {"channel_a": 0, "double_channel": 1})])

    # 7: a packet link still up when Pat returns: the radio waits for it to drop
    b0, p0 = r.mark()
    armed = r.connect("ax25:///N0DAJ?freq=145090", wait_end=False)
    check("7 link held: the session runs", armed and armed.get("via"), "Direwolf")
    wait_for(pat_called(r, p0), 10, 0.05)
    HUB["links"] = [LINK]
    check("  Pat returned", wait_for(pat_returned(r), 15), True)
    time.sleep(2.0)                       # negative check: the radio must not move yet
    check("  link still up: no restore yet, the session not ended",
          (r.writes(b0), r.ended()), ([("settings", None, {"channel_a": 2, "double_channel": 0})], None))
    check("  the add-on shows the radio tuned", (r.uvpro().get("qsy") or {}).get("on"), "channel 3")

    # 10: a page edit while tuned is refused
    n = len(jsonl(r.blog))
    r.page.events.clear()
    r.addon("settings", values={"channel_a": 1})
    check("10 page edit while tuned: refused", wait_for(lambda: has(r.notices("warn"), "using the radio"), 5), True)
    check("  nothing written for it", r.writes(n), [])

    # 11: the add-on can't be switched off while tuned
    r.page.events.clear()
    r.addon("power", on=False)
    check("11 switching off while tuned: refused",
          wait_for(lambda: has(r.notices("warn"), "tuned for a Winlink packet session"), 5), True)
    check("  the add-on stays on", r.uvpro().get("on"), True)

    HUB["links"] = []
    check("7 the link drops: the session ends", r.page.wait(L.ended, 15) is not None, True)
    check("  the radio was put back before the session ended",
          r.writes(b0), [("settings", None, {"channel_a": 2, "double_channel": 0}),
                         ("settings", None, {"channel_a": 0, "double_channel": 1})])
    check("  the add-on shows no tune now", r.uvpro().get("qsy"), None)

    # 8: the link held past restore_wait_s: warned, the record kept, restored first next time
    b0, p0 = r.mark()
    armed = r.connect("ax25:///N0DAJ?freq=145090", wait_end=False)
    wait_for(pat_called(r, p0), 10, 0.05)
    HUB["links"] = [LINK]
    check("8 link held too long: the session ends", r.page.wait(L.ended, 30) is not None, True)
    check("  the page is warned the radio is NOT back", has(r.notices("warn"), "NOT put back on channel 1"), True)
    check("  nothing put back", r.writes(b0), [("settings", None, {"channel_a": 2, "double_channel": 0})])
    check("  the record is kept", (r.uvpro().get("qsy") or {}).get("back"), "channel 1")
    b1, p1 = r.mark()
    armed = r.connect("ax25:///N0DAJ?freq=145090", wait_end=False)
    check("  a new connect with the link still up: refused", armed, None)
    check("  nothing written", r.writes(b1), [])
    HUB["links"] = []
    b1, p1 = r.mark()
    armed = r.connect("ax25:///N0DAJ?freq=145090")
    check("  the link gone: the next session runs", armed and armed.get("via"), "Direwolf")
    check("  the radio went back before it was tuned again",
          r.writes(b1), [("settings", None, {"channel_a": 0, "double_channel": 1}),
                         ("settings", None, {"channel_a": 2, "double_channel": 0}),
                         ("settings", None, {"channel_a": 0, "double_channel": 1})])
    check("  and no record left", r.uvpro().get("qsy"), None)
finally:
    r.stop()

# --- station.py: the same lane, by the hub's name ------------------------------
class StationRun(Run):
    """station.py with a packet hub, Pat and the UV-PRO add-on (no HF radios)."""

    def __init__(self, **uvpro):
        Run.n += 1
        self.blog = os.path.join(TMP, f"bridge-{Run.n}.jsonl")
        self.flog = os.path.join(TMP, f"pat-{Run.n}.jsonl")
        uv = {"enabled": True, "address": "00:11:22:33:44:55", "python": sys.executable,
              "bridge": os.path.join(HERE, "fake_uvpro_bridge.py"),
              "packet_status": f"http://127.0.0.1:{HUB_PORT}/api/status", "calls": ["N9ABC"],
              "restore_wait_s": 3, **uvpro}
        cfg = {"name": "Test Station", "radios": [],
               "packet": {"url": f"http://127.0.0.1:{HUB_PORT}", "agwpe": "127.0.0.1:8000", "name": "UV-50PRO"},
               "winlink": {"pat": testkit.fake_pat_cmd(), "pat_port": L.PATP, "lane_port": L.LANE},
               "addons": {"uvpro": uv}, "addons_state": os.path.join(TMP, f"state-{Run.n}.json")}
        path = os.path.join(TMP, f"station-{Run.n}.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        HUB["links"] = []
        env = dict(os.environ, FAKE_PAT_RIGCTL=str(L.LANE), FAKE_PAT_LOG=self.flog, FAKE_UVPRO_LOG=self.blog,
                   FAKE_UVPRO_CH3="145.09")
        self.srv = subprocess.Popen([sys.executable, "station.py", "--config", path, "--listen", str(L.WEB),
                                     "--rigctl-host", "127.0.0.1", "--rigctl-port", str(L.APPS)],
                                    cwd=L.ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        def up():
            try:
                for port in (L.WEB, L.PATP):
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                return True
            except OSError:
                return False
        if not wait_for(up, 20, 0.1):
            print(L.stop(self.srv))
            raise SystemExit("station did not start")
        time.sleep(2.5)                       # the hub seen up, Pat's event link
        self.page = L.Page()

    def ready(self):
        p = L.Page()
        try:
            uv = (p.hello.get("addons") or {}).get("uvpro") or {}
            return bool((uv.get("status") or {}).get("connected") and uv.get("settings") and uv.get("memories"))
        finally:
            p.close()


r = StationRun(control=f"tcp:127.0.0.1:{CONTROL}")
try:
    check("station: the UV-PRO add-on read the radio", wait_for(r.ready, 20, 0.3), True)
    b0, p0 = r.mark()
    armed = r.connect("ax25:///N0DAJ?freq=145090")
    check("station: the session runs via the hub, by its name", armed and armed.get("via"), "UV-50PRO")
    check("  tuned (channel 3), told", has(r.notices("info"), "UV-50PRO tuned to 145.090 MHz", "channel 3"), True)
    check("  Pat got the URL without freq=", r.pat_urls(p0), ["ax25:///N0DAJ"])
    check("  selected, then put back",
          r.writes(b0), [("settings", None, {"channel_a": 2, "double_channel": 0}),
                         ("settings", None, {"channel_a": 0, "double_channel": 1})])
finally:
    r.stop()

r = StationRun(hub=f"http://127.0.0.1:{HUB_PORT}")       # borrow mode: no "control"
try:
    b0, p0 = r.mark()
    armed = r.connect("ax25:///N0DAJ?freq=145090")
    check("station, borrow mode: refused, saying why",
          (armed, has(r.notices("warn"), "Not connecting", "borrows the radio", "\"control\"")), (None, True))
    check("  nothing written, Pat not asked", (r.writes(b0), r.pat_urls(p0)), ([], []))
finally:
    r.stop()

# --- 14: the server stopping while the link is held ----------------------------
r = Run({"FAKE_PAT_NO_PTT": "1", "FAKE_PAT_TX_S": "2"}, restore_wait_s=60)
try:
    b0, p0 = r.mark()
    armed = r.connect("ax25:///N0DAJ?freq=145090", wait_end=False)
    wait_for(pat_called(r, p0), 10, 0.05)
    HUB["links"] = [LINK]
    check("14 Pat returned with the link held", wait_for(pat_returned(r), 15), True)
    t0 = time.time()
    r.srv.terminate()
    try:
        out = r.srv.communicate(timeout=40)[0]
    except subprocess.TimeoutExpired:
        r.srv.kill()
        out = r.srv.communicate()[0]
    took = time.time() - t0
    check("14 the server exits within 20 s", took < 20, True)
    check("  a WARNING: the radio NOT put back", "WARNING" in out and "NOT put back" in out, True)
    check("  a WARNING: it may be off its APRS channel", "may be off its APRS channel" in out, True)
    HUB["links"] = []
finally:
    try:
        r.page.close()
    except Exception:   # noqa: BLE001 - the server is gone
        pass
    if r.srv.poll() is None:
        r.srv.kill()

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
