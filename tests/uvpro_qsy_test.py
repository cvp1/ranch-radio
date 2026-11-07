"""The UV-PRO add-on's Winlink QSY (qsy / restore), in process.

UvproAddon is built with a stub host and a stand-in for the bridge (its
_send_bridge answers through _on_bridge as the bridge would, or not at all,
or raises), so each rule is checked without a radio server:
  - the pick: the channel selected now first, then the lowest id; repeater,
    RX-only, AM and DMR channels on the frequency are skipped; 1 Hz tolerance
  - borrow mode ("hub", no "control") refuses, saying why, and writes nothing
  - the scratch channel: refused with a clear reason under Channel edit lock
  - VFO A is not used when it has a name or reads 0.0 (the UV-50PRO)
  - the recovery record exists before the first write
  - "No change." is success both ways (nothing written)
  - a restore with no record is a no-op; a record left over is restored
    before the next tune, and a restore that can't be done refuses the tune
  - _write_wait: no answer -> ok False, no record left, a late done ignored,
    no toast; _send_bridge raising -> ok False, record popped
  - can_stop says why while tuned; page edits are refused while tuned
  - a failed restore is retried until it works; stop() cancels the retry with
    a WARNING
  - a page's own write that gets no answer still says so (unchanged)

    .venv/bin/python tests/uvpro_qsy_test.py
"""
import asyncio
import copy
import json
import logging
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from addons import uvpro  # noqa: E402

logging.getLogger().addHandler(logging.NullHandler())    # the add-on's warnings are expected here
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:66} got {got!r}" + ("" if ok else f" want {want!r}"))


def ch(i, name, rx, tx=None, tone="", tx_disable=False, mod="FM", rx_tone=""):
    return {"id": i, "name": name, "rx_freq": rx, "tx_freq": rx if tx is None else tx, "rx_mod": mod, "tx_mod": mod,
            "rx_sub_audio": rx_tone, "tx_sub_audio": tone, "power": "high", "bandwidth": "WIDE", "scan": True,
            "tx_disable": tx_disable, "talk_around": False, "mute": False,
            "fixed": {"freq": False, "bandwidth": False, "power": False}}


BASE = [ch(0, "2M CALL", 146.52), ch(1, "RPT", 146.94, 146.34, "100.0"), ch(2, "APRS", 144.39),
        ch(3, "NOAA", 162.55, tx_disable=True)]
SETTINGS = {"channel_a": 0, "channel_b": 2, "scan": False, "squelch_level": 3, "ch_data_lock": False,
            "double_channel": 1, "auto_relay_en": False, "vox_en": False}


class Host:
    def __init__(self):
        self.notices, self.sent = [], []

    def notice(self, msg, level="info", ws=None):
        self.notices.append((level, msg))

    def _broadcast(self, payload):
        self.sent.append(json.loads(payload))

    async def _send(self, ws, payload):
        self.sent.append(json.loads(payload))


class FakeHub:
    """The packet hub's hold API as the add-on sees it: records each POST in
    the shared event list (with the radio's writes), answers 200, or a code,
    or raises as an unreachable hub does."""

    def __init__(self, events):
        self.events, self.code, self.up, self.holds = events, 200, True, []

    async def post(self, path, body):
        self.events.append(("hub", path, body.get("by")))
        if not self.up:
            raise ConnectionRefusedError("hub not running")
        if self.code == 200:
            if path == "/api/hold":
                self.holds = [body["by"]]
            elif path == "/api/hold/release":
                self.holds = []
        return self.code, ({} if self.code == 200 else {"error": f"HTTP {self.code}"})


class Radio:
    """The bridge's side: applies a write, sends it back, then done."""

    def __init__(self, a, mode="ok", events=None):
        self.a, self.mode, self.cmds, self.record_at_first = a, mode, [], "unset"
        self.events = events if events is not None else []
        self.fail = set()               # commands answered done ok:false

    async def send(self, d):
        if self.record_at_first == "unset" and d.get("cmd") in ("channel", "settings"):
            self.record_at_first = self.a._qsy is not None
        self.cmds.append(d)
        self.events.append(("radio", d.get("cmd"), d.get("id")))
        if self.mode == "raise":
            raise BrokenPipeError("bridge gone")
        if self.mode == "silent":
            return
        loop = asyncio.get_running_loop()
        loop.call_soon(self.answer, d)

    def answer(self, d):
        a, cmd, v = self.a, d["cmd"], d.get("values") or {}
        if cmd in self.fail:
            a._on_bridge({"ev": "done", "ref": d["ref"], "ok": False, "error": "INVALID_PARAMETER"})
            return
        if cmd == "settings":
            a._on_bridge({"ev": "settings", "values": {**a.settings, **v}})
        elif cmd == "channel":
            vfo = d["id"] in (251, 252)
            pool = a.vfos if vfo else a.channels
            c = {**next(x for x in pool if x["id"] == d["id"]), **v}
            if vfo:
                a._on_bridge({"ev": "vfo", "channels": [c if x["id"] == c["id"] else x for x in a.vfos]})
            else:
                a._on_bridge({"ev": "channel", "channel": c})
        a._on_bridge({"ev": "done", "ref": d["ref"], "ok": True, "error": None})

    def writes(self):
        return [(d["cmd"], d.get("id"), d.get("values")) for d in self.cmds]


