"""M2: the Mesh panel on the page, driven in headless Chromium.

Runs server.py --sim with tests/fake_mesh_bridge.py as the node, opens the
page in Chromium (DevTools protocol, no extra packages) and checks:
  - the panel shows only on a server with a mesh node
  - it shows the node, Connected, a tab per channel plus Direct, and the
    HomeNet key check
  - the HomeNet tab shows the incoming message
  - typing and Send posts to the channel; the row goes to delivered (✓✓)
  - Send is disabled past 200 bytes
  - clicking a node opens Direct addressed to it, and the DM goes to that node
  - a message on another tab while you read this one counts as unread
  - the star on a node makes it a favorite on the node
  - the list shows every known node (Recent narrows it to the last day)
  - the map shows the nodes with a position and says how many; clicking
    one opens its card; a packet heard from a node pulses on it
  - the card's Traceroute shows the path there and back with SNRs, and the
    map draws it
  - Settings: the node's sections and values; an edit marks the field and
    enables Save; an invalid value disables it; Save sends only the changed
    fields and the form shows the node's new values; a risky field asks
    first and nothing is sent when you say no; Revert drops the edits

    python3 tests/mesh_page_test.py [screenshot.png]
"""
import json
import os
import random
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

WEB = 18000 + random.randrange(1000)
CDP = 19000 + random.randrange(1000)
CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
SHOT = sys.argv[1] if len(sys.argv) > 1 else None
TMP = tempfile.mkdtemp(prefix="meshpage-")
KEY = os.path.join(TMP, "key")
CMD_LOG = os.path.join(TMP, "commands.jsonl")
with open(KEY, "w") as f:
    f.write(FAKE_KEY)
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} got {got!r}" + ("" if ok else f" want {want!r}"))


