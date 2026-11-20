"""Mission Control on one screen (UX plan PR 3b, 2026-11-19), driven in
headless Chromium.

station_page_test's fixture (two simulated radios, fake Pat and ARDOP, the
fake mesh bridge, a packet hub configured but not running) plus five Tools
add-ons: Logbook, Map, Propagation, Roster and HaLow (switched off, so it
reaches nothing). The Roster's state is given to the page raw, as the
station's socket delivers it (no logbook sync runs here). No network. Checks:

  fits      #view-home does not scroll at 1600x1000 or at 1440x900
  lanes     Messaging and Mesh share one row
  now       up to five fresh decodes with strong needs, the band strip's
            filter: a row from the previous band, another mode, a stale one
            or a weak level is not shown; the needs index unavailable is
            unknown, not "none"
  reply     a click on a Now entry sends exactly one message, the same one a
            click on that station's Roster row sends, and JTDX's outcome (the
            station's notice) shows as it does for the Roster
  log       the Log tile shows the needs summary's numbers and links to the
            Logbook tab
  stop      the rig bar's STOP TX stays in view at every size
  phone     at 390 px the order is bands, on air and needs, Now, radios, the
            rest

    python3 tests/mission_density_page_test.py [screenshot-dir]
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

os.environ["RANCH_RADIO_HOSTS"] = ""     # never this machine's own host files
os.environ["RANCH_RADIO_OFFLINE"] = "1"
os.environ.pop("CC_OBS_TOKENS_FILE", None)

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
TMP = tempfile.mkdtemp(prefix="mission-density-page-")

fp = testkit.free_port
WEB, SHARED, LANE, PAT, ARDOP, HUB, AGW, LOGBOOK, CDP = (fp() for _ in range(9))
G90 = {"web": fp(), "rigctl": fp(), "lane": fp()}
QMX = {"web": fp(), "rigctl": fp(), "lane": fp()}
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:66} got {got!r}" + ("" if ok else f" want {want!r}"), flush=True)


def answers(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


def radio(rig, p):
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--rig", rig, "--no-audio", "--host", "127.0.0.1",
         "--listen", str(p["web"]), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(p["rigctl"]),
         "--winlink-rigctl-port", str(p["lane"])],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert testkit.wait_for(lambda: answers(p["web"]), 60, 0.1), f"{rig} did not start"
    return proc


def stop(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def row(call, need, band="20m", mode="FT8", age=10, snr=-5):
    """A roster row as the Roster's view() makes it (named without its module:
    a module name here is an affected.py edge this test does not have)."""
    return {"key": f"1|0|500|CQ {call} AA00", "answer": "cq", "call": call, "grid": "AA00", "cq": True, "cq_word": "",
            "to_me": False, "to": "", "snr": snr, "dt": 0.1, "df": 500, "message": f"CQ {call} AA00", "age": age,
            "need": need, "reasons": [need] if need else [], "entity": "Testland", "cont": "", "lotw": False, "km": None,
            "bearing": None, "band": band, "mode": mode}


SUMMARY = {"qsos": 1234, "dxcc_worked": 156, "dxcc_confirmed": 98, "grids_worked": 812, "grids_confirmed": 455,
           "lotw_users": 0}
STRONG = [row("N9ABC", "dxcc"), row("N9ABD", "dxcc_band"), row("N9ABE", "dxcc_mode"), row("N9ABF", "grid"),
          row("N9ABG", "grid_band"), row("N9ABH", "grid_band", snr=-20)]
WEAK = [row("N9ABJ", "dxcc_unconfirmed"), row("N9ABK", "call_band_mode"), row("N9ABL", None)]
OUT = [row("N9ABM", "dxcc", band="40m"),            # kept from the previous band
       row("N9ABN", "dxcc", mode="FT4"),            # another mode
       row("N9ABP", "dxcc", age=200)]               # no longer fresh


def roster(rows, needs=SUMMARY):
    return {"t": "addon", "addon": "roster",
            "roster": {"band": "20m", "mode": "FT8", "dial": 14_074_000, "rows": rows, "needs": needs}}


cfg = {
    "name": "Test Station",
    "radios": [
        {"id": "g90", "name": "G90", **G90, "audio_in": "g90-in", "audio_out": "g90-out"},
        {"id": "qmx", "name": "QMX", **QMX, "audio_in": "qmx-in", "audio_out": "qmx-out"},
    ],
    "packet": {"url": f"http://127.0.0.1:{HUB}", "agwpe": f"127.0.0.1:{AGW}"},
    "winlink": {"pat": testkit.fake_pat_cmd(), "pat_port": PAT, "lane_port": LANE,
                "ardopcf": f"{sys.executable} tests/fake_ardopcf.py", "ardop_port": ARDOP,
                "ardop_logs": os.path.join(TMP, "ardop")},
    "mesh": {"port": "/dev/null", "python": sys.executable, "bridge": "tests/fake_mesh_bridge.py", "log": ""},
    # Five Tools add-ons; none reaches a device or the network here.
    "addons": {"logbook": {"url": f"http://127.0.0.1:{LOGBOOK}/"}, "map": {}, "propagation": {"swpc": False},
               "roster": {}, "halow": {"enabled": False, "host": "192.168.1.65"}},
    "addons_state": os.path.join(TMP, "addons.json"),   # never the real ~/.local/state one
}
with open(os.path.join(TMP, "station.json"), "w") as f:
    json.dump(cfg, f)

NOW = "[...document.querySelectorAll('#mc-now .mc-now-item')]"
SENT = "window.__sent.map((d) => JSON.parse(d)).filter((m) => m.do === 'reply')"
# Mission Control's scroll box is the view itself (station.css .st-view).
FITS = ("(() => { const v = document.querySelector('#view-home'), s = document.scrollingElement;"
        " return [v.scrollHeight <= v.clientHeight, s.scrollHeight <= s.clientHeight]; })()")
STOP_SEEN = ("(() => { const e = document.querySelector('#rig-bar .rb-stop-btn'); if (!e) return false;"
             " const r = e.getBoundingClientRect(), x = (r.left + r.right) / 2, y = (r.top + r.bottom) / 2;"
             " return !e.closest('[hidden]') && r.width > 0 && r.top >= 0 && r.bottom <= innerHeight && r.left >= 0"
             " && r.right <= innerWidth && e.contains(document.elementFromPoint(x, y)); })()")


def deliver(m):
    """A message as the station's socket delivers it (station.js's own handler)."""
    b.js(f"ws.onmessage({{ data: {json.dumps(json.dumps(m))} }})")


def shot(name):
    if SHOTS:
        b.screenshot(os.path.join(SHOTS, f"mission-density-{name}.png"))


def size(w, h, mobile=False):
    b.call("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=2 if mobile else 1, mobile=mobile)
    b.wait(f"innerWidth === {w} && innerHeight === {h}")


g90 = radio("g90", G90)
qmx = radio("qmx", QMX)
front = subprocess.Popen([sys.executable, "station.py", "--config", os.path.join(TMP, "station.json"),
                          "--listen", str(WEB), "--rigctl-port", str(SHARED)], cwd=ROOT,
                         env=dict(os.environ, HOME=TMP, FAKE_PAT_RIGCTL=str(LANE)),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
b = Browser(CHROME, CDP, size="1600,1000")
try:
    assert testkit.wait_for(lambda: answers(WEB), 60, 0.1), "station did not start"
    b.goto(f"http://127.0.0.1:{WEB}/#/home")
    b.wait("document.querySelector('#conn b')?.textContent === 'Connected'")
    b.wait("!!document.querySelector('#rig-bar .rb-rig.focus')")
    # Everything the fixture shows: both radios live, Winlink's mailbox, the
    # mesh, every add-on's card (five Tools ones), the band strip.
    b.wait("document.querySelectorAll('#st-cards .mc-f').length === 2 && "
           "document.querySelectorAll('.mc-lane-tools .mc-card').length === 5 && "
           "/Inbox \\d+/.test(document.querySelector('.mc-lane-messaging')?.textContent || '') && "
           "!document.querySelector('#mc-bands').hidden", timeout=60)

    print("# now: the band strip's filter")
    check("no logbook sync: Now says so, from the Roster's own snapshot",
          b.wait("document.querySelector('#mc-now .mc-now-empty')?.textContent || null"),
          "Now: the logbook sync isn't running on this station, so there's no roster")
    check("  the Log tile: unknown", b.js("document.querySelector('#mc-log').dataset.state"), "wait")
    deliver(roster(STRONG + WEAK + OUT, needs=None))
    check("the needs index unavailable: unknown, not none",
          b.wait("/unknown/.test(document.querySelector('#mc-now .mc-now-empty')?.textContent || '') && "
                 "document.querySelector('#mc-now .mc-now-empty').textContent"),
          "Now: strong needs unknown (the needs index is not ready)")
    check("  the Log tile: unknown", b.wait("document.querySelector('#mc-log')?.dataset.state || null"), "wait")
    deliver(roster(STRONG + WEAK + OUT))
    check("five fresh strong needs on 20m FT8, in the Roster's order",
          b.wait(f"{NOW}.length === 5 && {NOW}.map((e) => e.dataset.call)"),
          ["N9ABC", "N9ABD", "N9ABE", "N9ABF", "N9ABG"])
    check("  each names its need", b.js(f"{NOW}[0].textContent.includes('New DXCC')"), True)
    check("  and says what a click does",
          b.js(f"{NOW}[0].title"), "Click to answer N9ABC's CQ in JTDX")

    print("\n# log: the needs summary")
    LOG = "document.querySelector('#mc-log')"
    check("the Log tile: QSOs, DXCC and grids worked/confirmed",
          b.wait(f"{LOG}?.dataset.state === 'ok' && [...{LOG}.querySelectorAll('[data-k]')].map((e) => [e.dataset.k, e.textContent])"),
          [["qsos", "1,234"], ["dxcc", "156/98"], ["grids", "812/455"]])
    check("  it links to the Logbook tab", b.js(f"{LOG}.getAttribute('href')"), "#/logbook")

    print("\n# fits: no scroll, with each radio's mini spectrum drawn")
    # Scope frames (type 1: start, end, values) as a radio's server sends
    # them: live, every radio card carries its spectrum.
    for rid in ("g90", "qmx"):
        b.js("(() => { const n = 200, buf = new ArrayBuffer(17 + n), dv = new DataView(buf); dv.setUint8(0, 1);"
             " dv.setFloat64(1, 14_060_000, true); dv.setFloat64(9, 14_090_000, true);"
             " const d = new Uint8Array(buf, 17); for (let i = 0; i < n; i++) d[i] = 30 + (i === 93 ? 90 : (i * 7) % 11);"
             f" for (let k = 0; k < 5; k++) mission.scopeOf('{rid}').push(buf.slice(0)); }})()")
    check("  both radio cards show their spectrum",
          b.wait("[...document.querySelectorAll('#st-cards .mc-scope-slot canvas.mc-scope')].filter((c) => c.width > 0).length"), 2)
    check("  beside the readouts: each signal trace keeps a full-width box",
          b.wait("(() => { const t = [...document.querySelectorAll('#st-cards .mc-trace-slot canvas.mc-trace')];"
                 " return t.length === 2 && t.every((c) => c.clientWidth > 150 && c.clientHeight > 10"
                 " && c.clientWidth === c.parentElement.clientWidth); })()"), True)
    for w, h in ((1600, 1000), (1440, 900)):
        size(w, h)
        check(f"{w}x{h}: Mission Control does not scroll", b.wait(f"({FITS}).every(Boolean) && {FITS}"), [True, True])
        check(f"  the rig bar's STOP TX in view", b.wait(STOP_SEEN), True)
        check("  Messaging and Mesh share a row",
              b.js("(() => { const t = (s) => Math.round(document.querySelector(s).getBoundingClientRect().top);"
                   " return t('.mc-lane-messaging') === t('.mc-lane-mesh'); })()"), True)
        shot(f"{w}x{h}")

    print("\n# reply: the Roster's own")
    b.js("window.__sent = []; const s = WebSocket.prototype.send; "
         "WebSocket.prototype.send = function (d) { window.__sent.push(String(d)); return s.call(this, d); }")
    b.js(f"{NOW}[0].click()")
    now_sent = b.wait(f"{SENT}.length >= 1 && {SENT}")
    time.sleep(0.5)                    # a quiet window: checking that only one was sent
    now_sent = b.js(SENT)
    check("a Now click sends one Reply", len(now_sent), 1)
    check("  the Roster's reply for that decode", now_sent[0],
          {"t": "addon", "addon": "roster", "do": "reply", "key": STRONG[0]["key"], "call": "N9ABC"})
    check("  JTDX's outcome shows, as for the Roster (no sync here: why not)",
          b.wait("[...document.querySelectorAll('.toast')].some((t) => /roster can't answer/.test(t.textContent))"), True)
    check("  Mission Control stays open", b.js("location.hash"), "#/home")
    b.js("location.hash = '#/roster'")
    b.wait("!!document.querySelector('.rs-row[data-call=\"N9ABC\"]') && "
           "!document.querySelector('.rs-row[data-call=\"N9ABC\"]').closest('[hidden]')")
    b.js("window.__sent = []")
    b.js("document.querySelector('.rs-row[data-call=\"N9ABC\"]').click()")
    roster_sent = b.wait(f"{SENT}.length >= 1 && {SENT}")
    check("  the same message a Roster row click sends", now_sent, roster_sent)
    b.js("location.hash = '#/home'")
    b.wait("!document.querySelector('#view-home').hidden")

    print("\n# phone: 390 px")
    size(390, 844, mobile=True)
    order = b.wait("(() => { const at = (s) => { const e = document.querySelector(s);"
                   " return e && !e.closest('[hidden]') ? Math.round(e.getBoundingClientRect().top) : null; };"
                   " const o = { bands: at('#mc-bands'), air: at('#mc-air'), needs: at('#mc-needs'), now: at('#mc-now .mc-now-list'),"
                   " radios: at('.mc-lane-radios'), log: at('#mc-log'), messaging: at('.mc-lane-messaging'),"
                   " tools: at('.mc-lane-tools') };"
                   " return Object.values(o).every((v) => v !== null) && o; })()")
    ranked = sorted(order, key=lambda k: (order[k], k)) if order else []
    check("bands, on air and needs, Now, radios, the rest (top to bottom)",
          bool(order) and order["bands"] < order["air"] <= order["needs"] < order["now"] < order["radios"]
          < min(order["log"], order["messaging"], order["tools"]) or ranked, True)
    check("  STOP TX in view", b.wait(STOP_SEEN), True)
    check("  nothing wider than the screen",
          b.js("document.querySelector('#view-home').scrollWidth <= innerWidth"), True)
    shot("390")
finally:
    b.close()
    stop(front)
    stop(g90)
    stop(qmx)
    shutil.rmtree(TMP, ignore_errors=True)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
