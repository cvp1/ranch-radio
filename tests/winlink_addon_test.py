"""Winlink as an add-on (addons/winlink.py): the on/off switch, with tests/fake_pat.py as Pat.

The Winlink panel's protocol and the transmit lane are winlink_test.py's and
winlink_lane_test.py's (unchanged); this checks what the add-on system adds:
  - Winlink is on by default and has no add-on panel (its page is the panel)
  - switching off while a session runs is refused, and the session runs on
  - off stops Pat: its port closes, the mailbox answers 503, a connect is
    refused, and the lane stays unkeyed
  - off is kept across a restart, and Pat isn't started
  - switching on starts Pat again: the mailbox answers and a session completes
  - without addons/winlink.py the radio server runs on: no Winlink panel, and
    the lane (core, not the add-on's) still answers

    python3 tests/winlink_addon_test.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import winlink_test_lib as L  # noqa: E402

TMP = tempfile.mkdtemp(prefix="wladdon-")
STATE = os.path.join(TMP, "addons.json")     # never the real ~/.local/state one
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


def http_status(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{L.WEB}{path}", timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def pat_up():
    try:
        socket.create_connection(("127.0.0.1", L.PATP), timeout=0.5).close()
        return True
    except OSError:
        return False


def wait_for(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def start(env=None, *extra):
    return L.start(env, "--addons-state", STATE, *extra)


def start_off(env=None):
    """L.start, for a server whose Winlink is switched off: L.start waits for
    Pat's port too, which (rightly) never opens. Wait for the page and lane."""
    logf = tempfile.NamedTemporaryFile(prefix="fakepat-", suffix=".jsonl", delete=False).name
    e = dict(os.environ, FAKE_PAT_RIGCTL=str(L.LANE), FAKE_PAT_LOG=logf, **(env or {}))
    p = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(L.WEB),
         "--rigctl-port", str(L.APPS), "--winlink-rigctl-port", str(L.LANE),
         "--winlink-pat", f"{sys.executable} tests/fake_pat.py", "--winlink-pat-port", str(L.PATP),
         "--addons-state", STATE],
        cwd=L.ROOT, env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            for port in (L.WEB, L.LANE):
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return p, logf
        except OSError:
            time.sleep(0.1)
    p.kill()
    print(p.stdout.read())
    raise SystemExit("server did not start")


class Page(L.Page):
    """L.Page, also keeping the add-on registry's broadcasts ({"t": "addon"})."""

    def __init__(self):
        self.addon = []
        super().__init__()

    async def _read(self):
        import aiohttp
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                m = json.loads(msg.data)
                if m.get("t") in ("winlink", "notice"):
                    self.events.append(m)
                if m.get("t") == "addon":
                    self.addon.append(m)


