"""Band cells, Propagation's openings and band rows, and spot labels retune
the focus radio (UX plan PR 4c, 2026-11-19), driven in headless Chromium.

Two simulated radios behind station.py: an IC-7300 and an IC-7610 with
--auto-tune (a band change there would bring an ATU tune: a carrier in real
life; an Icom, so its scope draws spot labels). Both
radio servers and the station run the Propagation add-on asking nobody for
anything ("swpc": false, no skywatch, no PSKReporter); the station also runs
the Roster (no logbook sync) and the Map (POTA fetching off). The add-ons'
states are given to the pages raw, as their sockets deliver them. No network.
Nothing transmits. Every dial is read in Hz from a fresh socket to the radio's
own server, never from the page. A recorder in every document (the station,
each radio frame, a radio's own page) keeps each `set` sent. Checks:

  titles    every new control says a band change transmits a tune carrier on
            a station with auto-tune: a band cell's button, an opening, a
            board row, a table row, a spot label's hover
  locked    (the default) a band cell, a Next opening, a board row, a table
            row, a decode's label, a POTA label, and an add-on's api.tune on
            a radio's own page: no `freq` and no `mode` sent, the dial
            unchanged. Negative control: the same band cell with only the lock
            check fooled retunes, so the lock check is what blocks
  accepted  unlocked, each action reaches the radio, read back in Hz: a
            Next opening, a board row, a table row, a band cell (each by the
            shared band stack); a decode's label sets the dial it was decoded
            on (row dial, not dial + df) and sends no mode; a POTA CW spot
            sets khz * 1000 and CW; a POTA FT8 spot USB with data on; a POTA
            AM spot (not mapped) the frequency only; a POTA CW spot on a radio
            the page believes has no CW (its hello rewritten in the page):
            the frequency only; an add-on's api.tune on a radio's own page
  map       the POTA mode map: SSB is the band's own sideband (LSB on 40 m,
            USB on 60 m), digital is USB with data only where the radio has a
            data USB, AM and an empty label are frequency only
  focus     a label on a radio that is not the focus tunes nothing
  armed     a Winlink session armed and idle on the IC-7610 (auto-tune on): each
            action is refused, nothing sent, the dial never moves, the tuner
            never tunes; on the IC-7610's own page an add-on's api.tune is refused
            by the radio server. Positive control: disarmed, it moves
STOP TX and Release TX are untouched.

    python3 tests/tune_actions_page_test.py [screenshot-dir]
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

os.environ["RANCH_RADIO_HOSTS"] = ""     # never this machine's own host files
os.environ["RANCH_RADIO_OFFLINE"] = "1"
os.environ.pop("CC_OBS_TOKENS_FILE", None)

import aiohttp  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from cdp_browser import Browser  # noqa: E402
import testkit  # noqa: E402

CHROME = (os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
          or next((p for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                               "/Applications/Chromium.app/Contents/MacOS/Chromium") if os.path.exists(p)), None))
if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")
SHOTS = sys.argv[1] if len(sys.argv) > 1 else None
TMP = tempfile.mkdtemp(prefix="tune-actions-")

fp = testkit.free_port
WEB, SHARED, CDP = fp(), fp(), fp()
IC = {"web": fp(), "rigctl": fp(), "lane": fp()}
AT = {"web": fp(), "rigctl": fp(), "lane": fp()}
fails = 0
T0 = time.monotonic()


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:70} got {got!r}" + ("" if ok else f" want {want!r}"), flush=True)


def _answers(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


ADDONS = os.path.join(TMP, "radio-addons.json")
with open(ADDONS, "w") as f:
    json.dump({"propagation": {"swpc": False}}, f)


def radio(rig, p, *extra):
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--rig", rig, "--no-audio", "--host", "127.0.0.1",
         "--listen", str(p["web"]), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(p["rigctl"]),
         "--winlink-rigctl-port", str(p["lane"]), "--addons", ADDONS, "--addons-state", "", *extra],
        cwd=ROOT, stdout=open(os.path.join(TMP, f"{rig}.log"), "w"), stderr=subprocess.STDOUT)
    return proc


def stop(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def dial(p):
    """A fresh socket's hello from a radio's own server: (freq Hz, mode, data, armed)."""
    async def go():
        async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{p['web']}/ws") as ws:
            while True:
                m = await ws.receive(timeout=5)
                if m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data).get("t") == "hello":
                    h = json.loads(m.data)
                    st = h["state"]
                    return st.get("freq"), st.get("mode"), bool(st.get("data")), bool(h["winlink"]["armed"])
    return asyncio.run(go())


def dial_is(p, freq, mode=None, timeout=10):
    def ok():
        d = dial(p)
        return d[0] == freq and (mode is None or d[1] == mode)
    return testkit.wait_for(ok, timeout, 0.15)


def log_of(rig):
    with open(os.path.join(TMP, f"{rig}.log")) as f:
        return f.read()


class Other:
    """A second client on a radio's own server: it arms Winlink and keeps
    every message it is sent (the tuner's state among them)."""

    def __init__(self, p):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.msgs = []
        self._run(self._open(p["web"]))
        assert testkit.wait_for(lambda: any(m.get("t") == "hello" for m in self.msgs), 10), "no hello"

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(15)

    async def _open(self, port):
        self.s = aiohttp.ClientSession()
        self.ws = await self.s.ws_connect(f"http://127.0.0.1:{port}/ws")
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for m in self.ws:
            if m.type == aiohttp.WSMsgType.TEXT:
                self.msgs.append(json.loads(m.data))

    def send(self, **d):
        self._run(self.ws.send_str(json.dumps(d)))

    def tuned(self):
        return 2 in [m["d"]["tuner"] for m in list(self.msgs) if m.get("t") == "state" and "tuner" in m.get("d", {})]

    def close(self):
        async def go():
            await self.ws.close()
            await self.s.close()
        self._run(go())
        self.loop.call_soon_threadsafe(self.loop.stop)


# ---- the add-ons' states, raw, as the add-ons' snapshots make them
NOW = int(time.time())
PROP = {"swpc": False, "skywatch": True, "window_min": 60, "kp": None, "kp_stale": False, "xray": None, "f107": None,
        "space_error": None, "heard_error": None, "heard_checked": NOW - 30, "pskreporter": None, "psk": {},
        "psk_error": None, "psk_checked": None, "forecast_h": 12, "forecast_error": None, "forecast_issued": NOW - 3600,
        "bands": {"20m": {"n": 900, "best_snr": 5, "max_km": 8000, "sources": {"jtdx": 900}, "regions": [["EU", 900]]},
                  "40m": {"n": 120, "best_snr": 2, "max_km": 3000, "sources": {"jtdx": 120}, "regions": [["NA_EAST", 120]]}},
        "forecast": [{"band": "15m", "region": "SA", "start": NOW - 600, "end": NOW + 7200, "peak": 0.6}]}


def row(call, dial_hz, df, band="20m"):
    """A roster row as the Roster's view makes it, with the dial it arrived on."""
    return {"key": f"1|0|{df}|CQ {call} AA00", "answer": "cq", "call": call, "grid": "AA00", "cq": True, "cq_word": "",
            "to_me": False, "to": "", "snr": -5, "dt": 0.1, "df": df, "message": f"CQ {call} AA00", "age": 10,
            "need": "dxcc", "reasons": ["dxcc"], "entity": "", "cont": "", "lotw": False, "km": None,
            "bearing": None, "band": band, "mode": "FT8", "dial": dial_hz}


# A decode heard with JTDX on 14.090 at 1000 Hz: its label sits at 14.091.
ROSTER = {"t": "addon", "addon": "roster", "roster": {"band": "20m", "mode": "FT8", "dial": 14_090_000,
                                                       "rows": [row("N9ABC", 14_090_000, 1000)], "needs": None}}
POTA = [("K2ABC", "14074", "CW"), ("K3ABC", "14083", "FT8"), ("K5ABC", "14066", "AM")]
MAP = {"t": "addon", "addon": "map", "pota_age": 30, "pota_error": None,
       "pota": [{"call": c, "ref": "US-0001", "name": "Test Park", "mode": m, "khz": k, "band": "", "grid": "FN31",
                 "lat": 41.0, "lon": -73.0} for c, k, m in POTA]}

cfg_path = os.path.join(TMP, "station.json")
with open(cfg_path, "w") as f:
    json.dump({"name": "Test Station",
               "radios": [{"id": "ic7300", "name": "IC-7300", **IC}, {"id": "ic7610", "name": "IC-7610", **AT}],
               "addons": {"propagation": {"swpc": False}, "roster": {}, "map": {"pota": False}},
               "addons_state": os.path.join(TMP, "addons.json")}, f)

# In every document: record each message sent as type[:key]; in the top
# document only, when localStorage "test.dropModes" lists modes, take them out
# of a radio socket's hello caps (the page then believes the radio lacks them;
# the radio itself would still accept them, so a wrong send would show).
BOOT = r"""
window.__sent = [];
window.__setlog = [];      // every set, with its value, socket and sender (diagnostics, 2026-11-19)
const __o = WebSocket.prototype.send;
WebSocket.prototype.send = function (d) {
  try {
    const m = JSON.parse(d); window.__sent.push(m.t + (m.k ? ':' + m.k : ''));
    if (m.t === 'set') window.__setlog.push({k: m.k, v: m.v, url: String(this.url).replace(/^.*?\/\/[^/]+/, ''),
      from: window === window.top ? 'station' : 'frame', at: Math.round(performance.now())});
  } catch (e) { window.__sent.push('binary'); }
  return __o.call(this, d);
};
if (window === window.top) {
  const desc = Object.getOwnPropertyDescriptor(WebSocket.prototype, 'onmessage');
  Object.defineProperty(WebSocket.prototype, 'onmessage', { configurable: true,
    get() { return desc.get.call(this); },
    set(fn) {
      const sock = this;
      desc.set.call(this, fn && function (e) {
        let drop = [];
        try { drop = JSON.parse(localStorage.getItem('test.dropModes') || '[]'); } catch (x) {}
        if (drop.length && typeof e.data === 'string' && /\/r\/[^/]+\/ws/.test(sock.url)) {
          const m = JSON.parse(e.data);
          if ((m.t === 'hello' || m.t === 'config') && m.config && m.config.caps && m.config.caps.modes) {
            m.config.caps.modes = m.config.caps.modes.filter((x) => !drop.includes(x));
            return fn.call(this, { data: JSON.stringify(m) });
          }
        }
        return fn.call(this, e);
      });
    } });
}
"""
NOTE = "a band change transmits a tune carrier"
TOASTS = "[...document.querySelectorAll('#toasts .toast')].map((t) => t.textContent)"
FRAME = "document.querySelector('#view-radio iframe[src*=\"r/{}/\"]')"
SETS_ALL = ("JSON.stringify([window].concat([...document.querySelectorAll('iframe')].map((f) => f.contentWindow))"
            ".flatMap((w) => { try { return (w.__sent || []).filter((t) => t.startsWith('set:')); } catch (e) { return []; } }))")
RESET = ("[window].concat([...document.querySelectorAll('iframe')].map((f) => f.contentWindow))"
         ".forEach((w) => { try { if (w.__sent) w.__sent.length = 0; } catch (e) {} }), true")


def sets():
    return json.loads(b.js(SETS_ALL) or "[]")


def reset():
    b.js(RESET)
    b.js("document.querySelectorAll('#toasts .toast').forEach((t) => t.remove()), true")


def shot(name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        b.screenshot(os.path.join(SHOTS, f"tune-actions-{name}.png"))


def deliver(*ms):
    """Messages as the page's socket delivers them (its own handler)."""
    for m in ms:
        b.js(f"ws.onmessage({{ data: {json.dumps(json.dumps(m))} }}), true")


def deliver_station():
    deliver({"t": "addon", "addon": "propagation", "state": PROP}, ROSTER, MAP)


def mouse_click(x, y):
    b.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, button="none")
    for t in ("mousePressed", "mouseReleased"):
        b.call("Input.dispatchMouseEvent", type=t, x=x, y=y, button="left", clickCount=1)


def click(sel):
    """A real click at the middle of the first match of `sel`, scrolled into view."""
    xy = b.js(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); if (!e) return null; "
              "e.scrollIntoView({block: 'center'}); const r = e.getBoundingClientRect(); "
              "return [r.x + r.width / 2, r.y + r.height / 2]; })()")
    if not xy:
        return False
    mouse_click(*xy)
    return True