def addon(channels=None, settings=None, vfos=None, supports_vfo=False, mode="ok", **opts):
    a = uvpro.UvproAddon(Host(), {"address": "00:11:22:33:44:55", "control": "tcp:127.0.0.1:1", **opts})
    a.connected = True
    a.proc = SimpleNamespace(returncode=None, stdin=SimpleNamespace(is_closing=lambda: False))
    a.channels = copy.deepcopy(channels if channels is not None else BASE)
    a.settings = dict(SETTINGS, **(settings or {}))
    a.vfos = copy.deepcopy(vfos if vfos is not None else [ch(251, "", 0.0), ch(252, "", 0.0)])
    a.device = {"supports_vfo": supports_vfo}
    a.exact = {"channels": [c["id"] for c in a.channels + a.vfos], "settings": True, "beacon": True}
    a.status = {"is_in_tx": False}
    a.events = []
    radio = Radio(a, mode, a.events)
    a._send_bridge = radio.send
    a.fakehub = FakeHub(a.events)
    a.hub_url, a._hold_post = "http://fake-hub.invalid", a.fakehub.post   # never a real hub
    return a, radio


def run(coro):
    return asyncio.run(coro)


# --- the pick ------------------------------------------------------------------

async def pick_prefers_channel_a():
    chans = BASE + [ch(4, "X", 147.0), ch(5, "PKT1", 145.05), ch(6, "Y", 147.1), ch(7, "PKT2", 145.05)]
    a, radio = addon(chans, {"channel_a": 7})
    r = await a.qsy(145_050_000)
    out = [(r.ok, r.how)]
    a2, _ = addon(chans)
    r2 = await a2.qsy(145_050_000)
    out.append((r2.ok, r2.how, r2.back))
    return out, radio.writes()
got, w = run(pick_prefers_channel_a())
check("pick: the channel selected now (channel_a) first", got[0], (True, "channel 8"))
check("  and then only dual watch off is written", w, [("settings", None, {"double_channel": 0})])
check("pick: else the lowest id", got[1], (True, "channel 6", "channel 1"))


async def pick_skips():
    chans = BASE + [ch(4, "RPT", 145.05, 144.45), ch(5, "RXONLY", 145.05, tx_disable=True),
                    ch(6, "AM", 145.05, mod="AM"), ch(7, "DMR", 145.05, mod="DMR")]
    a, radio = addon(chans)
    r = await a.qsy(145_050_000)
    b, _ = addon(chans + [ch(8, "OK", 145.05)])
    r2 = await b.qsy(145_050_000)
    return (r.ok, r.why, radio.writes()), (r2.ok, r2.how)
got, got2 = run(pick_skips())
check("pick: repeater, RX only, AM, DMR skipped (no VFO, no scratch: refused)",
      got, (False, "no FM simplex channel on 145.050 MHz and no qsy_channel set", []))
check("  an FM simplex channel among them is used", got2, (True, "channel 9"))


async def pick_rx_tone_last():
    a, _ = addon(BASE + [ch(4, "TONED", 145.05, rx_tone="100.0"), ch(5, "X", 147.0), ch(6, "PLAIN", 145.05)])
    return (await a.qsy(145_050_000)).how
check("pick: one without an RX tone ahead of one with", run(pick_rx_tone_last()), "channel 7")


async def tolerance():
    a, _ = addon(BASE + [ch(4, "NEAR", 145.0500004)])
    b, _ = addon(BASE + [ch(4, "OFF", 145.050002)])
    return (await a.qsy(145_050_000)).ok, (await b.qsy(145_050_000)).ok
