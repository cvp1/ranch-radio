"""The JS8 tab's page (static/addons/js8call.js, js8call.css), in a real
browser: no server, the page's own CSS and script, a stub add-on registry.

  - not set up: the tab says why
  - status: connected, the rig's frequency and mode, receive only
  - activity: one row per audio offset, sorted, the latest text there
  - messages: complete decodes, newest first, from and to
  - heard: newest first; a later frame from a station moves it to the top
  - text from the air is text: a frame holding <img onerror=...> shows
    those characters, adds no element and runs nothing
  - an engine down: "Engine not connected" and why
  - unsafe: an engine reporting TX on turns the status into a red UNSAFE
    banner and the tab's dot to tx

    python3 tests/js8call_page_test.py [screenshot-dir]
"""
import http.server
import json
import os
import shutil
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import testkit  # noqa: E402
from cdp_browser import Browser  # noqa: E402

MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME = (os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
          or (MAC_CHROME if os.path.exists(MAC_CHROME) else None))
if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")
SHOTS = sys.argv[1] if len(sys.argv) > 1 else None
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"), flush=True)


STATUS = {"connected": True, "error": None, "unsafe": [], "radio_connected": True, "radio_freq_khz": 14078.0,
          "radio_mode": "DATA-U", "submode_name": "Normal", "callsign": "N0CALL", "grid": "FN31",
          "transmitting": False, "tx_enabled": False}
EVIL = "<img src=x onerror=window.__pwn=1>"
FRAMES = [
    {"kind": "frame", "utc": "2026-11-21T16:34:30Z", "submode": "Normal", "snr": 7, "freq_hz": 1201.0, "text": "N9ABC: N0CALL  "},
    {"kind": "frame", "utc": "2026-11-21T16:34:30Z", "submode": "Normal", "snr": -12, "freq_hz": 799.5, "text": "K1ABC: @ALLCALL CQ "},
    {"kind": "message", "utc": "2026-11-21T16:35:00Z", "submode": "Normal", "snr": -9, "freq_hz": 1993.0,
     "from": "W1AW", "to": "@HB", "text": "W1AW: @HB HEARTBEAT FN31", "grid": "FN31"},
    {"kind": "frame", "utc": "2026-11-21T16:35:15Z", "submode": "Fast", "snr": 3, "freq_hz": 1500.0, "text": EVIL},
]
SNAP = {"ready": True, "radios": {"ic7300": {"radio": "ic7300", "name": "IC-7300", "status": STATUS, "frames": FRAMES,
        "heard": [{"call": "W1AW", "utc": "2026-11-21T16:35:00Z", "snr": -9, "freq_hz": 1993.0, "submode": "Normal", "grid": "FN31"},
                  {"call": "K1ABC", "utc": "2026-11-21T16:34:30Z", "snr": -12, "freq_hz": 799.5, "submode": "Normal"},
                  {"call": "N9ABC", "utc": "2026-11-21T16:34:30Z", "snr": 7, "freq_hz": 1201.0, "submode": "Normal"}]}}}
css = "".join((ROOT / f).read_text() for f in ("static/app.css", "static/themes.css", "static/addons/js8call.css"))