def view(hash_, ready):
    b.js(f"location.hash = {json.dumps(hash_)}, true")
    return bool(b.wait(ready, 15))


CELL = "#mc-bands .bs-cell[data-band=\"{}\"] .bs-tune"
OPEN = "#view-addon-propagation .pg-open .pg-tune[data-band=\"{}\"]"
BOARD = "#view-addon-propagation .pg-board .pg-band .pg-tune[data-band=\"{}\"]"
TABLE = "#view-addon-propagation .prop-table .pg-tune[data-band=\"{}\"]"


def show_table():
    if not b.js("!!document.querySelector('#view-addon-propagation .prop-table')"):
        b.js("document.querySelector('#view-addon-propagation .pg-tbl').click(), true")
    return bool(b.wait(f"!!document.querySelector({json.dumps(TABLE.format('40m'))})", 5))


def frame_eval(rid, expr):
    return b.js(f"(() => {{ const w = {FRAME.format(rid)}?.contentWindow; "
                f"try {{ return w && w.rrFrame ? w.eval({json.dumps(expr)}) : null; }} catch (e) {{ return 'JS error: ' + e; }} }})()")


def scope_point(rid, fx, fy=0.85):
    """Page coordinates of a point on radio `rid`'s spectrum, as fractions of
    its width and height (low on it: clear of the spot labels at the top)."""
    return b.js(f"(() => {{ const f = {FRAME.format(rid)}; const a = f.getBoundingClientRect(); "
                f"const c = f.contentWindow.eval('scope.spec.getBoundingClientRect()'); "
                f"return [a.left + c.left + c.width * {fx}, a.top + c.top + c.height * {fy}]; }})()")


