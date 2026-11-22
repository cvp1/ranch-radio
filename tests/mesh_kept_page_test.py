"""The Mesh tab's node list with nodes the radio no longer lists (mesh.py
keep_nodes), driven in headless Chromium against server.py --sim and
tests/fake_mesh_bridge.py, with a kept-nodes file written before the start.

  - the list is the radio's nodes and the kept ones together, newest heard
    first whoever holds them; the kept ones are marked, and the count says
    how many are whose
  - "Recent" hides a kept node not heard in a day like any other
  - a kept node's card says why it is kept; Favorite and Ignore are off (they
    are the radio's), Remove is on
  - Remove forgets it: gone from the list, the count and the station's file
  - a kept node the radio lists again shows once, as the radio's

    python3 tests/mesh_kept_page_test.py
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
TMP = tempfile.mkdtemp(prefix="meshkeptpage-")
LOG = os.path.join(TMP, "mesh.jsonl")
KEPT = os.path.join(TMP, "mesh-nodes.json")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


def rec(nid, short, age_s):
    return {"num": int(nid[1:], 16), "id": nid, "shortName": short, "longName": f"{short} far node", "hwModel": "RAK4631",
            "role": "CLIENT", "last_heard": int(time.time() - age_s), "snr": None, "hops": 4, "lat": None, "lon": None,
            "alt": None, "mqtt": False, "licensed": False, "kept": True}


# Two nodes the radio dropped (one heard an hour ago, one three days ago) and
# one it still lists (!0000beef: the fake's BASE), as a restart would find them.
with open(KEPT, "w") as f:
    json.dump({"!00000a01": rec("!00000a01", "OLD1", 3600), "!00000a02": rec("!00000a02", "OLD2", 3 * 86400),
               "!0000beef": rec("!0000beef", "BASE", 7200)}, f)
adds = os.path.join(TMP, "addons-cfg.json")
with open(adds, "w") as f:
    json.dump({"mesh": {"port": "/dev/null", "python": sys.executable, "bridge": "tests/fake_mesh_bridge.py", "log": LOG}}, f)
server = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB), "--rigctl-port", "0",
                           "--addons-state", os.path.join(TMP, "state.json"), "--addons", adds],
                          cwd=ROOT, env=dict(os.environ, MESH_KEPT_SAVE_S="0.2"),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

ROWS = "[...document.querySelectorAll('#mesh-nodes .mesh-node')]"
SHORTS = f"{ROWS}.map((r) => r.querySelector('.mesh-short').textContent.slice(1))"
KEPT_ROWS = f"{ROWS}.filter((r) => r.classList.contains('kept')).map((r) => r.querySelector('.mesh-short').textContent.slice(1))"
COUNT = "document.querySelector('#mesh-count-nodes')"
BTN = "(a) => document.querySelector(`#mesh-card [data-act=${a}]`)"
b = Browser(CHROME, CDP)
try:
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", WEB), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    b.goto(f"http://127.0.0.1:{WEB}/?v=mesh#/mesh")
    b.wait(f"{ROWS}.length >= 4 && {SHORTS}.includes('BASE')")
    listed = b.js(f"{ROWS}.filter((r) => !r.classList.contains('kept')).length")
    check("the kept nodes are in the list, marked", sorted(b.js(KEPT_ROWS)), ["OLD1", "OLD2"])
    check("  BASE, which the radio lists, shows once and is the radio's",
          b.js(f"[{SHORTS}.filter((s) => s === 'BASE').length, {KEPT_ROWS}.includes('BASE')]"), [1, False])
    check("  newest heard first, kept or not: the hour-old one before the days-old one",
          b.js(f"{SHORTS}.indexOf('OLD1') < {SHORTS}.indexOf('OLD2')"), True)
    check("  the count is all of them", b.js(f"{COUNT}.textContent"), f"{listed + 2} known")
    check("  and says how many are whose", b.js(f"{COUNT}.title.startsWith('{listed} in the radio\\'s own list, 2 more kept')"), True)

    b.js("document.querySelector('#mesh-all').click()")
    check("Recent: a kept node not heard in a day is hidden, like any other",
          b.wait(f"!{SHORTS}.includes('OLD2') && {SHORTS}.includes('OLD1') && 'ok'"), "ok")
    b.js("document.querySelector('#mesh-all').click()")
    b.wait(f"{SHORTS}.includes('OLD2')")

    b.js(f"{ROWS}.find((r) => r.textContent.includes('OLD1')).click()")
    b.wait("!document.querySelector('#mesh-card').hidden")
    check("a kept node's card says why it is kept",
          (b.js("document.querySelector('#mesh-card .mesh-kept-note')?.textContent") or "").startswith("Kept by the station"), True)
    check("  Favorite and Ignore are off, Remove is on",
          b.js(f"[({BTN})('fav').disabled, ({BTN})('ignore').disabled, ({BTN})('remove').disabled]"), [True, True, False])
    b.js(f"{ROWS}.find((r) => r.textContent.includes('BASE')).click()")
    b.wait("document.querySelector('#mesh-card .mesh-card-head b').textContent === 'BASE'")
    check("a listed node's card has no such note, and its buttons work",
          b.js(f"[!!document.querySelector('#mesh-card .mesh-kept-note'), ({BTN})('fav').disabled, ({BTN})('ignore').disabled]"),
          [False, False, False])

    b.js(f"{ROWS}.find((r) => r.textContent.includes('OLD1')).click()")
    b.wait("document.querySelector('#mesh-card .mesh-card-head b').textContent === 'OLD1'")
    b.js("window.confirm = () => true")
    b.js(f"({BTN})('remove').click()")
    check("Remove forgets a kept node: gone from the list", b.wait(f"!{SHORTS}.includes('OLD1') && 'gone'"), "gone")
    check("  its card closes", b.js("document.querySelector('#mesh-card').hidden"), True)
    check("  the count follows", b.js(f"{COUNT}.textContent"), f"{listed + 1} known")
    check("  and so does the station's file",
          testkit.wait_for(lambda: "!00000a01" not in json.load(open(KEPT)) and "!00000a02" in json.load(open(KEPT)), 10), True)
    b.js("location.reload()")
    b.wait(f"{ROWS}.length >= 3 && {SHORTS}.includes('OLD2')")
    check("after a reload: still forgotten, the other still kept", sorted(b.js(KEPT_ROWS)), ["OLD2"])
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
