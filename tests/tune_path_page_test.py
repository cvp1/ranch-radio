"""One way to retune the focus radio from the station page (UX plan PR 1d,
2026-11-19), driven in headless Chromium: the rig bar's typed frequency,
through station.js tuneFocus.

Two simulated radios behind station.py: an IC-7300 and a G90 with --auto-tune
(so a band change there would bring an ATU tune, a carrier in real life).
Nothing transmits. Every dial is read back in Hz from a fresh socket to the
radio's own server, never from the page. Checks:
  1. every band-changing control says a band change transmits a tune carrier
     on a station with auto-tune: the bar's frequency, its input, the radio
     page's band chips, A/B and swap; the G90's says auto-tune is on
  2. locked (the default): a real click on the bar's frequency opens nothing,
     flashes the bar's padlock, sends nothing; the dial is unchanged
  3. the padlock is the radio page's lock: unlocking in the bar unlocks the
     frame, locking in the frame locks the bar
  4. unlocked: a real click opens the input; a typed frequency, Enter: the
     radio reads it back in Hz from a fresh socket (positive control)
  5. the lock changed while the input is open: from the radio page (a storage
     event) the input closes; written in the station's own document (no event)
     Enter sends nothing and flashes the padlock. Negative control: the same
     commit with only the lock check fooled retunes the radio, so the lock
     check is what stopped it
  6. the focus changed while the input is open: Enter sends nothing to either
     radio; a click on the other radio drops the input
  7. a refused mode after an accepted freq: the freq stays, the page says the
     mode did not change
  8. a Winlink session armed and idle on the G90 (auto-tune on): the bar
     refuses without sending; the radio's own page's band chip is refused by
     the radio server (S1); the dial never moves and the tuner never tunes.
     Positive control: disarmed, the same band chip moves the dial
STOP TX and Release TX are untouched (rigbar_page_test, tx_stop_page_test).

    python3 tests/tune_path_page_test.py [screenshot-dir]
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
TMP = tempfile.mkdtemp(prefix="tune-path-")

fp = testkit.free_port
WEB, SHARED, CDP = fp(), fp(), fp()
IC = {"web": fp(), "rigctl": fp(), "lane": fp()}
G90 = {"web": fp(), "rigctl": fp(), "lane": fp()}
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:64} got {got!r}" + ("" if ok else f" want {want!r}"))


def _answers(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


def radio(rig, p, *extra):
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--rig", rig, "--no-audio", "--host", "127.0.0.1",
         "--listen", str(p["web"]), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(p["rigctl"]),
         "--winlink-rigctl-port", str(p["lane"]), "--addons-state", "", *extra],
        cwd=ROOT, stdout=open(os.path.join(TMP, f"{rig}.log"), "w"), stderr=subprocess.STDOUT)
    assert testkit.wait_for(lambda: _answers(p["web"]), 60, 0.1), f"{rig} did not start"
    return proc


def stop(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def dial(p):
    """A fresh socket's hello from a radio's own server: (freq in Hz, mode, armed)."""
    async def go():
        async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{p['web']}/ws") as ws:
            while True:
                m = await ws.receive(timeout=5)
                if m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data).get("t") == "hello":
                    h = json.loads(m.data)
                    return h["state"].get("freq"), h["state"].get("mode"), bool(h["winlink"]["armed"])
    return asyncio.run(go())


def log_of(rig):
    with open(os.path.join(TMP, f"{rig}.log")) as f:
        return f.read()


class Other:
    """A second page on a radio's own server (its own thread and loop): it
    holds a Winlink session and keeps every message it is sent."""

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

    def tuner_seen(self, since=0):
        return [m["d"]["tuner"] for m in list(self.msgs)[since:] if m.get("t") == "state" and "tuner" in m.get("d", {})]

    def close(self):
        async def go():
            await self.ws.close()
            await self.s.close()
        self._run(go())
        self.loop.call_soon_threadsafe(self.loop.stop)


cfg = {"name": "Test Station",
       "radios": [{"id": "ic7300", "name": "IC-7300", **IC}, {"id": "g90", "name": "G90", **G90}],
       "addons_state": os.path.join(TMP, "addons.json")}
cfg_path = os.path.join(TMP, "station.json")
with open(cfg_path, "w") as f:
    json.dump(cfg, f)

FREQ_RE = "/^\\d+\\.\\d{3}\\.\\d{3}$/"
RIG = "document.querySelector('#rig-bar .rb-rig[data-id=\"{}\"]')"
FOC = "document.querySelector('#rig-bar .rb-rig.focus')"
SHOWN = "((e) => !!e && !e.closest('[hidden]') && e.offsetParent !== null)"
TOASTS = "[...document.querySelectorAll('#toasts .toast')].map((t) => t.textContent)"
NOTE = "a band change transmits a tune carrier"
# Records what the station page sends on any socket, as type[:key].
SENDS = ("(() => { window.__sent = []; if (window.__hooked) return true; window.__hooked = 1; "
         "const o = WebSocket.prototype.send; WebSocket.prototype.send = function (d) { let t = 'binary'; "
         "try { const m = JSON.parse(d); t = m.t + (m.k ? ':' + m.k : ''); } catch {} window.__sent.push(t); "
         "return o.call(this, d); }; return true; })()")
SETS = "(window.__sent || []).filter((t) => t.startsWith('set:'))"


def frame_js(rid, expr):
    """`expr` evaluated in radio `rid`'s frame, or null."""
    return b.js(f"(() => {{ const w = document.querySelector('#view-radio iframe[src*=\"r/{rid}/\"]')?.contentWindow; "
                f"try {{ return w && w.rrFrame ? w.eval({json.dumps(expr)}) : null; }} "
                f"catch (e) {{ return 'JS error: ' + e; }} }})()")


