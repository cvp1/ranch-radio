"""The Grids tab's page (static/addons/grids.js, grids.css), in a real
browser: no server, the page's own CSS and script, a stub add-on registry,
and the browser's sound, speech and toasts counted instead of played.

  draw      the 488 Lower-48 squares; worked, confirmed and missing as the
            index says, for the picked mode; the tally per mode
  ring      a missing square a station is in now is ringed and listed
            first; a worked one is not
  open      what is on the air when the page opens never sounds
  off       the default: a new missing square on the air shows (tab dot)
            but makes no sound
  chime     picking it chimes once (the click a browser wants first); then a
            new missing square chimes once with a toast, the same one again
            does not, a worked square does not, another mode's does not
  voice     speaks the square spelled out, with band and mode

    python3 tests/grids_page_test.py [screenshot-dir]
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

import grid_targets  # noqa: E402

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


# FN31 confirmed on FT8, EM12 worked on FT8 and FT4, EN10 only on SSB.
SNAP = {"ready": True, "qsos": 4, "me": "EM12",
        "cells": {"FN31": [["20m", "FT8", 1]], "EM12": [["20m", "FT4", 0], ["40m", "FT8", 0]], "EN10": [["20m", "SSB", 1]]},
        "sets": {"l48": {"label": "Lower 48", "grids": sorted(grid_targets.LOWER_48)}},
        "heard": [{"grid": "EM13", "call": "N9ABC", "band": "20m", "mode": "FT8", "snr": -12}]}
css = "".join((ROOT / f).read_text() for f in ("static/app.css", "static/themes.css", "static/addons/grids.css"))


class Blank(http.server.BaseHTTPRequestHandler):
    """An empty page on a real origin: grids.js keeps its picks in localStorage."""

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
b = Browser(CHROME, testkit.free_port(), size="1500,1000")


def setup(expr):
    r = b.js(expr)
    if isinstance(r, str) and r.startswith("JS error"):
        raise SystemExit(f"setup failed: {r}")
    return r


def heard(*hs):
    return json.dumps({"heard": [dict(zip(("grid", "call", "band", "mode", "snr"), h)) for h in hs]})


COUNTS = "[window.__notes, window.__toasts.length, JSON.stringify(window.__said)]"

try:
    b.goto(f"http://127.0.0.1:{web.server_address[1]}/")
    setup("""(() => {
      localStorage.clear();
      document.documentElement.dataset.theme = 'midnight';
      const s = document.createElement('style'); s.textContent = %s; document.head.appendChild(s);
      window.esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
      window.__toasts = []; window.toast = (m) => window.__toasts.push(m);
      window.__notes = 0; window.__said = [];
      window.AudioContext = function () {
        return { currentTime: 0, destination: {},
          createGain() { return { gain: {}, connect() {} }; },
          createOscillator() { return { frequency: {}, connect() {}, start() { window.__notes++; }, stop() {} }; } };
      };
      Object.defineProperty(window, 'speechSynthesis', { configurable: true, value: { speak(u) { window.__said.push(u.text); } } });   // a getter: plain assignment is ignored
      window.SpeechSynthesisUtterance = function (t) { this.text = t; };
      window.store = { get(k, d) { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); },
                       set(k, v) { localStorage.setItem(k, JSON.stringify(v)); } };
      window.__impl = {}; window.addons = {register(n, impl) { window.__impl[n] = impl; }};
      document.body.innerHTML = '<div class="addon-body" id="g"></div>';
    })()""" % json.dumps(css))
    setup((ROOT / "static/addons/grids.js").read_text())
    setup("__impl.grids.mount(document.getElementById('g'), {send() {}}); __impl.grids.hello(" + json.dumps(SNAP) + ")")
    b.wait("document.querySelectorAll('#g .gq').length > 0", 10)

    check("draw: the 488 Lower-48 squares", b.js("document.querySelectorAll('#g .gq').length"), 488)
    CLS = "(g) => { const e = document.querySelector(`#g .gq[data-g=\"${g}\"]`); return ['c', 'w', 'm'].find((k) => e.classList.contains(k)); }"
    check("draw (All): confirmed, worked, confirmed, missing", b.js(f"['FN31', 'EM12', 'EN10', 'EM13'].map({CLS})"), ["c", "w", "c", "m"])
    b.js("document.querySelector('#g [data-mode=\"FT8\"]').click()")
    check("draw (FT8): SSB's EN10 is missing", b.js(f"['FN31', 'EM12', 'EN10'].map({CLS})"), ["c", "w", "m"])
    check("tally: one row per mode, All first",
          b.js("[...document.querySelectorAll('#g .grids-tally tbody tr')].map((r) => r.dataset.mode)"), ["All", "FT8", "FT4", "SSB"])
    check("ring: EM13 (missing, on the air) is ringed and first in the list",
          b.js("[document.querySelector('#g .gq[data-g=\"EM13\"]').classList.contains('air'), "
               "document.querySelector('#g .grids-missing li b').textContent]"), [True, "EM13"])
    check("open: what is on the air at page open makes no sound", b.js(COUNTS), [0, 0, "[]"])

    setup(f"__impl.grids.event({heard(('EM13', 'N9ABC', '20m', 'FT8', -12), ('EM14', 'N9ABD', '20m', 'FT8', -5))})")
    check("off: a new missing square on the air, no sound", b.js(COUNTS), [0, 0, "[]"])
    check("  but the tab's dot says so", b.js("__impl.grids.tab().dot"), "tx")

    b.js("document.querySelector('#g [data-alert=\"chime\"]').click()")
    check("chime: picking it chimes once (two notes)", b.js(COUNTS), [2, 0, "[]"])
    setup(f"__impl.grids.event({heard(('EM13', 'N9ABC', '20m', 'FT8', -12), ('EM14', 'N9ABD', '20m', 'FT8', -5), ('EM15', 'N9ABE', '20m', 'FT8', -9))})")
    check("chime: a new missing square chimes once, with a toast", b.js(COUNTS), [4, 1, "[]"])
    check("  the toast names it", b.js("window.__toasts[0]"), "Grids: EM15 on the air — N9ABE 20m FT8 -9")
    setup(f"__impl.grids.event({heard(('EM15', 'N9ABE', '20m', 'FT8', -7))})")
    check("  the same square, band and mode again: quiet", b.js(COUNTS), [4, 1, "[]"])
    setup(f"__impl.grids.event({heard(('FN31', 'N9ABF', '20m', 'FT8', -7), ('EM16', 'N9ABG', '20m', 'FT4', -7))})")
    check("  a worked square, and FT4 while FT8 is picked: quiet", b.js(COUNTS), [4, 1, "[]"])

    b.js("document.querySelector('#g [data-alert=\"voice\"]').click()")
    setup(f"__impl.grids.event({heard(('DM53', 'N9ABH', '20m', 'FT8', -3))})")
    check("voice: speaks the square spelled out", b.js("window.__said"), ["Wanted grid D M 5 3, 20 metres FT8"])
    if SHOTS:
        b.screenshot(os.path.join(SHOTS, "grids.png"))
finally:
    b.close()
    web.shutdown()

print(f"\n{'all ok' if not fails else f'{fails} FAILED'}", flush=True)
sys.exit(1 if fails else 0)
