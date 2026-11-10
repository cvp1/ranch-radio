"""The Modules tab in headless Chromium.

On a simulated Mac radio server (Mesh on its fake bridge) and a Linux station,
both on scratch copies of the repo and home:
  - the radio page shows a Modules tab; its rows group by state
  - Mesh reads On; switching it off from the Modules tab flips Mesh's own
    switch too (one switch path), and on again
  - adding Winlink from the page: a bad sound card name shows its error under
    the field and writes nothing; a good one shows the diff, applies, and
    shows the install commands; the host file has the block and the radio a lane
  - a push from the server (Refresh) doesn't wipe a half-typed form
  - the station page has the tab too

    python3 tests/modules_page_test.py        (CHROME=/path/to/chrome if not found)
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from cdp_browser import Browser  # noqa: E402
import testkit  # noqa: E402

CHROME = (os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
          or next((p for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                               "/Applications/Chromium.app/Contents/MacOS/Chromium") if os.path.exists(p)), None))
if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")
TMP = tempfile.mkdtemp(prefix="modpage-")
REPO, HOME = os.path.join(TMP, "repo"), os.path.join(TMP, "home")
os.makedirs(os.path.join(REPO, "hosts"))
os.makedirs(HOME)
MARK = os.path.join(TMP, "mesh-marker")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:68} got {got!r}" + ("" if ok else f" want {want!r}"))


def write_host(name, h):
    with open(os.path.join(REPO, "hosts", f"{name}.json"), "w") as f:
        json.dump(h, f, indent=2)
        f.write("\n")


def read_host(name):
    with open(os.path.join(REPO, "hosts", f"{name}.json")) as f:
        return json.load(f)


write_host("testmac", {"host": "testmac", "os": "macos", "repo": "~/ranch-radio", "python": sys.executable,
                       "radios": [{"id": "ic7300", "name": "IC-7300", "label": "t.radio", "device": "/dev/null", "web": 7301}],
                       "station": {"https": False}, "mesh": {"port": MARK, "keys": []}})
write_host("testlinux", {"host": "testlinux", "os": "linux", "repo": "~/ranch-radio", "python": sys.executable,
                         "radios": [], "station": {"https": False}})


def wait_port(p, port):
    for _ in range(600):                  # 60 s ceiling; returns once the port answers
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return p
        except OSError:
            time.sleep(0.1)
    p.kill()
    print(p.stdout.read())
    raise SystemExit("did not start")


def stop(p):
    p.terminate()
    try:
        p.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()


env = dict(os.environ, RANCH_RADIO_REPO=REPO, RANCH_RADIO_HOME=HOME)
WEB = testkit.free_port()
mac_cfg = os.path.join(TMP, "station-mac.json")        # the Mac host's station: Modules + Mesh
with open(mac_cfg, "w") as f:
    json.dump({"name": "testmac", "radios": [], "host_file": "testmac", "addons_state": os.path.join(TMP, "state.json"),
               "mesh": {"port": MARK, "python": sys.executable, "bridge": "tests/fake_mesh_bridge.py", "log": ""}}, f)
srv = wait_port(subprocess.Popen(
    [sys.executable, "station.py", "--config", mac_cfg, "--listen", str(WEB), "--rigctl-port", "0"],
    cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True), WEB)
cfg = os.path.join(TMP, "station.json")
with open(cfg, "w") as f:
    json.dump({"name": "Test", "radios": [], "host_file": "testlinux",
               "addons_state": os.path.join(TMP, "st-state.json")}, f)
ST = testkit.free_port()        # from the OS, not WEB + 1: the browser or an add-on may hold that one (CI 2026-11-10)
st = wait_port(subprocess.Popen([sys.executable, "station.py", "--config", cfg, "--listen", str(ST),
                                 "--rigctl-port", "0"], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True), ST)
b = Browser(CHROME, WEB + 2)


def row(name):
    return f"document.querySelector('#modules .mod-row[data-name=\"{name}\"]')"


def click(sel):
    return b.js(f"(() => {{ const e = {sel}; if (!e) return 'missing'; e.click(); return 'ok'; }})()")


try:
    b.goto(f"http://127.0.0.1:{WEB}/#/modules")
    check("station page: a Modules tab in the bar",
          b.wait("[...document.querySelectorAll('#st-nav a')].some(a => a.textContent === 'Modules')"), True)
    check("Mesh reads On", b.wait(f"{row('mesh')}.querySelector('.mod-state').textContent === 'On'"), True)
    check("the other views are hidden on this one", b.js("document.querySelector('#view-radio').hidden && document.querySelector('#view-home').hidden"), True)
    check("the core row is listed", b.js(f"{row('core')} !== null"), True)
    check("omarchy-bar is folded under 'Not for this OS'",
          b.js(f"{row('omarchy-bar')}.closest('details') !== null"), True)
    check("uvpro is not: it runs on a Mac", b.js(f"{row('uvpro')}.closest('details') === null"), True)

    b.js("window.confirm = () => true")
    click(f"{row('mesh')}.querySelector('.mod-power')")
    check("switch Mesh off from the Modules tab: its row reads Off",
          b.wait(f"{row('mesh')}.querySelector('.mod-state').textContent === 'Off'", 10), True)
    check("  and Mesh's own switch flipped too (one switch path)",
          b.wait("document.querySelector('#mesh-power').textContent.includes('Switch on')", 5), True)
    NAV = "[...document.querySelectorAll('#st-nav .st-tab')].map(a => a.textContent)"
    check("  Mesh leaves the tab bar while it is off",
          b.wait(f"!{NAV}.some(t => t.startsWith('Mesh')) && 'gone'", 5), "gone")
    check("  the Modules tab names it",
          b.js("[...document.querySelectorAll('#st-nav a')].find(a => a.textContent === 'Modules').title.includes('Mesh')"), True)
    click(f"{row('mesh')}.querySelector('.mod-power')")
    check("  and on again", b.wait(f"{row('mesh')}.querySelector('.mod-state').textContent === 'On'", 10), True)
    check("  Mesh is back in the tab bar",
          b.wait(f"{NAV}.some(t => t.startsWith('Mesh')) && 'back'", 10), "back")

    click(f"{row('winlink')}.querySelector('.mod-add')")
    check("Add… opens a form with the fields only you can give",
          b.wait("document.querySelectorAll('#modules .mod-form input').length === 2"), True)
    b.js("document.querySelector('#modules input[name=\"ardopcf.audio_in\"]').value = 'USB; reboot'")
    b.js("document.querySelector('#modules input[name=\"ardopcf.audio_out\"]').value = 'USB Audio CODEC'")
    before = json.dumps(read_host("testmac"), sort_keys=True)
    click("document.querySelector('#modules .mod-plan')")
    check("a bad value shows its error under the field",
          bool(b.wait("document.querySelector('#modules .mod-err[data-err=\"ardopcf.audio_in\"]')?.textContent.length > 0", 20)), True)
    check("  and writes nothing", json.dumps(read_host("testmac"), sort_keys=True), before)
    b.js("document.querySelector('#modules input[name=\"ardopcf.audio_in\"]').value = 'USB Audio CODEC'")
    click("document.querySelector('#modules .mod-refresh')")
    time.sleep(2.5)
    check("a Refresh push keeps a half-typed form",
          b.js("(document.querySelector('#modules input[name=\"ardopcf.audio_in\"]')||{}).value"), "USB Audio CODEC")
    click("document.querySelector('#modules .mod-plan')")
    check("Show the change: the diff", bool(b.wait("(document.querySelector('#modules .mod-diff')||{}).textContent?.includes('winlink')")), True)
    check("  still nothing written", json.dumps(read_host("testmac"), sort_keys=True), before)
    click("document.querySelector('#modules .mod-apply')")
    check("Apply: the page shows the install commands",
          bool(b.wait("[...document.querySelectorAll('#modules .mod-plan-box.done code')].some(c => c.textContent === './ranch-radio install testmac --apply')")), True)
    h = read_host("testmac")
    check("  the host file has Winlink, and the radio a lane", ("winlink" in h, h["radios"][0].get("lane")), (True, 4537))
    check("  Winlink now reads 'Not running yet' (pending: install, restart)",
          b.wait(f"{row('winlink')}.querySelector('.mod-state').textContent === 'Not running yet'", 10), True)

    b.goto(f"http://127.0.0.1:{ST}/#/modules")
    check("station page: a Modules tab",
          b.wait("[...document.querySelectorAll('#st-nav a')].some(a => a.textContent === 'Modules')"), True)
    check("station page: rows, packet available on Linux",
          b.wait(f"({row('packet')}||{{querySelector:()=>null}}).querySelector('.mod-state')?.textContent === 'Available'", 15), True)
finally:
    b.close()
    stop(srv)
    stop(st)
    shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