def frame_wait(rid, expr, timeout=15):
    return testkit.wait_for(lambda: frame_js(rid, expr) is True, timeout, 0.1)


def click(sel):
    """A real click (trusted) at the middle of `sel`."""
    x, y = b.js(f"(() => {{ const r = document.querySelector({json.dumps(sel)}).getBoundingClientRect(); "
                f"return [r.x + r.width / 2, r.y + r.height / 2]; }})()")
    for t in ("mousePressed", "mouseReleased"):
        b.call("Input.dispatchMouseEvent", type=t, x=x, y=y, button="left", clickCount=1)


def type_enter(text):
    """Replace the focused input's text and press Enter, as a keyboard would."""
    b.js("document.activeElement.select && document.activeElement.select()")
    b.call("Input.insertText", text=text)
    for t in ("keyDown", "keyUp"):
        b.call("Input.dispatchKeyEvent", type=t, key="Enter", code="Enter", windowsVirtualKeyCode=13,
               **({"text": "\r"} if t == "keyDown" else {}))


def entry_open():
    return b.js(f"{SHOWN}(document.querySelector('#rig-bar .rb-in'))")


def open_entry():
    click("#rig-bar .rb-rig.focus .rb-f")
    return bool(b.wait(f"{SHOWN}(document.querySelector('#rig-bar .rb-in')) && "
                       "document.activeElement === document.querySelector('#rig-bar .rb-in')", 5))


def bar_locked():
    return b.js("document.querySelector('#rig-bar .rb-lock')?.getAttribute('aria-checked')")


def set_lock(v):
    if bar_locked() != str(v).lower():
        click("#rig-bar .rb-lock")
    return b.wait(f"document.querySelector('#rig-bar .rb-lock')?.getAttribute('aria-checked') === '{str(v).lower()}'", 5)


def focus_on(rid):
    b.js(f"localStorage.setItem('rr-focus', '{rid}')")
    b.goto(f"http://127.0.0.1:{WEB}/?f={rid}{time.monotonic_ns()}#/home")
    ok = b.wait(f"{RIG.format(rid)}?.classList.contains('focus') && {FREQ_RE}.test({FOC}.querySelector('.rb-f').textContent) "
                f"&& !!{FOC}.querySelector('button.rb-f')", 30)
    b.js(SENDS)
    return bool(ok) and frame_wait(rid, "typeof rrFrame === 'object'", 20)


def shot(name):
    if SHOTS:
        b.screenshot(os.path.join(SHOTS, f"tune-path-{name}.png"))


def quiet():
    time.sleep(1.0)          # a quiet window before checking that nothing happened


ic = radio("ic7300", IC)
g90 = radio("g90", G90, "--auto-tune")
front = subprocess.Popen([sys.executable, "station.py", "--config", cfg_path, "--listen", str(WEB),
                          "--rigctl-port", str(SHARED)], cwd=ROOT,
                         stdout=open(os.path.join(TMP, "station.log"), "w"), stderr=subprocess.STDOUT)
