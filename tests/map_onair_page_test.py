"""The Map tab end to end: who you hear, who hears you, and what is on the air.

A real station (station.py, in this process) with the simulated radio
(server.py --sim) behind it, a stand-in JTDX on UDP (tests/fake_jtdx.py) and
a stand-in PSKReporter on HTTP; a real browser on the station page's Map tab.
Nothing reaches the internet (no POTA, no NOAA, no tiles asked of anyone but
OpenStreetMap by the browser, which the checks never wait on) and nothing
transmits: "keyed" is the simulator's front-panel hook and JTDX's Status.

  you hear    decodes with a grid become stations; a station heard on the band
              and reported by PSKReporter on it is two-way; a report on
              another band is not on this band's map
  hears you   PSKReporter's stations at their own locators, with the SNR they
              gave; the list says where they come from and how old
  paths       one great-circle line per station, one line over the Pacific
  on the air  the radio's row follows its own server (frequency, RX, then TX
              when the simulator's panel keys); JTDX's row follows its Status
              (listening, transmitting -> the station called, with the beam);
              JTDX closing reads "not heard from", and a transmitting JTDX
              that goes silent does not stay "transmitting"
  the card    a click shows both directions; Answer sends JTDX one Reply
  the nav     the tab's dot goes red while transmitting

    python3 tests/map_onair_page_test.py          (SHOT=dir keeps screenshots)
"""
import os as _os
_os.environ["RANCH_RADIO_OFFLINE"] = "1"
_os.environ.pop("CC_OBS_TOKENS_FILE", None)

import asyncio  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import types  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
_os.chdir(ROOT)
import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

import fake_jtdx as J  # noqa: E402
import testkit  # noqa: E402
from cdp_browser import Browser  # noqa: E402

MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME = (_os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
          or (MAC_CHROME if _os.path.exists(MAC_CHROME) else None))
if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")
SHOT = _os.environ.get("SHOT")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"), flush=True)


