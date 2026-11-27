"""The QSO tab on the page (static/addons/qso.js, qso.css), in a real
browser. No server: the page's own CSS and script, a stub add-on registry,
api.send recorded and the station's answers handed in as it would send them.

  dial      the frequency, band and mode follow the radio (SSB with its
            sideband); a dial push moves them; CW makes the reports 599
  lookup    typing a call asks once typing stops, with the dial's kHz, band
            and mode; a late answer (for an older call) is ignored
  fill      the answer fills name, QTH, grid and the POTA park; a field the
            operator typed is left alone; the card shows DXCC, history, need
  escape    text from QRZ is shown as text, never as markup
  log       Log sends the form (call, MHz, mode, sideband, reports, time on,
            parks, own refs); the answer clears the form and lists the QSO
  login     no QRZ password: the login form shows; Save sends the password
            and clears the box; logged in, the form goes away
  no sync   without the logbook sync, Log is disabled and the tab says why

    python3 tests/qso_page_test.py [screenshot-dir]
"""
import http.server
import json
import os
import shutil
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
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


css = "".join((ROOT / f).read_text() for f in ("static/app.css", "static/themes.css", "static/callcard.css",
                                                "static/addons/qso.css"))
SNAP = {"rigs": [{"id": "g90", "name": "G90", "present": True, "connected": True, "freq": 14_285_000, "mode": "USB", "data": False},
                 {"id": "qmx", "name": "QMX", "present": True, "connected": True, "freq": 7_030_000, "mode": "CW", "data": False}],
        "radio": "g90", "my": {"my_pota": "US-0001"}, "own": "N0CALL", "logsync": True,
        "qrz": {"user": "N0CALL", "set": False, "state": "no_login"},
        "spots": {"n": 3, "age_s": 30, "error": None, "on": True}}


class Blank(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'<!doctype html><html><head></head><body></body></html>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


web = http.server.ThreadingHTTPServer(("127.0.0.1", testkit.free_port()), Blank)
threading.Thread(target=web.serve_forever, daemon=True).start()
b = Browser(CHROME, testkit.free_port(), size="1200,900")


def setup(expr):
    r = b.js(expr)
    if isinstance(r, str) and r.startswith("JS error"):
        raise SystemExit(f"setup failed: {r}")
    return r


def sent():
    return json.loads(b.js("JSON.stringify(window.__sent)"))


def v(sel):
    return b.js(f"document.querySelector({json.dumps(sel)}).value")


def text(sel):
    return b.js(f"document.querySelector({json.dumps(sel)}).textContent.trim()")


def typein(sel, value):
    b.js(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); e.value = {json.dumps(value)};"
         " e.dispatchEvent(new Event('input', {bubbles: true})); })()")


def event(m):
    b.js(f"__impl.qso.event({json.dumps(m)})")


def mount(snap):
    setup("document.getElementById('qb').innerHTML = ''; window.__sent = [];"
          "__impl.qso.mount(document.getElementById('qb'), {send: (verb, extra) => window.__sent.push({do: verb, ...(extra || {})})});"
          f"__impl.qso.hello({json.dumps(snap)})")


