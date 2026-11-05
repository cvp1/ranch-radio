"""The MeshCore tab in a real browser, against tests/fake_meshcore_bridge.py.

  - the tab shows the node, its link and the nodes it heard
  - Settings opens the form with the node's sections; the Wi-Fi password
    field is a password field and starts empty
  - a radio preset fills the form and enables Save; Save writes those
    values to the node (they reach the fake bridge) and the form shows the
    read-back
  - a typed Wi-Fi password reaches the node, and after the save no input
    on the page holds it

    python3 tests/meshcore_page_test.py      (SHOT=/path.png saves a screenshot)
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from cdp_browser import Browser  # noqa: E402
from testkit import free_port  # noqa: E402

MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME = (os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
          or (MAC_CHROME if os.path.exists(MAC_CHROME) else None))
if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")
TMP = tempfile.mkdtemp(prefix="mcpage-")
CFG, CMD_LOG = os.path.join(TMP, "addons.json"), os.path.join(TMP, "commands.jsonl")
PWD = "page-pa55word"
fails = 0

with open(CFG, "w") as f:
    json.dump({"meshcore": {"port": os.path.join(TMP, "port"), "python": sys.executable, "log": "",
                            "bridge": os.path.join(ROOT, "tests", "fake_meshcore_bridge.py"),
                            "presets": [{"label": "Local", "freq": 908.205, "bw": 62.5, "sf": 9, "cr": 8}]}}, f)


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


def sets():
    try:
        with open(CMD_LOG) as f:
            return [c for c in map(json.loads, filter(str.strip, f)) if c.get("cmd") == "set"]
    except OSError:
        return []


def wait_for(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        r = pred()
        if r:
            return r
        time.sleep(0.1)
    return pred()


web = free_port()
server = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(web),
                           "--rigctl-port", "0", "--addons", CFG, "--addons-state", os.path.join(TMP, "state.json")],
                          cwd=ROOT, env=dict(os.environ, FAKE_MC_LOG=CMD_LOG),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(150):
    try:
        socket.create_connection(("127.0.0.1", web), timeout=0.2).close()
        break
    except OSError:
        time.sleep(0.1)
b = Browser(CHROME, free_port(), size="1300,1000")
P = "document.querySelector('.addon-meshcore')"
try:
    b.goto(f"http://127.0.0.1:{web}/#/meshcore")
    check("the tab shows the node", b.wait(f"{P}?.querySelector('.mc-name')?.textContent === 'TestNode' && 'yes'"), "yes")
    check("  its link", b.wait(f"({P}.querySelector('.mc-sub')?.textContent || '').includes('via Wi-Fi') && 'yes'"), "yes")
    check("  and the nodes it heard", b.js(f"{P}.querySelectorAll('.mc-node').length"), 2)

    b.js(f"{P}.querySelector('[data-cfg]').click()")
    check("Settings opens with the node's sections",
          b.wait(f"[...{P}.querySelectorAll('[data-sec]')].map(x => x.dataset.sec).join(',') || null"),
          "node,radio,wifi,bluetooth")

    b.js(f"{P}.querySelector('[data-sec=radio]').click()")
    b.wait(f"{P}.querySelector('[data-preset]') && 'yes'")
    check("radio: Save starts disabled", b.js(f"{P}.querySelector('[data-save]').disabled"), True)
    b.js(f"{P}.querySelector('[data-preset=\"0\"]').click()")
    check("  a preset fills the form", b.js(f"{P}.querySelector('[data-f=sf]').value"), "9")
    check("  and enables Save", b.js(f"{P}.querySelector('[data-save]').disabled"), False)
    b.js("window.confirm = () => true")          # the risky-change question
    b.js(f"{P}.querySelector('[data-save]').click()")
    check("  Save writes the preset to the node", (wait_for(sets) or [{}])[-1].get("values"),
          {"freq": 908.205, "sf": 9, "cr": 8})
    check("  the form shows the read-back",
          b.wait(f"{P}.querySelector('[data-f=sf]')?.value === '9' && {P}.querySelector('[data-save]').disabled && 'yes'"),
          "yes")

    b.js(f"{P}.querySelector('[data-sec=wifi]').click()")
    b.wait(f"{P}.querySelector('[data-f=password]') && 'yes'")
    check("Wi-Fi: the password is a password field", b.js(f"{P}.querySelector('[data-f=password]').type"), "password")
    check("  and starts empty", b.js(f"{P}.querySelector('[data-f=password]').value"), "")
    b.js(f"""(() => {{ const i = {P}.querySelector('[data-f=password]'); i.value = {json.dumps(PWD)};
             i.dispatchEvent(new Event('input', {{bubbles: true}})); }})()""")
    b.js(f"{P}.querySelector('[data-save]').click()")
    check("  a typed password reaches the node",
          wait_for(lambda: any((c.get("values") or {}).get("password") == PWD for c in sets())), True)
    b.wait(f"{P}.querySelector('[data-save]')?.disabled && 'yes'")
    check("  and after the save no input holds it",
          b.js(f"[...document.querySelectorAll('input')].some(i => i.value === {json.dumps(PWD)})"), False)
    if os.environ.get("SHOT"):
        b.js(f"{P}.querySelector('[data-sec=radio]').click()")
        time.sleep(0.5)
        b.screenshot(os.environ["SHOT"])
finally:
    b.close()
    server.terminate()
    server.wait(10)

print("PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
