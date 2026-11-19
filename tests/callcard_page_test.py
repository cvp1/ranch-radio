"""The call card on the page (static/callcard.js, callcard.css; the Roster's
info button and the Map's station card; 2026-11-19, UX plan PR 4b), in a real
browser. No server: the page's own CSS and scripts, a stub add-on registry,
send() recorded and the station's answers handed in as it would send them.

  button    every Roster row has an info button
  click     a click on it asks the station once (do: "lookup", the row's
            call, grid, band and mode) and sends no Reply; a click on the
            row itself still does
  late      the card moved on to another call: the first call's answer,
            arriving late, is ignored; the current one fills the card
  answer    the QSOs, the confirmed tick, the need; a Roster push redraws
            the table and the card stays
  qrz       the link's path is the encoded call, target _blank with
            rel="noopener noreferrer"; the page fetches nothing from QRZ
  close     Escape closes it; an answer after that is ignored
  map       the Map's station card holds the same block, asks through the
            Map's route, and keeps its answer when the card redraws
  malformed the station's "not a callsign" shows as the card's error

    python3 tests/callcard_page_test.py [screenshot-dir]
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

import fake_jtdx as J  # noqa: E402
import logsync_cty as C  # noqa: E402
import logsync_needs as N  # noqa: E402
import logsync_roster as R  # noqa: E402
import logsync_udp as U  # noqa: E402

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


# ---- a roster as the sync builds it (as in tests/roster_page_test.py)
n = N.Needs(C.Cty((ROOT / "tests/fixtures/cty-sample.csv").read_text()))
n.add("JA1ABC", "20m", "FT8", "", "<GRIDSQUARE:4>PM95", True)
roster = R.Roster(needs=n)
roster.status(U.parse_status(J.status(dial=14_074_000, de="N0CALL", de_grid="FN31")))
for m in ("CQ JA5XYZ PM95", "N0CALL VK9NA -10", "CQ VK2ABC QF56"):
    roster.add(U.parse_decode(J.decode(msg=m)))
view = roster.view()
row = {r["call"]: r for r in view["rows"]}

css = "".join((ROOT / f).read_text() for f in ("static/app.css", "static/themes.css", "static/addons/roster.css",
                                                "static/addons/map.css", "static/callcard.css"))


class Blank(http.server.BaseHTTPRequestHandler):
    """An empty page on a real origin (roster.js keeps options in localStorage)."""

    def do_GET(self):
        body = b'<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body></body></html>'
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


def click(sel):
    """A real mouse click at the element's centre (the event bubbles as the operator's would)."""
    x, y = json.loads(b.js(f"(() => {{ const r = document.querySelector('{sel}').getBoundingClientRect(); "
                           "return JSON.stringify([r.x + r.width / 2, r.y + r.height / 2]); })()"))
    for t in ("mousePressed", "mouseReleased"):
        b.call("Input.dispatchMouseEvent", type=t, x=x, y=y, button="left", clickCount=1)


POP = "document.querySelector('.cc-pop')"
POP_TEXT = (f"(({POP} && !{POP}.hidden) ? [...{POP}.querySelectorAll('.cc-call, .cc-qrz, .cc-block > .cc-dim')]"
            ".map((e) => e.textContent.trim()).join(' ') : null)")


def answer(addon, a):
    b.js(f"__impl.{addon}.event({{t: 'addon', addon: '{addon}', callcard: {json.dumps(a)}}})")


try:
    b.goto(f"http://127.0.0.1:{web.server_address[1]}/")
    setup("""(() => {
      document.documentElement.dataset.theme = 'midnight';
      const s = document.createElement('style'); s.textContent = %s; document.head.appendChild(s);
      document.body.style.margin = '0';
      window.esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
      window.toast = () => {}; window.__sent = []; window.send = (m) => window.__sent.push(m);
      window.__impl = {}; window.addons = {register(n, impl) { window.__impl[n] = impl; }};
      document.body.innerHTML = '<div class="panel addon addon-roster"><div class="addon-body" id="rs"></div></div>' +
        '<div class="panel addon addon-map"><div class="addon-body" id="mapbox" style="height:420px"></div></div>';
    })()""" % json.dumps(css))
    setup((ROOT / "static/callcard.js").read_text())
    setup((ROOT / "static/basemaps.js").read_text() + "; window.basemaps = basemaps;")
    setup((ROOT / "static/addons/map.js").read_text())
    setup((ROOT / "static/addons/roster.js").read_text())
    setup("__impl.roster.mount(document.getElementById('rs')); __impl.roster.hello(" + json.dumps({"roster": view}) + ")")
    b.wait("document.querySelectorAll('#rs .rs-row').length === 3", 10)

    check("button: one info button on every Roster row",
          b.js("[...document.querySelectorAll('#rs .rs-row')].map((r) => r.querySelectorAll('.cc-info').length)"), [1, 1, 1])

    click("#rs .rs-row[data-call=\"VK2ABC\"] .cc-info")
    b.wait(f"{POP} && !{POP}.hidden", 5)
    s = sent()
    check("click: the info button asks the station once, with the row's call, grid, band and mode",
          [{k: v for k, v in m.items() if k != "id"} for m in s],
          [{"t": "logsync", "do": "lookup", "call": "VK2ABC", "addon": "roster", "grid": "QF56",
            "band": row["VK2ABC"]["band"], "mode": row["VK2ABC"]["mode"]}])
    check("  and sends no Reply", [m for m in s if m.get("do") == "reply"], [])
    check("  the card opens, waiting", b.js(POP_TEXT), "VK2ABC QRZ ↗ Looking it up in the logbook…")
    first = s[0]["id"]

    click("#rs .rs-row[data-call=\"JA5XYZ\"] .cc-info")
    b.wait(f"window.__sent.length === 2 && {POP_TEXT}.startsWith('JA5XYZ')", 5)
    second = sent()[1]["id"]
    check("late: a second card is a second request", (second != first, sent()[1]["call"]), (True, "JA5XYZ"))
    answer("roster", {"id": first, "call": "VK2ABC", "more": False, "need": None,
                      "qsos": [{"date": "2026-01-02", "band": "20m", "mode": "FT8", "confirmed": True}]})
    check("  the first call's answer, arriving late, is ignored", b.js(POP_TEXT), "JA5XYZ QRZ ↗ Looking it up in the logbook…")
    answer("roster", {"id": second, "call": "JA5XYZ", "more": False, "need": {"level": "call_band_mode", "reasons": ["call_band_mode"]},
                      "qsos": [{"date": "2026-03-01", "band": "20m", "mode": "FT8", "confirmed": False},
                               {"date": "2025-06-01", "band": "15m", "mode": "FT4", "confirmed": True}]})
    check("answer: the current one fills the card",
          b.js(f"[...{POP}.querySelector('.cc-sum').children].map((e) => e.textContent.trim()).join(' ')"), "2 QSOs · 1 confirmed Need: New call")
    check("  one row per QSO, the confirmed one ticked",
          json.loads(b.js(f"JSON.stringify([...{POP}.querySelectorAll('.cc-t tr')].slice(1).map((r) => [...r.cells].map((c) => c.textContent.trim())))")),
          [["2026-03-01", "20m", "FT8", ""], ["2025-06-01", "15m", "FT4", "✓"]])
    check("  still no Reply sent", [m for m in sent() if m.get("do") == "reply"], [])
    b.js("__impl.roster.event({t: 'addon', addon: 'roster', roster: " + json.dumps(view) + "})")
    check("  a Roster push redraws the table and the card stays", b.js(f"!{POP}.hidden && {POP_TEXT}.startsWith('JA5XYZ')"), True)
    if SHOTS:
        b.screenshot(os.path.join(SHOTS, "callcard-roster.png"))

    link = json.loads(b.js(f"JSON.stringify((() => {{ const a = {POP}.querySelector('a.cc-qrz'); return [a.getAttribute('href'), a.target, a.rel]; }})())"))
    check("qrz: the call's page, opened apart from this one", link, ["https://www.qrz.com/db/JA5XYZ", "_blank", "noopener noreferrer"])
    b.js("document.body.insertAdjacentHTML('beforeend', '<div id=\"enc\">' + callcard.block('t', 'roster', 'W1AW/P&<x>', {}) + '</div>')")
    check("  the path segment is encoded", b.js("document.querySelector('#enc a.cc-qrz').getAttribute('href')"),
          "https://www.qrz.com/db/W1AW%2FP%26%3Cx%3E")
    b.js("document.getElementById('enc').remove()")
    check("  the page fetched nothing from QRZ",
          b.js("performance.getEntriesByType('resource').filter((e) => /qrz/i.test(e.name)).length"), 0)

    b.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}))")
    check("close: Escape closes the card", b.js(f"{POP}.hidden"), True)
    answer("roster", {"id": second, "call": "JA5XYZ", "more": False, "need": None, "qsos": []})
    check("  an answer after that is ignored", b.js(f"{POP}.hidden || {POP_TEXT}"), True)

    click("#rs .rs-row[data-call=\"VK2ABC\"] .cc-info")
    b.wait(f"{POP} && !{POP}.hidden", 5)
    answer("roster", {"id": sent()[-1]["id"], "error": "not a callsign"})
    check("malformed: the station's answer is the card's error",
          b.js(f"{POP}.querySelector('.cc-err')?.textContent"), "not a callsign")
    click("#rs .rs-row[data-call=\"VK2ABC\"] .rs-c-age")      # clear of the card, which sits beside the button
    check("  a click outside closes it, and a click on the row is still a Reply",
          (b.js(f"{POP}.hidden"), [m["call"] for m in sent() if m.get("do") == "reply"]), (True, ["VK2ABC"]))

    # ---- the Map's station card
    now = 2_000_000
    dec = {"key": "k1", "answer": "cq", "call": "VK2ABC", "grid": "QF56", "need": "dxcc", "reasons": ["dxcc"], "snr": -12,
           "cq": True, "to_me": False, "entity": "Australia", "km": 16000, "bearing": 240, "age": 5, "band": "20m",
           "mode": "FT8", "message": "CQ VK2ABC QF56", "lotw": False}
    snap = {"now": now, "map": {"me": "FN31", "band": "20m", "showing": "20m", "grids": {}, "decodes": [dec]},
            "heard": [], "rig": None, "radios": [], "hears_me": None, "hears_me_why": "not set up", "pota": []}
    setup("__impl.map.mount(document.getElementById('mapbox')); __impl.map.hello(" + json.dumps(snap) + ")")
    b.wait("document.querySelector('#mapbox .rm-s[data-call=\"VK2ABC\"]')", 10)
    before = len(sent())
    b.js("document.querySelector('#mapbox .rm-s[data-call=\"VK2ABC\"]').dispatchEvent(new MouseEvent('click', {bubbles: true}))")
    b.wait("!!document.querySelector('#mapbox .rm-card:not([hidden]) .cc-block')", 5)
    asked = sent()[before:]
    check("map: the station card holds the call card and asks through the Map's route",
          [{k: v for k, v in m.items() if k != "id"} for m in asked],
          [{"t": "logsync", "do": "lookup", "call": "VK2ABC", "addon": "map", "grid": "QF56", "band": "20m", "mode": "FT8"}])
    answer("map", {"id": asked[0]["id"], "call": "VK2ABC", "more": False, "need": {"level": "dxcc", "reasons": ["dxcc"]},
                   "qsos": []})
    CARD = "[...document.querySelector('#mapbox .rm-card .cc-sum').children].map((e) => e.textContent.trim()).join(' ')"
    check("  the answer fills it", b.js(CARD), "Not in the logbook. Need: New DXCC")
    b.js("__impl.map.event({t: 'addon', addon: 'map', now: %d, map: %s})" % (now, json.dumps(snap["map"])))
    check("  a redraw keeps the answer and asks nothing new", (b.js(CARD), len(sent()) - before), ("Not in the logbook. Need: New DXCC", 1))
    check("  and no Reply from the card", [m for m in sent()[before:] if m.get("do") == "reply"], [])
finally:
    b.close()
    web.shutdown()

print(f"\n{'all ok' if not fails else f'{fails} FAILED'}", flush=True)
sys.exit(1 if fails else 0)
