"""The JS8 tab's page (static/addons/js8call.js, js8call.css), in a real
browser: no server, the page's own CSS and script, a stub add-on registry.

  - not set up: the tab says why
  - status card: a one-word state (Listening, TX armed, ON AIR, STOP TX),
    frequency, mode, speed, your call and grid; the send panel says to arm
  - YOU: frames addressed to your call are marked; others are not
  - arm: the Arm button sends arm with the minutes chosen; disabled with the
    reason when the radio can't key
  - disarmed: HB, CQ, the queries and the message box shown, disabled, saying why
  - armed: the countdown; HB sends hb; CQ sends @ALLCALL CQ CQ CQ GRID; a
    station picked from Heard, Messages or Band activity enables SNR? GRID?
    INFO? STATUS? HEARING? and addresses the message box; offset and speed
    go with a send; Send and Enter send the text (and clear the box); Disarm sends disarm; a frame arriving while typing
    keeps the text and the focus; ON AIR when the radio's server says keyed
  - bands: the JS8 calling frequencies, the one the radio is on marked; a
    click asks the page (api.tune) for that exact dial, USB with data on, on
    this radio, and sends the add-on nothing; armed, every band button is
    disabled saying why and a click tunes nothing; a page with no tuning
    disables them
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
    setup("window.__sent = []; window.__tuned = []; __impl.js8call.mount(document.getElementById('j'), {send: (v, x) => __sent.push([v, x]), canTune: () => window.__canTune !== false, tune: (o) => { __tuned.push(o); return Promise.resolve({ok: true}); }}); __impl.js8call.hello({ready: false, why: 'JS8 is not set up here'})")
    check("not set up: says why", b.js(f"({Q})('.js8-status').textContent"), "JS8 is not set up here")

    setup("__impl.js8call.hello(" + json.dumps(SNAP) + ")")
    b.wait("document.querySelectorAll('#j .js8-activity tbody tr').length > 0", 10)
    check("status card: state, frequency, mode, speed",
          b.js(f"[({Q})('.js8-state').textContent, ({Q})('.js8-status').textContent]"), ["Listening", "14.078 MHz DATA-U · Normal"])
    check("status card: your call and grid", b.js(f"({Q})('.js8-me').textContent"), "YouN0CALLFN31")
    check("send panel: says to arm first", b.js(f"({Q})('.js8-gate').textContent"), "Arm TX above to send")
    check("bands: the JS8 dials, the one it's on marked",
          b.js(f"[({TEXTS})('.js8-bands .chip'), ({Q})('.js8-bands .chip.on').dataset.band]"),
          [["160 m", "80 m", "40 m", "30 m", "20 m", "17 m", "15 m", "12 m", "10 m", "6 m"], "20"])
    check("bands: the title says the dial, data, and the ATU carrier",
          b.js(f"({Q})('.js8-bands [data-band=\"40\"]').title"),
          "7.078 MHz USB, data on. With auto-tune on, the ATU tunes the new band: a short carrier.")
    setup(f"({Q})('.js8-bands [data-band=\"40\"]').click()")
    check("bands: a click asks the page to tune the exact dial, USB data, on this radio",
          b.js("__tuned.at(-1)"), {"band": "40", "freq": 7078000, "mode": "USB", "data": True, "radio": "ic7300"})
    check("bands: it sends nothing to the add-on itself", b.js("__sent.length"), 0)
    check("lists: each says what it is", b.js(f"({TEXTS})('.js8-pane h3')"), ["Band activity", "Messages", "Stations heard"])
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
    check("a new frame: the offset's row shows it", b.js("document.querySelector('#j .js8-activity tr[data-bin=\"800\"] td.txt .air').textContent"),
          "K1ABC: N0CALL SNR? ")
    check("addressed to you: marked YOU", b.js("[...document.querySelectorAll('#j .js8-activity tr.to-me')].map((r) => r.dataset.bin)"),
          ["800", "1200"])
    check("not to you: not marked", b.js("document.querySelector('#j .js8-activity tr[data-bin=\"1500\"]').classList.contains('to-me')"), False)
    check("a new frame: its station moves to the top of heard", b.js(f"({TEXTS})('.js8-heard td.call')"), ["K1ABC", "W1AW", "N9ABC"])

    # ------------------------------------------------------------ sending
    check("disarmed: Arm shown; HB, CQ, queries and the box shown but disabled",
          b.js(f"[!!({Q})('.js8-arm'), ...['.js8-hb', '.js8-cq', '.js8-text', '.js8-send', '.js8-q'].map((x) => [({Q})(x).hidden, ({Q})(x).disabled])]"),
          [True, [False, True], [False, True], [False, True], [False, True], [False, True]])
    check("disarmed: buttons say why", b.js(f"({Q})('.js8-hb').title"), "Arm TX first")
    setup(f"({Q})('.js8-mins [data-min=\"5\"]').click()")
    check("minutes: the chip picked is on", b.js(f"({Q})('.js8-mins .chip.on').dataset.min"), "5")
    setup(f"({Q})('.js8-arm').click()")
    check("Arm sends arm with the minutes", b.js("__sent.at(-1)"), ["arm", {"radio": "ic7300", "minutes": 5}])
    setup("__impl.js8call.event({event: 'status', radio: 'ic7300', status: Object.assign(" + json.dumps(STATUS)
          + ", {why_not: 'transmit is off on this radio (--no-tx)'})})")
    check("can't key: Arm disabled, says why", b.js(f"[({Q})('.js8-arm').disabled, ({Q})('.js8-why').textContent]"),
          [True, "Can't arm: transmit is off on this radio (--no-tx)"])
    ARMED = dict(STATUS, armed=True, tx_enabled=True, why_not=None)
    setup("__impl.js8call.event({event: 'status', radio: 'ic7300', status: Object.assign(" + json.dumps(ARMED)
          + ", {armed_until: Date.now() / 1000 + 299.5})})")
    check("armed: Arm gone, Disarm shown, HB/CQ/box enabled",
          b.js(f"[!!({Q})('.js8-arm'), !!({Q})('.js8-disarm'), ({Q})('.js8-hb').disabled, ({Q})('.js8-cq').disabled, ({Q})('.js8-text').disabled]"),
          [False, True, False, False, False])
    check("armed: the band buttons are disabled, saying why",
          b.js(f"[[...document.querySelectorAll('#j .js8-bands .chip')].every((x) => x.disabled), ({Q})('.js8-bands .chip').title]"),
          [True, "Disarm first: a band change isn't made while JS8 is armed"])
    n = b.js("__tuned.length")
    setup(f"({Q})('.js8-bands [data-band=\"80\"]').click()")
    check("armed: a click on a band tunes nothing", b.js("__tuned.length"), n)
    check("armed: the gate says so", b.js(f"({Q})('.js8-gate').textContent.startsWith('Armed')"), True)
    check("armed: HB keeps its own tooltip", b.js(f"({Q})('.js8-hb').title.startsWith('Heartbeat:')"), True)
    check("armed, no station picked: queries disabled", b.js(f"[...document.querySelectorAll('#j .js8-q')].every((x) => x.disabled)"), True)
    setup(f"({Q})('.js8-hb').click()")
    check("HB sends hb", b.js("__sent.at(-1)"), ["hb", {"radio": "ic7300"}])
    setup(f"({Q})('.js8-cq').click()")
    check("CQ sends @ALLCALL CQ CQ CQ and the grid", b.js("__sent.at(-1)"), ["send", {"radio": "ic7300", "text": "@ALLCALL CQ CQ CQ FN31"}])
    setup("document.querySelector('#j .js8-heard tbody tr').click()")
    check("click a heard station: picked", b.js(f"[({Q})('.js8-to .call').textContent, !!document.querySelector('#j .js8-heard tr.sel')]"), ["K1ABC", True])
    check("picked: Ask names the station", b.js(f"[...document.querySelectorAll('#j .js8-field .label')].map((e) => e.textContent).includes('Ask K1ABC')"), True)
    check("picked: queries enabled", b.js(f"[...document.querySelectorAll('#j .js8-q')].every((x) => !x.disabled)"), True)
    setup("[...document.querySelectorAll('#j .js8-q')].find((x) => x.dataset.q === 'SNR?').click()")
    check("SNR? to the picked station", b.js("__sent.at(-1)"), ["send", {"radio": "ic7300", "text": "K1ABC SNR?"}])
    setup(f"({Q})('.js8-opts').open = true; ({Q})('.js8-offset').value = '1650'; ({Q})('.js8-speed [data-speed=\"fast\"]').click()")
    setup("[...document.querySelectorAll('#j .js8-q')].find((x) => x.dataset.q === 'GRID?').click()")
    check("offset and speed go with it", b.js("__sent.at(-1)"),
          ["send", {"radio": "ic7300", "text": "K1ABC GRID?", "offset_hz": 1650, "speed": "fast"}])
    setup(f"{{ const i = ({Q})('.js8-text'); i.value = 'tnx for the call'; ({Q})('.js8-send').click(); }}")
    check("a message to the picked station is addressed to it", b.js("__sent.at(-1)[1].text"), "K1ABC TNX FOR THE CALL")
    setup("document.querySelector('#j .js8-msgs li.pickable').click()")
    check("click a message: picks its sender", b.js(f"({Q})('.js8-to .call').textContent"), "W1AW")
    setup("document.querySelector('#j .js8-msgs li.pickable').click()")
    check("click it again: cleared", b.js(f"!!({Q})('.js8-to .call')"), False)
    setup("document.querySelector('#j .js8-activity tr[data-bin=\"1200\"]').click()")
    check("click band activity: picks the sender", b.js(f"({Q})('.js8-to .call').textContent"), "N9ABC")
    setup(f"({Q})('.js8-clear').click(); ({Q})('.js8-offset').value = ''; ({Q})('.js8-speed [data-speed=\"\"]').click()")
    check("clear: to everyone again", b.js(f"[!!({Q})('.js8-to .call'), ({Q})('.js8-prefix').textContent]"), [False, ""])
    check("armed: the countdown", b.js(f"/^TX armed · (4:5\\d|5:00)$/.test(({Q})('.js8-state').textContent)"), True)
    setup(f"{{ const i = ({Q})('.js8-text'); i.focus(); i.value = 'N9ABC SNR?'; }}")
    setup("__impl.js8call.event({event: 'frame', radio: 'ic7300', frame: {kind: 'frame', utc: '2026-11-21T16:36:15Z', snr: 2, freq_hz: 1300.0, text: 'K2ABC: CQ'}})")
    check("a frame while typing: text and focus kept", b.js(f"[({Q})('.js8-text').value, document.activeElement === ({Q})('.js8-text')]"),
          ["N9ABC SNR?", True])
    check("a frame while typing: the activity still updates",
          b.js("!!document.querySelector('#j .js8-activity tr[data-bin=\"1300\"]')"), True)
    setup(f"({Q})('.js8-send').click()")
    check("Send sends the text, clears the box", [b.js("__sent.at(-1)"), b.js(f"({Q})('.js8-text').value")],
          [["send", {"radio": "ic7300", "text": "N9ABC SNR?"}], ""])
    setup(f"{{ const i = ({Q})('.js8-text'); i.value = 'CQ CQ'; i.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter'}})); }}")
    check("Enter sends", b.js("__sent.at(-1)"), ["send", {"radio": "ic7300", "text": "CQ CQ"}])
    setup("__impl.js8call.event({event: 'status', radio: 'ic7300', status: Object.assign(" + json.dumps(ARMED)
          + ", {armed_until: Date.now() / 1000 + 200, radio_tx: true})})")
    check("keyed: ON AIR", b.js(f"[({Q})('.js8-state').textContent, ({Q})('.js8-head').classList.contains('onair')]"),
          ["ON AIR", True])
    check("armed: the tab's dot is tx", b.js("__impl.js8call.tab().dot"), "tx")
    if SHOTS:
        b.screenshot(os.path.join(SHOTS, "js8call-armed.png"))
    setup(f"({Q})('.js8-disarm').click()")
    check("Disarm sends disarm", b.js("__sent.at(-1)"), ["disarm", {"radio": "ic7300"}])
    setup("__impl.js8call.event({event: 'status', radio: 'ic7300', status: " + json.dumps(STATUS) + "})")
    check("disarmed again: Arm back", b.js(f"!!({Q})('.js8-arm')"), True)
    setup("__impl.js8call.event({event: 'status', radio: 'ic7300', status: Object.assign(" + json.dumps(STATUS)
          + ", {why_not: 'STOP TX is on at the radio; Resume there first'})})")
    check("STOP TX at the radio: the state says so, Arm says why",
          b.js(f"[({Q})('.js8-state').textContent, ({Q})('.js8-arm').disabled, ({Q})('.js8-why').textContent]"),
          ["STOP TX", True, "Can't arm: STOP TX is on at the radio; Resume there first"])
    if SHOTS:
        setup("document.querySelector('#j .js8-heard tbody tr').click()")
        setup("__impl.js8call.event({event: 'status', radio: 'ic7300', status: " + json.dumps(STATUS) + "})")
        b.screenshot(os.path.join(SHOTS, "js8call-disarmed.png"))

    setup("window.__canTune = false; __impl.js8call.event({event: 'status', radio: 'ic7300', status: " + json.dumps(STATUS) + "})")
    check("no tuning on this page: band buttons disabled, saying so",
          b.js(f"[({Q})('.js8-bands .chip').disabled, ({Q})('.js8-bands .chip').title]"), [True, "No tuning from this page"])
    setup("window.__canTune = true")
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
