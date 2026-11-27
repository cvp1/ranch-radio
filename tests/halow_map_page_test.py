"""The HaLow tab's map (static/addons/halow.js on static/meshmap.js), in a real
browser, from a snapshot the add-on's own parser (halow_ap.parse_read) builds
out of a bridge read. No server: the page's CSS and scripts, a stub add-on
registry, and api.send() recorded.

  nodes     the bridge and every station it has had joined, by its place's
            name; one that has left is still there, from the seen list
  signal    beside each joined station's name, the dBm the bridge hears it at,
            coloured by strength; one that left says "last"; the bridge says
            how many hear it, and never a dBm for how strongly they do
  direction each joined station's ring and line to the bridge in the Mesh
            map's words: both (a signal and its ACKs), out (a signal only)
  unplaced  a station with no place is drawn beside the bridge and says so
  stable    a new snapshot redraws the map in place, not a new map (the
            panel's tables are rebuilt on every read)
  popup     a station's card says both directions, the downlink as not
            measured, and its Ping asks the bridge to ping it

    python3 tests/halow_map_page_test.py [screenshot-dir]
"""
import http.server
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import halow_ap  # noqa: E402
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


# ---- a bridge read: the gate camera heard and ACKing; a second station heard
# but with nothing of ours ACKed yet (no tx packets), and no place set.
READ = """@@uptime
93784.21 370012.55
@@date
1781629508
@@board
{"hostname": "HT-H7608", "model": "Heltec HT-H7608"}
@@iwdev
phy#1
	Interface wlan0
		addr 02:00:00:00:00:02
		ssid Ranch
		type AP
@@sta wlan0
Station 02:00:00:00:aa:01 (on wlan0)
	inactive time:	120 ms
	tx packets:	5021
	tx failed:	2
	tx retries:	41
	signal:  	-71 dBm
	signal avg:	-70 dBm
	tx bitrate:	6.5 MBit/s MCS 3
	rx bitrate:	4.3 MBit/s MCS 2
	connected time:	86012 seconds
Station 02:00:00:00:aa:02 (on wlan0)
	inactive time:	900 ms
	signal:  	-60 dBm
	connected time:	40 seconds
@@neigh
192.168.1.120 dev br-ahwlan lladdr 02:00:00:00:aa:01 STALE
192.168.1.121 dev br-ahwlan lladdr 02:00:00:00:aa:02 REACHABLE
@@leases
"""
view = halow_ap.parse_read(READ)
now = time.time()
snap = {"target": "192.168.1.65", "at": now, "error": None, "poll": 30, "stale": False, **view,
        "position": {"lat": 40.0, "lon": -105.0},
        "places": {"02:00:00:00:aa:01": {"name": "Gate cam", "lat": 40.005, "lon": -105.01},
                   "02:00:00:00:aa:03": {"name": "Old cam", "lat": 39.995, "lon": -104.99}},
        "seen": [{"mac": "02:00:00:00:aa:03", "name": "", "ip": "192.168.1.130", "ifname": "wlan0", "signal": -76,
                  "signal_avg": -75, "dir": "both", "first": now - 9000, "last": now - 7200}]}
check("fixture: the parser proves the gate camera both ways, the other one out",
      [(s["mac"][-2:], s["dir"]) for s in view["stations"]], [("02", "out"), ("01", "both")])

css = "".join((ROOT / f).read_text() for f in ("static/app.css", "static/themes.css", "static/addons/halow.css"))


class Blank(http.server.BaseHTTPRequestHandler):
    """An empty page on a real origin (the map keeps its choices in localStorage)."""

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
b = Browser(CHROME, testkit.free_port(), size="1300,1000")


def setup(expr):
    r = b.js(expr)
    if isinstance(r, str) and r.startswith("JS error"):
        raise SystemExit(f"setup failed: {r}")
    return r


