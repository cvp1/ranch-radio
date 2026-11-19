"""S2: the station page (station.html), driven in headless Chromium.

Two simulated radios (G90, QMX), station.py in front with fake
Pat and mesh bridge, and a packet hub that is configured but not running.
Checks:
  - the navigation: Mission Control, one tab per connected radio with its rigctl
    ports (one carries the shared port), Winlink, Mesh, and
    Packet off the tab bar while the hub is down (its card stays)
  - the brand: the station's name and its callsign (Winlink's MyCall here);
    the packet card's short name, its full title on hover
  - Mission Control: a card per radio and service in lanes named like the
    tab bar's groups, names the shared port; a radio's card live (frequency,
    S-meter, signal trace, its mini spectrum from scope frames; on the air:
    power), an icon on every card, the UTC clock, the on-air line with Release TX, what needs
    you (unread mail, offline gear), keys 1-9 and 0, a click opens a tab, a
    key typed into a field is not a jump
  - a radio's tab shows that radio's own page in a frame, connected, without
    its own brand (the tab names it), Connected pill or theme picker (the
    station's bar has them) but with STOP TX, latency and power; the other
    radio's frame stays hidden
  - on a phone (390 px) the station's pill is its dot: brand, dot, clock and
    theme picker share one row, the tabs the row below
  - the address follows the view (#/qmx) and a reload comes back to it
  - Winlink's Via lists each HF radio and telnet (no packet: hub down);
    telnet fills the URL and hides the gateway list
  - a mesh message arriving while you're elsewhere shows as a count on Mesh,
    cleared by opening Mesh

    python3 tests/station_page_test.py [screenshot-dir]
"""
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from cdp_browser import Browser  # noqa: E402
import testkit  # noqa: E402

B = 24000 + random.randrange(0, 4000, 40)
WEB, SHARED, LANE, PAT, ARDOP, HUB, CDP = B, B + 1, B + 2, B + 3, B + 4, B + 6, B + 7
G90 = {"web": B + 10, "rigctl": B + 11, "lane": B + 12}
QMX = {"web": B + 20, "rigctl": B + 21, "lane": B + 22}
CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
SHOTS = sys.argv[1] if len(sys.argv) > 1 else None
TMP = tempfile.mkdtemp(prefix="station-page-")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


def wait_port(port, timeout=60):         # a ceiling; returns once the port answers
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def radio(rig, p):
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--rig", rig, "--no-audio", "--host", "127.0.0.1",
         "--listen", str(p["web"]), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(p["rigctl"]),
         "--winlink-rigctl-port", str(p["lane"])],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert wait_port(p["web"]), f"{rig} did not start"
    return proc