def label(rid, name, timeout=10):
    """The page coordinates of label `name` on radio `rid`'s scope, once drawn
    on a span that holds the radio's frequency and still in the same place
    0.3 s later. Under CI load the scope's frames can lag a retune: a label
    found on the new band, then a late frame of the old one, and the click
    landed on the scope instead (2026-11-20: 21.267 MHz after a 20 m click)."""
    find = f"(() => {{ if (scope.stale()) return null; const c = scope.spec.getBoundingClientRect(); " \
           f"const s = scope.spotBoxes.find((x) => x.label === {json.dumps(name)}); " \
           f"return s ? [c.left + s.left + s.w / 2, c.top + s.top + s.h / 2] : null; }})()"

    def at():
        return b.js(f"(() => {{ const f = {FRAME.format(rid)}; const a = f.getBoundingClientRect(); "
                    f"const p = f.contentWindow.eval({json.dumps(find)}); return p ? [a.left + p[0], a.top + p[1]] : null; }})()")
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        first = at()
        if first:
            time.sleep(0.3)
            if at() == first:
                return first
        time.sleep(0.1)
    return None


def click_label(rid, name):
    xy = label(rid, name)
    if not xy:
        return False
    mouse_click(*xy)
    return True


def radio_tab(rid):
    return view(f"#/{rid}", f"(() => {{ const w = {FRAME.format(rid)}?.contentWindow; "
                            f"return !!w && !!w.rrFrame && !{FRAME.format(rid)}.hidden && w.eval('scope.n > 0'); }})()")


