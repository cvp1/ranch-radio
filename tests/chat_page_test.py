"""The chat view (static/chat.js) on the Mesh tab, in headless Chromium,
against tests/fake_mesh_bridge.py:

  - the rail: a channel's line shows its last message and who sent it
  - Enter sends; Shift+Enter is a new line in the box, sent as one message
  - the emoji button opens the picker; a pick lands in the box at the caret
    and goes to Recent
  - a reply: the bar says so, Escape cancels it, so nothing goes as a reply
  - a message of only emoji shows big; text with one does not
  - one sender's run of messages shares one name line; a day line on top
  - a quote is a button: clicking it brings the original into view, flashed
  - the action bar's + opens the picker; a pick there sends a reaction
  - scrolled up, a new message doesn't move the reader: "↓ 1 new" shows,
    and clicking it goes to the bottom
  - drawn while its tab is hidden, a conversation still opens on its newest
    message (live 2026-11-19: the Mesh tab opened on the oldest)

    python3 tests/chat_page_test.py
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
from fake_mesh_bridge import FAKE_KEY  # noqa: E402
import testkit  # noqa: E402

CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")
WEB, CDP = testkit.free_port(), testkit.free_port()
TMP = tempfile.mkdtemp(prefix="chatpage-")
CMD_LOG = os.path.join(TMP, "commands.jsonl")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


def commands():
    try:
        with open(CMD_LOG) as f:
            return [json.loads(line) for line in f if line.strip()]
    except OSError:
        return []


def key(b, k, shift=False, text=None):
    mods = 8 if shift else 0
    code = {"Enter": 13, "Escape": 27}[k]
    for t in ("keyDown", "keyUp"):
        b.call("Input.dispatchKeyEvent", type=t, key=k, code=k, windowsVirtualKeyCode=code, modifiers=mods,
               **({"text": text} if text and t == "keyDown" else {}))


key_file = os.path.join(TMP, "key")
with open(key_file, "w") as f:
    f.write(FAKE_KEY)
adds = os.path.join(TMP, "addons.json")
with open(adds, "w") as f:
    json.dump({"mesh": {"port": "/dev/null", "python": sys.executable, "bridge": "tests/fake_mesh_bridge.py",
                        "keys": [f"HomeNet={key_file}"], "log": ""}}, f)
server = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB), "--rigctl-port", "0",
                           "--addons-state", adds, "--addons", adds], cwd=ROOT,
                          env=dict(os.environ, FAKE_MESH_LOG=CMD_LOG, MESH_ACK_WAIT_S="1.5"),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", WEB), timeout=0.2).close()
        break
    except OSError:
        time.sleep(0.1)
else:
    server.kill()
    raise SystemExit("server did not start")

b = Browser(CHROME, CDP, size="1500,950")
LIST = "#mesh-list"
BODIES = f"[...document.querySelectorAll('{LIST} .mesh-body')].map((e) => e.textContent)"
try:
    b.call("Page.addScriptToEvaluateOnNewDocument", source="window.__errs = []; addEventListener('error', (e) => __errs.push(String(e.message)));")
    time.sleep(1)                                   # the fake node's first messages
    b.goto(f"http://127.0.0.1:{WEB}/?v=mesh#/mesh")
    b.wait("document.querySelectorAll('#mesh-tabs .chat-conv').length === 2")
    b.js("document.querySelector('#mesh-tabs [data-conv=c1]').click()")
    b.wait(f"{BODIES}.includes('gate is closed')")
    check("the rail: HomeNet's last message and its sender",
          b.js("document.querySelector('#mesh-tabs [data-conv=c1] .chat-conv-last').textContent"), "RDG: thanks, I saw it")
    check("a day line on top", b.js(f"document.querySelector('{LIST} > :first-child').className"), "chat-day")
    check("  it says Today", b.js(f"document.querySelector('{LIST} .chat-day').textContent"), "Today")

    # Enter, Shift+Enter
    b.js("document.querySelector('#mesh-text').focus()")
    b.call("Input.insertText", text="line one")
    key(b, "Enter", shift=True, text="\r")
    b.call("Input.insertText", text="line two")
    check("Shift+Enter: a new line in the box", b.js("document.querySelector('#mesh-text').value"), "line one\nline two")
    check("  the box grew", b.js("document.querySelector('#mesh-text').offsetHeight") > 36, True)
    key(b, "Enter", text="\r")
    b.wait(f"{BODIES}.includes('line one\\nline two')", 10)
    sent = [c for c in commands() if c.get("text") == "line one\nline two"]
    check("Enter sends it, one message on HomeNet", sent and (len(sent), sent[0]["ch"]), (1, 1))
    check("  the box is empty and short again", (b.js("document.querySelector('#mesh-text').value"),
                                               b.js("document.querySelector('#mesh-text').offsetHeight") <= 36), ("", True))

    # The emoji picker
    b.call("Input.insertText", text="good ")
    b.js("document.querySelector('#mesh-emoji').click()")
    check("the emoji button opens the picker", b.js("!!document.querySelector('.chat-picker')"), True)
    b.js("document.querySelector('.chat-picker [data-emoji=\"🐴\"]') || document.querySelector('.chat-picker [data-cat=Outdoors]').click()")
    b.js("document.querySelector('.chat-picker [data-emoji=\"🐴\"]').click()")
    check("  a pick lands in the box, the picker closes", (b.js("document.querySelector('#mesh-text').value"),
                                                        b.js("!!document.querySelector('.chat-picker')")), ("good 🐴", False))
    b.js("document.querySelector('#mesh-emoji').click()")
    check("  and goes to Recent", b.js("[...document.querySelectorAll('.chat-picker-grid [data-emoji]')].map((e) => e.dataset.emoji)"), ["🐴"])
    key(b, "Escape")
    check("  Escape closes it", b.js("!!document.querySelector('.chat-picker')"), False)
    b.js("document.querySelector('#mesh-text').value = ''; document.querySelector('#mesh-text').dispatchEvent(new Event('input'))")

    # Reply, then Escape
    ROW = f"[...document.querySelectorAll('{LIST} .chat-msg')].find((r) => r.querySelector('.mesh-body')?.textContent === {{}})"
    b.js(f"({ROW.format(json.dumps('gate is closed'))}).querySelector('[data-act=reply]').click()")
    check("Reply: the bar says so", "Replying to BASE" in b.js("document.querySelector('#mesh-to').textContent"), True)
    b.js("document.querySelector('#mesh-text').focus()")
    key(b, "Escape")
    check("  Escape: not a reply any more", b.js("document.querySelector('#mesh-to').hidden"), True)

    # Big emoji, grouping
    for t in ("🎉", "party at the barn 🎉"):
        b.js("document.querySelector('#mesh-text').focus()")
        b.call("Input.insertText", text=t)
        key(b, "Enter", text="\r")
        b.wait(f"{BODIES}.includes({json.dumps(t)})", 10)
    big = f"({ROW.format(json.dumps('🎉'))}).querySelector('.chat-bubble').classList.contains('big')"
    check("only emoji: big", b.js(big), True)
    check("  text with one: not", b.js(f"({ROW.format(json.dumps('party at the barn 🎉'))}).querySelector('.chat-bubble').classList.contains('big')"), False)
    check("my run of messages: one group", b.js(f"({ROW.format(json.dumps('🎉'))}).closest('.chat-group') === "
                                                f"({ROW.format(json.dumps('party at the barn 🎉'))}).closest('.chat-group')"), True)

    # A quote jumps to the original
    b.js(f"document.querySelector('{LIST}').scrollTop = document.querySelector('{LIST}').scrollHeight")
    b.js(f"({ROW.format(json.dumps('thanks, I saw it'))}).querySelector('.chat-quote').click()")
    check("a quote: clicking it flashes the original",
          bool(b.wait(f"({ROW.format(json.dumps('gate is closed'))}).classList.contains('flash')", 5)), True)

    # + on the bar: a reaction from the picker
    b.js(f"({ROW.format(json.dumps('gate is closed'))}).querySelector('[data-act=react-more]').click()")
    b.js("document.querySelector('.chat-picker [data-cat=Outdoors]').click()")
    b.js("document.querySelector('.chat-picker [data-emoji=\"🌵\"]').click()")
    b.wait(f"(({ROW.format(json.dumps('gate is closed'))}).querySelector('.mesh-reacts').textContent || '').includes('🌵')", 10)
    r = [c for c in commands() if c.get("text") == "🌵"]
    check("+ on the bar: the pick goes as a reaction to it", r and (r[0]["reply_to"], r[0]["emoji"]), (1001, True))

    # Scrolled up: the reader stays; a button says what's new
    b.js(f"document.querySelector('{LIST}').style.maxHeight = '160px'")
    time.sleep(0.3)                                 # the pane resizes first, then the reader scrolls up
    b.js(f"document.querySelector('{LIST}').scrollTop = 0")
    time.sleep(0.3)
    b.js("mesh.event({ t: 'mesh', msg: { k: 'msg', id: 901, t: Date.now() / 1000, from: '!0000beef', to: '^all', "
         "ch: 1, dm: false, text: 'cattle out by the wash', snr: 5, hops: 0 } })")
    check("scrolled up: a new message leaves the reader there", b.js(f"document.querySelector('{LIST}').scrollTop"), 0)
    check("  and says so", b.js("document.querySelector('#mesh-main .chat-jump:not([hidden])')?.textContent"), "↓ 1 new")
    b.js("document.querySelector('#mesh-main .chat-jump').click()")
    check("  clicking it goes to the latest", bool(b.wait(f"(() => {{ const l = document.querySelector('{LIST}'); "
                                                           "return l.scrollHeight - l.scrollTop - l.clientHeight < 60; })()", 5)), True)

    # Drawn hidden, shown later: the newest is in view
    b.js(f"document.querySelector('{LIST}').style.maxHeight = '160px'; document.querySelector('#mesh').hidden = true")
    for i in range(8):
        b.js("mesh.event({ t: 'mesh', msg: { k: 'msg', id: %d, t: Date.now() / 1000, from: '!0000beef', to: '^all', "
             "ch: 1, dm: false, text: 'while away %d', snr: 5, hops: 0 } })" % (950 + i, i))
    b.js("document.querySelector('#mesh').hidden = false")
    check("drawn while hidden: opens on the newest", bool(b.wait(f"(() => {{ const l = document.querySelector('{LIST}'); "
          "return l.clientHeight > 0 && l.scrollHeight - l.scrollTop - l.clientHeight < 60; })()", 5)), True)
    check("  no jump button", b.js("!!document.querySelector('#mesh-main .chat-jump:not([hidden])')"), False)
    check("no page errors", b.js("window.__errs"), [])
finally:
    b.close()
    server.terminate()
    try:
        server.wait(10)
    except subprocess.TimeoutExpired:
        server.kill()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
