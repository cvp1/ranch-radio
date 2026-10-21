"""The add-on registry and the UV-PRO add-on, against tests/fake_uvpro_bridge.py.

Runs server.py --sim --addons with the fake bridge as the radio and a fake
packet hub status, and checks:
  - the page is told the add-on is there and on, and gets the radio's
    device info, channels, settings, beacon, state and battery
  - tuning A/B, renaming a channel and a plain setting reach the radio
  - channels are CHIRP memories (CHIRP's columns and values); Frequency,
    Duplex and Offset become the radio's RX/TX frequency and RX only (CHIRP's
    rules, confirm needed); the radio's own field names are refused; a TX the
    radio rewrites is reported not saved
  - CHIRP tone modes reach the radio as its TX/RX sub-audio; a change that
    alters the TX tone, power or talk-around needs confirm, an RX-only tone
    change does not; Mode changes the bandwidth only; Skip is scan
  - CHIRP values the UV-PRO can't hold are refused (DTCS-R, Skip P, AM on an
    FM channel, a tone or DTCS code not in CHIRP's lists, polarity other than NN)
  - CHIRP CSV export: CHIRP's header and number formats; import: a preview
    that writes nothing, rows that would clear a channel or name a missing
    Location refused, a frequency change shown as on-air, CHIRP's simplex
    Offset default accepted, confirm needed for on-air changes, only changed
    channels written, one summary notice
  - other on-air changes (beacon position) are refused without confirm and
    go through with it
  - schema: unknown field, wrong type, out of range, too long, bad tone,
    a callsign not in "calls" are refused before the bridge sees them
  - no change while the packet hub has a link up, or while the radio transmits
  - the radio refusing a change warns
  - switching the add-on off stops the bridge, refuses commands, and is kept
    across a restart; switching it on connects again
  - the radio going away restarts the bridge; an unreachable radio shows as
    not connected while the radio server runs on
  - a broken add-on (unknown module) is shown as not loaded; the server runs
  - without --addons there are none and an addon command is refused
  - the VFOs (records 251 / 252) are memories too: tuned with confirm, never
    named; A can be pointed at one
  - volume, region (confirm; the new region's channels arrive), FM radio,
    APRS path (checked, confirm) and radio power (off needs confirm) reach
    the bridge; bad values are refused before it
  - the bridge only ever gets the commands it has
  - borrow mode ("hub"): the radio is borrowed from the packet hub to read it
    at start and given back when idle; the page keeps the last read and can
    edit it; a save borrows the radio again (one lend for several saves),
    a refusal on what is held borrows nothing, the hub refusing (a link up)
    is shown, "give_back" ends the borrow at once, a channel the bridge
    doesn't read back byte for byte is not written

    python3 tests/uvpro_test.py
"""
import asyncio
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time

import aiohttp
from aiohttp import web

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = 19000 + random.randrange(1000)
HUB = WEB + 1000
fails = 0
TMP = tempfile.mkdtemp(prefix="uvprotest-")
CMD_LOG = os.path.join(TMP, "commands.jsonl")
STARTS = os.path.join(TMP, "starts")
TX_FLAG = os.path.join(TMP, "tx")
STATE = os.path.join(TMP, "state", "addons.json")
HUB_LINKS = []                  # what the fake hub reports as links
HUB_CALLS = []                  # lend / return requests the fake hub got


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


def write_cfg(name, addons):
    path = os.path.join(TMP, name)
    with open(path, "w") as f:
        json.dump(addons, f)
    return path


def uvpro_cfg(address="00:11:22:33:44:55", **extra):
    return {"uvpro": {"enabled": True, "address": address, "python": sys.executable,
                      "bridge": os.path.join(ROOT, "tests", "fake_uvpro_bridge.py"),
                      "calls": ["N9ABC", "N0CALL"], "packet_status": f"http://127.0.0.1:{HUB}/api/status",
                      **extra}}


# --- a fake packet hub: /api/status with HUB_LINKS

def run_hub():
    async def status(_):
        return web.json_response({"links": list(HUB_LINKS)})

    async def lend(req):            # as tnc_hub.py: refused while a link is up
        body = await req.json()
        if HUB_LINKS:
            HUB_CALLS.append("lend refused")
            return web.json_response({"error": f"a packet link is up ({HUB_LINKS[0]['remote']})"}, status=409)
        HUB_CALLS.append(f"lend {body.get('seconds')} to {body.get('to')}")
        return web.json_response({"lent": {"to": body.get("to")}})

    async def give_back(_):
        HUB_CALLS.append("return")
        return web.json_response({"lent": None})
    app = web.Application()
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/lend", lend)
    app.router.add_post("/api/return", give_back)
    loop = asyncio.new_event_loop()
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", HUB).start())
    loop.run_forever()


threading.Thread(target=run_hub, daemon=True).start()