def stop(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")

cfg = {
    "name": "Test Station",
    "radios": [
        {"id": "g90", "name": "G90", **G90, "audio_in": "g90-in", "audio_out": "g90-out"},
        {"id": "qmx", "name": "QMX", **QMX, "audio_in": "qmx-in", "audio_out": "qmx-out"},
    ],
    "packet": {"url": f"http://127.0.0.1:{HUB}", "agwpe": "127.0.0.1:8000"},
    "winlink": {"pat": testkit.fake_pat_cmd(), "pat_port": PAT, "lane_port": LANE,
                "ardopcf": f"{sys.executable} tests/fake_ardopcf.py", "ardop_port": ARDOP,
                "ardop_logs": os.path.join(TMP, "ardop")},
    "mesh": {"port": "/dev/null", "python": sys.executable, "bridge": "tests/fake_mesh_bridge.py", "log": ""},
    "addons_state": os.path.join(TMP, "addons.json"),   # never the real ~/.local/state one
}
cfg_path = os.path.join(TMP, "station.json")
with open(cfg_path, "w") as f:
    json.dump(cfg, f)

TEXT = "(s) => (document.querySelector(s)?.textContent || '')"
VISIBLE = "(s) => { const e = document.querySelector(s); return !!e && !e.closest('[hidden]') && e.offsetParent !== null; }"
TABS = "[...document.querySelectorAll('#st-nav .st-tab')]"
FRAME = "document.querySelector('#view-radio iframe:not([hidden])')"


def tabs():
    return b.js(f"{TABS}.map((t) => t.querySelector('span').textContent + (t.querySelector('small') ? ' ' + t.querySelector('small').textContent : ''))")


def shot(name):
    if SHOTS:
        b.screenshot(os.path.join(SHOTS, f"station-{name}.png"))


g90 = radio("g90", G90)
qmx = radio("qmx", QMX)
front = subprocess.Popen([sys.executable, "station.py", "--config", cfg_path, "--listen", str(WEB),
                          "--rigctl-port", str(SHARED)], cwd=ROOT,
                         env=dict(os.environ, FAKE_PAT_RIGCTL=str(LANE)),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
b = Browser(CHROME, CDP, size="1500,1000")
try:
    assert wait_port(WEB), "station did not start"
    time.sleep(2)
    b.goto(f"http://127.0.0.1:{WEB}/")
    b.wait(f"({TEXT})('#conn b') === 'Connected'")
    b.wait(f"{TABS}.length >= 4")
    check("nav, grouped: Mission Control, Radios, Winlink, Mesh (Packet's hub is down: no tab)",
          [x.split()[0] for x in tabs()], ["Mission", "Radios", "Winlink", "Mesh"])
    check("  Radios is a closed group with both radios in its title",
          b.js(f"(() => {{ const t = {TABS}[1]; return [t.classList.contains('st-group-tab'), t.title]; }})()"),
          [True, "Radios: G90, QMX"])
    check("  the Mission Control tab names it", "Packet" in (b.js(f"{TABS}[0].title") or ""), True)
    check("mission control: a card per radio and service, in the bar's group order",
          b.js("[...document.querySelectorAll('.st-card b')].map((e) => e.textContent)"),
          ["G90", "QMX", "Winlink", "Packet · UV-PRO", "Mesh"])
    check("  the packet card's short name; its full title on hover",
          b.js("[...document.querySelectorAll('.st-card b')].find((x) => x.textContent.startsWith('Packet')).title"), "Packet (UV-PRO)")
    check("  names the shared port", str(SHARED) in b.js(f"({TEXT})('#st-help')"), True)
    b.js(f"{TABS}[1].click()")
    b.wait("location.hash === '#/g90'")
    check("opening Radios opens its first radio and shows the group",
          b.wait(f"{TABS}.length >= 5 && {TABS}.map((t) => t.querySelector('span').textContent)"),
          ["Mission Control", "G90", "QMX", "Winlink", "Mesh"])
    check("  inside a frame named Radios",
          b.js("document.querySelector('#st-nav .st-group .st-group-name')?.textContent"), "Radios")
    t = tabs()
    check("  one radio carries the shared port", sum(" · " in x for x in t), 1)
    own = {x.split()[0]: x.split()[-1] for x in t if x.split()[0] in ("G90", "QMX")}
    check("  each radio's own port", own, {"G90": str(G90["rigctl"]), "QMX": str(QMX["rigctl"])})
    shot("overview")

    b.js(f"{TABS}.find((t) => t.textContent.startsWith('QMX')).click()")
    b.wait(f"location.hash === '#/qmx'")
    b.js("location.hash = '#/home'")
    b.wait(f"{TABS}[1].classList.contains('st-group-tab')")
    check("  Radios now opens the radio last used", b.js(f"{TABS}[1].getAttribute('href')"), "#/qmx")
    b.js(f"{TABS}[1].click()")
    b.wait(f"location.hash === '#/qmx'")
    check("QMX tab: its page in a frame", bool(b.wait(f"{FRAME}?.src.includes('/r/qmx/')")), True)
    check("  connected", b.wait(f"{FRAME}.contentDocument?.querySelector('#conn b')?.textContent === 'Connected' && 'yes'"), "yes")
    check("  the radio page's own brand is hidden",
          b.js(f"getComputedStyle({FRAME}.contentDocument.querySelector('.topbar .brand')).display"), "none")
    # The frame drops what the station's bar already shows (UX plan PR 0.1),
    # and keeps STOP TX, the latency and power.
    SHOWN = (f"((s) => {{ const e = {FRAME}.contentDocument.querySelector(s); "
             "return !!e && getComputedStyle(e).display !== 'none'; })")
    check("  the frame hides its Connected pill and theme picker",
          [b.js(f"{SHOWN}('#conn')"), b.js(f"{SHOWN}('.topbar .theme-pick')")], [False, False])
    # Power hides itself on a rig with no power-on (neither simulated rig has
    # one); what matters here is that the frame's CSS does not hide it.
    POWER = (f"(() => {{ const p = {FRAME}.contentDocument.querySelector('#power-btn'), h = p.hidden; "
             "p.hidden = false; const d = getComputedStyle(p).display; p.hidden = h; return d !== 'none'; })()")
    check("  and keeps STOP TX, latency and power",
          [b.js(f"{SHOWN}('#tx-stop')"), b.js(f"{SHOWN}('#rtt')"), b.js(POWER)], [True, True, True])
    check("  STOP TX is on screen", b.js(f"{FRAME}.contentDocument.querySelector('#tx-stop').offsetWidth > 0"), True)
    # Only Connected repeats the station's pill; a radio not responding is the frame's own news.
    b.js(f"{FRAME}.contentDocument.querySelector('#conn').className = 'pill pill-wait'")
    check("  its pill comes back when it is not Connected", b.js(f"{SHOWN}('#conn')"), True)
    b.js(f"{FRAME}.contentDocument.querySelector('#conn').className = 'pill pill-ok'")
    check("  the tab is marked", b.js(f"{TABS}.find((t) => t.classList.contains('on')).textContent.startsWith('QMX')"), True)
    shot("qmx")
    b.js(f"{TABS}.find((t) => t.textContent.startsWith('G90')).click()")
    b.wait(f"{FRAME}?.src.includes('/r/g90/')")
    check("G90 tab: G90 frame shown, QMX frame kept but hidden",
          sorted(b.js("[...document.querySelectorAll('#view-radio iframe')].map((f) => [f.src.split('/r/')[1], f.hidden])")),
          [["g90/", False], ["qmx/", True]])
    b.goto(f"http://127.0.0.1:{WEB}/#/qmx")
    check("reload at #/qmx comes back to the QMX", bool(b.wait(f"{FRAME}?.src.includes('/r/qmx/')")), True)
    # The station's callsign beside its name (station.py identity()): no logbook
    # sync here, so Winlink's MyCall, which the fake Pat gives as N9ABC.
    check("brand: the station's name and its callsign",
          b.wait(f"({TEXT})('#st-call') === 'N9ABC' && [({TEXT})('#st-name'), ({TEXT})('#st-call')]"), ["Test Station", "N9ABC"])

    b.js("location.hash = '#/winlink'")
    b.wait(f"({VISIBLE})('#winlink')")
    check("Winlink: Via lists each HF radio and telnet",
          b.wait("[...document.querySelectorAll('#wl-via option')].map((o) => o.textContent).length === 3 && "
                 "[...document.querySelectorAll('#wl-via option')].map((o) => o.textContent)"),
          ["HF · G90 (ARDOP)", "HF · QMX (ARDOP)", "Telnet (internet, no radio)"])
    b.js("(() => { const s = document.querySelector('#wl-via'); s.value = 'telnet'; s.dispatchEvent(new Event('change')); })()")
    check("  telnet fills the URL", b.js("document.querySelector('#wl-url').value.startsWith('telnet://')"), True)
    check("  and hides the gateway list", b.js("document.querySelector('#wl-gateways').hidden"), True)
    b.js("(() => { const s = document.querySelector('#wl-via'); s.value = 'qmx'; s.dispatchEvent(new Event('change')); })()")
    check("  back to the QMX: gateways shown", b.js("document.querySelector('#wl-gateways').hidden"), False)
    shot("winlink")

    # A mesh message arriving while Mesh is not open.
    b.js("(() => { const m = { t: 'mesh', msg: { k: 'msg', id: 10, t: Date.now() / 1000, from: '!0000beef', "
         "to: '^all', ch: 1, dm: false, text: 'gate shut' } }; ws.onmessage({ data: JSON.stringify(m) }); })()")
    check("mesh message elsewhere: count on the Mesh tab",
          b.js(f"{TABS}.find((t) => t.textContent.startsWith('Mesh')).querySelector('.st-badge')?.textContent"), "1")
    b.js("location.hash = '#/mesh'")
    b.wait(f"({VISIBLE})('#mesh')")
    check("  opening Mesh clears it",
          b.js(f"{TABS}.find((t) => t.textContent.startsWith('Mesh')).querySelector('.st-badge')"), None)
    shot("mesh")

    MESH_TAB = f"{TABS}.find((t) => t.textContent.startsWith('Mesh'))"
    b.wait(f"({TEXT})('#mesh-state') === 'Connected'")
    b.js("window.confirm = () => true")
    b.js("document.querySelector('#mesh-power').click()")
    check("Mesh switched off: the state says so", b.wait(f"({TEXT})('#mesh-state') === 'Switched off' && 'off'"), "off")
    check("  the Mesh tab's dot goes off", b.js(f"{MESH_TAB}.classList.contains('off')"), True)
    b.js("document.querySelector('#mesh-power').click()")
    check("  on again: Connected", b.wait(f"({TEXT})('#mesh-state') === 'Connected' && 'on'"), "on")
    check("  the dot comes back", b.wait(f"!{MESH_TAB}.classList.contains('off') && 'ok'"), "ok")

    # The rig bar (UX plan PR 1a, 2026-11-19; was the radio strip, hidden on
    # radio tabs and Mission Control): every HF radio on every view.
    # tests/rigbar_page_test.py covers it in depth.
    BAR = "[...document.querySelectorAll('#rig-bar .rb-rig')]"
    b.js("location.hash = '#/winlink'")
    b.wait(f"{BAR}.length === 2 && {BAR}.every((r) => /\\d+\\.\\d{{3}}\\.\\d{{3}}/.test(r.querySelector('.rb-f').textContent))")
    check("rig bar: both radios on Winlink",
          b.js(f"{BAR}.map((r) => r.querySelector('.rb-name b').textContent)"), ["G90", "QMX"])
    check("  with a frequency", b.js(f"/\\d+\\.\\d{{3}}\\.\\d{{3}}/.test({BAR}[0].querySelector('.rb-f').textContent)"), True)
    for view in ("g90", "home"):
        b.js(f"location.hash = '#/{view}'")
        check(f"  visible on #/{view} too",
              bool(b.wait(f"location.hash === '#/{view}' && !document.querySelector('#rig-bar').hidden && "
                          "document.querySelector('#rig-bar').offsetParent !== null")), True)

    # Mission Control: live cards, what is on the air, what needs you, keys.
    CARD = "[...document.querySelectorAll('#st-cards .mc-card')].find((c) => c.querySelector('b').textContent === '{}')"
    check("mission control: lanes named like the tab bar's groups",
          b.js("[...document.querySelectorAll('.mc-lane-name')].map((e) => e.textContent)"), ["Radios", "Messaging", "Mesh"])
    check("  a radio's card: its frequency live",
          bool(b.wait(f"/\\d+\\.\\d{{3}}\\.\\d{{3}}/.test({CARD.format('G90')}?.querySelector('.mc-f')?.textContent || '')")), True)
    check("  and its S-meter", bool(b.wait(f"/^S\\d/.test({CARD.format('G90')}?.querySelector('.mc-meter span')?.textContent || '')")), True)
    check("  every card has its icon", b.js("[...document.querySelectorAll('#st-cards .mc-card')]"
                                          ".every((c) => c.querySelector('.st-card-head svg.mc-ico'))"), True)
    check("  the clock: UTC to the second", bool(b.wait("/^\\d\\d:\\d\\d:\\d\\d$/.test(document.querySelector('#mc-clock .mc-utc').textContent)")), True)
    check("  a radio's card is double width, with its signal trace",
          bool(b.wait(f"{CARD.format('G90')}?.classList.contains('mc-wide') && "
                      f"!!{CARD.format('G90')}.querySelector('.mc-trace-slot canvas.mc-trace')")), True)
    # A scope frame (type 1: start, end, values) as the radio's server sends one.
    b.js("(() => { const n = 200, buf = new ArrayBuffer(17 + n), dv = new DataView(buf); dv.setUint8(0, 1);"
         " dv.setFloat64(1, 14_060_000, true); dv.setFloat64(9, 14_090_000, true);"
         " const d = new Uint8Array(buf, 17); for (let i = 0; i < n; i++) d[i] = 30 + (i === 93 ? 90 : (i * 7) % 11);"
         " for (let k = 0; k < 5; k++) mission.scopeOf('g90').push(buf.slice(0)); })()")
    check("  its mini spectrum, once scope frames come",
          bool(b.wait(f"{CARD.format('G90')}?.querySelector('.mc-scope-slot canvas.mc-scope')?.width > 0")), True)
    check("  nothing on the air", b.js(f"({TEXT})('#mc-air')"), "Nothing on the air")
    needs = b.wait("document.querySelectorAll('#mc-needs .mc-need').length >= 2 && "
                   "[...document.querySelectorAll('#mc-needs .mc-need')].map((a) => [a.textContent, a.getAttribute('href')])")
    check("  needs you: unread Winlink mail, opening Winlink",
          any("unread Winlink" in t and h == "#/winlink" for t, h in needs), True)
    check("  needs you: the packet hub offline", any(t == "Offline: UV-PRO" for t, _ in needs), True)
    check("  Winlink's card: its state and mailbox",
          bool(b.wait(f"/Inbox \\d+/.test({CARD.format('Winlink')}?.textContent || '')")), True)
    with socket.create_connection(("127.0.0.1", G90["rigctl"]), timeout=3) as c:
        f = c.makefile("r")
        c.sendall(b"T 1\n"); f.readline()
        b.wait("document.querySelector('#mc-air').classList.contains('tx')")
        check("  the G90 keyed: ON AIR with Release TX",
              (b.js("document.querySelector('#mc-air .mc-air-row a').textContent.startsWith('ON AIR G90')"),
               b.js("!!document.querySelector('#mc-air button')")), (True, True))
        check("  its card on the air, with power", bool(b.wait(f"{CARD.format('G90')}?.classList.contains('tx') && "
              f"/^\\d+ W$/.test({CARD.format('G90')}.querySelector('.mc-meter span').textContent)")), True)
        shot("mission-tx")
        b.js("document.querySelector('#mc-air button').click()")
        b.wait("!document.querySelector('#mc-air').classList.contains('tx')")
        c.sendall(b"t\n")
        check("  Release TX unkeys the radio", f.readline().strip(), "0")
    shot("mission")
    KEY = "document.dispatchEvent(new KeyboardEvent('keydown', {{ key: '{}', bubbles: true }}))"
    check("  the cards' keys in order", b.js("[...document.querySelectorAll('.mc-card[data-key]')].map((c) => "
                                             "[c.dataset.key, c.dataset.href])"),
          [["1", "#/g90"], ["2", "#/qmx"], ["3", "#/winlink"], ["4", "#/mesh"]])
    b.js(KEY.format("2"))
    check("  key 2 opens the QMX", b.wait("location.hash === '#/qmx' && location.hash"), "#/qmx")
    b.js(KEY.format("0"))
    check("  key 0 comes back", b.wait("location.hash === '#/home' && location.hash"), "#/home")
    b.js(f"{CARD.format('Winlink')}.click()")
    check("  a click on a card opens its tab", b.wait("location.hash === '#/winlink' && location.hash"), "#/winlink")
    b.js("(() => { const i = document.createElement('input'); document.body.append(i); i.focus(); "
         "i.dispatchEvent(new KeyboardEvent('keydown', { key: '0', bubbles: true })); i.remove(); })()")
    check("  a key typed into a field is not a jump", b.js("location.hash"), "#/winlink")

    # A phone (UX plan PR 0.1): the station's pill is its dot, so brand, dot,
    # clock and theme picker share the first row; the tabs wrap below by design.
    b.call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=2, mobile=True)
    b.js("location.hash = '#/home'")
    b.wait(f"({TEXT})('#conn b') === 'Connected' && innerWidth === 390")
    RECT = ("((s) => { const r = document.querySelector(s).getBoundingClientRect(); "
            "return [r.top, r.bottom, r.left, r.right]; })")
    row = {s: b.js(f"{RECT}('{s}')") for s in (".topbar .brand", "#conn", "#utc", ".topbar .theme-pick", "#st-nav")}
    head = [row[s] for s in (".topbar .brand", "#conn", "#utc", ".topbar .theme-pick")]
    mid = [(r[0] + r[1]) / 2 for r in head]
    check("phone: brand, dot, clock and theme picker on one row", max(mid) - min(mid) < 8, True)
    check("  all inside the screen", all(r[2] >= 0 and r[3] <= 390 and r[3] > r[2] for r in head), True)
    check("  the tabs are the row below", row["#st-nav"][0] >= max(r[1] for r in head), True)
    check("  the pill is its dot, its words in the title",
          [b.js("document.querySelector('#conn').offsetWidth <= 30"), b.js("document.querySelector('#conn').title")],
          [True, "Connected"])
    shot("phone")
finally:
    b.close()
    stop(front)
    stop(g90)
    stop(qmx)
    shutil.rmtree(TMP, ignore_errors=True)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