def focus_on(rid):
    b.js(f"localStorage.setItem('rr-focus', '{rid}')")
    b.goto(f"http://127.0.0.1:{WEB}/?f={time.monotonic_ns()}#/home")
    ok = b.wait(f"document.querySelector('#rig-bar .rb-rig.focus')?.dataset.id === '{rid}' && "
                "!!document.querySelector('#rig-bar .rb-rig.focus button.rb-f') && "
                "/^\\d+\\.\\d{3}\\.\\d{3}$/.test(document.querySelector('#rig-bar .rb-rig.focus .rb-f').textContent)", 30)
    deliver_station()
    return bool(ok) and bool(b.wait(f"!!document.querySelector({json.dumps(CELL.format('40m'))})", 10))


def quiet():
    time.sleep(1.0)          # a quiet window before checking that nothing happened


def own_page(p):
    """A radio's own page (not the station's frame), on its Propagation tab, with the forecast delivered."""
    b.goto(f"http://127.0.0.1:{p['web']}/?o={time.monotonic_ns()}#/propagation")
    b.wait("document.querySelector('#conn b')?.textContent === 'Connected' && !!document.querySelector('.panel.addon-propagation')", 20)
    deliver({"t": "addon", "addon": "propagation", "state": PROP})
    return bool(b.wait(f"!!document.querySelector('.pg-open .pg-tune[data-band=\"15m\"]')", 10))


