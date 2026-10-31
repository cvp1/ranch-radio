"""The Modules tab in a real browser (modules plan M2-M4), headless Chromium.

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
import random
import shutil
import socket
import subprocess
import sys
import os as _os
# Host files come from examples/hosts/ (or a scratch copy) only, never a
# real ~/.config/ranch-radio/hosts on the machine running the tests.
_os.environ["RANCH_RADIO_HOSTS"] = ""
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from cdp_browser import Browser  # noqa: E402

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
                       "radios": [{"id": "ic7300", "name": "IC-7300", "label": "t.radio", "device": "/dev/null"}],
                       "mesh": {"port": MARK, "keys": []}})
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
WEB = 19000 + random.randrange(900)
srv = wait_port(subprocess.Popen(
    [sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB), "--rigctl-port", "0",
     "--addons-state", os.path.join(TMP, "state.json"), "--mesh-port", MARK, "--mesh-python", sys.executable,
     "--mesh-bridge", "tests/fake_mesh_bridge.py", "--mesh-log", "", "--host-file", "testmac"],
    cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True), WEB)
cfg = os.path.join(TMP, "station.json")
with open(cfg, "w") as f:
    json.dump({"name": "Test", "radios": [], "host_file": "testlinux",
               "addons_state": os.path.join(TMP, "st-state.json")}, f)
ST = WEB + 1
st = wait_port(subprocess.Popen([sys.executable, "station.py", "--config", cfg, "--listen", str(ST),
                                 "--rigctl-port", "0"], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True), ST)
b = Browser(CHROME, WEB + 2)


def row(name):
    return f"document.querySelector('#modules .mod-row[data-name=\"{name}\"]')"


def state(name):
    return b.js(f"({row(name)}||{{}}).querySelector ? {row(name)}.querySelector('.mod-state').textContent : null")


def click(sel):
    return b.js(f"(() => {{ const e = {sel}; if (!e) return 'missing'; e.click(); return 'ok'; }})()")


try:
    b.goto(f"http://127.0.0.1:{WEB}/#/modules")
    check("radio page: a Modules tab in the bar",
          b.wait("[...document.querySelectorAll('#rr-nav a')].some(a => a.textContent === 'Modules')"), True)
    check("the Modules view shows rows", bool(b.wait(f"{row('mesh')} !== null", 15)), True)
    check("the other panels are hidden on this view", b.js("getComputedStyle(document.querySelector('.panel.vfo')).display"), "none")
    check("Mesh reads On", b.wait(f"{row('mesh')}.querySelector('.mod-state').textContent === 'On'"), True)
    check("radios and core come first: core row present", b.js(f"{row('core')} !== null"), True)
    check("uvpro is folded under 'Not for this OS'",
          b.js(f"{row('uvpro')}.closest('details') !== null"), True)

    b.js("window.confirm = () => true")
    click(f"{row('mesh')}.querySelector('.mod-power')")
    check("switch Mesh off from the Modules tab: its row reads Off",
          b.wait(f"{row('mesh')}.querySelector('.mod-state').textContent === 'Off'", 10), True)
    check("  and Mesh's own switch flipped too (one switch path)",
          b.wait("document.querySelector('#mesh-power').textContent.includes('Switch on')", 5), True)
    NAV = "[...document.querySelectorAll('#rr-nav .st-tab')].map(a => a.textContent)"
    check("  Mesh leaves the tab bar while it is off",
          b.wait(f"!{NAV}.some(t => t.startsWith('Mesh')) && 'gone'", 5), "gone")
    check("  the Modules tab names it",
          b.js("[...document.querySelectorAll('#rr-nav a')].find(a => a.textContent === 'Modules').title.includes('Mesh')"), True)
    click(f"{row('mesh')}.querySelector('.mod-power')")
    check("  and on again", b.wait(f"{row('mesh')}.querySelector('.mod-state').textContent === 'On'", 10), True)
    check("  Mesh is back in the tab bar",
          b.wait(f"{NAV}.some(t => t.startsWith('Mesh')) && 'back'", 10), "back")

    # Add Winlink: a bad sound card name first
    click(f"{row('winlink')}.querySelector('.mod-add')")
    check("Add… opens a form with the fields only you can give",
          b.wait("document.querySelectorAll('#modules .mod-form input').length === 2"), True)
    b.js("document.querySelector('#modules input[name=\"ardopcf.audio_in\"]').value = 'USB; reboot'")
    b.js("document.querySelector('#modules input[name=\"ardopcf.audio_out\"]').value = 'USB Audio CODEC'")
    before = json.dumps(read_host("testmac"), sort_keys=True)
    click("document.querySelector('#modules .mod-plan')")
    check("a bad value shows its error under the field",
          bool(b.wait("document.querySelector('#modules .mod-err[data-err=\"ardopcf.audio_in\"]').textContent.length > 0")), True)
    check("  and writes nothing", json.dumps(read_host("testmac"), sort_keys=True), before)
    # a server push (Refresh) must not wipe what's typed
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
    b.screenshot(os.path.join(TMP, "..", "modules-page.png"))

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
