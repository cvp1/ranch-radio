"""W4: the Winlink panel on the page, driven in headless Chromium.

Runs server.py --sim with tests/fake_pat.py as Pat, opens the page in
Chromium (DevTools protocol, no extra packages) and checks:
  - the panel shows only on a server with Winlink
  - the inbox lists Pat's message; clicking it shows the body
  - compose posts to Pat (and the outbox then lists it)
  - mailbox actions (own servers, rows picked by MID): opening an unread
    message marks it read at Pat (the bold goes only after Pat's 200; a
    failure keeps it and says so); Reply / Reply all / Forward fill the form
    Pat's way (quote, Re:/Fw:, Cc, the attachment re-attached, a note when
    it can't be read); Archive moves it; Delete needs a second click bound
    to the same message, and the ask expires; a stale open is dropped; while
    a session runs the outbox can't be changed (page and server)
  - Via lists this radio (ARDOP) and telnet; telnet fills the URL
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
import hashlib
import time
import urllib.request

import aiohttp
import testkit  # noqa: E402

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


def recs(path, route, **match):
    return [r for r in fake_log(path) if r.get("route") == route
            and all(r.get(k) == v for k, v in match.items())]


def wait_for(pred, timeout=10):
    """Poll pred until it holds (a ceiling, not a guess); its last value."""
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.1)
    return pred()


def api_get(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{WEB}{path}", timeout=5) as r:
            return json.load(r)
    except (OSError, ValueError) as e:
        return f"error: {e}"


def start(env_extra=None, *extra, winlink=True):
    logf = tempfile.NamedTemporaryFile(prefix="fakepat-", suffix=".jsonl", delete=False).name
    env = dict(os.environ, FAKE_PAT_RIGCTL=str(LANE), FAKE_PAT_LOG=logf, FAKE_PAT_TX_S="2")
    env.update(env_extra or {})
    wl = ["--winlink-rigctl-port", str(LANE), "--winlink-pat", testkit.fake_pat_cmd(),
          "--winlink-pat-port", str(PATP)] if winlink else []
    # Its own add-on state: P13 switches Winlink off, which must never land in
    # (or depend on) the user's ~/.local/state one.
    state = os.path.join(tempfile.mkdtemp(prefix="wlpage-"), "addons.json")
    p = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB),
         "--rigctl-port", str(APPS), "--addons-state", state, *wl, *extra],
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
        b.js("document.querySelector('#wl-list .wl-msg[data-mid=\"ABC123\"]').click()")
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

        # via: this radio for ARDOP and telnet (no Packet add-on on this server)
        vias = b.wait("[...document.querySelectorAll('#wl-via option')].map((o) => o.textContent).length === 2 && "
                      "[...document.querySelectorAll('#wl-via option')].map((o) => o.textContent)")
        check("Via lists this radio (ARDOP) and telnet",
              isinstance(vias, list) and [v.startswith("HF · ") and v.endswith(" (ARDOP)") for v in vias[:1]] + vias[1:],
              [True, "Telnet (internet, no radio)"])
        b.js("(() => { const s = document.querySelector('#wl-via'); s.value = 'telnet'; s.dispatchEvent(new Event('change')); })()")
        check("  telnet fills the URL and hides the gateways",
              (b.js("document.querySelector('#wl-url').value.startsWith('telnet://')"), b.js("document.querySelector('#wl-gateways').hidden")),
              (True, True))
        b.js("(() => { const s = document.querySelector('#wl-via'); s.value = 'radio'; s.dispatchEvent(new Event('change')); })()")
        check("  back to the radio: gateways shown", b.js("document.querySelector('#wl-gateways').hidden"), False)

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

    # --- mailbox actions ---
    SUBJ = {"ABC123": "Test message", "DEF456": "Re: Already a reply", "GHI789": "RE: Upper case"}

    def row(mid):
        return f"document.querySelector('#wl-list .wl-msg[data-mid=\"{mid}\"]')"

    def open_panel(tag):
        b.goto(f"http://127.0.0.1:{WEB}/?v={tag}#/winlink")
        b.wait("document.querySelector('#conn')?.textContent.includes('Connected')")
        b.js("window.__errs = []; window.addEventListener('error', (e) => window.__errs.push(e.message))")
        b.wait(f"({TEXT})('#wl-list').includes('Test message')")

    def show_box(v):
        b.js(f"document.querySelector('#wl-boxes [data-v=\"{v}\"]').click()")

    def open_msg(mid, subject=None, box="in"):
        """Click the row for mid (in that box) and wait for its read pane."""
        if b.js("document.querySelector('#wl-boxes .chip.on')?.dataset.v") != box:
            show_box(box)
        b.wait(f"!!{row(mid)} && document.querySelector('#wl-boxes .chip.on')?.dataset.v === '{box}'")
        b.js(f"{row(mid)}.click()")
        subject = subject or SUBJ[mid]
        return bool(b.wait(f"({VISIBLE})('#wl-read') && "
                           f"document.querySelector('#wl-read-head b')?.textContent === {json.dumps(subject)}"))

    def click(sel):
        b.js(f"document.querySelector('{sel}').click()")

    def val(sel):
        return b.js(f"document.querySelector('{sel}')?.value")

    def submit_and_wait(flog):
        n = len(recs(flog, "compose"))
        b.js("document.querySelector('#wl-compose').requestSubmit()")
        got = wait_for(lambda: len(recs(flog, "compose")) > n)
        return recs(flog, "compose")[-1] if got else None

    server, flog = start()
    try:
        open_panel("mail")
        # P1: opening an unread message marks it read at Pat
        check("P1 open ABC123", open_msg("ABC123"), True)
        got = wait_for(lambda: recs(flog, "read", mid="ABC123"), 5)
        check("P1   Pat got {read: true} for ABC123", [r["read"] for r in got or []], [True])
        check("P1   then the row is no longer bold", bool(b.wait(f"!{row('ABC123')}.classList.contains('unread')")), True)
        inbox = api_get("/winlink/api/mailbox/in")
        check("P1   Pat's inbox says ABC123 is read",
              [m["Unread"] for m in inbox if m["MID"] == "ABC123"] if isinstance(inbox, list) else inbox, [False])
        # P2: the action bar
        check("P2 the five action chips are shown",
              [b.js(f"({VISIBLE})('#{i}')") for i in ("wl-reply", "wl-reply-all", "wl-forward", "wl-archive", "wl-delete")],
              [True] * 5)
        check("P2   and enabled", b.js("[...document.querySelectorAll('#wl-actions button')].map((e) => e.disabled)"),
              [False] * 5)
        # P3: Reply
        click("#wl-reply")
        check("P3 Reply opens the form", bool(b.wait(f"({VISIBLE})('#wl-compose')")), True)
        check("P3   To = the sender, Re: subject", (val("#wl-to"), val("#wl-subject")), ("N0DAJ", "Re: Test message"))
        text = val("#wl-text") or ""
        check("P3   body: two blank lines, then the quote",
              (text.startswith("\n\n--- "), ">Hello from the test." in text, "\r" in text), (True, True, False))
        comp = submit_and_wait(flog)
        body = (comp or {}).get("fields", {}).get("body", "")
        check("P3   posted body carries the quote in CRLF, never \\r\\r",
              (">Hello from the test.\r\n" in body, "\r\r" in body), (True, False))
        # P4: Reply all, Cc null and Cc set
        check("P4 open ABC123 again", open_msg("ABC123"), True)
        click("#wl-reply-all")
        b.wait(f"({VISIBLE})('#wl-compose')")
        check("P4   Reply all on Cc: null -> Cc empty, no JS error", (val("#wl-cc"), b.js("window.__errs")), ("", []))
        check("P4 open DEF456", open_msg("DEF456"), True)
        click("#wl-reply-all")
        b.wait(f"({VISIBLE})('#wl-compose')")
        check("P4   Reply all: Cc without my call or the sender, subject kept",
              (val("#wl-cc"), val("#wl-to"), val("#wl-subject")), ("K7ABC", "N0DAJ", "Re: Already a reply"))
        comp = submit_and_wait(flog)
        check("P4   Pat got cc=K7ABC", (comp or {}).get("fields", {}).get("cc"), "K7ABC")
        # P4b: RE: in upper case is a reply already
        check("P4b open GHI789", open_msg("GHI789"), True)
        click("#wl-reply")
        b.wait(f"({VISIBLE})('#wl-compose')")
        check("P4b   Reply keeps 'RE: Upper case'", val("#wl-subject"), "RE: Upper case")
        # P5: Forward re-attaches the file
        check("P5 open DEF456", open_msg("DEF456"), True)
        click("#wl-forward")
        got = b.wait(f"({VISIBLE})('#wl-compose') && document.querySelector('#wl-files').files.length === 1 && "
                     "document.querySelector('#wl-files').files[0].name")
        check("P5   the attachment re-attached", got, "note.txt")
        check("P5   To empty, Fw: subject", (val("#wl-to"), val("#wl-subject")), ("", "Fw: Re: Already a reply"))
        b.js("document.querySelector('#wl-to').value = 'N0DAJ'")
        comp = submit_and_wait(flog) or {}
        check("P5   Pat got the file (name, size, type, sha256) and To",
              (comp.get("files"), comp.get("fields", {}).get("to")),
              ([{"name": "note.txt", "size": 5, "type": "text/plain", "sha256": hashlib.sha256(b"hello").hexdigest()}],
               "N0DAJ"))
        # P9: the Delete ask expires
        check("P9 open GHI789", open_msg("GHI789"), True)
        click("#wl-delete")
        check("P9   first click asks", "Confirm" in (b.js(f"({TEXT})('#wl-delete')") or ""), True)
        check("P9   the ask expires", bool(b.wait(f"({TEXT})('#wl-delete') === 'Delete'", 10)), True)
        check("P9   nothing deleted", recs(flog, "delete"), [])
        # P9b: the ask is bound to the message
        click("#wl-delete")
        check("P9b first click on GHI789 asks", "Confirm" in (b.js(f"({TEXT})('#wl-delete')") or ""), True)
        check("P9b open DEF456", open_msg("DEF456"), True)
        click("#wl-delete")
        check("P9b   a click on DEF456 asks again", "Confirm" in (b.js(f"({TEXT})('#wl-delete')") or ""), True)
        time.sleep(0.5)                                   # quiet window: nothing may happen
        check("P9b   nothing deleted", recs(flog, "delete"), [])
        click("#wl-reply")                                # any other action resets the ask
        check("P9b   another action resets it", b.js(f"({TEXT})('#wl-delete')"), "Delete")
        # P7: Archive (destructive from here on)
        check("P7 open DEF456", open_msg("DEF456"), True)
        click("#wl-archive")
        got = wait_for(lambda: recs(flog, "move"))
        check("P7   Pat moved it in -> archive", [(r["src"], r["mid"], r["dest"]) for r in got or []],
              [("in", "DEF456", "archive")])
        check("P7   pane hidden, row gone",
              bool(b.wait(f"!({VISIBLE})('#wl-read') && !{row('DEF456')} && ({TEXT})('#wl-list').includes('Test message')")), True)
        check("P7   listed in the Archive box", open_msg("DEF456", box="archive"), True)
        check("P7   no Archive chip in the Archive box", b.js(f"({VISIBLE})('#wl-archive')"), False)
        # P8: Delete, two clicks
        check("P8 open ABC123", open_msg("ABC123"), True)
        click("#wl-delete")
        check("P8   first click asks", "Confirm" in (b.js(f"({TEXT})('#wl-delete')") or ""), True)
        time.sleep(0.5)                                   # quiet window: nothing may happen
        check("P8   nothing deleted yet", recs(flog, "delete"), [])
        click("#wl-delete")
        got = wait_for(lambda: recs(flog, "delete"))
        check("P8   second click deletes it at Pat", [(r["box"], r["mid"]) for r in got or []], [("in", "ABC123")])
        check("P8   the row is gone", bool(b.wait(f"!{row('ABC123')} && ({TEXT})('#wl-list').includes('Upper case')")), True)
        check("no JS errors in the mailbox cases", b.js("window.__errs"), [])
    finally:
        stop(server)

    # P6: an attachment that can't be read
    server, flog = start({"FAKE_PAT_BROKEN_ATTACH": "1"})
    try:
        open_panel("p6")
        check("P6 open DEF456", open_msg("DEF456"), True)
        click("#wl-forward")
        check("P6   a toast says it could not be read",
              bool(b.wait("document.querySelector('#toasts').textContent.includes('could not be read')")), True)
        check("P6   and the form keeps the note",
              (b.js(f"({VISIBLE})('#wl-compose-note')"), "note.txt could not be read" in (b.js(f"({TEXT})('#wl-compose-note')") or "")),
              (True, True))
        check("P6   the form is open, no files", (b.js(f"({VISIBLE})('#wl-compose')"),
                                                  b.js("document.querySelector('#wl-files').files.length")), (True, 0))
    finally:
        stop(server)

    # P12: Pat refuses the mark-read
    server, flog = start({"FAKE_PAT_BROKEN_READ": "1"})
    try:
        open_panel("p12")
        check("P12 open ABC123", open_msg("ABC123"), True)
        check("P12   a toast says it could not be marked read",
              bool(b.wait("document.querySelector('#toasts').textContent.includes('Could not mark it read')")), True)
        check("P12   the row stays bold", b.js(f"{row('ABC123')}.classList.contains('unread')"), True)
    finally:
        stop(server)

    # P14: a slow open answered after the operator moved on is dropped
    server, flog = start({"FAKE_PAT_SLOW_MID": "ABC123"})
    try:
        open_panel("p14")
        b.js(f"{row('ABC123')}.click()")
        show_box("sent")
        time.sleep(3)                                     # past the 2 s answer: nothing may happen
        check("P14 the late answer is dropped (pane hidden, nothing open)",
              (b.js(f"({VISIBLE})('#wl-read')"), b.js("winlink._current()")), (False, None))
        check("P14   no Delete armed", b.js(f"({TEXT})('#wl-delete')"), "Delete")
        check("P14   opening it again works", open_msg("ABC123"), True)
    finally:
        stop(server)

    # P10, P13: the outbox during a session
    server, flog = start({"FAKE_PAT_TX_S": "30"})
    try:
        open_panel("p10")
        click("#wl-compose-btn")
        b.js("""(() => { document.querySelector('#wl-to').value = 'N0DAJ';
                 document.querySelector('#wl-subject').value = 'Queued';
                 document.querySelector('#wl-compose').requestSubmit(); })()""")
        check("P10 one message in the outbox", bool(b.wait(f"!!{row('OUT0')}")), True)
        b.js("document.querySelector('#wl-url').value = 'ardop:///N0DAJ?bw=2000MAX&freq=3588.5'")
        click("#wl-connect")
        click("#wl-connect")
        check("P10 session armed", bool(b.wait(f"({TEXT})('#wl-state').toLowerCase().includes('armed')")), True)
        check("P10 open the outbox message", open_msg("OUT0", "Queued", box="out"), True)
        check("P10   Delete disabled, Archive hidden",
              (b.js("document.querySelector('#wl-delete').disabled"), b.js(f"({VISIBLE})('#wl-archive')")), (True, False))
        check("P10   the title says why", "session" in (b.js("document.querySelector('#wl-delete').title") or ""), True)
        check("P10   Reply still enabled", b.js("document.querySelector('#wl-reply').disabled"), False)
        got = b.js("winlink._api(BASE + 'winlink/api/mailbox/out/OUT0', { method: 'DELETE' })")
        check("P13 the server's 409 sentence is what the page shows", got,
              "A Winlink session is running; the outbox can't be changed until it ends.")
        click("#wl-stop")
        check("P10 the session ends", bool(b.wait(f"({TEXT})('#wl-state').includes('ended')", 20)), True)
        check("P10   open it again", open_msg("OUT0", "Queued", box="out"), True)
        check("P10   Delete enabled after the session", b.js("document.querySelector('#wl-delete').disabled"), False)
        b.js("window.confirm = () => true")
        click("#wl-power")
        check("P13 switched off", bool(b.wait(f"({TEXT})('#wl-state').startsWith('Switched off')")), True)
        check("P13   the page shows the 503's error text",
              b.js("winlink._api(BASE + 'winlink/api/mailbox/in')"), "Winlink is switched off")
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

    # --- P2P (plan winlink-p2p): P2P-only compose, its badge, peer URLs, answering ---
    def compose_page(to, subject, p2p):
        b.js("document.querySelector('#wl-compose-btn').click()")
        b.wait(f"({VISIBLE})('#wl-compose')")
        b.js(f"""(() => {{ document.querySelector('#wl-to').value = {json.dumps(to)};
                 document.querySelector('#wl-subject').value = {json.dumps(subject)};
                 document.querySelector('#wl-text').value = 'b';
                 const box = document.querySelector('#wl-p2ponly'); if (box) box.checked = {json.dumps(p2p)};
                 document.querySelector('#wl-compose').requestSubmit(); }})()""")
        return wait_for(lambda: [r for r in recs(flog, "compose") if r["fields"].get("subject") == subject], 10)

    def set_via(v):
        b.js(f"(() => {{ const s = document.querySelector('#wl-via'); s.value = {json.dumps(v)};"
             " s.dispatchEvent(new Event('change')); })()")

    def type_in(sel, text):
        b.js(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); e.value = {json.dumps(text)};"
             " e.dispatchEvent(new Event('input')); })()")

    server, flog = start({"FAKE_PAT_TX_S": "0.5", "FAKE_PAT_P2P": "N0XYZ", "FAKE_PAT_NO_PTT": "1"})
    try:
        b.goto(f"http://127.0.0.1:{WEB}/?v=p2p#/winlink")
        b.wait("document.querySelector('#conn')?.textContent.includes('Connected')")
        b.js("window.__errs = []; window.addEventListener('error', (e) => window.__errs.push(e.message))")
        b.wait(f"({TEXT})('#wl-mycall').includes('N9ABC')")
        # A-1: the box ticked sends p2ponly=true; unticked, no field
        got = compose_page("N0XYZ", "P2P from the page", True)
        check("A-1 ticked: Pat got p2ponly=true", got and got[-1]["fields"].get("p2ponly"), "true")
        got = compose_page("N0DAJ", "Gateway from the page", False)
        check("A-1 unticked: no p2ponly field", bool(got) and "p2ponly" in got[-1]["fields"], False)
        # A-2: the P2P badge on P2POnly rows only, and in the reader
        b.js("document.querySelector('#wl-boxes [data-v=out]').click()")
        b.wait(f"({TEXT})('#wl-list').includes('Gateway from the page')")
        rows = b.js("[...document.querySelectorAll('#wl-list .wl-msg')].map((r) => "
                    "[r.querySelector('.wl-subj').textContent.replace(/^P2P/, ''), !!r.querySelector('.wl-badge')])"
                    ".sort()")
        check("A-2 P2P badge only on the P2P-only message", rows,
              [["Gateway from the page", False], ["P2P from the page", True]])
        b.js("[...document.querySelectorAll('#wl-list .wl-msg')].find((r) => r.textContent.includes('P2P from')).click()")
        check("A-2 the reader shows it P2P", bool(b.wait("!!document.querySelector('#wl-read-head .wl-badge')")), True)
        b.js("document.querySelector('#wl-reply').click()")
        check("A-1 reply to a P2P-only message preselects P2P only",
              b.wait("document.querySelector('#wl-p2ponly')?.checked === true", 5), True)
        b.js("document.querySelector('#wl-compose-cancel').click()")
        b.js("document.querySelector('#wl-boxes [data-v=in]').click()")
        b.wait(f"({TEXT})('#wl-list').includes('Test message')")
        b.js("document.querySelector('#wl-list .wl-msg[data-mid=\"ABC123\"]').click()")
        b.wait(f"({TEXT})('#wl-body').includes('Hello from the test.')")
        b.js("document.querySelector('#wl-reply').click()")
        check("A-1 reply to an ordinary message: not P2P only",
              b.wait("document.querySelector('#wl-p2ponly')?.checked === false", 5), True)
        b.js("document.querySelector('#wl-compose-cancel').click()")
        # A-3: peer URLs per via
        b.js("document.querySelector('#wl-mode [data-v=peer]').click()")
        check("A-3 peer mode: the callsign field shows, the gateway list doesn't",
              (b.js(f"({VISIBLE})('#wl-peer-call')"), b.js(f"({VISIBLE})('#wl-gateways')")), (True, False))
        set_via("radio")
        type_in("#wl-peer-call", "n0xyz")
        type_in("#wl-peer-freq", "7101.5")
        check("A-3 HF (ARDOP): ardop:///CALL?freq=", b.js("document.querySelector('#wl-url').value"),
              "ardop:///N0XYZ?freq=7101.5")
        check("A-3   one modem: no modem picker", b.js(f"({VISIBLE})('#wl-peer-modem')"), False)
        b.js("winlink.setPacket({hub_name: 'UV-PRO', up: true})")      # as addons/packet.js does
        set_via("packet")
        type_in("#wl-peer-freq", "145010")
        check("A-3 packet hub: ax25+agwpe:///CALL?freq=", b.js("document.querySelector('#wl-url').value"),
              "ax25+agwpe:///N0XYZ?freq=145010")
        set_via("telnet")
        check("A-3 telnet: host and password fields, no frequency",
              (b.js(f"({VISIBLE})('#wl-peer-host')"), b.js(f"({VISIBLE})('#wl-peer-freq')")), (True, False))
        type_in("#wl-peer-host", "127.0.0.1:8774")
        type_in("#wl-peer-pass", "pw")
        check("A-3 telnet: telnet://MYCALL:***@host:port/CALL shown, no freq",
              b.js("document.querySelector('#wl-url').value"), "telnet://N9ABC:***@127.0.0.1:8774/N0XYZ")
        b.js("document.querySelector('#wl-connect').click()")
        check("A-3 confirm names the peer", bool(b.wait(f"({TEXT})('#wl-connect').includes('peer N0XYZ')", 3)), True)
        b.js("document.querySelector('#wl-connect').click()")
        got = wait_for(lambda: recs(flog, "connect"), 10)
        check("A-3 telnet: Pat dials telnet://N9ABC:pw@127.0.0.1:8774/N0XYZ",
              got and got[-1]["url"], "telnet://N9ABC:pw@127.0.0.1:8774/N0XYZ")
        got = b.wait(f"({TEXT})('#wl-state').includes('Session ended')", 15)
        check("A-6 peer session ends: N sent, M left in the outbox, no warning",
              (b.js(f"({TEXT})('#wl-state')"), b.js("document.querySelector('#wl-state').classList.contains('warn')")),
              ("Session ended · sent 1, received 1 · 1 left in the outbox", False))
        # A-7: a station calls in
        b.js("document.querySelector('#wl-boxes [data-v=out]').click()")
        b.wait(f"({TEXT})('#wl-list').includes('Gateway from the page')")
        b.js("[...document.querySelectorAll('#wl-list .wl-msg')][0].click()")
        b.wait(f"({VISIBLE})('#wl-read')")
        urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{PATP}/fake/inbound", data=b'{"remote": "AX.25:N0XYZ"}', method="POST",
            headers={"Content-Type": "application/json"}), timeout=5).read()
        check("A-7 the page shows Answering N0XYZ", bool(b.wait(f"({TEXT})('#wl-state').includes('Answering N0XYZ')")),
              True)
        check("A-7   Connect and outbox Delete disabled",
              (b.js("document.querySelector('#wl-connect').disabled"), b.js("document.querySelector('#wl-delete').disabled")),
              (True, True))
        urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{PATP}/fake/inbound/end", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"}), timeout=5).read()
        check("A-7 after: Idle, Delete enabled again",
              (bool(b.wait(f"({TEXT})('#wl-state') === 'Idle'")), b.wait("!document.querySelector('#wl-delete').disabled")),
              (True, True))
        check("no JS errors in the P2P cases", b.js("window.__errs"), [])
    finally:
        stop(server)

    # --- A-11 on the page: Pat's config would listen, so Pat is not launched ---
    logf = tempfile.NamedTemporaryFile(prefix="fakepat-", suffix=".jsonl", delete=False).name
    state = os.path.join(tempfile.mkdtemp(prefix="wlpage-"), "addons.json")
    server = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB), "--rigctl-port", str(APPS),
         "--addons-state", state, "--winlink-rigctl-port", str(LANE),
         "--winlink-pat", testkit.fake_pat_cmd(listen=["telnet"]), "--winlink-pat-port", str(PATP)],
        cwd=ROOT, env=dict(os.environ, FAKE_PAT_LOG=logf), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        wait_for(lambda: not isinstance(api_get("/winlink/api/status"), str), 20)
        b.goto(f"http://127.0.0.1:{WEB}/?v=blocked#/winlink")
        b.wait("document.querySelector('#conn')?.textContent.includes('Connected')")
        check("A-11 the page says Winlink is off and why",
              bool(b.wait(f"({TEXT})('#wl-state').startsWith('Off') && ({TEXT})('#wl-state').includes('listen')")), True)
        check("A-11   Connect disabled", b.js("document.querySelector('#wl-connect').disabled"), True)
        check("A-11   Pat never started", recs(logf, "start"), [])
    finally:
        stop(server)
finally:
    b.close()

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