check("pick: within 1 Hz matches, 2 Hz off does not", run(tolerance()), (True, False))


# --- refusals ------------------------------------------------------------------

async def borrow():
    a = uvpro.UvproAddon(Host(), {"address": "00:11:22:33:44:55", "hub": "http://127.0.0.1:1"})
    sent = []

    async def rec(d):
        sent.append(d)
    a._send_bridge = rec
    r = await a.qsy(144_390_000)
    return r.ok, "borrows the radio" in (r.why or "") and "control" in (r.why or ""), sent, a._qsy
check("borrow mode: refused (borrowing drops the packet link), nothing sent", run(borrow()), (False, True, [], None))


async def locked():
    a, radio = addon(settings={"ch_data_lock": True}, qsy_channel=4)
    r = await a.qsy(145_050_000)
    return r.ok, r.why, radio.writes()
check("scratch under Channel edit lock: refused, saying so", run(locked()),
      (False, "no FM simplex channel on 145.050 MHz, and qsy_channel 4 can't be used: the radio's Channel "
              "edit lock is on", []))


async def vfo_unusable():
    named, _ = addon(vfos=[ch(251, "MYVFO", 146.52), ch(252, "", 446.0)], supports_vfo=True)
    zero, _ = addon(vfos=[ch(251, "", 0.0), ch(252, "", 0.0)], supports_vfo=True)
    good, gr = addon(vfos=[ch(251, "", 146.52), ch(252, "", 446.0)], supports_vfo=True)
    r1, r2, r3 = await named.qsy(145_050_000), await zero.qsy(145_050_000), await good.qsy(145_050_000)
    return (r1.ok, r2.ok, (r3.ok, r3.how, gr.writes()[0][:2]))
check("VFO A named or reading 0.0: not used; a plain one is",
      run(vfo_unusable()), (False, False, (True, "VFO A", ("channel", 251))))


async def transmitting():
    a, radio = addon()
    a.status = {"is_in_tx": True}
    r = await a.qsy(144_390_000)
    return r.ok, "transmitting" in (r.why or ""), radio.writes()
check("transmitting: refused, nothing written", run(transmitting()), (False, True, []))


# --- the record, "No change.", restore ------------------------------------------

async def record_first():
    a, radio = addon(qsy_channel=4)
    r = await a.qsy(145_050_000)
    return r.ok, radio.record_at_first, [c for c, _, _ in radio.writes()]
check("the recovery record exists before the first write", run(record_first()),
      (True, True, ["channel", "settings"]))


async def no_change():
    a, radio = addon(settings={"channel_a": 2, "double_channel": 0, "scan": False})
    r = await a.qsy(144_390_000)
    tuned = a._qsy is not None
    done = await a.restore()
    return r.ok, r.how, tuned, done, radio.writes(), a._qsy
check("\"No change.\" both ways: tuned and back, nothing written", run(no_change()),
      (True, "channel 3", True, (True, None), [], None))


async def restore_noop():
    a, radio = addon()
    return await a.restore(), radio.writes()
check("restore with no record: a no-op", run(restore_noop()), ((True, None), []))


async def restore_first():
    a, radio = addon(qsy_channel=4)
    await a.qsy(145_050_000)                       # left tuned: no restore
    n = len(radio.cmds)
    r = await a.qsy(144_390_000)
    return r.ok, [(c, i) for c, i, _ in radio.writes()[n:]]
check("a record left over: put back before the next tune", run(restore_first()),
      (True, [("settings", None), ("channel", 3), ("settings", None)]))


async def restore_first_fails():
    a, radio = addon()
    await a.qsy(144_390_000)
    radio.fail.add("settings")
    r = await a.qsy(144_390_000)
    if a._restore_task:
        a._restore_task.cancel()
    return r.ok, (r.why or "").startswith("the radio is still tuned from the last session and could not be put back")
check("  a restore that can't be done refuses the tune", run(restore_first_fails()), (False, True))


async def can_stop():
    a, _ = addon()
    before = a.can_stop()
    await a.qsy(144_390_000)
    tuned = a.can_stop()
    await a.restore()
    return before, bool(tuned and "Winlink" in tuned), a.can_stop()
check("can_stop: a reason while tuned, none otherwise", run(can_stop()), (None, True, None))


