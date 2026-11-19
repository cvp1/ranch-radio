"""The Roster tab's table (static/addons/roster.js, roster.css), in a real
browser, from a roster the logbook sync builds out of JTDX decodes (the
needs index and decodes as in tests/map_page_test.py). No server: the page's
own CSS and script, a stub add-on registry, and send() recorded.

  hint      once there are rows, the page says what a click does and that
            JTDX may refuse it (it was only a tooltip)
  need      "New call" (the lowest tier, most rows) is quieter than a new
            DXCC; the rows stay in the roster's order
  controls  the Roster's checkboxes and alert select, and the Map's band and
            window selects, share one look (app.css .rr-select, .rr-check)
  narrow    at 390 px the Need column ends inside the screen: Entity, Grid,
            DF, Message and Age step aside, Call, To, Distance, dB and Need
            stay; at 1500 px every column shows
  click     a row click still asks the sync to answer it (do: "reply")

    python3 tests/roster_page_test.py [screenshot-dir]
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


# ---- the roster, as the sync builds it: Japan worked and confirmed on 20m FT8
# in PM95, so JA5XYZ there is only a new call; VK2ABC is a new DXCC.
n = N.Needs(C.Cty((ROOT / "tests/fixtures/cty-sample.csv").read_text()))
n.add("JA1ABC", "20m", "FT8", "", "<GRIDSQUARE:4>PM95", True)
roster = R.Roster(needs=n)
roster.status(U.parse_status(J.status(dial=14_074_000, de="N0CALL", de_grid="FN31")))
for m in ("CQ JA5XYZ PM95", "N0CALL VK9NA -10", "CQ VK2ABC QF56"):
    roster.add(U.parse_decode(J.decode(msg=m)))
view = roster.view()
needs = {r["call"]: r["need"] for r in view["rows"]}
check("fixture: a new call and a new DXCC among the rows",
      (needs.get("JA5XYZ"), needs.get("VK2ABC")), ("call_band_mode", "dxcc"))

css = "".join((ROOT / f).read_text() for f in ("static/app.css", "static/themes.css", "static/addons/roster.css",
                                                "static/addons/map.css"))


class Blank(http.server.BaseHTTPRequestHandler):
    """An empty page on a real origin: roster.js keeps its options in
    localStorage, which about:blank refuses."""

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
b = Browser(CHROME, testkit.free_port(), size="1500,1000")


def setup(expr):
    """A step the checks stand on: a script error here is the test's, so stop."""
    r = b.js(expr)
    if isinstance(r, str) and r.startswith("JS error"):
        raise SystemExit(f"setup failed: {r}")
    return r


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
        '<div class="panel addon addon-map"><div class="addon-body" id="mapbox" style="height:300px"></div></div>';
    })()""" % json.dumps(css))
    setup((ROOT / "static/basemaps.js").read_text() + "; window.basemaps = basemaps;")
    setup((ROOT / "static/addons/map.js").read_text())
    setup((ROOT / "static/addons/roster.js").read_text())
    setup("__impl.roster.mount(document.getElementById('rs')); __impl.roster.hello(" + json.dumps({"roster": view}) + ")")
    setup("__impl.map.mount(document.getElementById('mapbox')); "
         "__impl.map.hello({map: {me: 'FN31', band: '20m', showing: '20m', grids: {}, decodes: []}, pota: []})")
    b.wait("document.querySelectorAll('#rs .rs-row').length === 3 && !!document.querySelector('#mapbox .rm-band')", 10)

    SHOWN = "((e) => !!e && getComputedStyle(e).display !== 'none' && e.getBoundingClientRect().width > 0)"
    check("hint: what a click does, on the page",
          b.js(f"(() => {{ const h = [...document.querySelectorAll('#rs *')].find((e) => e.children.length === 0 && "
               "/Click a station to answer it in JTDX/.test(e.textContent)); "
               f"return h ? [{SHOWN}(h), h.textContent] : null; }})()"),
          [True, "Click a station to answer it in JTDX (it answers a CQ; other messages only if its settings allow)."])

    NEED = ("(c) => { const r = [...document.querySelectorAll('#rs .rs-row')].find((x) => x.dataset.call === c); "
            "const t = r.querySelectorAll('td')[[...r.closest('table').querySelectorAll('th')].findIndex((h) => h.textContent === 'Need')]; "
            "const s = getComputedStyle(t); return [t.textContent, Number(s.fontWeight), s.color]; }")
    new_call, dxcc = b.js(f"({NEED})('JA5XYZ')"), b.js(f"({NEED})('VK2ABC')")
    dim = b.js("(() => { const e = document.createElement('i'); e.style.color = 'var(--dim)'; document.body.append(e); "
               "const c = getComputedStyle(e).color; e.remove(); return c; })()")
    check("need: 'New call' is quieter than 'New DXCC' (lighter, in the dim colour)",
          (new_call[0], dxcc[0], new_call[1] < dxcc[1], new_call[2]), ("New call", "New DXCC", True, dim))
    check("  the rows stay in the roster's order",
          b.js("[...document.querySelectorAll('#rs .rs-row')].map((r) => r.dataset.call)"), [r["call"] for r in view["rows"]])

    LOOK = ("(s) => { const e = document.querySelector(s), c = getComputedStyle(e); "
            "return [c.borderTopLeftRadius, c.fontFamily, c.backgroundColor, c.height]; }")
    looks = {s: b.js(f"({LOOK})('{s}')") for s in ("#rs .rs-alert", "#mapbox .rm-band", "#mapbox .rm-win")}
    plain = b.js("(() => { const e = document.createElement('select'); document.body.append(e); const c = getComputedStyle(e); "
                 "const r = [c.borderTopLeftRadius, c.fontFamily, c.backgroundColor, c.height]; e.remove(); return r; })()")
    check("controls: the Roster's alert and the Map's band and window selects look alike",
          len({json.dumps(v) for v in looks.values()}), 1)
    check("  and not like a bare select", looks["#rs .rs-alert"] != plain, True)
    accent = b.js("(() => { const e = document.createElement('i'); e.style.color = 'var(--accent)'; document.body.append(e); "
                  "const c = getComputedStyle(e).color; e.remove(); return c; })()")
    check("  the Roster's checkboxes take the theme's accent",
          b.js("[...document.querySelectorAll('#rs input[type=checkbox]')].map((e) => getComputedStyle(e).accentColor)"),
          [accent, accent])

    COLS = ("[...document.querySelectorAll('#rs .rs-t th')].filter((h) => getComputedStyle(h).display !== 'none')"
            ".map((h) => h.textContent)")
    ALL = ["Call", "To", "Entity", "Grid", "Distance", "dB", "DF", "Need", "Message", "Age"]
    check("wide (1500 px): every column", b.js(COLS), ALL)
    b.call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=2, mobile=True)
    # The layout viewport (what CSS lays out in), not innerWidth: a phone zooms out
    # to fit a too-wide page, which is the very thing the checks below look for.
    if not b.wait("matchMedia('(width: 390px)').matches", 5):
        raise SystemExit("setup failed: the page did not take the 390 px viewport")
    check("narrow (390 px): Call, To, Distance, dB and Need", b.js(COLS), ["Call", "To", "Distance", "dB", "Need"])
    edge = b.js("(() => { const h = [...document.querySelectorAll('#rs .rs-t th')].find((x) => x.textContent === 'Need'); "
                "return Math.ceil(h.getBoundingClientRect().right); })()")
    check("  the Need column ends inside the screen", (edge <= 390, edge > 0), (True, True))
    if SHOTS:
        b.screenshot(os.path.join(SHOTS, "roster-narrow.png"))
    b.call("Emulation.clearDeviceMetricsOverride")

    b.js("document.querySelector('#rs .rs-row[data-call=\"VK2ABC\"]').click()")
    sent = b.js("JSON.stringify(window.__sent)")
    key = next(r["key"] for r in view["rows"] if r["call"] == "VK2ABC")
    check("click: a row asks the sync to answer that decode",
          json.loads(sent), [{"t": "addon", "addon": "roster", "do": "reply", "key": key, "call": "VK2ABC"}])
finally:
    b.close()
    web.shutdown()

print(f"\n{'all ok' if not fails else f'{fails} FAILED'}", flush=True)
sys.exit(1 if fails else 0)