def start(mesh=True):
    args = [sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB), "--rigctl-port", "0",
            "--addons-state", os.path.join(TMP, "addons.json")]   # never the real ~/.local/state one
    if mesh:
        args += ["--mesh-port", "/dev/null", "--mesh-python", sys.executable,
                 "--mesh-bridge", "tests/fake_mesh_bridge.py", "--mesh-key", f"HomeNet={KEY}",
                 "--mesh-log", ""]
    p = subprocess.Popen(args, cwd=ROOT, env=dict(os.environ, FAKE_MESH_LOG=CMD_LOG),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", WEB), timeout=0.2).close()
            return p
        except OSError:
            time.sleep(0.1)
    p.kill()
    raise SystemExit("server did not start")


def stop(p):
    p.terminate()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()


def commands():
    try:
        with open(CMD_LOG) as f:
            return [json.loads(l) for l in f if l.strip()]
    except OSError:
        return []


if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")

TEXT = "(s) => (document.querySelector(s)?.textContent || '')"
VISIBLE = "(s) => { const e = document.querySelector(s); return !!e && !e.closest('[hidden]') && e.offsetParent !== null; }"
TABS = "[...document.querySelectorAll('#mesh-tabs .chip')]"


def tab(name):
    return f"{TABS}.find((b) => b.textContent.startsWith({json.dumps(name)}))"


def type_text(s):
    b.js(f"(() => {{ const i = document.querySelector('#mesh-text'); i.value = {json.dumps(s)}; "
         "i.dispatchEvent(new Event('input')); })()")


b = Browser(CHROME, CDP)
try:
    server = start(mesh=False)
    try:
        b.goto(f"http://127.0.0.1:{WEB}/")
        b.wait("document.querySelector('#conn b')?.textContent === 'Connected'")
        check("no mesh on the server: no panel", b.js(f"({VISIBLE})('#mesh')"), False)
    finally:
        stop(server)

    server = start()
    try:
        time.sleep(1)                           # let the fake node's message arrive first
        b.goto(f"http://127.0.0.1:{WEB}/?v=mesh#/mesh")
        check("panel shown", bool(b.wait(f"({VISIBLE})('#mesh')")), True)
        check("node name", b.wait(f"({TEXT})('#mesh-me')"), "Test Node (TEST)")
        check("state: Connected", b.wait(f"({TEXT})('#mesh-state') === 'Connected' && 'Connected'"), "Connected")
        check("tabs", b.js(f"{TABS}.map((b) => b.textContent.trim())"), ["LongFast", "HomeNet", "Direct"])
        check("key check shown", b.js(f"({TEXT})('#mesh-key')"), "HomeNet key ✓")
        check("the key itself is not in the page", FAKE_KEY in b.js("document.documentElement.outerHTML"), False)

        b.js(f"{tab('HomeNet')}.click()")
        check("HomeNet shows the incoming message",
              bool(b.wait("[...document.querySelectorAll('#mesh-list .mesh-text')].some((e) => e.textContent === 'gate is closed')")), True)
        check("  sender shown by short name",
              b.js("document.querySelector('#mesh-list .mesh-who')?.textContent"), "BASE")

        type_text("x" * 201)
        check("Send disabled past 200 bytes", b.js("document.querySelector('#mesh-send').disabled"), True)
        type_text("opening the gate")
        check("Send enabled", b.js("document.querySelector('#mesh-send').disabled"), False)
        b.js("document.querySelector('#mesh-send').click()")
        check("my message goes to delivered",
              b.wait("[...document.querySelectorAll('#mesh-list .mesh-msg.mine')].some((r) => "
                     "r.textContent.includes('opening the gate') && r.querySelector('.mesh-ack.delivered'))"), True)
        sent = [c for c in commands() if c.get("text") == "opening the gate"]
        check("  sent on channel 1 to everyone", sent and (sent[0]["ch"], sent[0]["to"]), (1, None))
        check("  input cleared", b.js("document.querySelector('#mesh-text').value"), "")

        b.js("[...document.querySelectorAll('#mesh-nodes .mesh-node')].find((n) => n.textContent.includes('BASE')).click()")
        check("clicking a node opens its card", b.js(f"({VISIBLE})('#mesh-card') && ({TEXT})('#mesh-card .mesh-card-head b')"), "BASE")
        b.js("document.querySelector('#mesh-card [data-act=dm]').click()")
        check("  Message opens Direct", b.js(f"{tab('Direct')}.classList.contains('on')"), True)
        check("  addressed to it", "To BASE" in b.js(f"({TEXT})('#mesh-to')"), True)
        type_text("are you there")
        b.js("document.querySelector('#mesh-send').click()")
        b.wait("[...document.querySelectorAll('#mesh-list .mesh-msg.mine')].some((r) => r.textContent.includes('are you there'))")
        dm = [c for c in commands() if c.get("text") == "are you there"]
        check("  the DM goes to !0000beef", dm and dm[0]["to"], "!0000beef")

        # A channel message arriving while Direct is open: unread on HomeNet.
        b.js("send({ t: 'mesh_send', text: 'from another page', ch: 1 })")
        time.sleep(0.5)
        check("own sends elsewhere don't count as unread",
              b.js(f"{tab('HomeNet')}.querySelector('.mesh-unread')?.textContent || ''"), "")
        b.js("mesh.event({ t: 'mesh', msg: { k: 'msg', id: 77, t: Date.now() / 1000, from: '!0000beef', "
             "to: '^all', ch: 1, dm: false, text: 'cattle out', snr: 5, hops: 0 } })")
        check("a message on another tab counts as unread",
              b.js(f"{tab('HomeNet')}.querySelector('.mesh-unread')?.textContent"), "1")
        b.js(f"{tab('HomeNet')}.click()")
        check("  opening the tab clears it", b.js(f"{tab('HomeNet')}.querySelector('.mesh-unread')"), None)

        # Favorites
        b.js("[...document.querySelectorAll('#mesh-nodes .mesh-node')].find((n) => n.textContent.includes('BASE'))"
             ".querySelector('.mesh-fav').click()")
        check("star: the node becomes a favorite",
              bool(b.wait("[...document.querySelectorAll('#mesh-nodes .mesh-fav.on')].length === 1")), True)
        fav = [c for c in commands() if c.get("cmd") == "favorite"]
        check("  sent to the bridge", fav and (fav[-1]["to"], fav[-1]["fav"]), ("!0000beef", True))
        check("  clicking the star doesn't open Direct", b.js(f"{tab('HomeNet')}.classList.contains('on')"), True)

        # Every node, the map, traceroute
        NODES = "[...document.querySelectorAll('#mesh-nodes .mesh-node .mesh-short')].map((e) => e.textContent.slice(1))"
        check("the list shows every known node", sorted(b.js(NODES)), ["BASE", "RDG", "TRK"])
        b.js("document.querySelector('#mesh-all').click()")
        check("  Recent: only nodes heard in the last day", sorted(b.js(NODES)), ["BASE", "RDG"])
        b.js("document.querySelector('#mesh-all').click()")
        check("map: visible", b.js(f"({VISIBLE})('#mesh-map')"), True)
        check("  nodes with a position on it",
              sorted(b.js("[...document.querySelectorAll('#mesh-map .mm-node text')].map((t) => t.textContent)")),
              ["BASE", "RDG", "TEST"])
        check("  says how many", b.js(f"({TEXT})('#mesh-map .mm-note')"), "2 of 3 nodes on the map")
        check("  tiles requested", b.js("document.querySelectorAll('#mesh-map .mm-tile').length > 0"), True)
        b.js("[...document.querySelectorAll('#mesh-map .mm-node')].find((g) => g.textContent.startsWith('RDG'))"
             ".dispatchEvent(new MouseEvent('click', { bubbles: true }))")
        check("clicking a node on the map opens its card", b.js(f"({TEXT})('#mesh-card .mesh-card-head b')"), "RDG")
        check("  and marks it on the map", b.js("document.querySelector('#mesh-map .mm-node.sel text')?.textContent"), "RDG")
        b.js("mesh.event({ t: 'mesh', rx: { from: '!0000beef', port: 'POSITION_APP' } })")
        check("a packet heard pulses on the map", b.js("document.querySelectorAll('#mesh-map .mm-pulse').length"), 1)
        b.js("[...document.querySelectorAll('#mesh-nodes .mesh-node')].find((n) => n.textContent.includes('BASE')).click()")
        b.js("document.querySelector('#mesh-card [data-act=trace]').click()")
        check("Traceroute: the card shows the path",
              bool(b.wait("(document.querySelector('#mesh-card .mesh-trace')?.textContent || '').includes('RDG (6 dB)')")), True)
        trace_text = b.js("document.querySelector('#mesh-card .mesh-trace').textContent")
        check("  there: TEST → RDG → BASE with SNRs", "thereTEST → RDG (6 dB) → BASE (4.5 dB)" in trace_text, True)
        check("  back: BASE → RDG → TEST", "backBASE → RDG (5 dB) → TEST (?)" in trace_text, True)
        check("  the map draws it", b.js("!!document.querySelector('#mesh-map .mm-out') && !!document.querySelector('#mesh-map .mm-back')"), True)
        tr = [c for c in commands() if c.get("cmd") == "trace"]
        check("  sent to the bridge for !0000beef", tr and tr[-1]["to"], "!0000beef")
        if SHOT:
            b.js("document.querySelector('#mesh').scrollIntoView()")
            b.screenshot(SHOT.replace(".png", "-map.png"))

        # Settings
        CFG_TABS = "[...document.querySelectorAll('#mesh-cfg-tabs .chip')]"
        FIELD = ("(l) => [...document.querySelectorAll('#mesh-cfg-form label')]"
                 ".find((x) => x.textContent.startsWith(l))")

        def set_field(label, value, ev="input"):
            b.js(f"(() => {{ const i = ({FIELD})({json.dumps(label)}).querySelector('input,select'); "
                 f"if (i.type === 'checkbox') i.checked = {json.dumps(value)}; else i.value = {json.dumps(value)}; "
                 f"i.dispatchEvent(new Event({json.dumps(ev)})); }})()")

        b.js("document.querySelector('#mesh-cfg-btn').click()")
        check("Settings: the form replaces messages", b.js(f"({VISIBLE})('#mesh-cfg') && !({VISIBLE})('#mesh-list')"), True)
        check("  a tab per section", b.js(f"{CFG_TABS}.map((b) => b.textContent)"), ["Owner", "Device", "LoRa"])
        check("  owner's long name", b.js(f"({FIELD})('Long name').querySelector('input').value"), "Test Node")
        b.js(f"{CFG_TABS}[2].click()")
        check("  LoRa hop limit", b.js(f"({FIELD})('Hop limit').querySelector('input').value"), "3")
        check("  Save disabled before an edit", b.js("document.querySelector('#mesh-cfg-save').disabled"), True)
        set_field("Hop limit", "9")
        check("out of range: Save disabled", b.js("document.querySelector('#mesh-cfg-save').disabled"), True)
        check("  and says why", "0–7" in b.js(f"({TEXT})('#mesh-cfg-note')"), True)
        set_field("Hop limit", "5")
        check("an edit marks the field", b.js(f"({FIELD})('Hop limit').classList.contains('dirty')"), True)
        check("  and the tab", b.js(f"{CFG_TABS}[2].textContent"), "LoRa •")
        n = len([c for c in commands() if c.get("cmd") == "set"])
        b.js("document.querySelector('#mesh-cfg-save').click()")
        check("Save: the form shows the node's new value",
              bool(b.wait(f"!({FIELD})('Hop limit').classList.contains('dirty') && "
                          f"({FIELD})('Hop limit').querySelector('input').value === '5'")), True)
        sets = [c for c in commands() if c.get("cmd") == "set"]
        check("  only the changed field went", sets[n:] and (sets[-1]["section"], sets[-1]["values"]),
              ("lora", {"hop_limit": 5}))
        check("  the tab is clean again", b.js(f"{CFG_TABS}[2].textContent"), "LoRa")

        b.js("window.confirm = () => false")
        set_field("Transmit", False, "change")
        n = len(commands())
        b.js("document.querySelector('#mesh-cfg-save').click()")
        time.sleep(0.5)
        check("risky field, answer no: nothing sent", len(commands()), n)
        check("  the edit is kept", b.js(f"({FIELD})('Transmit').classList.contains('dirty')"), True)
        b.js("window.confirm = (q) => { window.asked = q; return true; }")
        b.js("document.querySelector('#mesh-cfg-save').click()")
        b.wait(f"!({FIELD})('Transmit').classList.contains('dirty')")
        check("risky field, answer yes: saved", commands()[-1].get("values"), {"tx_enabled": False})
        check("  the question names the field", "Transmit: true → false" in (b.js("window.asked") or ""), True)

        set_field("Region", "EU_868", "change")
        b.js("document.querySelector('#mesh-cfg-revert').click()")
        check("Revert: back to the node's value", b.js(f"({FIELD})('Region').querySelector('select').value"), "US")
        if SHOT:
            b.js("document.querySelector('#mesh').scrollIntoView()")
            b.screenshot(SHOT.replace(".png", "-settings.png"))
        b.js("mesh.event({ t: 'mesh', status: { connected: false, error: 'port busy' } })")
        check("not connected: the note gives the reason",
              b.js(f"({TEXT})('#mesh-cfg-note')"), "The node is not connected: port busy")
        check("  and the fields are disabled", b.js("[...document.querySelectorAll('#mesh-cfg-form select')].every((e) => e.disabled)"), True)
        b.js("mesh.event({ t: 'mesh', status: { connected: true, error: null } })")
        b.js("document.querySelector('#mesh-cfg-btn').click()")
        check("Settings closed: messages again", b.js(f"({VISIBLE})('#mesh-list')"), True)

        # The add-on's on/off switch (addons/mesh.py), in the Mesh tab's header.
        check("power: the switch reads 'Switch off'", b.js(f"({TEXT})('#mesh-power')"), "Switch off")
        b.js("window.confirm = (q) => { window.asked = q; return true; }")
        b.js("document.querySelector('#mesh-power').click()")
        check("  off: the state says so", b.wait(f"({TEXT})('#mesh-state') === 'Switched off' && 'off'"), "off")
        check("  it asked first", "Switch Mesh off?" in (b.js("window.asked") or ""), True)
        check("  the switch now reads 'Switch on'", b.js(f"({TEXT})('#mesh-power')"), "Switch on")
        check("  Send is disabled", b.js("document.querySelector('#mesh-send').disabled"), True)
        b.js("window.asked = null")
        b.js("document.querySelector('#mesh-power').click()")
        check("  on again: Connected", b.wait(f"({TEXT})('#mesh-state') === 'Connected' && 'on'"), "on")
        check("  switching on doesn't ask", b.js("window.asked"), None)
        check("  the messages are back", bool(b.wait("document.querySelectorAll('#mesh-list .mesh-text').length > 0")), True)
        if SHOT:
            b.js("document.querySelector('#mesh').scrollIntoView()")
            b.screenshot(SHOT)
    finally:
        stop(server)
finally:
    b.close()
    shutil.rmtree(TMP, ignore_errors=True)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