async def page_edit_refused():
    a, radio = addon()
    await a.qsy(144_390_000)
    n = len(radio.cmds)
    a.host.notices.clear()
    await a.command(None, "settings", {"values": {"channel_a": 1}})
    await a.command(None, "channel", {"id": 0, "values": {"Name": "X"}})
    await a.command(None, "volume", {"values": {"level": 3}})       # not the channel: allowed
    return ([m for lvl, m in a.host.notices if lvl == "warn"], [c for c, _, _ in radio.writes()[n:]])
got = run(page_edit_refused())
check("page edits while tuned: settings and channel refused", got[0], [uvpro.TUNED_REFUSAL] * 2)
check("  volume still goes through", got[1], ["volume"])


# --- _write_wait and the bridge's answers ----------------------------------------

async def write_timeout():
    uvpro.WRITE_TIMEOUT_S, saved = 0.3, uvpro.WRITE_TIMEOUT_S
    try:
        a, radio = addon(mode="silent")
        res = await a._write_wait("settings", {"scan": True}, None, "settings")
        ref = radio.cmds[-1]["ref"]
        a._on_bridge({"ev": "done", "ref": ref, "ok": True, "error": None})       # late
        await asyncio.sleep(0)
        return res, dict(a._writes), a.host.notices
    finally:
        uvpro.WRITE_TIMEOUT_S = saved
check("_write_wait: no answer -> failed, no record, late done ignored, no toast", run(write_timeout()),
      ({"ok": False, "error": "no answer from the radio"}, {}, []))


async def send_raises():
    a, radio = addon(mode="raise")
    res = await a._write_wait("settings", {"scan": True}, None, "settings")
    return res["ok"], "BrokenPipeError" in res["error"], dict(a._writes), a.host.notices
check("_send_bridge raising: failed, record popped, no toast", run(send_raises()), (False, True, {}, []))


async def page_quiet_timeout():
    uvpro.WRITE_TIMEOUT_S, saved = 0.3, uvpro.WRITE_TIMEOUT_S
    try:
        a, _ = addon(mode="silent")
        a.extras = {"volume": 8}
        await a.command(None, "volume", {"values": {"level": 3}})
        await asyncio.sleep(0.6)
        return [lvl for lvl, m in a.host.notices if "no answer" in m]
    finally:
        uvpro.WRITE_TIMEOUT_S = saved
check("a page's own write with no answer still warns (unchanged)", run(page_quiet_timeout()), ["warn"])


async def partial_failure():
    a, radio = addon(qsy_channel=4)
    before = copy.deepcopy(next(c for c in a.channels if c["id"] == 3))
    radio.fail.add("settings")
    r = await a.qsy(145_050_000)
    radio.fail.clear()
    after = next(c for c in a.channels if c["id"] == 3)
    return r.ok, [(c, i) for c, i, _ in radio.writes()], a._qsy, after == before
check("select fails after the scratch write: written back, refused, no record",
      run(partial_failure()), (False, [("channel", 3), ("settings", None), ("channel", 3)], None, True))


# --- the deferred retry -----------------------------------------------------------

class Grab(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, rec):
        self.lines.append((rec.levelname, rec.getMessage()))


async def retry():
    saved = uvpro.QSY_RETRY_EVERY_S, uvpro.QSY_POLL_S
    uvpro.QSY_RETRY_EVERY_S, uvpro.QSY_POLL_S = 0.2, 0.05
    try:
        a, radio = addon(restore_wait_s=0.2)
        await a.qsy(144_390_000)
        a.status = {"is_in_tx": True}          # the radio busy: the restore times out
        first = await a.restore()
        retrying = a._restore_task is not None and not a._restore_task.done()
        a.status = {"is_in_tx": False}
        for _ in range(100):
            if a._qsy is None:
                break
            await asyncio.sleep(0.05)
        back = a._qsy is None
        # stopped while a retry runs: cancelled, with a WARNING
        await a.qsy(144_390_000)
        a.status = {"is_in_tx": True}
        await a.restore()
        g = Grab()
        uvpro.log.addHandler(g)
        try:
            a.proc = None                      # no bridge process to close here
            await a.stop()
        finally:
            uvpro.log.removeHandler(g)
        return (first[0], retrying, back, a._restore_task,
                any(lvl == "WARNING" and "may be off its APRS channel" in m for lvl, m in g.lines))
    finally:
        uvpro.QSY_RETRY_EVERY_S, uvpro.QSY_POLL_S = saved
check("a failed restore: retried until it works; stop() cancels it, warning",
      run(retry()), (False, True, True, None, True))


# --- B-10: the record outlives the process (plan winlink-listen, F16) -------------

