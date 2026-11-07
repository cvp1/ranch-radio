"""The radio page's STOP TX button, in a real browser (headless Chromium).

The button is in the top bar in every view (radio, Winlink) and on a narrow
screen; a press latches on the server: the banner shows, PTT is disabled, and
both survive a reload; Resume (after its confirm) clears it. Server-side
behaviour is tests/tx_stop_test.py; this is the page.

    python3 tests/tx_stop_page_test.py          # SHOTS=dir to keep screenshots
"""
import os
import shutil
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import testkit  # noqa: E402
from cdp_browser import Browser  # noqa: E402

MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME = (os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
          or (MAC_CHROME if os.path.exists(MAC_CHROME) else None))
if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")
SHOTS = os.environ.get("SHOTS")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


VISIBLE = "(s) => { const e = document.querySelector(s); return !!e && !e.closest('[hidden]') && e.offsetParent !== null; }"
TEXT = "(s) => (document.querySelector(s)?.textContent || '')"

web, rig = testkit.free_port(), testkit.free_port()
env = dict(os.environ)
env.pop("CC_OBS_TOKENS_FILE", None)
p = subprocess.Popen([sys.executable, "server.py", "--sim", "--rig", "ic7300", "--no-audio",
                      "--listen", str(web), "--host", "127.0.0.1",
                      "--rigctl-port", str(rig), "--rigctl-host", "127.0.0.1"],
                     cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", web), timeout=0.2).close()
        break
    except OSError:
        time.sleep(0.1)
b = Browser(CHROME, testkit.free_port(), size="1500,1000")
try:
    url = f"http://127.0.0.1:{web}/"
    b.goto(url)
    b.wait(f"({TEXT})('#conn b') === 'Connected'")
    check("STOP TX visible in the radio view", b.js(f"({VISIBLE})('#tx-stop')"), True)
    check("its label", b.js(f"({TEXT})('#tx-stop')"), "STOP TX")
    check("no banner before a press", b.js(f"({VISIBLE})('#tx-stop-banner')"), False)
    b.goto(url + "#/winlink")
    b.wait(f"({TEXT})('#conn b') === 'Connected'")
    check("STOP TX visible in the Winlink view", b.js(f"({VISIBLE})('#tx-stop')"), True)
    b.call("Emulation.setDeviceMetricsOverride", width=390, height=800, deviceScaleFactor=2, mobile=True)
    time.sleep(0.3)
    check("STOP TX visible on a phone-width screen", b.js(f"({VISIBLE})('#tx-stop')"), True)
    b.call("Emulation.clearDeviceMetricsOverride")
    b.goto(url)
    b.wait(f"({TEXT})('#conn b') === 'Connected'")

    b.js("document.querySelector('#tx-stop').click()")
    b.wait(f"({VISIBLE})('#tx-stop-banner')", 5)
    check("pressed: the banner shows", b.js(f"({VISIBLE})('#tx-stop-banner')"), True)
    check("pressed: the button says so", b.js(f"({TEXT})('#tx-stop')"), "TX STOPPED")
    check("pressed: PTT disabled", b.js("document.querySelector('#ptt').disabled"), True)
    if SHOTS:
        b.screenshot(os.path.join(SHOTS, "tx-stopped.png"))
    b.goto(url)                                      # a reload
    b.wait(f"({TEXT})('#conn b') === 'Connected'")
    b.wait(f"({VISIBLE})('#tx-stop-banner')", 5)
    check("reload: still latched (the server holds it)", b.js(f"({VISIBLE})('#tx-stop-banner')"), True)
    check("reload: the banner says who and when", "by 127.0.0.1" in b.js(f"({TEXT})('#tx-stop-detail')"), True)

    b.js("window.confirm = () => false; document.querySelector('#tx-resume').click()")
    time.sleep(0.6)
    check("Resume cancelled at its confirm: still latched", b.js(f"({VISIBLE})('#tx-stop-banner')"), True)
    b.js("window.confirm = () => true; document.querySelector('#tx-resume').click()")
    b.wait(f"!({VISIBLE})('#tx-stop-banner')", 5)
    check("Resume: banner gone", b.js(f"({VISIBLE})('#tx-stop-banner')"), False)
    check("Resume: label back", b.js(f"({TEXT})('#tx-stop')"), "STOP TX")
finally:
    b.close()
    p.terminate()
    out = p.communicate(timeout=10)[0]
check("no server traceback", "Traceback" in out, False)
check("server logged STOP and Resume", ("STOP TX by" in out, "STOP TX cleared" in out), (True, True))

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
