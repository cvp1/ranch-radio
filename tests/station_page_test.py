"""S2: the station page (station.html), driven in headless Chromium.

Two simulated radios (G90, QMX), station.py in front with fake
Pat and mesh bridge, and a packet hub that is configured but not running.
Checks:
  - the navigation: Mission Control, one tab per connected radio with its rigctl
    ports (one carries the shared port), Winlink, Mesh, and
    Packet off the tab bar while the hub is down (its card stays)
  - Mission Control: a card per radio and service in lanes named like the
    tab bar's groups, names the shared port; a radio's card live (frequency,
    S-meter, signal trace, its mini spectrum from scope frames; on the air:
    power), an icon on every card, the UTC clock, the on-air line with Release TX, what needs
    you (unread mail, offline gear), keys 1-9 and 0, a click opens a tab, a
    key typed into a field is not a jump
  - a radio's tab shows that radio's own page in a frame, connected, without
    its own brand (the tab names it); the other radio's frame stays hidden
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
          ["G90", "QMX", "Winlink", "Packet (UV-PRO)", "Mesh"])
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
    check("  the tab is marked", b.js(f"{TABS}.find((t) => t.classList.contains('on')).textContent.startsWith('QMX')"), True)
    shot("qmx")
    b.js(f"{TABS}.find((t) => t.textContent.startsWith('G90')).click()")
    b.wait(f"{FRAME}?.src.includes('/r/g90/')")
    check("G90 tab: G90 frame shown, QMX frame kept but hidden",
          sorted(b.js("[...document.querySelectorAll('#view-radio iframe')].map((f) => [f.src.split('/r/')[1], f.hidden])")),
          [["g90/", False], ["qmx/", True]])
    b.goto(f"http://127.0.0.1:{WEB}/#/qmx")
    check("reload at #/qmx comes back to the QMX", bool(b.wait(f"{FRAME}?.src.includes('/r/qmx/')")), True)

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

    # The radio strip: every HF radio's frequency and TX on views that hide its page.
    b.js("location.hash = '#/winlink'")
    b.wait("document.querySelectorAll('#st-strip .sr').length === 2")
    check("strip: both radios on Winlink",
          b.js("[...document.querySelectorAll('#st-strip .sr b')].map((x) => x.textContent)"), ["G90", "QMX"])
    check("  with a frequency", b.js("/\\d+\\.\\d{3}\\.\\d{3}/.test(document.querySelector('#st-strip .sr .f').textContent)"), True)
    b.js("location.hash = '#/g90'")
    b.wait("document.querySelector('#st-strip').hidden")
    check("  hidden on a radio's own page", b.js("document.querySelector('#st-strip').hidden"), True)
    b.js("location.hash = '#/home'")
    b.wait("document.querySelector('#st-strip').hidden")
    check("  hidden on Mission Control (its own on-air line says it)", b.js("document.querySelector('#st-strip').hidden"), True)

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
finally:
    b.close()
    stop(front)
    stop(g90)
    stop(qmx)
    shutil.rmtree(TMP, ignore_errors=True)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