class Blank(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<!doctype html><html><head></head><body></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


web = http.server.ThreadingHTTPServer(("127.0.0.1", testkit.free_port()), Blank)
threading.Thread(target=web.serve_forever, daemon=True).start()
b = Browser(CHROME, testkit.free_port(), size="1500,900")


def setup(expr):
    r = b.js(expr)
    if isinstance(r, str) and r.startswith("JS error"):
        raise SystemExit(f"setup failed: {r}")
    return r


Q = "(s) => document.querySelector('#j ' + s)"
TEXTS = "(s) => [...document.querySelectorAll('#j ' + s)].map((e) => e.textContent)"

try:
    b.goto(f"http://127.0.0.1:{web.server_address[1]}/")
    setup("""(() => {
      document.documentElement.dataset.theme = 'midnight';
      const s = document.createElement('style'); s.textContent = %s; document.head.appendChild(s);
      window.__impl = {}; window.addons = {register(n, impl) { window.__impl[n] = impl; }};
      document.body.innerHTML = '<div class="addon-body" id="j"></div>';
    })()""" % json.dumps(css))
    setup((ROOT / "static/addons/js8call.js").read_text())
    setup("__impl.js8call.mount(document.getElementById('j')); __impl.js8call.hello({ready: false, why: 'JS8 is not set up here'})")
    check("not set up: says why", b.js(f"({Q})('.js8-status').textContent"), "JS8 is not set up here")

    setup("__impl.js8call.hello(" + json.dumps(SNAP) + ")")
    b.wait("document.querySelectorAll('#j .js8-activity tbody tr').length > 0", 10)
    check("status: listening, frequency, mode, receive only",
          b.js(f"({Q})('.js8-status').textContent"), "Listening · 14.078 MHz DATA-U · N0CALL FN31 · receive only")
    check("activity: one row per offset, sorted",
          b.js("[...document.querySelectorAll('#j .js8-activity tbody tr')].map((r) => r.dataset.bin)"),
          ["800", "1200", "1500"])
    check("messages: from, to and text", b.js(f"({TEXTS})('.js8-msgs .from').concat(({TEXTS})('.js8-msgs .to'))"),
          ["W1AW", "→ @HB"])
    check("heard: as the snapshot has it", b.js(f"({TEXTS})('.js8-heard td.call')"), ["W1AW", "K1ABC", "N9ABC"])
    check("heard: a message's grid", b.js("document.querySelector('#j .js8-heard tbody tr').children[2].textContent"), "FN31")

    check("air text is text: the characters show", b.js("[...document.querySelectorAll('#j .js8-activity td.txt')].some((e) => e.textContent === %s)" % json.dumps(EVIL)), True)
    check("air text is text: no element added", b.js("document.querySelectorAll('#j img').length"), 0)
    check("air text is text: nothing ran", b.js("window.__pwn === undefined"), True)

    setup("__impl.js8call.event({event: 'frame', radio: 'ic7300', frame: {kind: 'frame', utc: '2026-11-21T16:36:00Z', snr: 1, freq_hz: 799.0, submode: 'Normal', text: 'K1ABC: N0CALL SNR? '}})")
    check("a new frame: the offset's row shows it", b.js("document.querySelector('#j .js8-activity tr[data-bin=\"800\"] td.txt').textContent"),
          "K1ABC: N0CALL SNR? ")
    check("a new frame: its station moves to the top of heard", b.js(f"({TEXTS})('.js8-heard td.call')"), ["K1ABC", "W1AW", "N9ABC"])

    setup("__impl.js8call.event({event: 'status', radio: 'ic7300', status: {connected: false, error: 'engine not reachable on 127.0.0.1:4537 (ClientConnectorError)', unsafe: []}})")
    check("engine down: says so, and why", b.js(f"({Q})('.js8-status').textContent"),
          "Engine not connected: engine not reachable on 127.0.0.1:4537 (ClientConnectorError)")
    check("engine down: the tab's dot is off", b.js("__impl.js8call.tab().dot"), "off")

    setup("__impl.js8call.event({event: 'status', radio: 'ic7300', status: Object.assign(" + json.dumps(STATUS) + ", {unsafe: ['tx_enabled'], tx_enabled: true})})")
    check("unsafe: a red banner", b.js(f"[({Q})('.js8-status').classList.contains('unsafe'), ({Q})('.js8-status').textContent.startsWith('UNSAFE: the engine reports tx_enabled on')]"),
          [True, True])
    check("unsafe: the tab's dot is tx", b.js("__impl.js8call.tab().dot"), "tx")
    if SHOTS:
        setup("__impl.js8call.event({event: 'status', radio: 'ic7300', status: " + json.dumps(STATUS) + "})")
        b.screenshot(os.path.join(SHOTS, "js8call.png"))
finally:
    b.close()
    web.shutdown()

print(f"\n{'all ok' if not fails else f'{fails} FAILED'}", flush=True)
sys.exit(1 if fails else 0)
