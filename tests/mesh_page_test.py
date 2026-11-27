"""The Mesh panel on the page (headless Chromium), against tests/fake_mesh_bridge.py.

Covers: the panel only with a mesh node; node, state, channel tabs and key
check (never the key); messages, a long text's parts and Send's 9-part limit, delivery ticks,
an automatic resend's countdown and Cancel,
DMs from a node card, unread counts; favorites; the node list and map;
traceroute; the map's names with each node's SNR (direct) or hops, the Names
switch, the card at a clicked node (its signal report, Traceroute from it, the
legs' SNRs on the map, Copy report, Esc); Health (band, findings, charts, talkers, packet log, a finding
opens its node); Settings (dirty marking, validation, only changed fields sent,
risky fields ask first, Revert, disabled while disconnected); the on/off switch.
Also: replies (quoted, sent with reply_to), reactions (sent, shown as chips,
incoming ones on our message), Retry on a failed or unknown send, Direct as a list of
conversations, the header's counters, a card's distance / environment / Ask
for (position) / Ignore, who a node hears and who hears it (by name, a node gone from the list by
the name it had, and why one isn't on the map), NeighborInfo links on the map (lines that show, which way a selected node's go,
nodes sharing a position fanned apart), Reboot node, who
hears us ("heard by N" and its list). Conversations: the ⋯ menu (archive, kept
across a reload; out of Archived from the chat head), rail search by name and text,
deleting a message with Undo, deleting a DM conversation (it asks first).

    python3 tests/mesh_page_test.py [screenshot.png]
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

WEB = testkit.free_port()
CDP = testkit.free_port()
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
        adds = os.path.join(TMP, "addons.json")
        with open(adds, "w") as f:                 # Mesh is an add-on: the server takes it from a file
            json.dump({"mesh": {"port": "/dev/null", "python": sys.executable, "bridge": "tests/fake_mesh_bridge.py",
                                "keys": [f"HomeNet={KEY}"], "log": ""}}, f)
        args += ["--addons", adds]
    p = subprocess.Popen(args, cwd=ROOT, env=dict(os.environ, FAKE_MESH_LOG=CMD_LOG, MESH_ACK_WAIT_S="1.5", MESH_SELF_NEIGHBORS_FIRST_S="3600",
                                          MESH_RESEND_S="600,600,600",   # a resend waits: the page shows it
                                          FAKE_MESH_BURST="1", MESH_HEALTH_EVERY_S="0.3"),   # traffic for Health
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
TABS = "[...document.querySelectorAll('#mesh-tabs .chat-conv')]"   # the rail of conversations (chat.js)


def tab(name):
    return f"{TABS}.find((b) => b.querySelector('b').textContent === {json.dumps(name)})"


def conv(node):
    """A DM conversation on the rail, by node id."""
    return f"document.querySelector('#mesh-tabs [data-conv={json.dumps('d' + node)}]')"


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
        check("the rail: one line per channel", b.js(f"{TABS}.map((b) => b.querySelector('b').textContent)"), ["LongFast", "HomeNet"])
        check("  then Direct", b.js("[...document.querySelectorAll('#mesh-tabs .chat-rail-head')].map((e) => e.textContent)"), ["Channels", "Direct"])
        check("key check shown", b.js(f"({TEXT})('#mesh-key')"), "HomeNet key ✓")
        check("the key itself is not in the page", FAKE_KEY in b.js("document.documentElement.outerHTML"), False)

        b.js(f"{tab('HomeNet')}.click()")
        check("HomeNet shows the incoming message",
              bool(b.wait("[...document.querySelectorAll('#mesh-list .mesh-text')].some((e) => e.textContent === 'gate is closed')")), True)
        check("  sender shown by name, short name beside it",
              b.js("[document.querySelector('#mesh-list .mesh-who')?.textContent, document.querySelector('#mesh-list .chat-tag')?.textContent]"),
              ["Ranch Base", "BASE"])
        check("  in a bubble, the name in its own colour", b.js("!!document.querySelector('#mesh-list .chat-group .chat-bubble') && "
              "!!document.querySelector('#mesh-list .chat-group').style.getPropertyValue('--who')"), True)

        type_text("x" * 201)
        check("past 200 bytes: Send stays enabled, it goes in parts",
              b.js("document.querySelector('#mesh-send').disabled"), False)
        check("  the counter says how many", b.js("document.querySelector('#mesh-count').textContent"), "201 B · 2 parts")
        type_text("x" * (194 * 9 + 1))
        check("past 9 parts: Send disabled", b.js("document.querySelector('#mesh-send').disabled"), True)
        check("  the input takes 9 parts' worth", b.js("document.querySelector('#mesh-text').maxLength") >= 194 * 9, True)
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
        check("  Message opens its conversation", b.js(f"{conv('!0000beef')}?.classList.contains('on')"), True)
        check("  the conversation's head names it", b.js(f"({TEXT})('#mesh-chat-head b')"), "Ranch Base")
        check("  addressed to it", "To BASE" in b.js(f"({TEXT})('#mesh-to')"), True)
        type_text("are you there")
        b.js("document.querySelector('#mesh-send').click()")
        b.wait("[...document.querySelectorAll('#mesh-list .mesh-msg.mine')].some((r) => r.textContent.includes('are you there'))")
        dm = [c for c in commands() if c.get("text") == "are you there"]
        check("  the DM goes to !0000beef", dm and dm[0]["to"], "!0000beef")

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
              sorted(b.js("[...document.querySelectorAll('#mesh-map .mm-node text:not(.mm-badge)')].map((t) => t.textContent)")),
              ["BASE", "RDG", "TEST"])
        BADGES = ("Object.fromEntries([...document.querySelectorAll('#mesh-map .mm-node')].filter((g) => g.querySelector('.mm-badge'))"
                  ".map((g) => [g.querySelector('text').textContent, [g.querySelector('.mm-badge').textContent, g.querySelector('.mm-badge').getAttribute('class')]]))")
        # This node's own badge is how it is received (2026-11-27): RDG's NeighborInfo lists us at 7.25.
        check("  beside each name: the SNR of a node heard direct; this node, who hears it and the best SNR",
              b.js(BADGES), {"RDG": ["+3.25 dB", "mm-badge h0"], "BASE": ["+7.5 dB", "mm-badge h0"],
                             "TEST": ["heard by 1 · best +7.25 dB", "mm-badge h0"]})
        check("  says how many", b.js(f"({TEXT})('#mesh-map .mm-note')").split(" · ")[0], "2 of 3 nodes on the map")
        # Nothing selected: this node's own links, coloured by direction (RDG's NeighborInfo
        # lists us; packets from RDG and BASE arrived with no hop used).
        b.js("document.querySelector('#mesh-nodes .mesh-node.on')?.click()")        # the open card's node: close it
        b.wait("!document.querySelector('#mesh-nodes .mesh-node.on')", 5)
        mine = b.js("[...document.querySelectorAll('#mesh-map line.mm-link')].map((l) => [l.getAttribute('class'), "
                    "l.querySelector('title')?.textContent || '']).filter((x) => x[1].includes('you')).sort((a, b) => (a[1] < b[1] ? -1 : 1))")
        check("  with nothing selected: who hears this node and who it hears, by direction", mine,
              [["mm-link both", "RDG hears you at 7.25 dB; you hear RDG at 3.25 dB"], ["mm-link out", "you hear BASE at 7.5 dB"]])
        check("  the map's note is the key", "you: green hears you, blue you hear, purple both" in b.js(f"({TEXT})('#mesh-map .mm-note')"), True)
        check("  a pair is drawn once (ours in colour, not also as a plain NeighborInfo line)",
              b.js("document.querySelectorAll('#mesh-map line.mm-link').length"), 3)
        # What each node's mark says: routers are diamonds, a ring for which way we hear each other,
        # the hover text the rest; distance rings around this node.
        NODE = "((name) => [...document.querySelectorAll('#mesh-map .mm-node')].find((g) => g.querySelector('text:not(.mm-glyph)')?.textContent === name))"
        check("  marks: both routers are diamonds; RDG's ring is both ways, BASE's the way we hear it; this node a plain dot",
              b.js(f"[{NODE}('RDG').querySelector('path.mm-mark') !== null, {NODE}('RDG').querySelector('.mm-ring').getAttribute('class'), "
                   f"{NODE}('BASE').querySelector('.mm-ring').getAttribute('class'), {NODE}('TEST').querySelector('circle.mm-mark') !== null, "
                   f"{NODE}('TEST').querySelector('.mm-ring')]"),
              [True, "mm-ring both", "mm-ring out", True, None])
        tip = b.js(f"{NODE}('RDG').querySelector('title').textContent").split("\n")
        check("  hover: its name, role and how it reached us, when and how far, and both directions",
              (tip[0], tip[1], tip[2].split(" · ")[1:], tip[3]),
              ("Ridge Relay (RDG) !0000cafe", "ROUTER · direct, 3.25 dB", ["11 km NE"], "hears you at 7.25 dB · you hear it at 3.25 dB"))
        rings = b.js("[...document.querySelectorAll('#mesh-map .mm-range-t')].map((t) => t.textContent)")
        check("  distance rings around this node, each saying how far", (1 <= len(rings) <= 3, all(r.endswith(" km") for r in rings)), (True, True))
        b.js("window.__nodes0 = mesh._nodes(); mesh.event({ t: 'mesh', nodes: __nodes0.map((n) => n.id === '!0000dead' ? "
             "{ ...n, lat: 40.95, lon: -105.2, last_heard: Date.now() / 1000 } : n) })")
        check("  a node two hops away carries its hops, in the mark and beside its name; it wears no ring",
              b.js(f"[{NODE}('TRK').querySelector('.mm-glyph').textContent, {NODE}('TRK').querySelector('.mm-badge').textContent, "
                   f"{NODE}('TRK').querySelector('.mm-badge').getAttribute('class'), {NODE}('TRK').querySelector('.mm-ring')]"),
              ["2", "2 hops", "mm-badge h2", None])
        # Names: "fit" names what fits; "all" names every node, overlaps and all (kept in the browser).
        LBL = "document.querySelector('#mesh-map [data-labels]')"
        check("  Names: fit by default", b.js(f"{LBL}.textContent"), "Names: fit")
        b.js(f"{LBL}.click()")
        check("  Names: all names every placed node, and is kept",
              (b.js(f"{LBL}.textContent"), b.js("document.querySelectorAll('#mesh-map .mm-node text:not(.mm-glyph):not(.mm-badge)').length"),
               b.js("document.querySelectorAll('#mesh-map .mm-node').length"), b.js("localStorage.getItem('ic7300.mm.labels')")),
              ("Names: all", 4, 4, '"all"'))
        b.js(f"{LBL}.click()")
        b.js("mesh.event({ t: 'mesh', nodes: __nodes0 })")
        # Who hears whom, by name, beside the map (as on the Map tab).
        SIDE = "document.querySelector('#mesh-map .mm-side')"
        check("the list beside the map: two-way, hear you, you hear, with counts",
              b.js(f"[...{SIDE}.querySelectorAll('[data-tab]')].map((t) => t.textContent)"), ["Two-way 1", "Hear you 1", "You hear 2"])
        ROWS = f"[...{SIDE}.querySelectorAll('.mm-row')].map((r) => [...r.children].map((c) => c.textContent).filter(Boolean).join(' '))"
        check("  hear you: the node, how far, the SNR we hear it at and the SNR it hears us at", b.js(ROWS), ["RDG Ridge Relay 11 km ▼+3.25 ▲+7.25"])
        b.js(f"{SIDE}.querySelector('[data-tab=\"out\"]').click()")
        check("  you hear: farthest first; one that has not said it hears us has no ▲", b.js(ROWS),
              ["RDG Ridge Relay 11 km ▼+3.25 ▲+7.25", "BASE Ranch Base 2.0 km ▼+7.5"])
        b.js(f"{SIDE}.querySelector('.mm-row[data-id=\"!0000beef\"]').click()")
        check("  a click on a row opens that node's card and marks the row",
              (b.js(f"({TEXT})('#mesh-card .mesh-card-head b')"), b.js(f"{SIDE}.querySelector('.mm-row.sel b').textContent")), ("BASE", "BASE"))
        b.js(f"{SIDE}.querySelector('[data-toggle]').click()")
        check("  ✕ puts it away (kept in the browser); Reach brings it back",
              (b.js(f"{SIDE}.classList.contains('shut')"), b.js("localStorage.getItem('ic7300.mesh.reach')"), b.js(f"{SIDE}.textContent")), (True, "false", "Reach"))
        b.js(f"{SIDE}.querySelector('[data-toggle]').click()")
        check("  back", b.js(f"{SIDE}.querySelectorAll('.mm-row').length"), 2)
        # The map given the room: the conversation steps aside, and comes back when one is asked for.
        h0 = b.js("document.querySelector('#mesh-map').clientHeight")
        b.js("document.querySelector('#mesh-map [data-big]').click()")
        check("⤢: the map takes the room; the conversation is put away",
              (b.js("document.querySelector('#mesh-main').classList.contains('mesh-bigmap')"), b.js("getComputedStyle(document.querySelector('#mesh-main .chat-main')).visibility"),
               b.js("document.querySelector('#mesh-map').clientHeight") > h0), (True, "hidden", True))
        b.js("document.querySelector('#mesh-card [data-act=dm]').click()")
        check("  Message on a node's card brings the conversation back",
              (b.js("document.querySelector('#mesh-main').classList.contains('mesh-bigmap')"), b.js("getComputedStyle(document.querySelector('#mesh-main .chat-main')).visibility") == "visible"),
              (False, True))
        # A click on the small map opens it (2026-11-19: "click it to drive around"); a drag only pans.
        SPOT = """(() => { const m = document.querySelector('#mesh-map'), r = m.getBoundingClientRect();
          for (let y = r.top + 40; y < r.bottom - 30; y += 9) for (let x = r.left + 12; x < r.right - 12; x += 9) {
            const e = document.elementFromPoint(x, y);
            if (e && m.contains(e) && !e.closest('.mm-node, .mm-ctl, .mm-side, .mm-attr, .mm-note')) return [x, y]; }
          return null; })()"""

        def mouse(kind, x, y):
            b.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", buttons=1 if kind != "mouseReleased" else 0, clickCount=1)

        x, y = b.js(SPOT)
        mouse("mousePressed", x, y)
        mouse("mouseMoved", x + 40, y + 10)
        mouse("mouseReleased", x + 40, y + 10)
        check("a drag on the small map pans, it doesn't open it", b.js("document.querySelector('#mesh-main').classList.contains('mesh-bigmap')"), False)
        x, y = b.js(SPOT)
        mouse("mousePressed", x, y)
        mouse("mouseReleased", x, y)
        check("a click on the small map opens it big", b.js("document.querySelector('#mesh-main').classList.contains('mesh-bigmap')"), True)
        check("  it fills the panel", b.js("document.querySelector('#mesh-map').clientWidth > document.querySelector('#mesh-main').clientWidth * 0.6"), True)
        check("  ⤢ is now the way back", b.js("document.querySelector('#mesh-map [data-big]').textContent"), "✕ Close map")
        x, y = b.js(SPOT)
        mouse("mousePressed", x, y)
        mouse("mouseReleased", x, y)
        check("  a click on the big map keeps it big", b.js("document.querySelector('#mesh-main').classList.contains('mesh-bigmap')"), True)
        b.call("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27)
        b.call("Input.dispatchKeyEvent", type="keyUp", key="Escape", code="Escape", windowsVirtualKeyCode=27)
        check("  Esc gives the room back", (b.js("document.querySelector('#mesh-main').classList.contains('mesh-bigmap')"),
                                           b.js("document.querySelector('#mesh-map [data-big]').textContent")), (False, "⤢"))
        b.js(f"{tab('HomeNet')}.click(); document.querySelector('#mesh-nodes .mesh-node.on')?.click()")
        b.wait("!document.querySelector('#mesh-nodes .mesh-node.on')", 5)
        check("  tiles requested", b.js("document.querySelectorAll('#mesh-map .mm-tile').length > 0"), True)
        # The background (basemaps.js): terrain until the operator picks another, the pick kept in the browser.
        BG = "[document.querySelector('#mesh-map .mm-tile').src.split('/')[2], document.querySelector('#mesh-map .mm-attr').textContent, document.querySelector('#mesh-map .mm-tiles').dataset.basemap]"
        check("  background: terrain by default, with its own attribution", b.js(BG),
              ["tile.opentopomap.org", "© OpenStreetMap contributors, SRTM | © OpenTopoMap (CC-BY-SA)", "terrain"])
        check("  a menu to pick another", b.js("[...document.querySelectorAll('#mesh-map .bm-pick option')].map((o) => o.textContent)"),
              ["Earth", "Terrain", "Topo (US)", "Imagery (US)", "Streets", "Dark"])
        b.js("(() => { const s = document.querySelector('#mesh-map .bm-pick'); s.value = 'topo'; s.dispatchEvent(new Event('change')); })()")
        check("  picked: the tiles, the attribution and the dimming follow; the choice is kept",
              (b.js(BG), b.js("localStorage.getItem('rr-basemap.local')")),
              (["basemap.nationalmap.gov", "USGS The National Map", "topo"], "topo"))
        check("  no tile of the old background is left", b.js("[...document.querySelectorAll('#mesh-map .mm-tile')].every((i) => i.src.includes('nationalmap'))"), True)
        b.js("[...document.querySelectorAll('#mesh-map .mm-node')].find((g) => g.textContent.startsWith('RDG'))"
             ".dispatchEvent(new MouseEvent('click', { bubbles: true }))")
        check("clicking a node on the map opens its card", b.js(f"({TEXT})('#mesh-card .mesh-card-head b')"), "RDG")
        check("  and marks it on the map", b.js("document.querySelector('#mesh-map .mm-node.sel text')?.textContent"), "RDG")
        b.js("mesh.event({ t: 'mesh', rx: { from: '!0000beef', port: 'POSITION_APP' } })")
        check("a packet heard pulses on the map", b.js("document.querySelectorAll('#mesh-map .mm-pulse:not(.tx)').length"), 1)
        b.js("send({ t: 'mesh_send', text: 'on the air', ch: 1 })")
        check("  a message of ours pulses red at this node",
              bool(b.wait("document.querySelectorAll('#mesh-map .mm-pulse.tx').length === 1", 5)), True)
        # The card at a node on the map: its signal report and what to do, Traceroute among them.
        POP = "document.querySelector('#mesh-map .mm-pop')"
        b.js(f"{POP}.hidden || document.querySelector('#mesh-map .mm-pop-x').click()")
        b.js("[...document.querySelectorAll('#mesh-map .mm-node')].find((g) => g.querySelector('text')?.textContent === 'BASE')"
             ".dispatchEvent(new MouseEvent('click', { bubbles: true }))")
        check("a node clicked on the map: its card opens at it, and the side card follows",
              (b.js(f"!{POP}.hidden"), b.js(f"({TEXT})('#mesh-map .mm-pop-h b')"), b.js(f"({TEXT})('#mesh-card .mesh-card-head b')")), (True, "BASE", "BASE"))
        ROWS_POP = f"[...{POP}.querySelectorAll('.mm-pop-r')].map((r) => r.children[0].textContent + ': ' + r.children[1].textContent)"
        rows = b.js(ROWS_POP)
        check("  its signal report: hops, how we hear it, how far",
              (rows[:2], rows[2].startswith("distance: 2.0 km ")), (["hops: direct", "we hear it: +7.5 dB SNR"], True))
        check("  its actions", b.js(f"[...{POP}.querySelectorAll('[data-act]')].map((x) => x.textContent)"),
              ["Traceroute", "Message", "Position", "Telemetry", "Neighbours", "Copy report"])
        b.js(f"{POP}.querySelector('[data-act=trace]').click()")
        check("Traceroute from the map: the card at the node shows the path with each leg's SNR",
              bool(b.wait(f"({TEXT})('#mesh-map .mm-pop-tr').startsWith('TEST → RDG +6 → BASE +4.5')")), True)
        check("  the map writes each leg's SNR on the path, there and back",
              sorted(b.js("[...document.querySelectorAll('#mesh-map .mm-trace-snr')].map((t) => t.getAttribute('class').split(' ')[1] + ' ' + t.textContent)")),
              sorted(["out +6 dB", "out +4.5 dB", "back +5 dB", "back ?"]))
        BOX = "(q) => [...document.querySelectorAll(q)].map((t) => t.getBoundingClientRect())"
        check("  no leg's SNR sits on another label (the other leg's, a link's)",
              b.js(f"(() => {{ const a = ({BOX})('#mesh-map .mm-trace-snr'), o = [...a, ...({BOX})('#mesh-map .mm-link-label')];"
                   " return a.some((r, i) => o.some((q, j) => j !== i && r.left < q.right && r.right > q.left && r.top < q.bottom && r.bottom > q.top)); })()"), False)
        if SHOT:
            b.js("document.querySelector('#mesh-map [data-big]').click()")
            time.sleep(0.5)
            b.screenshot(SHOT.replace(".png", "-pop.png"))
            b.js("document.querySelector('#mesh-map [data-big]').click()")
        b.js(f"{POP}.querySelector('[data-act=copy]').click()")
        report = b.js("mesh._report()").split("\n")
        check("  Copy report: name, how we hear it, and the trace", (report[0].split(" · ")[:2], report[1]),
              (["BASE (Ranch Base) !0000beef", "heard direct, SNR +7.5 dB"], "trace: TEST > RDG (+6 dB) > BASE (+4.5 dB)"))
        b.call("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27)
        b.call("Input.dispatchKeyEvent", type="keyUp", key="Escape", code="Escape", windowsVirtualKeyCode=27)
        check("  Esc puts the card at the node away", b.js(f"{POP}.hidden"), True)
        b.js("document.querySelector('#mesh-card [data-act=trace]').click()")
        check("  the side card's Traceroute still asks (the node allows one every 30 s, and says so)",
              bool(b.wait("[...document.querySelectorAll('#toasts .toast')].some((t) => t.textContent.includes('one traceroute every'))", 5)), True)
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

        # Replies, reactions, retry
        b.js(f"{tab('HomeNet')}.click()")
        ROW = "[...document.querySelectorAll('#mesh-list .mesh-msg')].find((r) => r.querySelector('.mesh-body')?.textContent === {})"
        check("a reply shows what it answers",
              b.js(f"({ROW.format(json.dumps('thanks, I saw it'))})?.querySelector('.mesh-quote')?.textContent"),
              "Ranch Basegate is closed")
        b.js(f"({ROW.format(json.dumps('gate is closed'))}).querySelector('[data-act=reply]').click()")
        check("Reply: the compose bar says so", "Replying to BASE" in b.js(f"({TEXT})('#mesh-to')"), True)
        type_text("on it")
        b.js("document.querySelector('#mesh-send').click()")
        b.wait(f"!!({ROW.format(json.dumps('on it'))})")
        c = [c for c in commands() if c.get("text") == "on it"]
        check("  sent with reply_to", c and (c[0]["reply_to"], c[0]["ch"]), (1001, 1))
        check("  the bar clears", "Replying" in b.js(f"({TEXT})('#mesh-to')"), False)
        b.js(f"({ROW.format(json.dumps('gate is closed'))}).querySelector('[data-react=\"❤️\"]').click()")
        check("React: a chip on the message",
              bool(b.wait(f"(({ROW.format(json.dumps('gate is closed'))})?.querySelector('.mesh-reacts')?.textContent || '').includes('❤️')")), True)
        c = [c for c in commands() if c.get("text") == "❤️"]
        check("  sent as a reaction to it", c and (c[0]["reply_to"], c[0]["emoji"]), (1001, True))
        check("  not a row of its own", b.js("[...document.querySelectorAll('#mesh-list .mesh-body')].some((e) => e.textContent === '❤️')"), False)
        type_text("react to me")
        b.js("document.querySelector('#mesh-send').click()")
        check("an incoming reaction shows on our message",
              bool(b.wait(f"(({ROW.format(json.dumps('react to me'))})?.querySelector('.mesh-reacts')?.textContent || '').includes('👍')")), True)
        type_text("nak")
        b.js("document.querySelector('#mesh-send').click()")
        check("a NAK: it goes again by itself, with a countdown and Cancel",
              bool(b.wait(f"/again in/.test(({ROW.format(json.dumps('nak'))})?.querySelector('.mesh-retry-in')?.textContent || '') && "
                          f"!!({ROW.format(json.dumps('nak'))})?.querySelector('[data-act=cancel]')")), True)
        b.js(f"({ROW.format(json.dumps('nak'))}).querySelector('[data-act=cancel]').click()")
        check("  Cancel: it stays failed and offers Retry", bool(b.wait(f"!!({ROW.format(json.dumps('nak'))})?.querySelector('[data-act=retry]') && "
                                                                       f"!({ROW.format(json.dumps('nak'))})?.querySelector('.mesh-retry-in')")), True)
        b.js(f"({ROW.format(json.dumps('nak'))}).querySelector('[data-act=retry]').click()")
        time.sleep(0.5)
        check("  Retry sends it again", len([c for c in commands() if c.get("text") == "nak"]), 2)
        type_text("silent")
        b.js("document.querySelector('#mesh-send').click()")
        check("no ACK at all: unknown, and Retry offered",
              bool(b.wait(f"!!({ROW.format(json.dumps('silent'))})?.querySelector('.mesh-ack.unknown') && "
                          f"!!({ROW.format(json.dumps('silent'))})?.querySelector('[data-act=retry]')")), True)

        # Direct: the conversations, on the rail under Direct
        check("Direct: one line per conversation",
              b.js("[...document.querySelectorAll('#mesh-tabs .chat-conv[data-conv^=d] b')].map((e) => e.textContent)"), ["Ranch Base"])
        b.js("mesh.event({ t: 'mesh', msg: { k: 'msg', id: 88, t: Date.now() / 1000, from: '!0000cafe', "
             "to: '!b2a6fd78', ch: 0, dm: true, text: 'you up?', snr: 3, hops: 0 } })")
        check("  a new DM: its conversation on top, unread", b.js("document.querySelector('#mesh-tabs .chat-conv[data-conv^=d]')?.textContent"),
              "RRRidge Relaynowyou up?1")
        b.js(f"{conv('!0000cafe')}.click()")
        check("  open it: only that node's messages",
              b.js("[...document.querySelectorAll('#mesh-list .mesh-body')].map((e) => e.textContent)"), ["you up?"])
        check("  addressed to it", "To RDG" in b.js(f"({TEXT})('#mesh-to')"), True)

        # Header, card, map links
        check("header: this node's counters", b.js(f"({TEXT})('#mesh-stats')").startswith("ch 12.5%tx 1.3%⚡ powered3 online"), True)
        check("header: heard by 1 (RDG's NeighborInfo lists us)", b.js(f"({TEXT})('#mesh-hears-btn')"), "heard by 1")
        b.js("document.querySelector('#mesh-hears-btn').click()")
        check("  click: the list opens", b.js(f"({VISIBLE})('#mesh-hears')"), True)
        check("  one row a node: RDG (traceroute + NeighborInfo, newest SNR), BASE apart as a DM",
              b.js("[...document.querySelectorAll('#mesh-hears .mesh-hear')].map((e) => [...e.children].slice(0, 4).map((c) => c.textContent))"),
              [["RDG", "Ridge Relay", "first hop of a traceroute · says it hears us", "6 dB"],
               ["BASE", "Ranch Base", "got our DM", ""]])
        b.js("document.querySelector('#mesh-hears .mesh-hear').click()")
        check("  a row opens that node's card", b.js(f"({TEXT})('#mesh-card .mesh-card-head b')"), "RDG")
        b.js("mesh.event({ t: 'mesh', hears: [{ id: '!0000cafe', t: Date.now() / 1000, snr: 7.25, how: 'neighbor' }, "
             "{ id: '!0000beef', t: Date.now() / 1000, snr: null, how: 'dm' }] })")
        check("  a DM ACK is listed apart and not counted as direct",
              [b.js(f"({TEXT})('#mesh-hears-btn')"),
               b.js("[...document.querySelectorAll('#mesh-hears .mesh-hears-h')].map((e) => e.textContent)")],
              ["heard by 1", ["Hear us directly", "Got our DMs (maybe through relays)"]])
        b.js("document.querySelector('#mesh-hears-btn').click()")
        check("  click again: closed", b.js(f"({VISIBLE})('#mesh-hears')"), False)
        if not b.js(f"({VISIBLE})('#mesh-card') && ({TEXT})('#mesh-card .mesh-card-head b') === 'BASE'"):
            b.js("[...document.querySelectorAll('#mesh-nodes .mesh-node')].find((n) => n.textContent.includes('BASE')).click()")
        check("BASE's card is open", b.js(f"({VISIBLE})('#mesh-card') && ({TEXT})('#mesh-card .mesh-card-head b')"), "BASE")
        card = b.js(f"({TEXT})('#mesh-card')")
        check("card: distance and bearing", "distance 2.0 km SE (123°)" in card, True)
        check("  environment", "temp 21.5 °C / 71 °F" in card and "pressure 850.2 hPa" in card, True)
        nbs = "[...document.querySelectorAll('#mesh-card .mesh-nbs > div')].map((d) => d.textContent.replace(/ · .*$/, ''))"
        check("  who it hears and who hears it, by name with SNR", b.js(nbs), ["hearsRDG 6.5 dB", "heard byRDG 2 dB"])
        b.js("mesh.event({ t: 'mesh', neighbors: { '!0000beef': { t: Date.now() / 1000, neighbors: ["
             "{ id: '!0000cafe', snr: 6.5 }, { id: '!0000dead', snr: -4 }, { id: '!00c0ffee', snr: 1 }] } } })")
        check("  one the map can't place says why", b.js(nbs)[0],
              "hearsRDG 6.5 dBTRK -4 dB (no position)!00c0ffee 1 dB (not in this node's list)")
        b.js("mesh.event({ t: 'mesh', names: { '!00c0ffee': { short: 'CAFE', long: 'Coffee Hill', hw: '' } }, "
             "neighbors: { '!0000beef': { t: Date.now() / 1000, neighbors: ["
             "{ id: '!0000cafe', snr: 6.5 }, { id: '!0000dead', snr: -4 }, { id: '!00c0ffee', snr: 1 }] } } })")
        check("  one gone from the list keeps the name it had (names)", b.js(nbs)[0],
              "hearsRDG 6.5 dBTRK -4 dB (no position)CAFE 1 dB (not in this node's list)")
        b.js("document.querySelector('#mesh-card .mesh-nb[data-go=\"!0000cafe\"]').click()")
        check("  a name opens that node's card", b.js(f"({TEXT})('#mesh-card .mesh-card-head b')"), "RDG")
        b.js("mesh.event({ t: 'mesh', neighbors: { '!0000beef': { t: Date.now() / 1000, neighbors: [{ id: '!0000cafe', snr: 6.5 }] }, '!0000cafe': { t: Date.now() / 1000, neighbors: [{ id: '!b2a6fd78', snr: 7.25 }, { id: '!0000beef', snr: 2 }] } } })")
        b.js("[...document.querySelectorAll('#mesh-nodes .mesh-node')].find((n) => n.textContent.includes('BASE')).click()")
        # BASE selected: it hears RDG (6.5) and RDG hears it (2.0), so one link, both ways.
        # (The page takes each neighbors event as the whole set, as the server sends it.)
        b.js("mesh.event({ t: 'mesh', neighbors: { '!0000beef': { t: Date.now() / 1000, neighbors: [{ id: '!0000cafe', snr: 6.5 }] }, "
             "'!0000cafe': { t: Date.now() / 1000, neighbors: [{ id: '!b2a6fd78', snr: 7.25 }, { id: '!0000beef', snr: 2 }] } } })")
        check("map: BASE's link to RDG, both ways, with both SNRs",
              b.js("[...document.querySelectorAll('#mesh-map .mm-link-label')].map((t) => t.textContent)"), ["2 dB / 6.5 dB"])
        line = "document.querySelector('#mesh-map line.mm-link')"
        check("  drawn as a visible line (a stroke, not none), styled 'both'",
              b.js(f"[{line}.getAttribute('class'), getComputedStyle({line}).stroke !== 'none', "
                   f"parseFloat(getComputedStyle({line}).strokeWidth) > 0]"), ["mm-link both", True, True])
        check("  its tooltip says who hears whom",
              b.js(f"{line}.querySelector('title').textContent"), "RDG hears BASE at 2 dB; BASE hears RDG at 6.5 dB")
        check("  the legend", "BASE: green hears it, blue it hears, purple both" in b.js(f"({TEXT})('#mesh-map .mm-note')"), True)
        b.js("mesh.event({ t: 'mesh', neighbors: { '!0000beef': { t: Date.now() / 1000, neighbors: [] }, '!0000cafe': { t: Date.now() / 1000, neighbors: [{ id: '!b2a6fd78', snr: 7.25 }, { id: '!0000beef', snr: 2 }] } } })")
        check("  only RDG's list has BASE: 'in' (RDG hears BASE)",
              b.js(f"{line}?.getAttribute('class')"), "mm-link in")
        # Two nodes at the very same position (a channel's grid cell): fanned apart, and the line between them shows.
        b.js("mesh.event({ t: 'mesh', nodes: mesh._nodes().map((n) => n.id === '!0000cafe' ? { ...n, lat: 40.9, lon: -105.1 } : n) })")
        spots = ("[...document.querySelectorAll('#mesh-map .mm-node')].filter((g) => ['BASE', 'RDG'].includes(g.querySelector('text')?.textContent))"
                 ".map((g) => g.getAttribute('transform'))")
        got = b.js(spots)
        check("same position: BASE and RDG drawn apart", len(got) == 2 and got[0] != got[1], True)
        check("  a ring marks the shared spot", b.js("document.querySelectorAll('#mesh-map .mm-fan').length"), 1)
        x = b.js(f"[{line}.getAttribute('x1'), {line}.getAttribute('x2'), {line}.getAttribute('y1'), {line}.getAttribute('y2')]")
        check("  the link between them has length", x[0] != x[1] or x[2] != x[3], True)
        b.js("mesh.event({ t: 'mesh', neighbors: { '!0000beef': { t: Date.now() / 1000, neighbors: [{ id: '!0000cafe', snr: 6.5 }] }, '!0000cafe': { t: Date.now() / 1000, neighbors: [{ id: '!b2a6fd78', snr: 7.25 }, { id: '!0000beef', snr: 2 }] } } })")
        check("card: Ask for has Neighbours",
              b.js("document.querySelector('#mesh-card [data-req=neighbors]')?.textContent"), "Neighbours")
        b.js("document.querySelector('#mesh-card [data-req=position]').click()")
        check("Ask for position: answered",
              bool(b.wait(f"({TEXT})('#mesh-card').includes('Position: answered')")), True)
        rq = [c for c in commands() if c.get("cmd") == "request"]
        check("  sent to the bridge", rq and (rq[-1]["to"], rq[-1]["kind"]), ("!0000beef", "position"))
        b.js("document.querySelector('#mesh-card [data-act=ignore]').click()")
        check("Ignore: the node is dimmed in the list",
              bool(b.wait("[...document.querySelectorAll('#mesh-nodes .mesh-node.ignored')].some((n) => n.textContent.includes('BASE'))")), True)
        b.js("document.querySelector('#mesh-card [data-act=ignore]').click()")
        b.wait("![...document.querySelectorAll('#mesh-nodes .mesh-node.ignored')].length")

        # Health
        b.js("document.querySelector('#mesh-health-btn').click()")
        check("Health: the view replaces messages", b.js(f"({VISIBLE})('#mesh-health') && !({VISIBLE})('#mesh-list')"), True)
        check("  the band says Attention", bool(b.wait("document.querySelector('.mesh-health-band b')?.textContent === 'Attention'")), True)
        check("  the button is marked too", b.js("document.querySelector('#mesh-health-btn').classList.contains('mesh-health-warn')"), True)
        check("  the chatty node, by name", bool(b.wait("[...document.querySelectorAll('.mesh-finding')].some((f) => f.textContent.includes('TRK is chatty'))")), True)
        check("  the main relay, by name", b.js("[...document.querySelectorAll('.mesh-finding')].some((f) => f.textContent.includes('through RDG'))"), True)
        check("  two charts", b.js("document.querySelectorAll('#mesh-health svg.mh-chart').length"), 2)
        check("  the packets-a-minute chart has a bar", b.js("document.querySelectorAll('#mesh-health .mh-bar').length > 0"), True)
        check("  top talker first: TRK", b.js("document.querySelector('.mesh-click tbody tr td')?.textContent"), "TRK")
        check("  the packet log", b.js("document.querySelectorAll('#mesh-packets tbody tr').length >= 36"), True)
        if SHOT:
            b.js("document.querySelector('#mesh').scrollIntoView()")
            b.screenshot(SHOT.replace(".png", "-health.png"))
        b.js("[...document.querySelectorAll('.mesh-finding')].find((f) => f.textContent.includes('TRK is chatty')).click()")
        check("  a finding opens its node's card", b.js(f"({VISIBLE})('#mesh-card') && ({TEXT})('#mesh-card').includes('Old Truck')"), True)
        check("    with its traffic", b.js(f"({TEXT})('#mesh-card').includes('mostly position')"), True)
        check("    and the view closes", b.js(f"({VISIBLE})('#mesh-health')"), False)
        b.js("document.querySelector('#mesh-card [data-act=close]')?.click()")
        b.js("document.querySelector('#mesh-cfg-btn').click()")
        b.js("document.querySelector('#mesh-health-btn').click()")
        check("  Health after Settings: only Health shows", b.js(f"({VISIBLE})('#mesh-health') && !({VISIBLE})('#mesh-cfg')"), True)
        b.js("document.querySelector('#mesh-health-btn').click()")
        check("  Health again: back to messages", b.js(f"({VISIBLE})('#mesh-list')"), True)

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
        b.js("window.confirm = () => true")
        b.js("document.querySelector('#mesh-reboot').click()")
        time.sleep(0.5)
        check("Reboot node: sent after asking", [c for c in commands() if c.get("cmd") == "reboot"] != [], True)
        b.js("document.querySelector('#mesh-cfg-btn').click()")
        check("Settings closed: messages again", b.js(f"({VISIBLE})('#mesh-list')"), True)

        # the add-on's on/off switch
        b.js(f"{tab('HomeNet')}.click()")
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
        check("  still on the channel it was on", b.js(f"{tab('HomeNet')}.classList.contains('on')"), True)
        # Conversations: the ⋯ menu, Archived, search, delete and Undo
        HEADS = "[...document.querySelectorAll('#mesh-tabs .chat-rail-head')].map((e) => e.textContent)"
        MENU = "[...document.querySelectorAll('.chat-menu [data-act]')].map((e) => e.dataset.act)"
        NAMES = f"{TABS}.map((t) => t.querySelector('b').textContent)"
        FIND = "(() => {{ const f = document.querySelector('#mesh-tabs .chat-rail-find'); f.value = {}; f.dispatchEvent(new Event('input')); }})()"
        b.js("document.querySelector('#mesh-tabs [data-menu=c1]').click()")
        check("conv menu: read state, pin, mute, archive, clear", b.js(MENU)[1:], ["pin", "mute", "archive", "clear"])
        check("  it says delete is local only", "cannot unsend" in b.js("document.querySelector('.chat-menu-note').textContent"), True)
        b.js("document.querySelector('.chat-menu [data-act=archive]').click()")
        check("archive: HomeNet goes under a folded Archived", bool(b.wait(
            f"{HEADS}.some((h) => h.includes('Archived 1')) && !{NAMES}.includes('HomeNet')")), True)
        b.goto(f"http://127.0.0.1:{WEB}/?v=mesh#/mesh")
        check("  still archived after a reload (the station keeps it)", bool(b.wait(f"{HEADS}.some((h) => h.includes('Archived 1'))")), True)
        b.js("document.querySelector('#mesh-tabs [data-fold]').click()")
        check("  the fold opens", b.js(NAMES)[-1], "HomeNet")
        b.js(FIND.format(json.dumps("longf")))
        check("search: only what matches", b.js(NAMES), ["LongFast"])
        b.js(FIND.format(json.dumps("opening the gate")))
        check("  matches a message's text too", b.js(NAMES), ["HomeNet"])
        b.js(FIND.format(json.dumps("")))
        b.js(f"{tab('HomeNet')}.click()")
        b.js("document.querySelector('#mesh-chat-head [data-head=menu]').click()")
        b.js("document.querySelector('.chat-menu [data-act=unarchive]').click()")
        check("the chat head's ⋯: out of Archived", bool(b.wait(f"!{HEADS}.some((h) => h.startsWith('Archived'))")), True)
        GATE = "[...document.querySelectorAll('#mesh-list .mesh-msg')].find((r) => r.textContent.includes('opening the gate'))"
        b.wait(GATE)
        b.js(f"{GATE}.querySelector('[data-act=delete]').click()")
        check("delete a message: gone from the list", bool(b.wait(f"!{GATE}")), True)
        check("  a toast offers Undo", bool(b.wait("document.querySelector('.toast .toast-act')?.textContent === 'Undo'")), True)
        b.js("document.querySelector('.toast .toast-act').click()")
        check("  Undo brings it back", bool(b.wait(f"!!{GATE}")), True)
        b.js("window.confirm = (q) => { window.asked = q; return true; }")
        b.js("document.querySelector('#mesh-tabs [data-menu=\"d!0000beef\"]').click()")
        b.js("document.querySelector('.chat-menu [data-act=delete]').click()")
        check("delete a DM conversation: it asks, saying the mesh keeps it", "cannot unsend" in (b.js("window.asked") or ""), True)
        check("  the conversation is gone from the rail", bool(b.wait(f"!{conv('!0000beef')}")), True)
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
