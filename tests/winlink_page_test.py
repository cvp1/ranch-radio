"""W4: the Winlink panel on the page, driven in headless Chromium.

Runs server.py --sim with tests/fake_pat.py as Pat, opens the page in
Chromium (DevTools protocol, no extra packages) and checks:
  - the panel shows only on a server with Winlink
  - the inbox lists Pat's message; clicking it shows the body
  - compose posts to Pat (and the outbox then lists it)
  - the gateway list fills from Pat and picking a gateway fills the URL
  - Connect needs a second click to confirm; then the session log runs,
    the panel shows the session armed, and it ends with "sent 1, received 1"
  - Stop ends a running session
  - a busy-channel prompt shows on the panel and Abort answers it
  - the codec level is shown when the server sets one

    .venv/bin/python tests/winlink_page_test.py
"""
import asyncio
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPS = 15000 + random.randrange(1000)
LANE = 16000 + random.randrange(1000)
PATP = 17000 + random.randrange(1000)
WEB = 18000 + random.randrange(1000)
CDP = 19000 + random.randrange(1000)
CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:56} got {got!r}" + ("" if ok else f" want {want!r}"))


def fake_log(path):
    try:
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]
    except OSError:
        return []


def start(env_extra=None, *extra, winlink=True):
    logf = tempfile.NamedTemporaryFile(prefix="fakepat-", suffix=".jsonl", delete=False).name
    env = dict(os.environ, FAKE_PAT_RIGCTL=str(LANE), FAKE_PAT_LOG=logf, FAKE_PAT_TX_S="2")
    env.update(env_extra or {})
    wl = ["--winlink-rigctl-port", str(LANE), "--winlink-pat", f"{sys.executable} tests/fake_pat.py",
          "--winlink-pat-port", str(PATP)] if winlink else []
    p = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB),
         "--rigctl-port", str(APPS), *wl, *extra],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    ports = (WEB, LANE, PATP) if winlink else (WEB,)
    for _ in range(100):
        try:
            for port in ports:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            time.sleep(0.5)
            return p, logf
        except OSError:
            time.sleep(0.1)
    p.kill()
    print(p.stdout.read())
    raise SystemExit("server did not start")


def stop(p):
    p.terminate()
    try:
        return p.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired:
        p.kill()
        return p.communicate()[0]