try:
    b.goto(f"http://127.0.0.1:{web.server_address[1]}/")
    setup("""(() => {
      document.documentElement.dataset.theme = 'midnight';
      const s = document.createElement('style'); s.textContent = %s; document.head.appendChild(s);
      window.esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
      window.toast = () => {}; window.__sent = [];
      window.__impl = {}; window.addons = {register(n, impl) { window.__impl[n] = impl; }};
      document.body.innerHTML = '<div class="panel addon addon-qso"><div class="addon-body" id="qb"></div></div>';
    })()""" % json.dumps(css))
    setup((ROOT / "static/addons/qso.js").read_text())
    mount(SNAP)

    check("dial: from the radio", (v(".qso-mhz"), v(".qso-mode"), text(".qso-dial")),
          ("14.28500", "SSB", "14.28500 MHz · 20m · SSB (USB)"))
    check("dial: the boxes follow, not typed", b.js("document.querySelector('.qso-mhz').disabled"), True)
    event({"event": "rigs", "rigs": [{**SNAP["rigs"][0], "freq": 7_200_000, "mode": "LSB"}, SNAP["rigs"][1]]})
    check("dial: a push moves it", text(".qso-dial"), "7.20000 MHz · 40m · SSB (LSB)")
    b.js("(() => { const s = document.querySelector('.qso-radio'); s.value = 'qmx'; s.dispatchEvent(new Event('change')); })()")
    check("dial: another radio, CW, reports 599", (v(".qso-mode"), v(".qso-rsts"), v(".qso-rstr")), ("CW", "599", "599"))
    b.js("(() => { const s = document.querySelector('.qso-radio'); s.value = 'g90'; s.dispatchEvent(new Event('change')); })()")
    check("dial: back to phone, reports 59", (v(".qso-mode"), v(".qso-rsts")), ("SSB", "59"))
    event({"event": "rigs", "rigs": SNAP["rigs"]})

    check("login: no QRZ password, the form shows", b.js("document.querySelector('.qso-login').hidden"), False)
    check("my station: the sticky ref is there", v('.qso-my-f[data-k="my_pota"]'), "US-0001")

    typein(".qso-call", "n9a")
    typein(".qso-call", "n9abc")
    b.wait("window.__sent.some((m) => m.do === 'lookup')", 5)
    look = [m for m in sent() if m["do"] == "lookup"]
    check("lookup: once, after typing stops", len(look), 1)
    check("lookup: the call, upper-cased, with the dial", (look[0]["call"], look[0]["khz"], look[0]["band"], look[0]["mode"]),
          ("N9ABC", 14285, "20m", "SSB"))
    check("time on: set when the call was first typed", bool(b.js("document.querySelector('.qso-ton').dataset.auto")), True)

    typein('.qso-f[data-k="name"]', "Patricia")
    event({"event": "lookup", "id": "q0", "call": "W1AW", "qrz": {"name": "Wrong"}})
    check("late: an older call's answer is ignored", v('.qso-f[data-k="qth"]'), "")
    event({"event": "lookup", "id": look[0]["id"], "call": "N9ABC",
           "dxcc": {"dxcc": 291, "name": "United States", "cont": "NA", "cq": 4, "itu": 7},
           "qsos": [{"date": "2026-10-01", "band": "40m", "mode": "SSB", "confirmed": True}],
           "pota": [{"ref": "US-1234", "name": "A Park", "loc": "US-IL", "khz": 14285, "mode": "SSB", "age_s": 120, "on_freq": True}],
           "pota_ref": "US-1234", "spots_age_s": 30, "qrz_state": "ok",
           "qrz": {"name": "Pat <img src=x id=pwn>", "qth": "Springfield", "state": "IL", "county": "Sangamon",
                   "country": "United States", "grid": "EN59AX", "dxcc": "291"},
           "need": {"level": "grid", "reasons": []}, "qrz_login": SNAP["qrz"]})
    check("fill: QTH, state, grid, park", (v('.qso-f[data-k="qth"]'), v('.qso-f[data-k="state"]'), v('.qso-f[data-k="grid"]'),
                                           v('.qso-f[data-k="pota"]')), ("Springfield", "IL", "EN59AX", "US-1234"))
    check("fill: a typed name is left alone", v('.qso-f[data-k="name"]'), "Patricia")
    card = text(".qso-card")
    check("card: DXCC, history, need", ("United States" in card, "worked 1×" in card, "New grid" in card), (True, True, True))
    check("escape: QRZ's text is never markup", b.js("!!document.getElementById('pwn')"), False)
    check("spots: the park is offered, marked on the dial", text(".qso-park"), "US-1234 ●")
    if SHOTS:
        b.screenshot(os.path.join(SHOTS, "qso-filled.png"))

    b.js("document.querySelector('.qso-log').click()")
    lg = [m for m in sent() if m["do"] == "log"]
    check("log: one request", len(lg), 1)
    f = lg[0]["qso"]
    check("log: the form", (f["call"], f["mhz"], f["mode"], f["sideband"], f["rst_sent"], f["pota"], f["my_pota"], f["name"],
                            f["country"], f["dxcc"]),
          ("N9ABC", "14.28500", "SSB", "USB", "59", "US-1234", "US-0001", "Patricia", "United States", "291"))
    check("log: the time on, HH:MM:SS", len(f["time_on"]), 8)
    event({"event": "logged", "id": lg[0]["id"], "qid": 7, "what": "new", "call": "N9ABC", "band": "20m", "mode": "USB", "time": "1500"})
    check("log: the form is cleared", (v(".qso-call"), v('.qso-f[data-k="name"]'), v('.qso-f[data-k="pota"]')), ("", "", ""))
    check("log: the own ref stays", v('.qso-my-f[data-k="my_pota"]'), "US-0001")
    check("log: listed", text(".qso-done td:nth-child(2)"), "N9ABC")
    check("log: the status says it waits to send", "waiting to send" in text(".qso-status"), True)
    event({"event": "logged", "id": "l9", "error": "frequency: a number in MHz"})
    check("log: an error is shown", text(".qso-status"), "frequency: a number in MHz")

    b.js("document.querySelector('.qso-qp').value = 'hunter2'; document.querySelector('.qso-qgo').click()")
    li = [m for m in sent() if m["do"] == "login"]
    check("login: sent with the password", (len(li), li[0]["user"], li[0]["password"]), (1, "N0CALL", "hunter2"))
    check("login: the box is cleared as it is sent", v(".qso-qp"), "")
    event({"event": "login", "qrz_login": {"user": "N0CALL", "set": True, "state": "ok"}})
    check("login: logged in, the form goes away", b.js("document.querySelector('.qso-login').hidden"), True)
    check("tab: ok, a badge for the QSO logged", json.loads(b.js("JSON.stringify(__impl.qso.tab())")),
          {"dot": "ok", "title": "Log a contact", "badge": 1})

    mount({**SNAP, "logsync": False})
    check("no sync: Log disabled, and said", (b.js("document.querySelector('.qso-log').disabled"),
                                              "isn't running" in text(".qso-warn")), (True, True))
    if SHOTS:
        b.screenshot(os.path.join(SHOTS, "qso-nosync.png"))
finally:
    b.close()
    web.shutdown()

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