b = Browser(CHROME, CDP, size="1500,900")
try:
    assert testkit.wait_for(lambda: _answers(WEB), 60, 0.1), "station did not start"
    b.goto(f"http://127.0.0.1:{WEB}/#/home")
    b.wait(f"{FREQ_RE}.test({RIG.format('ic7300')}?.querySelector('.rb-f')?.textContent || '')", 30)
    b.js("localStorage.removeItem('ic7300.locked')")    # the radio page's default: locked
    check("focus on the IC-7300, its frequency a button", focus_on("ic7300"), True)
    start_f, start_m, _ = dial(IC)

    # 1. Say what a band change does.
    check("1. the bar's frequency title says a band change can tune",
          NOTE in (b.js(f"{FOC}.querySelector('.rb-f').title") or ""), True)
    check("  the bar's input title says it", NOTE in (b.js(f"{FOC}.querySelector('.rb-in').title") or ""), True)
    titles = frame_js("ic7300", "[...document.querySelectorAll('#bands .chip')].map((c) => c.title)"
                                " .concat([document.querySelector('#btn-ab').title, document.querySelector('#btn-swap').title,"
                                " document.querySelector('#freq').title])")
    check("  the radio page: every band chip, A/B, swap and the dial say it",
          bool(titles) and len(titles) > 5 and all(NOTE in t for t in titles), True)
    check("  auto-tune off on the IC-7300: the title says so", "(off here)" in titles[0], True)

    # 2. Locked by default: a click flashes the padlock, opens nothing, sends nothing.
    check("2. locked by default (the radio page's key)", bar_locked(), "true")
    click("#rig-bar .rb-rig.focus .rb-f")
    check("  a click on the frequency flashes the padlock",
          bool(b.wait("document.querySelector('#rig-bar .rb-lock').classList.contains('flash')", 3)), True)
    shot("locked-flash")
    check("  and opens no input", entry_open(), False)
    check("  and says tuning is locked", bool(b.wait(f"{TOASTS}.some((t) => /Tuning is locked/.test(t))", 3)), True)
    quiet()
    check("  nothing sent; the dial unchanged (fresh socket)", (b.js(SETS), dial(IC)[0]), ([], start_f))

    # 3. One lock for the bar and the radio pages.
    check("3. unlock in the bar", bool(set_lock(False)), True)
    check("  the frame's lock follows (storage event)", frame_wait("ic7300", "locked === false"), True)
    frame_js("ic7300", "setLocked(true)")
    check("  locked in the frame: the bar follows",
          bool(b.wait("document.querySelector('#rig-bar .rb-lock').getAttribute('aria-checked') === 'true'", 5)), True)
    set_lock(False)

    # 4. Unlocked: a typed frequency reaches the radio, in Hz.
    check("4. unlocked: a click opens the input, focused", open_entry(), True)
    check("  it holds the dial in MHz", b.js("document.querySelector('#rig-bar .rb-in').value"),
          f"{start_f / 1e6:.6f}".rstrip("0").rstrip("."))
    b.js("document.querySelector('#rig-bar .rb-in').value = '7074.5'")
    shot("entry-open")
    type_enter("7074.5")
    check("  typed 7074.5 (kHz), Enter: the radio reads 7074500 Hz",
          testkit.wait_for(lambda: dial(IC)[0] == 7_074_500, 10, 0.2), True)
    check("  the bar shows it, the input closed",
          (bool(b.wait(f"{FOC}.querySelector('.rb-f').textContent === '7.074.500'", 5)), entry_open()), (True, False))
    check("  it sent set freq, and nothing else", b.js(SETS), ["set:freq"])
    type_ok = dial(IC)[0]

    # 5. The lock changed while the input is open.
    b.js(SENDS)
    open_entry()
    frame_js("ic7300", "setLocked(true)")
    check("5. locked from the radio page while open: the input closes",
          bool(b.wait(f"!{SHOWN}(document.querySelector('#rig-bar .rb-in'))", 5)), True)
    set_lock(False)
    open_entry()
    b.js("localStorage.setItem('ic7300.locked', 'true')")    # this document: no storage event here
    type_enter("14.2")
    check("  locked in this document while open: Enter flashes the padlock",
          bool(b.wait("document.querySelector('#rig-bar .rb-lock').classList.contains('flash')", 3)), True)
    quiet()
    check("  nothing sent; the dial unchanged (fresh socket)", (b.js(SETS), dial(IC)[0]), ([], type_ok))
    # Negative control: the same commit, with only the lock check fooled.
    b.js("localStorage.setItem('ic7300.locked', 'false')")
    open_entry()
    b.js("localStorage.setItem('ic7300.locked', 'true'); window.__get = store.get; "
         "store.get = (k, d) => k === 'locked' ? false : window.__get(k, d)")
    type_enter("14.2")
    check("  negative control: the lock check fooled, the same Enter retunes",
          testkit.wait_for(lambda: dial(IC)[0] == 14_200_000, 10, 0.2), True)
    b.js("store.get = window.__get")
    b.js("localStorage.setItem('ic7300.locked', 'false')")
    type_ok = dial(IC)[0]

    # 6. The focus changed while the input is open.
    b.js(SENDS)
    g_before = dial(G90)[0]
    open_entry()
    b.js("localStorage.setItem('rr-focus', 'g90')")          # this document: no storage event
    type_enter("14.1")
    # Two right answers (2026-11-19): Enter says the focus changed, or the bar
    # redrew for the new focus first and closed the entry (PR 1d closes an open
    # entry on a focus change), so Enter reached nothing. Mission Control's
    # redraws (PR 3b) make the second likelier. Either way nothing is sent,
    # which the next check proves.
    check("6. focus changed while open: Enter says so, or the entry closed",
          bool(b.wait(f"{TOASTS}.some((t) => /focus radio changed/.test(t)) || "
                      f"!{SHOWN}(document.querySelector('#rig-bar .rb-in'))", 3)), True)
    quiet()
    check("  nothing sent; neither dial moved",
          (b.js(SETS), dial(IC)[0], dial(G90)[0]), ([], type_ok, g_before))
    b.js("localStorage.setItem('rr-focus', 'ic7300')")
    b.goto(f"http://127.0.0.1:{WEB}/?r=6#/home")
    b.wait(f"!!{FOC}?.querySelector('button.rb-f') && {FREQ_RE}.test({FOC}.querySelector('.rb-f').textContent)", 20)
    b.js(SENDS)
    open_entry()
    b.js(f"{RIG.format('g90')}.click()")
    check("  a click on the other radio drops the input",
          bool(b.wait(f"{RIG.format('g90')}.classList.contains('focus') && !document.querySelector('#rig-bar .rb-in')?.offsetParent", 5)), True)
    check("  nothing sent", b.js(SETS), [])

    # 7. A refused mode after an accepted freq (a mode this radio doesn't have).
    check("7. back on the IC-7300", focus_on("ic7300"), True)
    r = b.js("station.tuneFocus({ freq: 14074000, mode: 'WFM' }, 'ic7300')")
    check("  freq accepted, mode refused, reported",
          (r or {}).get("ok"), False)
    check("  tuneFocus says which", ((r or {}).get("freq"), (r or {}).get("mode")), (True, False))
    check("  the toast says the mode stayed",
          bool(b.wait(f"{TOASTS}.some((t) => /on 14\\.074\\.000, but mode stayed/.test(t))", 3)), True)
    f7, m7, _ = dial(IC)
    check("  fresh socket: on 14074000, mode unchanged", (f7, m7), (14_074_000, start_m))
    check("  it sent set freq then set mode", b.js(SETS), ["set:freq", "set:mode"])

    # 8. A Winlink session armed and idle on the G90 (auto-tune on).
    check("8. focus on the G90", focus_on("g90"), True)
    check("  auto-tune on: the bar says so", "Auto-tune is on here" in (b.js(f"{FOC}.querySelector('.rb-f').title") or ""), True)
    other = Other(G90)
    other.send(t="winlink_arm", v=True)
    check("  another client arms Winlink", testkit.wait_for(lambda: dial(G90)[2], 15, 0.2), True)
    check("  the bar sees it armed", bool(b.wait(f"/WINLINK/.test({FOC}.querySelector('.rb-tag').textContent)", 10)), True)
    armed_f = dial(G90)[0]
    mark = len(other.msgs)
    b.js(SENDS)
    open_entry()
    type_enter("7.1")
    check("  the bar refuses: a session is armed",
          bool(b.wait(f"{TOASTS}.some((t) => /Winlink session is armed/.test(t))", 3)), True)
    quiet()
    check("  the bar sent nothing", b.js(SETS), [])
    frame_wait("g90", "typeof bandStack === 'function'")
    frame_js("g90", "bandStack('40'), true")
    check("  the radio page's band chip: refused by the radio server (S1)",
          testkit.wait_for(lambda: "refused browser freq" in log_of("g90"), 10, 0.2), True)
    time.sleep(4)            # past AUTO_TUNE_SETTLE_S: a tune would show by now
    check("  the dial never moved (fresh socket), the tuner never tuned",
          (dial(G90)[0], 2 in other.tuner_seen(mark)), (armed_f, False))
    check("  still armed", dial(G90)[2], True)
    other.close()
    check("  the other client leaving disarms it", testkit.wait_for(lambda: not dial(G90)[2], 15, 0.2), True)
    b.wait(f"!/WINLINK/.test({FOC}.querySelector('.rb-tag').textContent)", 10)
    frame_js("g90", "bandStack('40'), true")
    check("  positive control: disarmed, the same band chip moves the dial",
          testkit.wait_for(lambda: 7_000_000 <= (dial(G90)[0] or 0) <= 7_300_000, 10, 0.2), True)

    # Nothing here removed or hid STOP TX or Release TX.
    check("STOP TX still in the bar", b.js(f"{SHOWN}(document.querySelector('#rig-bar .rb-stop-btn'))"), True)
    check("Release TX still in each radio's bar entry",
          b.js("document.querySelectorAll('#rig-bar .rb-release').length"), 2)
finally:
    b.close()
    for p in (front, ic, g90):
        stop(p)

print("PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
