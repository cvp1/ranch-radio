"""S2: the station page (station.html), driven in headless Chromium.

Two simulated radios (G90 first, then QMX), station.py in front with fake
Pat and mesh bridge, and a packet hub that is configured but not running.
Checks:
  - the navigation: Overview, one tab per connected radio with its rigctl
    ports (the shared one on the radio connected first), Winlink, Mesh, and
    Packet dimmed while the hub is down
  - the overview has a card per radio and service and names the shared port
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
    "winlink": {"pat": f"{sys.executable} tests/fake_pat.py", "pat_port": PAT, "lane_port": LANE,
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
time.sleep(0.5)
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
    b.wait(f"{TABS}.length >= 5")
    t = tabs()
    check("nav: Overview, both radios, Winlink, Mesh, Packet",
          [x.split()[0] for x in t], ["Overview", "G90", "QMX", "Winlink", "Mesh", "Packet"])
    check("  one radio carries the shared port", sum(" · " in x for x in t), 1)
    own = {x.split()[0]: x.split()[-1] for x in t if x.split()[0] in ("G90", "QMX")}
    check("  each radio's own port", own, {"G90": str(G90["rigctl"]), "QMX": str(QMX["rigctl"])})
    check("  Packet dimmed (hub down)", b.js(f"{TABS}.find((t) => t.textContent.startsWith('Packet')).classList.contains('off')"), True)
    check("overview: a card per radio and service",
          b.js("[...document.querySelectorAll('.st-card b')].map((e) => e.textContent)"),
          ["G90", "QMX", "Winlink", "Mesh", "Packet (UV-PRO)"])
    check("  names the shared port", str(SHARED) in b.js(f"({TEXT})('#st-help')"), True)
    shot("overview")

    b.js(f"{TABS}.find((t) => t.textContent.startsWith('QMX')).click()")
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
          b.js("[...document.querySelectorAll('#view-radio iframe')].map((f) => [f.src.split('/r/')[1], f.hidden])"),
          [["qmx/", True], ["g90/", False]])
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

    # A message arriving on the station's socket while Mesh is not open.
    b.js("(() => { const m = { t: 'mesh', msg: { k: 'msg', id: 10, t: Date.now() / 1000, from: '!0000beef', "
         "to: '^all', ch: 1, dm: false, text: 'gate shut' } }; ws.onmessage({ data: JSON.stringify(m) }); })()")
    check("mesh message elsewhere: count on the Mesh tab",
          b.js(f"{TABS}.find((t) => t.textContent.startsWith('Mesh')).querySelector('.st-badge')?.textContent"), "1")
    b.js("location.hash = '#/mesh'")
    b.wait(f"({VISIBLE})('#mesh')")
    check("  opening Mesh clears it",
          b.js(f"{TABS}.find((t) => t.textContent.startsWith('Mesh')).querySelector('.st-badge')"), None)
    shot("mesh")

    # The Mesh add-on's switch on the station (addons/mesh.py): off frees the node.
    MESH_TAB = f"{TABS}.find((t) => t.textContent.startsWith('Mesh'))"
    b.wait(f"({TEXT})('#mesh-state') === 'Connected'")
    b.js("window.confirm = () => true")
    b.js("document.querySelector('#mesh-power').click()")
    check("Mesh switched off: the state says so", b.wait(f"({TEXT})('#mesh-state') === 'Switched off' && 'off'"), "off")
    check("  the Mesh tab's dot goes off", b.js(f"{MESH_TAB}.classList.contains('off')"), True)
    b.js("document.querySelector('#mesh-power').click()")
    check("  on again: Connected", b.wait(f"({TEXT})('#mesh-state') === 'Connected' && 'on'"), "on")
    check("  the dot comes back", b.wait(f"!{MESH_TAB}.classList.contains('off') && 'ok'"), "ok")

    # The radio strip (review 2026-10-18): every HF radio's frequency and TX on
    # the views that hide its page, with Release TX.
    import socket as _sock
    b.js("location.hash = '#/home'")
    b.wait("document.querySelectorAll('#st-strip .sr').length === 2")
    check("strip: both radios on the Overview",
          b.js("[...document.querySelectorAll('#st-strip .sr b')].map((x) => x.textContent)"), ["G90", "QMX"])
    check("  with a frequency", b.js("/\\d+\\.\\d{3}\\.\\d{3}/.test(document.querySelector('#st-strip .sr .f').textContent)"), True)
    with _sock.create_connection(("127.0.0.1", G90["rigctl"]), timeout=3) as c:
        f = c.makefile("r")
        c.sendall(b"T 1\n"); f.readline()
        b.wait("!!document.querySelector('#st-strip .sr.tx')")
        check("  the G90 keyed: ON AIR and Release TX",
              (b.js("document.querySelector('#st-strip .sr.tx b').textContent"),
               b.js("!!document.querySelector('#st-strip button')")), ("G90", True))
        shot("strip-tx")
        b.js("document.querySelector('#st-strip button').click()")
        b.wait("!document.querySelector('#st-strip .sr.tx')")
        c.sendall(b"t\n")
        check("  Release TX unkeys the radio", f.readline().strip(), "0")
    b.js("location.hash = '#/g90'")
    b.wait("document.querySelector('#st-strip').hidden")
    check("  hidden on a radio's own page", b.js("document.querySelector('#st-strip').hidden"), True)
    b.js("location.hash = '#/winlink'")
    b.wait("!document.querySelector('#st-strip').hidden")
    check("  shown on Winlink", b.js("document.querySelector('#st-strip').hidden"), False)
finally:
    b.close()
    stop(front)
    stop(g90)
    stop(qmx)
    shutil.rmtree(TMP, ignore_errors=True)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