async def record_on_disk_first():
    import tempfile
    a, radio = addon(qsy_channel=4)
    a.state_dir = tempfile.mkdtemp(prefix="uvqsy-")
    path = os.path.join(a.state_dir, "uvpro-qsy.json")
    seen = []
    send = radio.send

    async def spy(d):
        if d.get("cmd") in ("channel", "settings") and not seen:
            seen.append(os.path.exists(path))
        await send(d)
    a._send_bridge = spy
    r = await a.qsy(145_050_000)
    on_disk = os.path.exists(path)
    done = await a.restore()
    return r.ok, seen, on_disk, done, os.path.exists(path)
check("B-10 the record is on disk before the first write, gone once back", run(record_on_disk_first()),
      (True, [True], True, (True, None), False))


async def new_instance_restores():
    """A server that dies mid-QSY (no restore, its bridge gone with it); a new
    add-on instance, reading the record, puts the radio back (fake bridge
    with a radio that keeps its state)."""
    import tempfile
    d = tempfile.mkdtemp(prefix="uvqsy-")
    radio_file = os.path.join(d, "radio.json")
    os.environ.update(FAKE_UVPRO_STATE=radio_file, FAKE_UVPRO_CH3="145.09")
    opts = {"address": "00:11:22:33:44:55", "control": "tcp:127.0.0.1:1", "python": sys.executable,
            "bridge": os.path.join(ROOT, "tests", "fake_uvpro_bridge.py"), "restore_wait_s": 5}

    def make():
        a = uvpro.UvproAddon(Host(), opts)
        a.state_dir = d
        a.events = []
        a.fakehub = FakeHub(a.events)
        a.hub_url, a._hold_post = "http://fake-hub.invalid", a.fakehub.post
        return a

    def radio():
        try:
            with open(radio_file) as f:
                st = json.load(f)["settings"]
            return st["channel_a"], st["double_channel"]
        except (OSError, ValueError, KeyError):
            return None

    async def until(pred, t=20):
        for _ in range(int(t / 0.05)):
            if pred():
                return True
            await asyncio.sleep(0.05)
        return pred()
    out = {}
    a1 = make()
    await a1.start()
    await until(lambda: a1._bridge_up() and a1.settings is not None and a1.channels)
    r = await a1.qsy(145_090_000)
    out["tuned"] = (r.ok, radio())
    # The crash: the supervisor and bridge die with the process, no restore().
    a1._closing = True
    a1._task.cancel()
    a1.proc.kill()
    await a1.proc.wait()
    a2 = make()
    await a2.start()
    out["restored"] = await until(lambda: radio() == (0, 1)) and radio()
    out["record gone"] = await until(lambda: not os.path.exists(os.path.join(d, "uvpro-qsy.json")), 5)
    out["hold"] = [(p, b) for _, p, b in a2.events]
    await a2.stop()
    for k in ("FAKE_UVPRO_STATE", "FAKE_UVPRO_CH3"):
        os.environ.pop(k, None)
    return out
r = run(new_instance_restores())
check("B-10 precondition: tuned to channel 3, dual watch off", r.get("tuned"), (True, (2, 0)))
check("B-10 a new add-on instance puts the radio back (channel 1, dual watch A)", r.get("restored"), (0, 1))
check("B-10   and its record is gone", r.get("record gone"), True)
check("B-10   the new instance re-asserts the hold before restoring, releases after",
      r.get("hold"), [("/api/hold", "winlink-qsy"), ("/api/hold/release", "winlink-qsy")])


# --- the off-channel hold (APRS messaging plan 2026-11-07, 3.2) -------------------

async def hold_before_first_write():
    a, radio = addon(qsy_channel=4)
    r = await a.qsy(145_050_000)
    first_radio = next(i for i, e in enumerate(a.events) if e[0] == "radio")
    holds = [i for i, e in enumerate(a.events) if e[:2] == ("hub", "/api/hold")]
    return r.ok, bool(holds) and holds[0] < first_radio, a.fakehub.holds
check("qsy: the hold is set (and answered) before the first channel write", run(hold_before_first_write()),
      (True, True, ["winlink-qsy"]))


async def hold_hub_unreachable():
    a, radio = addon(qsy_channel=4)
    a.fakehub.up = False
    r = await a.qsy(145_050_000)
    return r.ok, (r.why or "").startswith("APRS hold not set: the packet hub did not answer"), radio.writes(), a._qsy
check("hub unreachable: qsy refuses, the radio is not written, no record", run(hold_hub_unreachable()),
      (False, True, [], None))