def wait_addon(page, on, since, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        for m in page.addon[since:]:
            if m.get("addon") == "winlink" and m.get("on") is on:
                return m
        time.sleep(0.1)
    return None


server, flog = start({"FAKE_PAT_TX_S": "3"})
try:
    page = Page()
    check("on by default: config.winlink", page.hello["config"].get("winlink"), True)
    check("no add-on panel for Winlink (its page is the panel)",
          [a["name"] for a in page.hello["config"].get("addons", [])], [])
    check("not off in the snapshot", page.hello["winlink"].get("off"), None)
    check("the mailbox answers", http_status("/winlink/api/mailbox/in"), 200)

    # A session running: switching off is refused, and it completes.
    page.send({"t": "winlink_connect", "url": L.URL})
    check("a session starts", page.wait(lambda e: e.get("session") == "started") is not None, True)
    n_notice, n_addon = len(page.events), len(page.addon)
    page.send({"t": "addon", "addon": "winlink", "do": "power", "on": False})
    warn = page.wait(lambda e: e.get("t") == "notice" and "session is running" in e.get("msg", ""), timeout=5)
    check("switch off mid-session: refused, with the reason", warn is not None, True)
    check("  and not switched off", wait_addon(page, False, n_addon, timeout=2), None)
    done = page.wait(L.ended, timeout=20)
    check("  the session completes", bool(done and done.get("ok")), True)
    check("  unkeyed after it", L.ptt(), "0")

    # Off: Pat stops, the mailbox and connects are refused, the lane stays unkeyed.
    n_addon = len(page.addon)
    page.send({"t": "addon", "addon": "winlink", "do": "power", "on": False})
    off = wait_addon(page, False, n_addon)
    check("switch off: the page is told", off is not None, True)
    check("  with the off snapshot", ((off or {}).get("snapshot") or {}).get("off"), True)
    check("  Pat has stopped (its port is closed)", wait_for(lambda: not pat_up()), True)
    check("  the mailbox answers 503", http_status("/winlink/api/mailbox/in"), 503)
    n = len(page.events)
    page.send({"t": "winlink_connect", "url": L.URL})
    warn = page.wait(lambda e: e.get("t") == "notice" and "switched off" in e.get("msg", ""), timeout=5)
    check("  a connect is refused: switched off", warn is not None, True)
    check("  no session started", page.wait(lambda e: e.get("session") == "started"
                                            and page.events.index(e) >= n, timeout=2), None)
    check("  the lane is not armed: Pat's T 1 is refused", L.lane_ptt_refused(), True)
    check("  and the radio unkeyed", L.ptt(), "0")
    with open(STATE) as f:
        check("  the state file says off", json.load(f).get("winlink"), False)
    page.close()

    L.stop(server)
    server, flog = start_off({"FAKE_PAT_TX_S": "1"})
    page = Page()
    check("after a restart: still off", page.hello["winlink"].get("off"), True)
    check("  the Winlink panel is still there", page.hello["config"].get("winlink"), True)
    time.sleep(1.5)
    check("  and Pat was not started", pat_up(), False)

    n_addon = len(page.addon)
    page.send({"t": "addon", "addon": "winlink", "do": "power", "on": True})
    on = wait_addon(page, True, n_addon)
    check("switch on: the page gets a live snapshot", ((on or {}).get("snapshot") or {}).get("off"), None)
    check("  Pat is up again", wait_for(pat_up), True)
    check("  the mailbox answers", wait_for(lambda: http_status("/winlink/api/mailbox/in") == 200), True)
    time.sleep(1)                     # Radio Remote's websocket to the new Pat
    page.send({"t": "winlink_connect", "url": L.URL})
    done = page.wait(L.ended, timeout=20)
    check("  a session completes", bool(done and done.get("ok")), True)
    check("  unkeyed after it", L.ptt(), "0")
    page.close()
finally:
    L.stop(server)

# --- the switch on the page (headless Chromium; skipped without one)
import random  # noqa: E402
import shutil  # noqa: E402
CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
if CHROME:
    from cdp_browser import Browser  # noqa: E402
    TEXT = "(s) => (document.querySelector(s)?.textContent || '')"
    os.remove(STATE)                       # start on
    server, flog = start()
    b = Browser(CHROME, 19000 + random.randrange(1000))
    try:
        b.goto(f"http://127.0.0.1:{L.WEB}/")
        b.wait("document.querySelector('#conn b')?.textContent === 'Connected'")
        check("page: the Winlink switch reads 'Switch off'", b.wait(f"({TEXT})('#wl-power') === 'Switch off' && 1"), 1)
        b.js("window.confirm = (q) => { window.asked = q; return true; }")
        b.js("document.querySelector('#wl-power').click()")
        check("  off: the state says so",
              b.wait(f"({TEXT})('#wl-state').startsWith('Switched off') && 'off'"), "off")
        check("  it asked first", "Switch Winlink off?" in (b.js("window.asked") or ""), True)
        check("  Connect and Compose are disabled",
              b.js("[document.querySelector('#wl-connect').disabled, document.querySelector('#wl-compose-btn').disabled]"),
              [True, True])
        b.js("window.asked = null")
        b.js("document.querySelector('#wl-power').click()")
        check("  on again: Idle", b.wait(f"({TEXT})('#wl-state') === 'Idle' && 'on'"), "on")
        check("  switching on doesn't ask", b.js("window.asked"), None)
        check("  Connect is enabled", b.js("document.querySelector('#wl-connect').disabled"), False)
    finally:
        b.close()
        L.stop(server)
else:
    print("(no chromium: page checks skipped; set CHROME=)")

# --- the falsifier: no addons/winlink.py. sitecustomize blocks the import, as
# if the file were deleted; the radio server and its lane must run on.
blocker = os.path.join(TMP, "block")
os.makedirs(blocker)
with open(os.path.join(blocker, "sitecustomize.py"), "w") as f:
    f.write('import sys\nsys.modules["addons.winlink"] = None\n')
os.environ["PYTHONPATH"] = blocker + os.pathsep + os.environ.get("PYTHONPATH", "")
server, flog = start_off()                     # no add-on, so no Pat: don't wait for its port
try:
    page = Page()
    check("without addons/winlink.py: the page loads, no Winlink panel",
          page.hello["config"].get("winlink"), False)
    check("  the lane still answers (core): T 1 refused, not armed", L.lane_ptt_refused(), True)
    check("  and the radio is unkeyed", L.ptt(), "0")
    page.close()
finally:
    out = L.stop(server)
del os.environ["PYTHONPATH"]
check("  the server said why", "add-on winlink not loaded" in (out or ""), True)

print(f"\n{'PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