procs = []
b = None
try:
    procs.append(radio("ic7300", IC))
    procs.append(radio("ic7610", AT, "--auto-tune"))
    for p in (IC, AT):
        assert testkit.wait_for(lambda: _answers(p["web"]), 60, 0.1), "a radio did not start"
    procs.append(subprocess.Popen([sys.executable, "station.py", "--config", cfg_path, "--listen", str(WEB),
                                   "--rigctl-port", str(SHARED)], cwd=ROOT, env=dict(os.environ, HOME=TMP),
                                  stdout=open(os.path.join(TMP, "station.log"), "w"), stderr=subprocess.STDOUT))
    assert testkit.wait_for(lambda: _answers(WEB), 60, 0.1), "station did not start"
    b = Browser(CHROME, CDP, size="1500,1000")
    b.call("Page.addScriptToEvaluateOnNewDocument", source=BOOT)
    b.goto(f"http://127.0.0.1:{WEB}/#/home")
    check("focus on the IC-7300, the band strip's buttons drawn", focus_on("ic7300"), True)
    start_f, start_m, _, _ = dial(IC)
    check("  the IC-7300 starts on 20m", 14_000_000 <= start_f <= 14_350_000, True)

    # ---------------------------------------------------------------- titles
    t_cell = b.js(f"document.querySelector({json.dumps(CELL.format('40m'))}).title")
    check("titles: a band cell's button says what a band change does", NOTE in (t_cell or ""), True)
    check("  it names the focus radio and the default it falls back to",
          ("IC-7300" in t_cell, "7.150.000 LSB" in t_cell, "(off here)" in t_cell), (True, True, True))
    if SHOTS:
        # A headless screenshot draws no native tooltip: the title is shown in
        # a box the test adds beside the button, text taken from the button.
        b.js(f"""(() => {{ const e = document.querySelector({json.dumps(CELL.format('40m'))}), r = e.getBoundingClientRect();
            e.classList.add('hovered'); e.style.color = 'var(--accent)';
            const d = document.createElement('div'); d.id = 'test-tip'; d.textContent = 'title: ' + e.title;
            Object.assign(d.style, {{position: 'fixed', left: r.left + 'px', top: r.bottom + 6 + 'px', maxWidth: '520px',
              background: '#ffffe0', color: '#111', font: '12px sans-serif', padding: '6px 8px', border: '1px solid #888',
              zIndex: 9999, whiteSpace: 'normal'}});
            document.body.append(d); return true; }})()""")
        shot("1-band-cell-title")
        b.js("document.getElementById('test-tip')?.remove(), true")
    check("  Propagation: the tab is there", view("#/propagation", f"!!document.querySelector({json.dumps(OPEN.format('15m'))})"), True)
    check("  a Next opening says it", NOTE in (b.js(f"document.querySelector({json.dumps(OPEN.format('15m'))}).title") or ""), True)
    check("  a board row says it", NOTE in (b.js(f"document.querySelector({json.dumps(BOARD.format('40m'))}).title") or ""), True)
    check("  the table shows", show_table(), True)
    check("  a table row says it", NOTE in (b.js(f"document.querySelector({json.dumps(TABLE.format('40m'))}).title") or ""), True)
    check("  the radio's tab draws its scope", radio_tab("ic7300"), True)
    xy = label("ic7300", "K2ABC")
    if xy:
        b.call("Input.dispatchMouseEvent", type="mouseMoved", x=xy[0], y=xy[1], button="none")
    check("  a spot label's hover says it",
          testkit.wait_for(lambda: NOTE in (frame_eval("ic7300", "document.querySelector('#scope-body').title") or ""), 5), True)

    # ---------------------------------------------------------------- locked
    print("\n# locked (the default)", flush=True)
    check("locked by default", b.js("store.get('locked', true) !== false"), True)
    reset()
    acts = []
    acts.append(("a decode's label", click_label("ic7300", "N9ABC")))
    acts.append(("a POTA label", click_label("ic7300", "K2ABC")))
    view("#/home", f"!!document.querySelector({json.dumps(CELL.format('40m'))})")
    acts.append(("a band cell", click(CELL.format("40m"))))
    check("  a band cell's click flashes the bar's padlock",
          bool(b.wait("document.querySelector('#rig-bar .rb-lock').classList.contains('flash')", 3)), True)
    view("#/propagation", f"!!document.querySelector({json.dumps(OPEN.format('15m'))})")
    acts.append(("a Next opening", click(OPEN.format("15m"))))
    acts.append(("a board row", click(BOARD.format("40m"))))
    show_table()
    acts.append(("a table row", click(TABLE.format("40m"))))
    check("  every action was clicked", [n for n, ok in acts if not ok], [])
    quiet()
    check("  locked: no `set` from any of them (station and frames)", sets(), [])
    check("  the dial unchanged (fresh socket)", dial(IC)[:2], (start_f, start_m))

    # Negative control: the same band cell, with only the lock check fooled.
    view("#/home", f"!!document.querySelector({json.dumps(CELL.format('40m'))})")
    b.js("window.__get = store.get; store.get = (k, d) => k === 'locked' ? false : window.__get(k, d), true")
    click(CELL.format("40m"))
    check("negative control: the lock check fooled, the same band cell retunes (7.150 LSB)",
          dial_is(IC, 7_150_000, "LSB"), True)
    b.js("store.get = window.__get, true")
    check("  it sent set freq and set mode", sets(), ["set:freq", "set:mode"])

    # ---------------------------------------------------------------- accepted
    print("\n# unlocked: each action reaches the radio", flush=True)
    b.js("document.querySelector('#rig-bar .rb-lock').click(), true")
    check("unlock in the rig bar", bool(b.wait("store.get('locked', true) === false", 5)), True)
    view("#/propagation", f"!!document.querySelector({json.dumps(OPEN.format('15m'))})")
    reset()
    click(OPEN.format("15m"))
    check("a Next opening (15m): 21,250,000 Hz USB (the band's default)", dial_is(IC, 21_250_000, "USB"), True)
    reset()
    click(BOARD.format("40m"))
    check("a board row (40m): back where 40m was left, 7,150,000 Hz LSB", dial_is(IC, 7_150_000, "LSB"), True)
    reset()
    show_table()
    click(TABLE.format("15m"))
    check("a table row (15m): 21,250,000 Hz USB", dial_is(IC, 21_250_000, "USB"), True)
    view("#/home", f"!!document.querySelector({json.dumps(CELL.format('20m'))})")
    reset()
    click(CELL.format("20m"))
    check("a band cell (20m): where 20m was left, in Hz", dial_is(IC, start_f, start_m), True)
    check("  it sent only set freq (20m's mode is the mode it had)", sets(), ["set:freq"])

    radio_tab("ic7300")
    reset()
    check("a decode's label: clicked", click_label("ic7300", "N9ABC"), True)
    decoded = dial_is(IC, 14_090_000)
    check("  the dial it was decoded on, 14,090,000 Hz (not dial + df)", decoded, True)
    if not decoded:
        # This failed on CI about half the time on 2026-11-19 and never
        # locally; every later spot check then failed too. Print what was
        # sent (value, socket, station or frame), what the radio reads, and
        # what the frame's scope holds, so the next failure names its cause.
        print("  diag: dial now", dial(IC), flush=True)
        print("  diag: sets", b.js("JSON.stringify([window].concat([...document.querySelectorAll('iframe')]"
                                   ".map((f) => f.contentWindow)).flatMap((w) => { try { return w.__setlog || []; } "
                                   "catch (e) { return []; } }))"), flush=True)
        print("  diag: scope", frame_eval("ic7300", "JSON.stringify({labels: scope.spotBoxes.map((s) => s.label), "
                                                    "hidden: scope.spotHidden, locked: store.get('locked', true)})"), flush=True)
        print("  diag: focus", b.js("localStorage.getItem('rr-focus')"), flush=True)
    quiet()
    check("  no mode sent: only set freq", sets(), ["set:freq"])
    check("  the mode unchanged (fresh socket)", dial(IC)[1], start_m)
    reset()
    check("a POTA CW spot: clicked", click_label("ic7300", "K2ABC"), True)
    check("  khz * 1000 and CW: 14,074,000 Hz CW", dial_is(IC, 14_074_000, "CW"), True)
    check("  set freq, then set mode", sets(), ["set:freq", "set:mode"])
    shot("2-spot-click-retuned")
    reset()
    check("a POTA FT8 spot: clicked", click_label("ic7300", "K3ABC"), True)
    check("  14,083,000 Hz USB, data on (the IC-7300 has data on USB)",
          testkit.wait_for(lambda: dial(IC)[:3] == (14_083_000, "USB", True), 10, 0.15), True)
    check("  set freq, mode, data", sets(), ["set:freq", "set:mode", "set:data"])
    reset()
    check("a POTA AM spot (no mode mapped): clicked", click_label("ic7300", "K5ABC"), True)
    check("  14,066,000 Hz, the mode left as it was", dial_is(IC, 14_066_000, "USB"), True)
    quiet()
    check("  the frequency only", sets(), ["set:freq"])

    m = json.loads(b.js("""JSON.stringify([
        station.potaTune(7074000, 'SSB', {modes: ['LSB', 'USB', 'CW']}),
        station.potaTune(5357000, 'ssb', {modes: ['LSB', 'USB', 'CW']}),
        station.potaTune(14074000, 'FT4', {modes: ['LSB', 'USB'], data_modes: ['LSB', 'USB']}),
        station.potaTune(14074000, 'FT8', {modes: ['LSB', 'USB'], data_modes: []}),
        station.potaTune(14074000, 'AM', {modes: ['AM', 'USB']}),
        station.potaTune(14074000, '', {modes: ['USB']}),
        station.potaTune(14074000, 'CW', {modes: ['USB']})])"""))
    check("map: SSB on 40m is LSB, on 60m USB", [m[0].get("mode"), m[1].get("mode")], ["LSB", "USB"])
    check("  FT4 USB with data; FT8 on a radio with no data setting: USB, no data",
          [(m[2].get("mode"), m[2].get("data")), (m[3].get("mode"), m[3].get("data"))], [("USB", True), ("USB", None)])
    check("  AM and an empty label: frequency only; CW on a radio without CW: frequency only",
          [m[4], m[5], m[6]], [{"freq": 14074000}] * 3)

    # The page believes the IC-7300 has no CW (its hello rewritten in the page).
    b.js("localStorage.setItem('test.dropModes', '[\"CW\"]'), true")
    focus_on("ic7300")
    radio_tab("ic7300")
    before = dial(IC)
    reset()
    check("unsupported mode: a POTA CW spot on a radio without CW (in the page's eyes)", click_label("ic7300", "K2ABC"), True)
    check("  14,074,000 Hz, mode unchanged", dial_is(IC, 14_074_000, before[1]), True)
    quiet()
    check("  the frequency only (the radio would have taken CW)", sets(), ["set:freq"])
    b.js("localStorage.removeItem('test.dropModes'), true")

    # Click-to-tune on a scope still showing the old band (centre mode): the
    # frames held on a 15 m span while the radio is on 20 m.
    radio_tab("ic7300")
    before = dial(IC)[:2]
    reset()
    frame_eval("ic7300", "scope.__push = scope.pushLine; scope.pushLine = () => {}; "
                         "scope.start = 21_225_000; scope.end = 21_275_000; scope.dirty = true; true")
    check("stale span: the scope says it is behind", frame_eval("ic7300", "scope.stale()"), True)
    mouse_click(*scope_point("ic7300", 0.25))
    quiet()
    check("  a click on it sends nothing, the IC-7300 unmoved", (sets(), dial(IC)[:2]), ([], before))
    frame_eval("ic7300", "scope.pushLine = scope.__push; delete scope.__push; true")
    check("  the next frames catch up", testkit.wait_for(lambda: frame_eval("ic7300", "scope.stale()") is False, 5, 0.1), True)
    reset()
    mouse_click(*scope_point("ic7300", 0.25))
    check("negative control: the same click on a current span tunes (20 m, below the dial)",
          testkit.wait_for(lambda: 14_000_000 < dial(IC)[0] < before[0], 10, 0.2), True)
    check("  it sent set freq", sets(), ["set:freq"])

    # A label on a radio that is not the focus: nothing.
    check("focus: on the IC-7610", focus_on("ic7610"), True)
    radio_tab("ic7300")
    before = dial(IC)[0]
    reset()
    click_label("ic7300", "K3ABC")
    check("  a label on the IC-7300's scope says spots tune the focus radio",
          bool(b.wait(f"{TOASTS}.some((t) => /Spots tune the focus radio/.test(t))", 3)), True)
    quiet()
    check("  nothing sent, the IC-7300 unmoved", (sets(), dial(IC)[0]), ([], before))

    # The add-on api on a radio's own page (its band stack path).
    check("own page: the IC-7300's Propagation tab, locked", own_page(IC), True)
    before = dial(IC)[:2]
    b.js(RESET)
    click(".pg-open .pg-tune[data-band=\"15m\"]")
    check("  locked: the opening flashes the page's lock",
          bool(b.wait("document.querySelector('#btn-lock').classList.contains('flash')", 3)), True)
    quiet()
    check("  locked: no `set`, the dial unchanged", (sets(), dial(IC)[:2]), ([], before))
    b.js("setLocked(false), true")
    b.js(RESET)
    click(".pg-open .pg-tune[data-band=\"15m\"]")
    check("  unlocked: api.tune({band: '15m'}) reaches the radio (21,250,000 Hz USB)",
          dial_is(IC, 21_250_000, "USB"), True)

    # ---------------------------------------------------------------- armed
    print("\n# a Winlink session armed and idle on the IC-7610 (auto-tune on)", flush=True)
    check("focus on the IC-7610", focus_on("ic7610"), True)
    check("  unlocked here (one lock with the radio pages)", b.js("store.get('locked', true) === false"), True)
    # Wait for the strip to redraw with the IC-7610's config: right after the
    # focus switch it can still carry the previous radio's titles (2026-11-19).
    check("  auto-tune on: the band cell says it",
          bool(b.wait(f"(document.querySelector({json.dumps(CELL.format('40m'))})?.title || '')"
                      ".includes('Auto-tune is on here')", 10)), True)
    other = Other(AT)
    other.send(t="winlink_arm", v=True)
    check("  another client arms Winlink", testkit.wait_for(lambda: dial(AT)[3], 15, 0.2), True)
    check("  the bar sees it",
          bool(b.wait("/WINLINK/.test(document.querySelector('#rig-bar .rb-rig.focus .rb-tag').textContent)", 10)), True)
    armed_f = dial(AT)[0]
    reset()
    refused = f"{TOASTS}.some((t) => /Winlink session is armed/.test(t))"

    def armed(name, ok):
        """Clicked, and the page said why it refused (then clear the toasts)."""
        said = bool(ok) and bool(b.wait(refused, 3))
        b.js("document.querySelectorAll('#toasts .toast').forEach((t) => t.remove()), true")
        return name, said
    acts = [armed("a band cell", click(CELL.format("40m")))]
    view("#/propagation", f"!!document.querySelector({json.dumps(OPEN.format('15m'))})")
    acts.append(armed("a Next opening", click(OPEN.format("15m"))))
    acts.append(armed("a board row", click(BOARD.format("40m"))))
    show_table()
    acts.append(armed("a table row", click(TABLE.format("40m"))))
    check("  the IC-7610's tab draws its scope", radio_tab("ic7610"), True)
    acts.append(armed("a decode's label", click_label("ic7610", "N9ABC")))
    acts.append(armed("a POTA label", click_label("ic7610", "K2ABC")))
    check("  each action clicked and refused: a Winlink session is armed", [n for n, ok in acts if not ok], [])
    quiet()
    check("  nothing sent from any of them", sets(), [])
    check("own page: the IC-7610's Propagation tab", own_page(AT), True)
    b.js("setLocked(false), true")
    click(".pg-open .pg-tune[data-band=\"15m\"]")
    check("  api.tune there: refused by the radio server (S1)",
          testkit.wait_for(lambda: "refused browser freq" in log_of("ic7610"), 10, 0.2), True)
    time.sleep(3)            # past the auto-tune settle: a tune would show by now
    check("  the dial never moved (fresh socket), the tuner never tuned",
          (dial(AT)[0], other.tuned()), (armed_f, False))
    check("  still armed", dial(AT)[3], True)
    other.close()
    check("  the other client leaving disarms it", testkit.wait_for(lambda: not dial(AT)[3], 15, 0.2), True)
    click(".pg-open .pg-tune[data-band=\"15m\"]")
    check("  positive control: disarmed, the same api.tune moves the dial (15m)",
          testkit.wait_for(lambda: 21_000_000 <= (dial(AT)[0] or 0) <= 21_450_000, 10, 0.2), True)

    b.goto(f"http://127.0.0.1:{WEB}/?z=1#/home")
    b.wait("!!document.querySelector('#rig-bar .rb-stop-btn')", 15)
    check("STOP TX still in the bar", b.js("!!document.querySelector('#rig-bar .rb-stop-btn')"), True)
    check("Release TX still in each radio's bar entry", b.js("document.querySelectorAll('#rig-bar .rb-release').length"), 2)
finally:
    if b:
        b.close()
    for p in procs:
        stop(p)
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{time.monotonic() - T0:.0f} s", flush=True)
print("PASS" if not fails else f"{fails} FAILED", flush=True)
sys.exit(1 if fails else 0)