async def hold_hub_refuses():
    a, radio = addon(qsy_channel=4)
    a.fakehub.code = 500
    r = await a.qsy(145_050_000)
    return r.ok, "HTTP 500" in (r.why or ""), radio.writes()
check("hub answers 500 (hold not saved): qsy refuses, nothing written", run(hold_hub_refuses()), (False, True, []))


async def hold_no_hub_named():
    a = uvpro.UvproAddon(Host(), {"address": "00:11:22:33:44:55", "control": "tcp:127.0.0.1:1"})
    a.connected, a.proc = True, SimpleNamespace(returncode=None, stdin=SimpleNamespace(is_closing=lambda: False))
    a.channels, a.settings, a.status = copy.deepcopy(BASE), dict(SETTINGS), {"is_in_tx": False}
    a.exact = {"channels": [c["id"] for c in a.channels], "settings": True, "beacon": True}
    sent = []

    async def rec(d):
        sent.append(d)
    a._send_bridge = rec
    r = await a.qsy(144_390_000)
    return r.ok, "no packet hub is named" in (r.why or ""), sent
check("no hub named (no \"hub\", no packet_status): refused, nothing sent", run(hold_no_hub_named()),
      (False, True, []))
check("the hub's URL comes from \"hub\", else from packet_status",
      (uvpro.UvproAddon(Host(), {"address": "x", "control": "tcp:127.0.0.1:1", "hub": "http://h:1"}).hub_url,
       uvpro.UvproAddon(Host(), {"address": "x", "control": "tcp:127.0.0.1:1",
                                 "packet_status": "http://h:2/api/status"}).hub_url),
      ("http://h:1", "http://h:2"))


async def hold_released_after_restore():
    a, radio = addon(qsy_channel=4)
    await a.qsy(145_050_000)
    held = list(a.fakehub.holds)
    n = len(a.events)
    done = await a.restore()
    after = a.events[n:]
    rel = [i for i, e in enumerate(after) if e[:2] == ("hub", "/api/hold/release")]
    last_radio = max(i for i, e in enumerate(after) if e[0] == "radio")
    return held, done, bool(rel) and rel[0] > last_radio, a.fakehub.holds
check("restore: released only after the restore's writes", run(hold_released_after_restore()),
      (["winlink-qsy"], (True, None), True, []))


async def hold_kept_on_give_up():
    saved = uvpro.QSY_RETRY_EVERY_S, uvpro.QSY_POLL_S, uvpro.QSY_RETRY_MAX_S
    uvpro.QSY_RETRY_EVERY_S, uvpro.QSY_POLL_S, uvpro.QSY_RETRY_MAX_S = 0.05, 0.02, 0.3
    try:
        a, radio = addon(restore_wait_s=0.05)
        await a.qsy(144_390_000)
        radio.fail.add("settings")             # every restore fails
        await a.restore()
        for _ in range(100):
            if a._gave_up:
                break
            await asyncio.sleep(0.05)
        return (a._gave_up, a.fakehub.holds, [e for e in a.events if e[:2] == ("hub", "/api/hold/release")],
                any("keeps holding APRS" in m for lvl, m in a.host.notices if lvl == "warn"))
    finally:
        uvpro.QSY_RETRY_EVERY_S, uvpro.QSY_POLL_S, uvpro.QSY_RETRY_MAX_S = saved
check("a restore given up after QSY_RETRY_MAX_S keeps the hold, and says so", run(hold_kept_on_give_up()),
      (True, ["winlink-qsy"], [], True))


async def hold_reasserted_from_record():
    import tempfile
    a, radio = addon()
    a.state_dir = tempfile.mkdtemp(prefix="uvqsy-")
    await a.qsy(144_390_000)                   # tuned, record on disk; then "the add-on restarts"
    b, radio_b = addon()
    b.state_dir = a.state_dir
    b.settings = dict(a.settings)
    loaded = b._load_qsy()
    b._read.set()
    await b._recover()
    first_hold = next(i for i, e in enumerate(b.events) if e[:2] == ("hub", "/api/hold"))
    first_radio = next(i for i, e in enumerate(b.events) if e[0] == "radio")
    rel = next(i for i, e in enumerate(b.events) if e[:2] == ("hub", "/api/hold/release"))
    return loaded, first_hold < first_radio < rel, b._qsy
check("add-on restart with a record: hold re-asserted, then the restore, then the release",
      run(hold_reasserted_from_record()), (True, True, None))


print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