class Bg:
    """An event loop on its own thread: the station, and the stand-in PSKReporter."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coro, timeout=30):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def call(self, fn, *a):
        async def go():
            return fn(*a)
        return self.run(go())


BG = Bg()
tmp = Path(tempfile.mkdtemp(prefix="map-onair-"))
shutil.copy(ROOT / "tests/fixtures/cty-sample.csv", tmp / "cty.csv")     # the cached copy (offline: never fetched)
NOW = int(time.time())


def report(call, loc, hz, snr, ago, country):
    return (f'<receptionReport receiverCallsign="{call}" receiverLocator="{loc}" senderCallsign="N0CALL" senderLocator="FN31pr" '
            f'frequency="{hz}" flowStartSeconds="{NOW - ago}" mode="FT8" receiverDXCC="{country}" sNR="{snr}" />')


# JA1ABC: also decoded below, on 20m: two-way. JK1AKC: hears us only. K6ABC: on 40m, not this band's map.
PSK = {"xml": '<?xml version="1.0"?><receptionReports>' + "".join([
    report("JA1ABC", "PM95vq", 14075548, -17, 240, "Japan"), report("JK1AKC", "QM05ft", 14075532, -9, 400, "Japan"),
    report("JK1AKC", "QM05ft", 14075533, -21, 900, "Japan"), report("K6ABC", "CM87", 7075000, 3, 100, "United States"),
    report("X1X", "", 9999, 0, 10, "")]) + "</receptionReports>", "asked": [], "status": 200}


async def pskr(request):
    PSK["asked"].append(dict(request.query))
    return web.Response(text=PSK["xml"], content_type="text/xml", status=PSK["status"])


async def serve(app, port):
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


psk_port, web_p, radio_web, rig_p, udp_p = (testkit.free_port() for _ in range(5))
psk_app = web.Application()
psk_app.router.add_get("/query", pskr)
BG.run(serve(psk_app, psk_port))

radio = subprocess.Popen([sys.executable, "server.py", "--sim", "--rig", "ic7300", "--no-audio", "--host", "127.0.0.1",
                          "--listen", str(radio_web), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(rig_p)],
                         cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

import station as S  # noqa: E402
from addons import Addons  # noqa: E402
from addons import map as M  # noqa: E402
from addons import propagation as P  # noqa: E402

P.URL_PSK = f"http://127.0.0.1:{psk_port}/query"
M.TICK_S = 0.3                                   # a new PSKReporter answer reaches the page at once
cfg = {"name": "test", "radios": [{"id": "ic7300", "name": "IC-7300", "web": radio_web, "rigctl": rig_p}],
       "addons_state": str(tmp / "addons.json"),
       "logsync": {"call": "N0CALL", "udp": {"bind": "127.0.0.1", "port": udp_p}, "ledger": str(tmp / "l.db"),
                   "timing": {"pull_at_start": False}}}
args = types.SimpleNamespace(token=None, rigctl_port=0, rigctl_host="127.0.0.1", host="127.0.0.1", listen=web_p,
                             https=False, tls_name=[], allow_host=[])


async def station_up():
    st = S.Station(cfg, args)
    st.addons = Addons(st, {"map": {"enabled": True, "pota": False},
                            "propagation": {"enabled": True, "swpc": False, "pskreporter": {"call": "N0CALL"}}},
                       cfg["addons_state"])
    st.make_logsync(cfg["logsync"])
    return st, await serve(S.make_app(st, args), web_p)


st, runner = BG.run(station_up())
jt = J.FakeJtdx()
to = ("127.0.0.1", udp_p)
b = None


def status(**kw):
    jt.send(J.status(dial=14_074_000, de="N0CALL", de_grid="FN31pr", **kw), to)


def shot(name):
    if SHOT and b:
        _os.makedirs(SHOT, exist_ok=True)
        b.screenshot(_os.path.join(SHOT, f"{name}.png"))


try:
    check("station: the radio is found and watched (no Winlink lane needed)",
          testkit.wait_for(lambda: BG.call(lambda: st.rigs_view()[0]["connected"]) is True, 20), True)
    rv = BG.call(st.rigs_view)[0]
    check("  its own server's word: RX on 14.074 USB", (rv["present"], rv["tx"], rv["freq"], rv["mode"]), (True, False, 14_074_000, "USB"))
    check("  PSKReporter was asked once, for our callsign", testkit.wait_for(lambda: len(PSK["asked"]) == 1, 10)
          and PSK["asked"][0].get("senderCallsign"), "N0CALL")

    status(dx_call=None, tx_enabled=False, transmitting=False, decoding=False)
    for msg, snr, df in (("CQ JA1ABC PM95", -12, 1200), ("CQ VK2ABC QF56", -19, 1400), ("N0CALL K1ABC FN42", 2, 1600)):
        jt.send(J.decode(msg=msg, snr=snr, df=df), to)
    check("JTDX: its Status is taken as listening",
          testkit.wait_for(lambda: BG.call(st.logsync.rig_view).get("alive") is True, 5)
          and BG.call(st.logsync.rig_view), {"alive": True, "transmitting": False, "tx_enabled": False, "decoding": False,
                                           "dx_call": "", "dx_grid": "", "mode": "FT8", "dial": 14_074_000, "band": "20m"})

    b = Browser(CHROME, testkit.free_port(), size="1400,900")
    b.goto(f"http://127.0.0.1:{web_p}/#/map")
    got = b.wait("window.rrMap && rrMap.stations().length >= 4 && document.querySelectorAll('.rm-s').length >= 4", 20)
    check("page: the Map tab draws the stations", bool(got), True)
    kinds = json.loads(b.js("JSON.stringify(Object.fromEntries(rrMap.stations().map((r) => [r.call, r.kind])))"))
    check("  heard and reported on 20m: two-way; heard only; reported only; the 40m report is not on 20m's map",
          kinds, {"JA1ABC": "both", "VK2ABC": "rx", "K1ABC": "rx", "JK1AKC": "tx"})
    dom = json.loads(b.js("""JSON.stringify({both: document.querySelectorAll('.rm-s-both .rm-ring').length,
      rx: document.querySelectorAll('.rm-s-rx .rm-d').length, tx: document.querySelectorAll('.rm-s-tx .rm-dia').length,
      paths: document.querySelectorAll('.rm-paths .rm-p').length, me: document.querySelectorAll('.rm-mep').length,
      n: [...document.querySelectorAll('.rm-sum b')].map((x) => x.textContent)})"""))
    check("  a ringed dot, two dots, a diamond; a path each; you; the counts",
          dom, {"both": 1, "rx": 2, "tx": 1, "paths": 4, "me": 1, "n": ["3", "2", "1"]})
    ja = json.loads(b.js("JSON.stringify((() => { const r = rrMap.stations().find((s) => s.call === 'JA1ABC'); "
                         "return {km: Math.round(r.km / 100), lon: Math.round(r.lon), snr: [r.rx.snr, r.tx.snr], newest: r.tx.t}; })())"))
    check("  Japan from FN31: ~10,900 km, drawn west over the Pacific (one line, not two halves); both SNRs",
          (106 <= ja["km"] <= 112, ja["lon"] < -180, ja["snr"]), (True, True, [-12, -17]))
    jk = json.loads(b.js("JSON.stringify(rrMap.stations().find((s) => s.call === 'JK1AKC').tx)"))
    check("  a station's newest report is the one shown", (jk["snr"], NOW - jk["t"]), (-9, 400))
    src = b.js("document.querySelector('.rm-src').textContent")
    check("  the list says where 'hears you' comes from", "PSKReporter reports of N0CALL" in src and "every 10 min" in src, True)
    shot("1-listening")

    # ---- on the air: the radio's own word, and JTDX's
    # A row's words, one space between its parts (they are laid out by CSS, not by spaces).
    WORDS = "((x) => [...x.children].map((c) => c.textContent.replace(/\\s+/g, ' ').trim()).filter(Boolean).join(' '))"

    def hud():
        return b.js(f"[...document.querySelectorAll('.rm-air')].map({WORDS}).join(' | ')")

    check("on the air: the radio is receiving on its frequency; JTDX is listening",
          hud(), "IC-7300 14.074 USB RX | JTDX 20m FT8 listening")
    check("  nothing is drawn as transmitting", b.js("document.querySelectorAll('.rm-txring, .rm-beamp').length"), 0)
    status(dx_call="JA1ABC", dx_grid="PM95", tx_enabled=True, transmitting=True, decoding=False)
    check("JTDX transmits: rings leave you and a beam runs to the station it is calling",
          bool(b.wait("document.querySelectorAll('.rm-txring').length === 3 && document.querySelectorAll('.rm-beamp').length === 1", 8)), True)
    check("  the panel says so, and to whom", (hud().split(" | ")[1], b.js("document.querySelector('.rm-hud-h').textContent"),
                                             b.js("document.querySelector('.rm-beamt').textContent")),
          ("JTDX 20m FT8 transmitting → JA1ABC", "ON THE AIR", "→ JA1ABC"))
    check("  the tab's dot is red", bool(b.wait("!!document.querySelector('a.st-tab[href=\"#/map\"] .st-dot.tx, .st-group-tab .st-dot.tx')", 5)), True)
    shot("2-transmitting")
    status(dx_call="JA1ABC", dx_grid="PM95", tx_enabled=True, transmitting=False, decoding=True)
    check("JTDX's turn ends: no rings, no beam; it will call again",
          (bool(b.wait("document.querySelectorAll('.rm-txring, .rm-beamp').length === 0", 8)), hud().split(" | ")[1]),
          (True, "JTDX 20m FT8 listening · will call JA1ABC · decoding"))

    async def panel(d):
        async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{radio_web}/ws") as ws:
            await ws.receive(timeout=5)
            await ws.send_str(json.dumps({"t": "sim_panel", "d": d}))
            await asyncio.sleep(0.2)

    BG.run(panel({"tx": True, "freq": 14_230_000}))      # the simulator's front panel: nothing is on the air
    check("the radio keys (at its own panel, no app): its row says TX on the new frequency",
          bool(b.wait("document.querySelector('.rm-air.tx') && document.querySelector('.rm-air').textContent.includes('14.230')", 8))
          and hud().split(" | ")[0], "IC-7300 14.230 USB TX")
    check("  and you are drawn transmitting", b.js("document.querySelectorAll('.rm-meg.tx .rm-txring').length"), 3)
    BG.run(panel({"tx": False}))
    check("  unkeyed: RX again", bool(b.wait("!document.querySelector('.rm-air.tx')", 8)) and hud().split(" | ")[0], "IC-7300 14.230 USB RX")

    # ---- a station's card, and answering it
    b.js("document.querySelector('.rm-s[data-call=\"JA1ABC\"]').dispatchEvent(new MouseEvent('click', {bubbles: true}))")
    b.wait("!!document.querySelector('.rm-card:not([hidden]) .rm-dir')", 5)
    card = (b.js(f"[...document.querySelector('.rm-card').children].map({WORDS}).join(' | ')") or "") + " | " + \
        (b.js("document.querySelector('.rm-card-w').textContent") or "")
    check("a click on a station: its card, both directions",
          [w in card for w in ("JA1ABC", "two-way", "Japan", "▼ You hear it -12 dB 20m FT8", "▲ It hears you -17 dB 20m FT8", "(PSKReporter)", "new DXCC Answer CQ")],
          [True] * 7)
    check("  its path is picked out", b.js("document.querySelectorAll('.rm-p.sel').length"), 1)
    shot("3-card")
    jt.poll()
    jt.got.clear()
    b.js("document.querySelector('.rm-card [data-answer]').click()")
    got = []
    testkit.wait_for(lambda: bool(got.extend(jt.poll()) or [d for d, _ in got if J.type_of(d) == 4]), 5)
    check("  Answer: JTDX gets one Reply", len([d for d, _ in got if J.type_of(d) == 4]), 1)
    b.js("document.querySelector('.rm-list [data-tab=\"tx\"]').click()")
    check("the list, 'Hear you': the stations that reported us, farthest first",
          b.js("[...document.querySelectorAll('.rm-row b')].map((x) => x.textContent).join(',')"), "JA1ABC,JK1AKC")

    # ---- a fresh decode pulses at its station and runs in along its path
    jt.send(J.decode(msg="CQ VK2ABC QF56", snr=-15, df=1400, t_ms=12 * 3600 * 1000 + 15000), to)
    check("a fresh decode: a pulse at its station and a dash along its path",
          bool(b.wait("document.querySelectorAll('.rm-fx .rm-pulse').length >= 1 && document.querySelectorAll('.rm-fx .rm-in').length >= 1", 8)), True)

    # ---- PSKReporter: a new answer reaches the page; a failed one keeps the stations and says so
    PSK["xml"] = PSK["xml"].replace("</receptionReports>", report("VK2ABC", "QF56od", 14075100, -22, 30, "Australia") + "</receptionReports>")
    prop = st.addons.get("propagation")

    async def repoll():
        prop.psk = await prop._poll_psk()

    BG.run(repoll())
    check("PSKReporter's next answer: VK2ABC hears us too, so it is two-way now",
          bool(b.wait("(rrMap.stations().find((s) => s.call === 'VK2ABC') || {}).kind === 'both'", 8)), True)
    PSK["status"] = 503
    BG.run(repoll())
    check("PSKReporter down: the stations stay, at their age, and the list says so",
          (bool(b.wait("!!document.querySelector('.rm-src.warn')", 8)), b.js("rrMap.stations().filter((s) => s.tx).length"),
           "showing the last answer" in (b.js("document.querySelector('.rm-src').textContent") or "")), (True, 3, True))

    # ---- JTDX leaving
    status(dx_call="JA1ABC", dx_grid="PM95", tx_enabled=True, transmitting=True, decoding=False)
    b.wait("document.querySelectorAll('.rm-txring').length === 3", 8)
    # The sync is loaded by name, as the station loads it (a seam for tests/affected.py).
    sys.modules[type(st.logsync).__module__].RIG_SILENT_S = 1.0
    jt.send(J.heartbeat(), to)                    # the last word from a JTDX that then dies mid-transmission
    check("a transmitting JTDX that goes silent does not stay 'transmitting'",
          (bool(b.wait("document.querySelectorAll('.rm-txring').length === 0", 8)), hud().split(" | ")[1]),
          (True, "JTDX not heard from"))
    status(dx_call=None, tx_enabled=False, transmitting=False, decoding=False)
    b.wait("document.querySelector('.rm-hud').textContent.includes('listening')", 8)
    jt.send(J.close(), to)
    check("JTDX closing: 'not heard from' at once", bool(b.wait("document.querySelector('.rm-hud').textContent.includes('not heard from')", 5)), True)
    shot("4-end")
finally:
    if b:
        b.close()
    jt.close()
    try:
        BG.run(runner.cleanup(), 15)
    except Exception as e:
        print(f"station stop: {type(e).__name__}", flush=True)
    radio.terminate()
    try:
        radio.wait(5)
    except subprocess.TimeoutExpired:
        radio.kill()
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'all ok' if not fails else f'{fails} FAILED'}", flush=True)
_os._exit(1 if fails else 0)