try:
    b.goto(f"http://127.0.0.1:{web.server_address[1]}/")
    setup("""(() => {
      document.documentElement.dataset.theme = 'midnight';
      const s = document.createElement('style'); s.textContent = %s; document.head.appendChild(s);
      window.esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
      window.store = {get: (k, d) => d, set() {}};
      window.toast = () => {}; window.__sent = [];
      window.__impl = {}; window.addons = {register(n, impl) { window.__impl[n] = impl; }};
      document.body.innerHTML = '<div class="panel addon addon-halow"><div class="addon-body" id="hl"></div></div>';
    })()""" % json.dumps(css))
    setup((ROOT / "static/basemaps.js").read_text() + "; window.basemaps = basemaps;")
    setup((ROOT / "static/meshmap.js").read_text() + "; window.meshMap = meshMap;")
    setup((ROOT / "static/addons/halow.js").read_text())
    setup("__impl.halow.mount(document.getElementById('hl'), {send: (verb, m) => window.__sent.push([verb, m || {}])}); "
          "__impl.halow.hello(" + json.dumps(snap) + ")")
    b.wait("document.querySelectorAll('#hl .hl-map .mm-node').length === 4", 10)

    check("the map sits above the tables",
          b.js("[...document.querySelector('#hl .halow').children].map((e) => e.className)"), ["mesh-map hl-map", "hl-main"])
    NODES = ("Object.fromEntries([...document.querySelectorAll('#hl .mm-node')].map((g) => [g.querySelector('text:not(.mm-badge)')?.textContent, "
             "{cls: g.getAttribute('class'), badge: g.querySelector('.mm-badge')?.textContent || '', "
             "badgeCls: g.querySelector('.mm-badge')?.getAttribute('class') || '', ring: g.querySelector('.mm-ring')?.getAttribute('class') || ''}]))")
    nodes = b.js(NODES)
    check("nodes: the bridge, the placed camera, the unplaced station by its address, the one that left",
          sorted(nodes), ["192.168.1.121", "Gate cam", "HT-H7608", "Old cam"])
    check("signal: beside each name, how the bridge hears it, coloured by strength",
          {k: [v["badge"], v["badgeCls"]] for k, v in nodes.items()},
          {"HT-H7608": ["heard by 1", "mm-badge h0"], "Gate cam": ["-70 dBm", "mm-badge h2"],
           "192.168.1.121": ["-60 dBm", "mm-badge h0"], "Old cam": ["-75 dBm last", "mm-badge"]})
    check("direction: rings in the Mesh map's words; one that left proves nothing now",
          {k: v["ring"] for k, v in nodes.items()},
          {"HT-H7608": "", "Gate cam": "mm-ring both", "192.168.1.121": "mm-ring out", "Old cam": ""})
    LINKS = ("[...document.querySelectorAll('#hl .mm-link')].map((l) => [l.getAttribute('class'), l.querySelector('title')?.textContent])")
    links = b.js(LINKS)
    check("  a line to the bridge for each joined station, by direction",
          sorted(c for c, _ in links), ["mm-link both", "mm-link out"])
    check("  its hover text never gives a dBm for how it hears the bridge",
          [t for c, t in links if c == "mm-link both"],
          ["Gate cam: the bridge hears it at -70 dBm (now -71); it hears the bridge (its ACKs come back; how strongly isn't measured from the bridge)"])
    check("unplaced: drawn beside the bridge, marked, and the ring says why",
          ("k-unplaced" in nodes["192.168.1.121"]["cls"],
           "no place set" in (b.js("document.querySelector('#hl .mm-fan title')?.textContent") or "")), (True, True))
    check("  the note says how many are placed and what the colours mean",
          b.js("document.querySelector('#hl .mm-note').textContent"),
          "3 of 3 nodes on the map · green hears the bridge, blue the bridge hears it, purple both · beside a name: how the bridge hears it")

    b.js("document.querySelector('#hl .hl-map').__mark = 1")
    snap2 = {**snap, "stations": [s for s in snap["stations"] if s["mac"].endswith("01")], "at": now + 30}
    setup("__impl.halow.event({snapshot: " + json.dumps(snap2) + "})")
    check("stable: a new read redraws the same map, with one line fewer",
          (b.js("document.querySelector('#hl .hl-map').__mark === 1"), len(b.js(LINKS))), (True, 1))
    setup("__impl.halow.event({snapshot: " + json.dumps(snap) + "})")

    b.js("(() => { const g = [...document.querySelectorAll('#hl .mm-node')].find((n) => n.querySelector('text')?.textContent === 'Gate cam'); "
         "g.dispatchEvent(new MouseEvent('click', {bubbles: true})); })()")
    b.wait("!document.querySelector('#hl .mm-pop').hidden", 5)
    ROWS = "Object.fromEntries([...document.querySelectorAll('#hl .mm-pop .mm-pop-r')].map((r) => [r.children[0].textContent, r.children[1].textContent]))"
    rows = b.js(ROWS)
    check("popup: both directions, the downlink as not measured",
          (rows.get("bridge hears it"), rows.get("it hears the bridge"), rows.get("link")),
          ("-70 dBm", "yes, its ACKs (not measured)", "both ways"))
    if SHOTS:
        b.screenshot(os.path.join(SHOTS, "halow-map.png"))
    b.js("document.querySelector('#hl .mm-pop [data-act=ping]').click()")
    check("  its Ping asks the bridge to ping it", json.loads(b.js("JSON.stringify(window.__sent)")),
          [["ping", {"ip": "192.168.1.120"}]])
finally:
    b.close()
    web.shutdown()

print(f"\n{'all ok' if not fails else f'{fails} FAILED'}", flush=True)
sys.exit(1 if fails else 0)