class Browser:
    """Headless Chromium on one page, through the DevTools protocol."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="wl-chrome-")
        self.proc = subprocess.Popen(
            [CHROME, "--headless=new", f"--remote-debugging-port={CDP}", f"--user-data-dir={self.dir}",
             "--no-first-run", "--no-default-browser-check", "--window-size=1500,1400", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json"))
                page = next(t for t in targets if t["type"] == "page")
                break
            except (OSError, StopIteration):
                time.sleep(0.1)
        else:
            raise SystemExit("chromium did not start")
        self.loop = asyncio.new_event_loop()
        self.n = 0
        self.ws = self.loop.run_until_complete(self._open(page["webSocketDebuggerUrl"]))
        self.call("Runtime.enable")
        self.call("Page.enable")

    async def _open(self, url):
        self.sess = aiohttp.ClientSession()
        return await self.sess.ws_connect(url, max_msg_size=0)

    async def _call(self, method, params):
        self.n += 1
        n = self.n
        await self.ws.send_str(json.dumps({"id": n, "method": method, "params": params or {}}))
        while True:
            m = json.loads((await self.ws.receive(timeout=15)).data)
            if m.get("id") == n:
                return m

    def call(self, method, **params):
        return self.loop.run_until_complete(self._call(method, params))

    def js(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        res = r.get("result", {})
        if "exceptionDetails" in res:
            return f"JS error: {res['exceptionDetails'].get('exception', {}).get('description')}"
        return res.get("result", {}).get("value")

    def goto(self, url):
        self.call("Page.navigate", url=url)
        time.sleep(1.5)

    def wait(self, expr, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            v = self.js(expr)
            if v and not (isinstance(v, str) and v.startswith("JS error")):
                return v
            time.sleep(0.15)
        return self.js(expr)

    def screenshot(self, path):
        import base64
        r = self.call("Page.captureScreenshot", format="png", captureBeyondViewport=True)
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["result"]["data"]))

    def close(self):
        try:
            self.loop.run_until_complete(self.sess.close())
        finally:
            self.proc.terminate()
            self.proc.wait(5)
            shutil.rmtree(self.dir, ignore_errors=True)


if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")

TEXT = "(s) => (document.querySelector(s)?.textContent || '')"
VISIBLE = "(s) => { const e = document.querySelector(s); return !!e && !e.closest('[hidden]') && e.offsetParent !== null; }"

b = Browser()
try:
    # --- no Winlink on this server: no panel ---
    server, _ = start(winlink=False)
    try:
        b.goto(f"http://127.0.0.1:{WEB}/")
        b.wait("document.querySelector('#conn')?.textContent.includes('Connected')")
        check("no Winlink: panel hidden", b.js(f"({VISIBLE})('#winlink')"), False)
        check("no Winlink or Mesh: no tab bar", b.js(f"({VISIBLE})('#rr-nav')"), False)
        check("no tab bar: radio panels shown", b.js(f"({VISIBLE})('.vfo')"), True)
    finally:
        stop(server)

    # --- Winlink on, codec level set ---
    idlog = tempfile.mkdtemp(prefix="ardoplog-")
    server, flog = start({"FAKE_PAT_ARDOP_LOG": idlog, "FAKE_PAT_ID": "end"},
                         "--winlink-codec-db", "-38", "--winlink-ardop-log", idlog)
    try:
        b.goto(f"http://127.0.0.1:{WEB}/?v=tabs")
        b.wait("document.querySelector('#conn')?.textContent.includes('Connected')")
        check("tab bar shown", bool(b.wait(f"({VISIBLE})('#rr-nav')")), True)
        check("tabs: radio and Winlink", b.js("[...document.querySelectorAll('#rr-nav .st-tab')].map((a) => a.textContent.trim())"),
              ["IC-7300", "Winlink"])
        check("radio tab first: Winlink panel not shown", b.js(f"({VISIBLE})('#winlink')"), False)
        check("radio tab first: VFO shown", b.js(f"({VISIBLE})('.vfo')"), True)
        b.js("document.querySelector('#rr-nav a[href=\"#/winlink\"]').click()")
        check("panel shown", bool(b.wait(f"({VISIBLE})('#winlink')")), True)
        check("Winlink tab: VFO not shown", b.js(f"({VISIBLE})('.vfo')"), False)
        check("Winlink tab marked on", b.js("document.querySelector('#rr-nav .st-tab.on')?.getAttribute('href')"), "#/winlink")
        check("my call shown", bool(b.wait(f"({TEXT})('#wl-mycall').includes('N9ABC')")), True)
        check("codec level shown (idle 0 dB)", bool(b.wait(f"({TEXT})('#wl-codec').includes('0 dB')")), True)

        # mailbox
        got = b.wait(f"({TEXT})('#wl-list').includes('Test message')")
        check("inbox lists Pat's message", bool(got), True)
        b.js("document.querySelector('#wl-list .wl-msg').click()")
        got = b.wait(f"({TEXT})('#wl-body').includes('Hello from the test.')")
        check("clicking it shows the body", bool(got), True)

        # compose
        b.js("document.querySelector('#wl-compose-btn').click()")
        check("compose form opens", bool(b.wait(f"({VISIBLE})('#wl-compose')")), True)
        b.js("""(() => { document.querySelector('#wl-to').value = 'N0DAJ';
                 document.querySelector('#wl-subject').value = 'From the page';
                 document.querySelector('#wl-text').value = 'Body typed on the page';
                 document.querySelector('#wl-compose').requestSubmit(); })()""")
        time.sleep(1.5)
        comp = [r for r in fake_log(flog) if r["route"] == "compose"]
        check("compose reached Pat", comp and (comp[-1]["fields"].get("to"), comp[-1]["fields"].get("subject"),
                                               comp[-1]["fields"].get("body")),
              ("N0DAJ", "From the page", "Body typed on the page"))
        check("compose form closes", b.js(f"({VISIBLE})('#wl-compose')"), False)
        got = b.wait(f"({TEXT})('#wl-list').includes('From the page')")
        check("outbox shows the new message", bool(got), True)

        # gateways
        b.js("document.querySelector('#wl-gw-refresh').click()")
        got = b.wait(f"({TEXT})('#wl-gateways').includes('N0DAJ')")
        check("gateway list from Pat", bool(got), True)
        q = [r["query"] for r in fake_log(flog) if r["route"] == "rmslist"]
        check("gateway list asked for ARDOP", bool(q) and q[-1].get("mode"), "ardop")
        b.js("document.querySelector('#wl-gateways .wl-gw').click()")
        check("picking a gateway fills the URL", b.js("document.querySelector('#wl-url').value"),
              "ardop:///N0DAJ?bw=2000MAX&freq=3588.5")

        # connect: first click asks, second click connects
        b.js("document.querySelector('#wl-connect').click()")
        time.sleep(0.8)
        check("first Connect click does not connect",
              any(r["route"] == "connect" for r in fake_log(flog)), False)
        check("Connect asks to confirm", "confirm" in b.js(f"({TEXT})('#wl-connect')").lower(), True)
        b.js("document.querySelector('#wl-connect').click()")
        got = b.wait(f"({TEXT})('#wl-log').includes('Connecting to')")
        check("session log shows Pat's lines", bool(got), True)
        check("panel shows armed", bool(b.wait(f"({TEXT})('#wl-state').toLowerCase().includes('armed')")), True)
        check("codec at the data level during the session", bool(b.wait(f"({TEXT})('#wl-codec').includes('-38 dB')")), True)
        check("Stop enabled during the session", b.js("!document.querySelector('#wl-stop').disabled"), True)
        got = b.wait(f"({TEXT})('#wl-state').includes('sent 1, received 1')", 15)
        check("session ends with sent 1, received 1 (the composed message)", bool(got), True)
        check("codec back to 0 dB", bool(b.wait(f"({TEXT})('#wl-codec').includes('0 dB')")), True)
        check("station ID shown as sent after the session",
              bool(b.wait(f"({TEXT})('#wl-id').includes('ID sent')")), True)
        b.screenshot(os.path.join(tempfile.gettempdir(), "winlink-panel.png"))

        # Stop
        before = len([r for r in fake_log(flog) if r["route"] == "disconnect"])
        b.js("document.querySelector('#wl-connect').click()")
        b.js("document.querySelector('#wl-connect').click()")
        b.wait(f"({TEXT})('#wl-state').toLowerCase().includes('armed')")
        time.sleep(0.5)
        b.js("document.querySelector('#wl-stop').click()")
        got = b.wait(f"({TEXT})('#wl-state').toLowerCase().includes('ended')", 10)
        check("Stop ends the session", bool(got), True)
        check("Stop told Pat to disconnect",
              len([r for r in fake_log(flog) if r["route"] == "disconnect"]) > before, True)
    finally:
        stop(server)

    # --- busy channel prompt ---
    server, flog = start({"FAKE_PAT_BUSY": "1"})
    try:
        b.goto(f"http://127.0.0.1:{WEB}/?v=busy#/winlink")
        b.wait("document.querySelector('#conn')?.textContent.includes('Connected')")
        check("without ardopcf's log the ID is shown as not tracked",
              bool(b.wait(f"({TEXT})('#wl-id').includes('not tracked')")), True)
        b.js("document.querySelector('#wl-url').value = 'ardop:///N0DAJ?bw=2000MAX&freq=3588.5'")
        b.js("document.querySelector('#wl-connect').click()")
        b.js("document.querySelector('#wl-connect').click()")
        got = b.wait(f"({VISIBLE})('#wl-prompt') && ({TEXT})('#wl-prompt').includes('clear channel')")
        check("busy-channel prompt shown", bool(got), True)
        b.js("document.querySelector('#wl-prompt [data-v=abort]').click()")
        time.sleep(1.5)
        ans = [r for r in fake_log(flog) if r["route"] == "prompt_answer"]
        check("Abort answers Pat", ans and ans[-1]["value"], "abort")
        check("prompt hidden after answer", b.js(f"({VISIBLE})('#wl-prompt')"), False)
        check("never keyed", any(r["route"] == "ptt_on" for r in fake_log(flog)), False)
    finally:
        stop(server)
finally:
    b.close()

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
