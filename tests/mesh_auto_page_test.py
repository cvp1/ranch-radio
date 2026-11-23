"""The Mesh tab's "Auto-trace" switch (addons/mesh.py "mesh_auto", mesh.py
set_auto_trace), driven in headless Chromium against server.py --sim and
tests/fake_mesh_bridge.py, with two nodes.

  - an "Auto-trace" chip before "Find radios", saying off for a node whose
    host file leaves auto_trace off, with a title that says what it sends
  - a click turns it on: the chip says so, and the station keeps it per node
    beside the add-on state file
  - each node has its own: the other node's chip is still off; turning that
    one on keeps both
  - off again: said, kept, and a reload shows the kept choice

    python3 tests/mesh_auto_page_test.py
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
import testkit  # noqa: E402

WEB = testkit.free_port()
CDP = testkit.free_port()
CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
TMP = tempfile.mkdtemp(prefix="meshautopage-")
KEPT = os.path.join(TMP, "mesh-auto.json")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


def kept():
    try:
        with open(KEPT) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


adds = os.path.join(TMP, "addons-cfg.json")
with open(adds, "w") as f:
    json.dump({"mesh": {"port": "/dev/null", "label": "MF18", "python": sys.executable, "log": "",
                        "bridge": "tests/fake_mesh_bridge.py",
                        "radios": [{"label": "LF", "port": "/dev/null-lf", "log": ""}]}}, f)
server = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB), "--rigctl-port", "0",
                           "--addons-state", os.path.join(TMP, "state.json"), "--addons", adds], cwd=ROOT,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

BTN = "document.querySelector('#mesh-auto-btn')"
STATE = f"[{BTN}.textContent, {BTN}.getAttribute('aria-pressed')]"
b = Browser(CHROME, CDP)
try:
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", WEB), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    b.goto(f"http://127.0.0.1:{WEB}/?v=mesh#/mesh")
    b.wait(f"{BTN} && document.querySelector('#mesh-state').textContent === 'Connected'")
    check("an Auto-trace chip, before Find radios", b.js(f"{BTN}.nextElementSibling.id"), "mesh-scan-btn")
    check("  off: the host file leaves it off", b.js(STATE), ["Auto-trace off", "false"])
    check("  its title says this station sends nothing by itself", "sends nothing by itself" in b.js(f"{BTN}.title"), True)
    check("  nothing kept yet", kept(), None)

    b.js(f"{BTN}.click()")
    b.wait(f"{BTN}.textContent === 'Auto-trace on'")
    check("a click: on", b.js(STATE), ["Auto-trace on", "true"])
    check("  its title says what it sends", "traceroutes nodes it hears over relays" in b.js(f"{BTN}.title"), True)
    testkit.wait_for(lambda: kept() == {"": True}, 5, 0.1)
    check("  kept for this node", kept(), {"": True})

    b.js("document.querySelector('.mesh-radio[data-node=\"lf\"]').click()")
    b.wait(f"document.querySelector('.mesh-radio.on')?.dataset.node === 'lf' && {BTN}.textContent === 'Auto-trace off'")
    check("the other node: its own, still off", b.js(STATE), ["Auto-trace off", "false"])
    b.js(f"{BTN}.click()")
    b.wait(f"{BTN}.textContent === 'Auto-trace on'")
    testkit.wait_for(lambda: kept() == {"": True, "lf": True}, 5, 0.1)
    check("  on too: both kept", kept(), {"": True, "lf": True})

    b.js("document.querySelector('.mesh-radio[data-node=\"\"]').click()")
    b.wait(f"document.querySelector('.mesh-radio.on')?.dataset.node === '' && {BTN}.textContent === 'Auto-trace on'")
    b.js(f"{BTN}.click()")
    b.wait(f"{BTN}.textContent === 'Auto-trace off'")
    testkit.wait_for(lambda: kept() == {"": False, "lf": True}, 5, 0.1)
    check("off again: said and kept", (b.js(STATE), kept()), (["Auto-trace off", "false"], {"": False, "lf": True}))

    b.goto(f"http://127.0.0.1:{WEB}/?v=mesh#/mesh")
    b.wait(f"{BTN} && document.querySelector('#mesh-state').textContent === 'Connected' && "
           "document.querySelector('.mesh-radio.on')?.dataset.node === ''")
    check("a reload shows the kept choice", b.js(STATE), ["Auto-trace off", "false"])
finally:
    b.close()
    server.terminate()
    try:
        server.wait(10)
    except subprocess.TimeoutExpired:
        server.kill()
    shutil.rmtree(TMP, ignore_errors=True)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
