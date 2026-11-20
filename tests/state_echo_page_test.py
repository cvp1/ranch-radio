"""The radio page's echo window (static/app.js, 2026-11-20), in headless
Chromium against one simulated radio. Nothing transmits.

For ECHO_MS after this page changes a setting, a server state that repeats a
value this page had or sent is a stale echo of its own edit and is dropped;
any other value is another client's change and is applied. Before this, the
window dropped every unconfirmed state, so a second page's retune inside it
left this page showing the old frequency (the server sends diffs only, so it
stayed stale until the next change).

  rules, states fed straight to the page's socket handler:
    1. our pre-edit value inside the window: dropped (the echo the window is for)
    2. a value this page never held, inside the window: applied
    3. two quick edits (a wheel): the first edit's value arriving late is dropped
    4. after the window: our old value is applied
    5. a confirmed state (the server refused us): applied inside the window
  end to end:
    6. page A edits, a second client retunes 1 ms later: page A shows the
       second client's frequency within the window, and a fresh socket reads
       the same frequency back in Hz from the radio's own server

    python3 tests/state_echo_page_test.py
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

os.environ["RANCH_RADIO_HOSTS"] = ""     # never this machine's own host files
os.environ["RANCH_RADIO_OFFLINE"] = "1"
os.environ.pop("CC_OBS_TOKENS_FILE", None)

import aiohttp  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from cdp_browser import Browser  # noqa: E402
import testkit  # noqa: E402

CHROME = (os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
          or next((p for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                               "/Applications/Chromium.app/Contents/MacOS/Chromium") if os.path.exists(p)), None))
if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")
TMP = tempfile.mkdtemp(prefix="state-echo-")
WEB, RIGCTL, LANE, CDP = (testkit.free_port() for _ in range(4))
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


def dial():
    """A fresh socket's hello from the radio's own server: the frequency in Hz."""
    async def go():
        async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{WEB}/ws") as ws:
            while True:
                m = await ws.receive(timeout=5)
                if m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data).get("t") == "hello":
                    return json.loads(m.data)["state"].get("freq")
    return asyncio.run(go())


class Other:
    """A second client on the radio's own server, on its own thread and loop."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.hello = False
        self._run(self._open())
        assert testkit.wait_for(lambda: self.hello, 10), "no hello"

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(15)

    async def _open(self):
        self.s = aiohttp.ClientSession()
        self.ws = await self.s.ws_connect(f"http://127.0.0.1:{WEB}/ws")
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for m in self.ws:
            if m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data).get("t") == "hello":
                self.hello = True

    def send(self, **d):
        self._run(self.ws.send_str(json.dumps(d)))

    def close(self):
        async def go():
            await self.ws.close()
            await self.s.close()
        self._run(go())
        self.loop.call_soon_threadsafe(self.loop.stop)


# A state message handed to the page's own socket handler, as if the server sent it.
FEED = "((d, confirmed) => ws.onmessage({data: JSON.stringify({t: 'state', d, confirmed})}))"
# Wait out the window from the page's own last edit of freq.
QUIET = "(() => !localEdit.freq || performance.now() - localEdit.freq.t > ECHO_MS + 50)()"

radio = subprocess.Popen(
    [sys.executable, "server.py", "--sim", "--no-audio", "--host", "127.0.0.1", "--listen", str(WEB),
     "--rigctl-host", "127.0.0.1", "--rigctl-port", str(RIGCTL), "--winlink-rigctl-port", str(LANE),
     "--addons-state", ""],
    cwd=ROOT, stdout=open(os.path.join(TMP, "radio.log"), "w"), stderr=subprocess.STDOUT)
b = other = None
try:
    assert testkit.wait_for(lambda: answers(WEB), 60, 0.1), "the simulated radio did not start"
    b = Browser(CHROME, CDP)
    b.goto(f"http://127.0.0.1:{WEB}/")
    b.wait("document.querySelector('#conn b')?.textContent === 'Connected' && typeof S.freq === 'number'", 20)
    check("the window is 700 ms", b.js("ECHO_MS"), 700)

    print("\n# rules, states fed to the page's handler", flush=True)
    b.js("setRadio('freq', 14074000)")
    b.wait(QUIET, 5)
    check("1. our pre-edit value inside the window is dropped",
          b.js(f"setRadio('freq', 14075000); {FEED}({{freq: 14074000}}); S.freq"), 14075000)
    check("2. a value this page never held, inside the window, is applied",
          b.js(f"{FEED}({{freq: 14076500}}); S.freq"), 14076500)
    b.wait(QUIET, 5)
    check("3. a wheel: the first edit's value arriving late is dropped",
          b.js(f"setRadio('freq', 14077000); setRadio('freq', 14078000); {FEED}({{freq: 14077000}}); S.freq"), 14078000)
    b.wait(QUIET, 5)
    check("4. after the window our old value is applied", b.js(f"{FEED}({{freq: 14077000}}); S.freq"), 14077000)
    # read in the same call: the server's own echo of the accepted edit follows
    check("5. a confirmed state is applied inside the window",
          b.js(f"setRadio('freq', 14079000); {FEED}({{freq: 14077000}}, true); S.freq"), 14077000)

    print("\n# end to end: a second client retunes inside page A's window", flush=True)
    b.wait(QUIET, 5)
    b.js("setRadio('freq', 14070000)")
    b.wait(QUIET, 5)
    check("  page A and the radio agree before (fresh socket)", (b.js("S.freq"), dial()), (14070000, 14070000))
    other = Other()
    b.js("window.__seen = null; window.__edit = performance.now(); setRadio('freq', 14071000); "
         "window.__watch = setInterval(() => { if (S.freq === 14072500 && window.__seen === null) "
         "window.__seen = performance.now(); }, 5)")
    other.send(t="set", k="freq", v=14072500)
    b.wait("window.__seen !== null || performance.now() - window.__edit > 3000", 10)
    seen = b.js("window.__seen === null ? null : Math.round(window.__seen - window.__edit)")
    check("6. page A shows the second client's frequency", seen is not None, True)
    check("   ...inside the window, not after it", seen is not None and seen < 700, True)
    print(f"  measured: {seen} ms after page A's own edit", flush=True)
    check("   a fresh socket reads it back in Hz", dial(), 14072500)
    check("   the page's dial says it too", b.js("document.querySelector('#freq').textContent.replace(/\\D/g, '')"),
          "14072500")
finally:
    if other:
        other.close()
    radio.terminate()
    try:
        radio.wait(10)
    except subprocess.TimeoutExpired:
        radio.kill()
    if b:
        b.close()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'FAIL' if fails else 'PASS'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