class Page:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.events = []
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.run(self._open())

    def run(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _open(self):
        self.sess = aiohttp.ClientSession()
        self.ws = await self.sess.ws_connect(f"http://127.0.0.1:{WEB}/ws")
        self.hello = json.loads((await self.ws.receive()).data)
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                m = json.loads(msg.data)
                if m.get("t") in ("addon", "notice"):
                    self.events.append(m)

    def send(self, m):
        self.run(self.ws.send_str(json.dumps(m)))

    def do(self, verb, **extra):
        self.send({"t": "addon", "addon": "uvpro", "do": verb, **extra})

    def wait(self, pred, timeout=10, after=0):
        end = time.time() + timeout
        while time.time() < end:
            for e in list(self.events[after:]):
                if pred(e):
                    return e
            time.sleep(0.05)
        return None

    def mark(self):
        return len(self.events)

    def notice_after(self, n, level=None, timeout=3):
        end = time.time() + timeout
        while time.time() < end:
            for e in self.events[n:]:
                if e.get("t") == "notice" and (level is None or e.get("level") == level):
                    return e["msg"]
            time.sleep(0.05)
        return None

    def close(self):
        async def _c():
            await self.ws.close()
            await self.sess.close()
        self.run(_c())


def start(*extra):
    env = dict(os.environ, FAKE_UVPRO_LOG=CMD_LOG, FAKE_UVPRO_STARTS=STARTS, FAKE_UVPRO_TX=TX_FLAG)
    args = [sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB),
            "--rigctl-port", "0", "--addons-state", STATE, *extra]
    p = subprocess.Popen(args, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", WEB), timeout=0.2).close()
            return p
        except OSError:
            time.sleep(0.1)
    p.kill()
    print(p.stdout.read())
    raise SystemExit("server did not start")


def stop(p):
    p.terminate()
    try:
        out, _ = p.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
    return out


def commands():
    try:
        with open(CMD_LOG) as f:
            return [json.loads(l) for l in f if l.strip()]
    except OSError:
        return []


def starts():
    try:
        with open(STARTS) as f:
            return len(f.read().split())
    except OSError:
        return 0


def connected_page():
    """A page once the bridge has reported everything."""
    pg = Page()
    snap = (pg.hello.get("addons") or {}).get("uvpro") or {}
    if not (snap.get("status") or {}).get("connected") or not snap.get("channels"):
        pg.wait(lambda e: e.get("addon") == "uvpro" and "battery" in e)
    return pg


def refused(pg, verb, why_part, **m):
    n_cmd, n = len(commands()), pg.mark()
    pg.do(verb, **m)
    msg = pg.notice_after(n, "warn")
    time.sleep(0.2)
    return bool(msg and why_part in msg) and len(commands()) == n_cmd, msg


def accepted(pg, verb, **m):
    n = pg.mark()
    pg.do(verb, **m)
    msg = pg.notice_after(n, timeout=5)
    return bool(msg and "saved" in msg and "not saved" not in msg), msg


cfg = write_cfg("addons.json", uvpro_cfg())
server = start("--addons", cfg)
try:
    pg = connected_page()
    desc = pg.hello["config"]["addons"]
    check("config lists the add-on, on", [(d["name"], d["on"], d["error"]) for d in desc], [("uvpro", True, None)])
    time.sleep(0.5)
    pg2 = Page()                     # a page opened after the bridge reported: all in hello
    snap = pg2.hello["addons"]["uvpro"]
    check("hello: connected", snap["status"]["connected"], True)
    check("hello: four channels as CHIRP memories", [(c["Location"], c["Name"]) for c in snap["memories"]],
          [("1", "2M CALL"), ("2", "RPT"), ("3", "APRS"), ("4", "NOAA")])
    rpt = snap["memories"][1]
    check("RPT in CHIRP terms", (rpt["Frequency"], rpt["Duplex"], rpt["Offset"], rpt["Tone"], rpt["rToneFreq"],
                                 rpt["Mode"], rpt["Power"], rpt["Skip"]),
          ("146.940000", "-", "0.600000", "Tone", "100.0", "FM", "High", ""))
    check("NOAA (RX only) is Duplex off", (snap["memories"][3]["Duplex"], snap["memories"][3]["Offset"]),
          ("off", "0.000000"))
    check("hello: settings, beacon, state, battery",
          (snap["settings"]["channel_a"], snap["beacon"]["aprs_callsign"], snap["state"]["is_gps_locked"],
           snap["battery"]["percent"]), (0, "N9ABC", True, 87))
    check("hello: schema has channel/settings/beacon", sorted(snap["schema"]), ["beacon", "channel", "settings"])
    pg2.close()

    # --- plain changes
    ok, msg = accepted(pg, "settings", values={"channel_a": 1})
    check("tune A to channel 2: saved", ok, True)
    check("tune A: reached the radio", commands()[-1], {"cmd": "settings", "ref": commands()[-1]["ref"],
                                                       "values": {"channel_a": 1}})
    ok, _ = accepted(pg, "channel", id=0, values={"Name": "SIMPLEX"})
    check("rename channel 1 (no confirm needed)", ok, True)
    check("  the radio got its name field", commands()[-1]["values"], {"name": "SIMPLEX"})
    e = pg.wait(lambda e: (e.get("memory") or {}).get("Name") == "SIMPLEX")
    check("page gets the renamed channel", bool(e), True)
    ok, _ = accepted(pg, "settings", values={"squelch_level": 5})
    check("squelch 5: saved", ok, True)
    ok, msg = refused(pg, "settings", "No change", values={"squelch_level": 5})
    check("same value again: nothing sent", ok, True)

    # --- on-air changes need confirm
    ok, msg = refused(pg, "channel", "Needs confirming", id=0, values={"Power": "Low"})
    check("power without confirm: refused", ok, True)
    ok, _ = accepted(pg, "channel", id=0, values={"Power": "Low"}, confirm=True)
    check("power with confirm: saved", ok, True)
    check("  as the radio's power", commands()[-1]["values"], {"power": "low"})

    # --- CHIRP tones, mode, skip
    ok, _ = accepted(pg, "channel", id=1, values={"Tone": "TSQL", "cToneFreq": "100.0"})
    check("Tone -> TSQL same tone: RX tone only, no confirm", ok, True)
    check("  radio got rx_sub_audio only", commands()[-1]["values"], {"rx_sub_audio": "100.0"})
    ok, msg = refused(pg, "channel", "Needs confirming", id=1, values={"cToneFreq": "103.5"})
    check("TSQL tone change (TX tone too) without confirm: refused", ok, True)
    ok, _ = accepted(pg, "channel", id=1, values={"cToneFreq": "103.5"}, confirm=True)
    check("  with confirm: both tones", commands()[-1]["values"], {"tx_sub_audio": "103.5", "rx_sub_audio": "103.5"})
    ok, _ = accepted(pg, "channel", id=2, values={"Tone": "Cross", "CrossMode": "->DTCS", "RxDtcsCode": "23"})
    check("Cross ->DTCS (\"23\" read as 023): RX DCS only", ok and commands()[-1]["values"], {"rx_sub_audio": "D023"})
    ok, _ = accepted(pg, "channel", id=0, values={"Mode": "NFM", "Skip": "S"})
    check("Mode NFM + Skip S: bandwidth and scan", ok and commands()[-1]["values"], {"bandwidth": "NARROW", "scan": False})
    ok, _ = accepted(pg, "channel", id=0, values={"talk_around": True}, confirm=True)
    check("talk-around (CHIRP extra) with confirm", ok and commands()[-1]["values"], {"talk_around": True})
    ok, msg = refused(pg, "beacon", "Needs confirming", values={"should_share_location": True})
    check("beacon position on without confirm: refused", ok, True)
    ok, _ = accepted(pg, "beacon", values={"aprs_callsign": "n0call"}, confirm=True)
    check("beacon callsign N0CALL (lower case in) with confirm: saved", ok, True)
    check("callsign upper-cased for the radio", commands()[-1]["values"], {"aprs_callsign": "N0CALL"})

    # --- frequencies: CHIRP's Frequency / Duplex / Offset, on-air
    ok, msg = refused(pg, "channel", "Needs confirming", id=1, values={"Frequency": "146.400000"})
    check("Frequency without confirm: refused", ok, True)
    for label, cid, vals, want in [
        ("Frequency (Duplex - kept: TX follows)", 1, {"Frequency": "146.400000"}, {"rx_freq": 146.4, "tx_freq": 145.8}),
        ("Duplex +", 1, {"Duplex": "+"}, {"tx_freq": 147.0}),
        ("Offset 0.5", 1, {"Offset": "0.500000"}, {"tx_freq": 146.9}),
        ("split", 1, {"Duplex": "split", "Offset": "147.000000"}, {"tx_freq": 147.0}),
        ("back to the repeater", 1, {"Frequency": "146.94", "Duplex": "-", "Offset": "0.6"},
         {"rx_freq": 146.94, "tx_freq": 146.34}),
        ("RX-only channel to simplex", 3, {"Duplex": ""}, {"tx_disable": False}),
        ("and back to RX only", 3, {"Duplex": "off"}, {"tx_disable": True}),
    ]:
        ok, _ = accepted(pg, "channel", id=cid, values=vals, confirm=True)
        got = {k: round(v, 6) if isinstance(v, float) else v for k, v in commands()[-1]["values"].items()}
        check(f"frequency: {label}", ok and got, want)
    ok, _ = accepted(pg, "channel", id=3, values={"Name": "WX"})
    check("RX-only channel renamed", ok, True)
    for label, vals, why in [("Frequency 5 MHz", {"Frequency": "5.0"}, "outside what the UV-PRO tunes"),
                             ("TX below the radio (split 2 MHz)", {"Duplex": "split", "Offset": "2.0"}, "outside"),
                             ("Frequency not a number", {"Frequency": "abc"}, "not a frequency"),
                             ("Duplex x", {"Duplex": "x"}, "CHIRP's are"),
                             ("the radio's tx_freq field", {"tx_freq": 146.40}, "No CHIRP field")]:
        ok, msg = refused(pg, "channel", why, id=1, values=vals, confirm=True)
        check(f"frequency refused: {label}", ok, True)
    n = pg.mark()
    pg.do("channel", id=0, values={"Duplex": "split", "Offset": "99.999"}, confirm=True)
    msg = pg.notice_after(n, timeout=5)
    check("a TX the radio rewrites: not saved, says so", bool(msg and "not saved" in msg and "rewrote" in msg), True)
    accepted(pg, "channel", id=0, values={"Duplex": "", "Offset": "0"}, confirm=True)

    # --- the VFOs, as memories
    snap = Page().hello["addons"]["uvpro"]
    check("VFOs: A and B, as memories", [(v["Location"], v["Frequency"], v["vfo"]) for v in snap["vfos"]],
          [("VFO A", "146.520000", True), ("VFO B", "446.000000", True)])
    ok, msg = refused(pg, "channel", "Needs confirming", id=251, values={"Frequency": "147.42"})
    check("VFO A tune without confirm: refused", ok, True)
    ok, _ = accepted(pg, "channel", id=251, values={"Frequency": "147.42"}, confirm=True)
    check("VFO A tuned to 147.42 (TX follows, simplex)",
          ok and {k: round(v, 6) for k, v in commands()[-1]["values"].items()}, {"rx_freq": 147.42, "tx_freq": 147.42})
    check("  bridge got record 251", commands()[-1]["id"], 251)
    ok, msg = refused(pg, "channel", "has no name", id=252, values={"Name": "X"})
    check("VFO: never named", ok, True)
    ok, _ = accepted(pg, "settings", values={"channel_a": 251})
    check("A pointed at VFO A", ok, True)
    accepted(pg, "settings", values={"channel_a": 1})

    # --- volume, region, FM, APRS path, radio power
    ok, msg = refused(pg, "volume", "0 to 15", values={"level": 16})
    check("volume 16: refused", ok, True)
    n = pg.mark()
    pg.do("volume", values={"level": 11})
    e = pg.wait(lambda e: (e.get("extras") or {}).get("volume") == 11, after=n)
    check("volume 11: the radio has it, page told", bool(e), True)
    check("  bridge got it", commands()[-1], {"cmd": "volume", "ref": commands()[-1]["ref"], "values": {"level": 11}})
    ok, msg = refused(pg, "region", "Needs confirming", values={"region": 1})
    check("region switch without confirm: refused", ok, True)
    ok, msg = refused(pg, "region", "regions 1 to 3", values={"region": 3}, confirm=True)
    check("region 4 of 3: refused", ok, True)
    ok, msg = refused(pg, "region", "already", values={"region": 0}, confirm=True)
    check("region it is in: refused", ok, True)
    n = pg.mark()
    ok, _ = accepted(pg, "region", values={"region": 1}, confirm=True)
    e = pg.wait(lambda e: any(c["Name"].startswith("TRA") for c in (e.get("memories") or [])), after=n)
    check("region TRAVEL: saved, the region's channels arrive", (ok, bool(e)), (True, True))
    accepted(pg, "region", values={"region": 0}, confirm=True)
    n = pg.mark()
    pg.do("fm", values={"on": True})
    e = pg.wait(lambda e: ((e.get("extras") or {}).get("fm") or {}).get("on") is True, after=n)
    check("FM on", bool(e), True)
    pg.do("fm", values={"freq": 101.1})
    e = pg.wait(lambda e: ((e.get("extras") or {}).get("fm") or {}).get("freq") == 101.1, after=n)
    check("FM 101.1", bool(e), True)
    pg.do("fm", values={"seek": "up"})
    e = pg.wait(lambda e: ((e.get("extras") or {}).get("fm") or {}).get("freq") == 101.5, after=n)
    check("FM seek up", bool(e), True)
    for label, vals in [("FM 120 MHz", {"freq": 120}), ("FM seek sideways", {"seek": "left"}),
                        ("FM two at once", {"on": True, "freq": 99.0})]:
        ok, msg = refused(pg, "fm", "FM", values=vals)
        check(f"refused: {label}", ok, True)
    pg.do("fm", values={"on": False})
    pg.wait(lambda e: ((e.get("extras") or {}).get("fm") or {}).get("on") is False, after=n)
    ok, msg = refused(pg, "aprs_path", "Needs confirming", values={"path": "WIDE2-1"})
    check("APRS path without confirm: refused", ok, True)
    for label, path in [("lower case, spaces: WIDE1-1, wide2-2", "WIDE1-1,WIDE2-2")]:
        ok, _ = accepted(pg, "aprs_path", values={"path": "wide1-1, wide2-2"}, confirm=True)
        check(f"APRS path {label}", ok and commands()[-1]["values"], {"path": path})
    for label, path in [("SSID 16", "WIDE1-16"), ("9 hops", ",".join(["WIDE1-1"] * 9)), ("call too long", "ABCDEFG")]:
        ok, msg = refused(pg, "aprs_path", "APRS path", values={"path": path}, confirm=True)
        check(f"APRS path refused: {label}", ok, True)
    ok, msg = refused(pg, "radio_power", "Needs confirming", values={"on": False})
    check("radio power off without confirm: refused", ok, True)
    n = pg.mark()
    ok, _ = accepted(pg, "radio_power", values={"on": False}, confirm=True)
    e = pg.wait(lambda e: (e.get("state") or {}).get("is_power_on") is False, after=n)
    check("radio power off: saved, state says off", (ok, bool(e)), (True, True))
    ok, _ = accepted(pg, "radio_power", values={"on": True})
    check("radio power on (no confirm needed)", ok and commands()[-1], {"cmd": "power", "ref": commands()[-1]["ref"],
                                                                        "values": {"on": True}})

    # --- schema
    for label, verb, m, why in [
        ("unknown field", "channel", {"id": 0, "values": {"tx_power_watts": 5}}, "No CHIRP field"),
        ("Tone mode DTCS-R", "channel", {"id": 0, "values": {"Tone": "DTCS-R"}}, "can't hold"),
        ("Skip P", "channel", {"id": 0, "values": {"Skip": "P"}}, "scan on or off"),
        ("Mode AM without confirm", "channel", {"id": 0, "values": {"Mode": "AM"}}, "Needs confirming"),
        ("Mode DV", "channel", {"id": 0, "values": {"Mode": "DV"}, "confirm": True}, "the UV-PRO has"),
        ("dual watch 3", "settings", {"values": {"double_channel": 3}}, "not one of the choices"),
        ("auto relay without confirm", "settings", {"values": {"auto_relay_en": False}}, "Needs confirming"),
        ("VOX without confirm", "settings", {"values": {"vox_en": True}}, "Needs confirming"),
        ("DTCS polarity NR", "channel", {"id": 2, "values": {"DtcsPolarity": "NR"}}, "NN"),
        ("DTCS code 024", "channel", {"id": 2, "values": {"DtcsCode": "024"}}, "CHIRP's DTCS codes"),
        ("Power 5W", "channel", {"id": 0, "values": {"Power": "5W"}, "confirm": True}, "the UV-PRO has"),
        ("bool as string", "settings", {"values": {"scan": "yes"}}, "not on/off"),
        ("squelch 12", "settings", {"values": {"squelch_level": 12}}, "outside 0..9"),
        ("channel A 9 (radio has 4)", "settings", {"values": {"channel_a": 9}}, "no channel 10"),
        ("name 11 bytes", "channel", {"id": 0, "values": {"Name": "ABCDEFGHIJK"}}, "10 characters"),
        ("tone 99.9", "channel", {"id": 0, "values": {"Tone": "Tone", "rToneFreq": "99.9"}, "confirm": True},
         "CHIRP's tones"),
        ("no such channel", "channel", {"id": 7, "values": {"Name": "X"}}, "no channel 8"),
        ("callsign W1AW", "beacon", {"values": {"aprs_callsign": "W1AW"}, "confirm": True}, "not one of"),
        ("interval 25 s (10 s steps)", "beacon", {"values": {"location_share_interval": 25}, "confirm": True},
         "multiple of 10"),
    ]:
        ok, msg = refused(pg, verb, why, **m)
        check(f"refused before the radio: {label}", ok, True)

    # --- CHIRP CSV export
    n = pg.mark()
    pg.do("export")
    e = pg.wait(lambda e: "export" in e, after=n)
    lines = (e or {}).get("export", {}).get("csv", "").splitlines()
    check("export: CHIRP's header", lines[:1], ["Location,Name,Frequency,Duplex,Offset,Tone,rToneFreq,cToneFreq,"
                                                "DtcsCode,DtcsPolarity,RxDtcsCode,CrossMode,Mode,TStep,Skip,Power,"
                                                "Comment,URCALL,RPT1CALL,RPT2CALL,DVCODE"])
    check("export: four rows", len(lines), 5)
    check("export: RPT row", lines[2], "2,RPT,146.940000,-,0.600000,TSQL,88.5,103.5,023,NN,023,Tone->Tone,FM,5.00,,High,,,,,")
    # (TSQL: CHIRP's split_tone_decode sets cToneFreq only; rToneFreq keeps CHIRP's 88.5 default)
    check("export: NOAA row", lines[4].split(",")[:5], ["4", "WX", "162.550000", "off", "0.000000"])

    # --- CHIRP CSV import
    import csv as _csv, io as _io
    rows = list(_csv.reader(_io.StringIO("\n".join(lines))))
    head = rows[0]
    col = {h: i for i, h in enumerate(head)}
    rows[1][col["Name"]] = "CALL"                       # a plain change
    rows[1][col["Offset"]] = "0.600000"                 # CHIRP's default offset on a simplex row: fine
    rows[2][col["Frequency"]] = "146.970000"            # moves RX and TX: a change, on-air
    rows[3][col["Power"]] = "Low"                       # on-air: needs confirm
    extra = list(rows[4]); extra[col["Location"]] = "9"  # no such channel
    blank = [""] * len(head); blank[col["Location"]] = "4"; blank[col["Name"]] = "GONE"   # would clear
    text = "\n".join(",".join(r) for r in rows + [extra, blank]) + "\n"
    n, n_cmd = pg.mark(), len(commands())
    pg.do("import_preview", csv=text)
    e = pg.wait(lambda e: "import_plan" in e, after=n)
    plan = (e or {}).get("import_plan", {})
    st = {r["loc"]: r["status"] for r in plan.get("rows", [])}
    check("  1 CALL: change", st.get("1"), "change")
    check("  2 Frequency moved: change, on-air", (st.get("2"), [r["risky"] for r in plan["rows"] if r["loc"] == "2"]),
          ("change", [["Frequency", "TX frequency"]]))
    check("  3 Power Low: change, on-air", (st.get("3"), [r["risky"] for r in plan["rows"] if r["loc"] == "3"]),
          ("change", [["Power"]]))
    check("  9: refused (no such channel)", [r["status"] for r in plan["rows"] if r["loc"] == "9"], ["refused"])
    check("  blank row 4: refused (would clear)",
          [r["why"][:24] for r in plan["rows"] if r["loc"] == "4" and r["status"] == "refused"],
          ["An empty row would clear"])
    check("  counts", (plan["changes"], plan["refused"], plan["risky"]), (3, 2, True))
    time.sleep(0.3)
    check("  preview wrote nothing", len(commands()), n_cmd)
    ok, msg = refused(pg, "import_apply", "needs confirming", csv=text)
    check("import apply without confirm: refused", ok, True)
    n, n_cmd = pg.mark(), len(commands())
    pg.do("import_apply", csv=text, confirm=True)
    msg = pg.notice_after(n, timeout=6)
    check("import apply: one summary notice", msg, "UV-PRO import: 3 channels saved.")
    wrote = commands()[n_cmd:]
    check("  only the changed channels written", sorted((c["id"], tuple(sorted(c["values"]))) for c in wrote),
          [(0, ("name",)), (1, ("rx_freq", "tx_freq")), (2, ("power",))])
    ok, msg = refused(pg, "import_preview", "", csv="a,b\n1,2\n")
    e = pg.wait(lambda e: (e.get("import_plan") or {}).get("error"), after=n)
    check("not a CHIRP CSV: error", bool(e and "Not a CHIRP CSV" in e["import_plan"]["error"]), True)

    # --- busy
    HUB_LINKS[:] = [{"local": "N9ABC", "remote": "W7XXX-10", "state": "connected"}]
    ok, msg = refused(pg, "settings", "packet link is up", values={"scan": True})
    check("packet link up on the hub: refused", ok, True)
    HUB_LINKS[:] = []
    ok, _ = accepted(pg, "settings", values={"scan": True})
    check("link gone: saved", ok, True)
    open(TX_FLAG, "w").close()
    n = pg.mark()
    pg.do("refresh")
    pg.wait(lambda e: (e.get("state") or {}).get("is_in_tx") is True, after=n)
    ok, msg = refused(pg, "settings", "transmitting", values={"scan": False})
    check("radio transmitting: refused", ok, True)
    check("transmitting, nothing self-keying: no cause named", "Auto relay" in (msg or ""), False)
    os.remove(TX_FLAG)
    n = pg.mark()
    pg.do("refresh")
    pg.wait(lambda e: (e.get("state") or {}).get("is_in_tx") is False, after=n)
    # Auto relay on (2026-10-17): the radio digipeats by itself and reads as
    # transmitting; the refusal names the setting so the fix is obvious.
    ok, _ = accepted(pg, "settings", values={"auto_relay_en": True}, confirm=True)
    check("auto relay on: saved", ok, True)
    open(TX_FLAG, "w").close()
    n = pg.mark()
    pg.do("refresh")
    pg.wait(lambda e: (e.get("state") or {}).get("is_in_tx") is True, after=n)
    ok, msg = refused(pg, "settings", "Auto relay is on", values={"scan": False})
    check("transmitting with auto relay on: refusal names it", ok, True)
    os.remove(TX_FLAG)
    n = pg.mark()
    pg.do("refresh")
    pg.wait(lambda e: (e.get("state") or {}).get("is_in_tx") is False, after=n)
    ok, _ = accepted(pg, "settings", values={"auto_relay_en": False}, confirm=True)
    check("auto relay off: saved", ok, True)
    n = pg.mark()
    pg.do("refresh")
    pg.wait(lambda e: (e.get("state") or {}).get("is_in_tx") is False, after=n)

    # --- the radio refuses
    n = pg.mark()
    pg.do("channel", id=1, values={"Name": "fail"})
    msg = pg.notice_after(n, "warn", timeout=5)
    check("radio refuses: warning says not saved", bool(msg and "not saved" in msg), True)

    # --- off and on
    before = starts()
    n = pg.mark()
    pg.do("power", on=False)
    e = pg.wait(lambda e: e.get("addon") == "uvpro" and e.get("on") is False)
    check("switched off: pages told", bool(e), True)
    ok, msg = refused(pg, "settings", "switched off", values={"scan": False})
    check("switched off: commands refused", ok, True)
    with open(STATE) as f:
        check("state file keeps off", json.load(f), {"uvpro": False})
    pg.close()
    stop(server)
    server = start("--addons", cfg)
    pg = Page()
    check("after restart: still off", pg.hello["config"]["addons"][0]["on"], False)
    time.sleep(1)
    check("after restart: no bridge started", starts(), before)
    pg.do("power", on=True)
    e = pg.wait(lambda e: e.get("addon") == "uvpro" and e.get("on") is True)
    check("switched on: pages told", bool(e), True)
    e = pg.wait(lambda e: (e.get("status") or {}).get("connected") is True)
    check("switched on: connected again", bool(e), True)
    check("switched on: bridge started once more", starts(), before + 1)

    # --- the radio goes away
    pg.wait(lambda e: "battery" in e)
    n = pg.mark()
    pg.do("channel", id=0, values={"Name": "die"})
    e = pg.wait(lambda e: (e.get("status") or {}).get("connected") is False, after=n)
    check("radio gone: disconnected", bool(e), True)
    e = pg.wait(lambda e: (e.get("status") or {}).get("connected") is True, timeout=12, after=n)
    check("radio gone: bridge restarted and reconnected", bool(e), True)
    check("radio gone: starts", starts(), before + 2)

    # --- the bridge got only what it may
    check("bridge commands: only the bridge's",
          sorted({c["cmd"] for c in commands()}),
          ["aprs_path", "beacon", "channel", "fm", "power", "refresh", "region", "settings", "volume"])
    pg.close()
finally:
    out = stop(server)

# --- borrow mode: the packet hub lends the radio (the add-on is on: state file from above)
os.environ["FAKE_UVPRO_INEXACT"] = "2"


def wait_for(pred, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def lends():
    return sum(c.startswith("lend ") for c in HUB_CALLS)


cfg3 = write_cfg("addons3.json", uvpro_cfg(hub=f"http://127.0.0.1:{HUB}", idle_s=1))
HUB_CALLS.clear()
before = starts()
server = start("--addons", cfg3)
try:
    check("borrow: read at start, then given back", wait_for(lambda: HUB_CALLS[-1:] == ["return"]), True)
    check("  one lend of 60 s to uvpro", HUB_CALLS, ["lend 60 to uvpro", "return"])
    check("  one bridge start", starts(), before + 1)
    pg = Page()
    snap = pg.hello["addons"]["uvpro"]
    st = snap["status"]
    check("borrow: page sees the last read, not connected",
          (st["connected"], st["borrow"], st["borrowed"], bool(st["read_at"]), len(snap["memories"])),
          (False, True, False, True, 4))
    check("  and what reads back exactly", snap["exact"],
          {"channels": [0, 1, 3, 251, 252], "settings": True, "beacon": True})

    n_cmd = len(commands())
    ok, msg = refused(pg, "channel", "outside what the UV-PRO tunes", id=0, values={"Frequency": "5.0"}, confirm=True)
    check("borrow: refused on the last read, nothing borrowed", (ok, lends()), (True, 1))
    ok, msg = refused(pg, "channel", "byte for byte", id=2, values={"Name": "X"})
    check("borrow: inexact channel not written, nothing borrowed", (ok, lends()), (True, 1))

    ok, msg = accepted(pg, "channel", id=0, values={"Name": "BORROW"})
    check("borrow: a save borrows the radio and is written", (ok, lends()), (True, 2))
    check("  the radio got the name", commands()[-1]["values"], {"name": "BORROW"})
    ok, msg = accepted(pg, "settings", values={"squelch_level": 4})
    check("  a second save in the same borrow: no new lend", (ok, lends()), (True, 2))
    check("  given back once idle", wait_for(lambda: HUB_CALLS[-1:] == ["return"]), True)
    e = pg.wait(lambda e: (e.get("status") or {}).get("borrowed") is False and e["status"]["connected"] is False)
    check("  page told: with packet again", bool(e), True)
    check("  the renamed channel is in the last read", pg.hello is not None and
          any(m.get("memory", {}).get("Name") == "BORROW" for m in pg.events), True)

    HUB_LINKS[:] = [{"local": "N9ABC", "remote": "W7XXX-10", "state": "connected"}]
    n_start = starts()
    ok, msg = refused(pg, "settings", "packet link is up", values={"squelch_level": 5})
    check("borrow: a packet link up: refused, no bridge started", (ok, starts()), (True, n_start))
    HUB_LINKS[:] = []

    n = pg.mark()
    pg.do("refresh")
    e = pg.wait(lambda e: (e.get("status") or {}).get("connected") is True, after=n)
    check("borrow: Read radio borrows and reads", (bool(e), lends()), (True, 3))
    n_ret = HUB_CALLS.count("return")
    pg.do("give_back")
    check("borrow: give_back returns the radio at once",
          wait_for(lambda: HUB_CALLS.count("return") == n_ret + 1, timeout=0.9), True)
    check("bridge commands in borrow mode: no refresh needed for a read",
          sorted({c["cmd"] for c in commands()[n_cmd:]}), ["channel", "settings"])
    pg.close()
finally:
    stop(server)
del os.environ["FAKE_UVPRO_INEXACT"]

# --- radio unreachable, broken add-on, no add-ons
os.remove(STATE)
cfg2 = write_cfg("addons2.json", {**uvpro_cfg(address="absent"), "nosuch": {"enabled": True}})
server = start("--addons", cfg2)
try:
    pg = Page()
    desc = {d["name"]: d for d in pg.hello["config"]["addons"]}
    check("broken add-on: not loaded, off", (desc["nosuch"]["on"], (desc["nosuch"]["error"] or "")[:10]),
          (False, "not loaded"))
    e = pg.wait(lambda e: (e.get("status") or {}).get("connected") is False and (e.get("status") or {}).get("error"))
    snap = pg.hello["addons"]["uvpro"]
    got = e["status"]["error"] if e else (snap.get("status") or {}).get("error")
    check("unreachable radio: not connected, with why", bool(got and "not found" in got), True)
    check("radio server still runs (hello has radio state)", "state" in pg.hello, True)
    pg.close()
finally:
    stop(server)

server = start()
try:
    pg = Page()
    check("without --addons: none", (pg.hello["config"]["addons"], pg.hello["addons"]), ([], {}))
    n = pg.mark()
    pg.do("settings", values={"scan": True})
    msg = pg.notice_after(n, "warn")
    check("without --addons: addon command refused", bool(msg and "No add-ons" in msg), True)
    pg.close()
finally:
    stop(server)

# --- through the hub's control port ("control"): no borrowing, connected all along
ARGV = os.path.join(TMP, "argv.jsonl")
os.environ["FAKE_UVPRO_ARGV"] = ARGV
cfg4 = write_cfg("addons4.json", uvpro_cfg(hub=f"http://127.0.0.1:{HUB}", control="tcp:127.0.0.1:8002"))
HUB_CALLS.clear()
HUB_LINKS[:] = []
server = start("--addons", cfg4)
try:
    pg = connected_page()
    st = pg.hello["addons"]["uvpro"]["status"] if pg.hello["addons"]["uvpro"]["status"]["connected"] else None
    e = st or (pg.wait(lambda e: (e.get("status") or {}).get("connected") is True) or {}).get("status")
    check("control port: connected, via the hub, not borrowing",
          e and (e["connected"], e["via"], e["borrow"]), (True, "hub", False))
    with open(ARGV) as f:
        argv = json.loads(f.read().splitlines()[-1])
    check("  the bridge is pointed at the control port", "--transport=tcp:127.0.0.1:8002" in argv, True)
    n = pg.mark()
    pg.do("volume", values={"level": 4})
    e = pg.wait(lambda e: (e.get("extras") or {}).get("volume") == 4, after=n)
    check("control port: a change goes through", bool(e), True)
    time.sleep(0.5)
    check("  and nothing was borrowed", HUB_CALLS, [])
    HUB_LINKS[:] = [{"local": "N9ABC", "remote": "W7XXX-10", "state": "connected"}]
    ok, msg = refused(pg, "settings", "packet link is up", values={"scan": True})
    check("  a packet link up on the hub still blocks changes", ok, True)
    HUB_LINKS[:] = []
    pg.close()
finally:
    stop(server)
os.environ.pop("FAKE_UVPRO_ARGV", None)

# --- the same add-on under the station front (station.json "addons")
station_cfg = os.path.join(TMP, "station.json")
with open(station_cfg, "w") as f:
    json.dump({"name": "test", "radios": [], "addons": uvpro_cfg(), "addons_state": STATE}, f)
env = dict(os.environ, FAKE_UVPRO_LOG=CMD_LOG, FAKE_UVPRO_STARTS=STARTS)
st = subprocess.Popen([sys.executable, "station.py", "--config", station_cfg, "--listen", str(WEB),
                       "--rigctl-host", "127.0.0.1", "--rigctl-port", str(WEB + 2000)],
                      cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", WEB), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    pg = connected_page()
    check("station: config lists the add-on", [d["name"] for d in pg.hello["config"]["addons"]], ["uvpro"])
    ok, _ = accepted(pg, "settings", values={"channel_b": 3})
    check("station: a change reaches the radio", ok, True)
    ok, _ = accepted(pg, "channel", id=0, values={"Frequency": "146.55"}, confirm=True)
    check("station: a frequency change reaches the radio", ok, True)
    pg.close()
finally:
    stop(st)

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
