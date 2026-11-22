"""ranch-radio demo (ranch_radio/demo.py): a simulated station to try without
a radio, until Ctrl-C. Driven as a user would: the CLI in its own process
group, the page in headless Chromium (muted). Nothing transmits.

  it prints the station's address on loopback, and serves it
  the station page's Roster shows the README fixture's made-up stations,
    and the Map their decodes (from a fresh socket's hello, the server's word)
  a Roster click answers that this is the demo, and reaches no JTDX
  the radio behind it is the simulator
  this process's HOME is untouched (the sentinel's own snapshot of the
    station's directories), and no host file appears
  Ctrl-C: exit 0, both ports closed, no process left in its group, and its
    scratch directory gone

    python3 tests/demo_test.py
"""
import asyncio
import glob
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "tests"), os.path.join(ROOT, "tests", "ci"), os.path.join(ROOT, "tools"), ROOT]
import aiohttp  # noqa: E402
from cdp_browser import Browser  # noqa: E402
import readme_fixture as F  # noqa: E402
import sentinel  # noqa: E402
import testkit  # noqa: E402

CHROME = (os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
          or next((p for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                               "/Applications/Chromium.app/Contents/MacOS/Chromium") if os.path.exists(p)), None))
if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")
PORT, CDP = testkit.free_port(), testkit.free_port()
HOME = os.path.expanduser("~")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:66} got {got!r}" + ("" if ok else f" want {want!r}"))


def answers(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


def hello(port):
    async def go():
        async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
            while True:
                m = await ws.receive(timeout=10)
                if m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data).get("t") == "hello":
                    return json.loads(m.data)
    return asyncio.run(go())


scratch = lambda: set(glob.glob(os.path.join(tempfile.gettempdir(), "ranch-radio-demo-*")))  # noqa: E731
home_before, scratch_before = sentinel.snapshot(HOME), scratch()
hosts_before = sorted(glob.glob(os.path.join(HOME, ".config", "ranch-radio", "hosts", "*")))
out_path = os.path.join(tempfile.mkdtemp(prefix="demo-test-"), "out.txt")
demo = subprocess.Popen([sys.executable, "ranch-radio", "demo", "--port", str(PORT)], cwd=ROOT,
                        env={**os.environ, "RANCH_RADIO_NO_VENV": "1"}, stdout=open(out_path, "w"),
                        stderr=subprocess.STDOUT, start_new_session=True)
b = radio_port = None
try:
    url = f"http://127.0.0.1:{PORT}/"
    testkit.wait_for(lambda: url in open(out_path).read() or demo.poll() is not None, 90, 0.2)
    check("it prints the station's address", url in open(out_path).read(), True)
    if url not in open(out_path).read():
        raise SystemExit("the demo did not start:\n" + open(out_path).read())
    check("  and serves it", answers(PORT), True)
    radio_port = int(open(out_path).read().split("its own page: http://127.0.0.1:")[1].split("/")[0])

    h = hello(PORT)
    snap = h.get("addons") or {}
    rows = ((snap.get("roster") or {}).get("roster") or {}).get("rows") or []
    check("the Roster holds the fixture's stations", sorted(r["call"] for r in rows),
          sorted(r["call"] for r in F.ROSTER["rows"]))
    decodes = ((snap.get("map") or {}).get("map") or {}).get("decodes") or []
    check("the Map holds their decodes", len(decodes), len(F.MAP["map"]["decodes"]))
    check("the radio behind it is the simulator", "sim" in json.dumps(hello(radio_port).get("config", {})).lower(), True)

    b = Browser(CHROME, CDP)
    b.goto(url + "#/roster")
    n = b.wait("document.querySelectorAll('#view-addon-roster .rs-row').length", 20)
    check("the page shows the Roster's rows", n, len(F.ROSTER["rows"]))
    b.js("document.querySelector('#view-addon-roster .rs-row[data-call=\"VK2ABC\"] td').click()")
    said = b.wait("[...document.querySelectorAll('#toasts .toast')].map((t) => t.textContent).join(' | ')"
                  " .includes('demo') && [...document.querySelectorAll('#toasts .toast')].map((t) => t.textContent)"
                  ".join(' | ')", 10)
    check("a Roster click says it is the demo; no JTDX is reached", "no JTDX" in str(said), True)
finally:
    if b:
        b.close()
    if demo.poll() is None:
        os.killpg(demo.pid, signal.SIGINT)          # Ctrl-C: the whole group, as a terminal sends it
    try:
        rc = demo.wait(30)
    except subprocess.TimeoutExpired:
        os.killpg(demo.pid, signal.SIGKILL)
        rc = "hung"

check("Ctrl-C: exit 0", rc, 0)
check("  it says it stopped", "stopped" in open(out_path).read(), True)
check("  both ports closed", (answers(PORT), bool(radio_port) and answers(radio_port)), (False, False))
try:
    os.killpg(demo.pid, 0)
    left = True
except ProcessLookupError:
    left = False
check("  no process left in its group", left, False)
check("  its scratch directory is gone", scratch() - scratch_before, set())
check("this process's HOME is untouched", sentinel.snapshot(HOME) == home_before, True)
check("no host file appeared", sorted(glob.glob(os.path.join(HOME, ".config", "ranch-radio", "hosts", "*"))) == hosts_before,
      True)
shutil.rmtree(os.path.dirname(out_path), ignore_errors=True)
print(f"\n{'FAIL' if fails else 'PASS'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
