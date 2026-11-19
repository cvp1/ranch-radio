"""The Mesh panel's server side, against tests/fake_mesh_bridge.py.

Runs server.py --sim with the fake bridge as the Meshtastic node and checks:
  - the page gets the node's info, channels, nodes and an incoming message
  - the channel key check comes through as key_ok only; the key never reaches
    a page, the history file or the log; a wrong key shows as key_ok false
  - channel and direct sends go sending -> sent -> delivered; a NAK and a
    failed write show as failed; a send the node never reports on becomes
    unknown after MESH_ACK_WAIT_S; bad sends are refused before the bridge
  - the node going away: disconnected, the bridge restarts, the send fails
  - message history (with ACK states) survives a restart, mode 600
  - settings are schema-checked before the bridge; a good change is written
    and echoed; a node refusal warns
  - favorites; traceroute (one at a time, 30 s apart, result in later snapshots)
  - replies and reactions: reply_id / emoji in and out, checked against a held
    message, kept across a restart
  - requests (position, telemetry, ...): one per node at a time, each kind 60 s
    apart, answered or not, in later snapshots; ignore / remove / reboot;
    NeighborInfo and the node's own counters reach the page
  - who hears us (hears_us): a node whose NeighborInfo lists us, a
    traceroute's first hop, a node that ACKed our DM; NeighborInfo and
    traceroutes kept across a bridge restart and a server restart, mode 600
  - conversations (mesh_conv): archive and read reach the page and survive a
    restart; a bad action or a non-string message type is refused; a delete
    hands back a ref, Undo brings it back once, a delete survives a restart;
    the state and trash files are mode 600
  - every name the node has given is kept (names) after the node drops it,
    and across a server restart, mode 600
  - a node list longer than asyncio's default 64 KiB line still arrives
  - the station asks its own node for its neighbours by itself (only that
    kind may go to this node)
  - the Health view: rx carries hops, hop limit and relay; the counts and
    findings (chatty node, hop limit, main relay) reach the page and a later
    page's snapshot
  - without the Mesh add-on, mesh commands are refused

    python3 tests/mesh_test.py
"""
import asyncio
import base64
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
from fake_mesh_bridge import FAKE_KEY  # noqa: E402
import testkit  # noqa: E402

WEB = testkit.free_port()
fails = 0
TMP = tempfile.mkdtemp(prefix="meshtest-")
GOOD_KEY = os.path.join(TMP, "good_key")
BAD_KEY = os.path.join(TMP, "bad_key")
MESH_LOG = os.path.join(TMP, "state", "mesh.jsonl")
CMD_LOG = os.path.join(TMP, "commands.jsonl")
STARTS = os.path.join(TMP, "starts")
with open(GOOD_KEY, "w") as f:
    f.write(FAKE_KEY + "\n")
with open(BAD_KEY, "w") as f:
    f.write(base64.b64encode(bytes(32)).decode())


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} got {got!r}" + ("" if ok else f" want {want!r}"))


class Page:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.events = []
        self.raw = []
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.run(self._open())

    def run(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _open(self):
        self.sess = aiohttp.ClientSession()
        self.ws = await self.sess.ws_connect(f"http://127.0.0.1:{WEB}/ws")
        first = (await self.ws.receive()).data
        self.raw.append(first)
        self.hello = json.loads(first)
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                self.raw.append(msg.data)
                m = json.loads(msg.data)
                if m.get("t") in ("mesh", "notice"):
                    self.events.append(m)

    def send(self, m):
        self.run(self.ws.send_str(json.dumps(m)))

    def wait(self, pred, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            for e in list(self.events):
                if pred(e):
                    return e
            time.sleep(0.05)
        return None

    def latest(self, field, timeout=10):
        """The newest mesh `field` (info, nodes): from an event, or from the
        hello snapshot when the bridge reported before this page connected."""
        e = self.wait(lambda e: field in e, timeout)
        return e[field] if e else (self.hello.get("mesh") or {}).get(field)

    def warned(self, n_before, timeout=2):
        end = time.time() + timeout
        while time.time() < end:
            if any(e.get("t") == "notice" and e.get("level") == "warn" for e in self.events[n_before:]):
                return True
            time.sleep(0.05)
        return False

    def close(self):
        async def _c():
            await self.ws.close()
            await self.sess.close()
        self.run(_c())


def start(*extra, mesh=True, key=GOOD_KEY, env_extra=None):
    env = dict(os.environ, FAKE_MESH_LOG=CMD_LOG, FAKE_MESH_STARTS=STARTS, MESH_ACK_WAIT_S="1.5",
               MESH_RESEND_S="0.3,0.3,0.3", MESH_PART_GAP_S="0.2",
               MESH_SELF_NEIGHBORS_FIRST_S="3600")    # the self-ask only where a run wants it
    env.update(env_extra or {})
    args = [sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB),
            "--rigctl-port", "0", "--addons-state", os.path.join(TMP, "addons.json"), *extra]
    if mesh:
        # Mesh is an add-on (addons/mesh.py); the radio's server takes add-ons from a file.
        adds = os.path.join(TMP, f"addons-{len(os.listdir(TMP))}.json")
        with open(adds, "w") as f:
            json.dump({"mesh": {"port": "/dev/null", "python": sys.executable, "bridge": "tests/fake_mesh_bridge.py",
                                "keys": [f"HomeNet={key}"], "log": MESH_LOG}}, f)
        args += ["--addons", adds]
    p = subprocess.Popen(args, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", WEB), timeout=0.2).close()
            return p
        except OSError:
            time.sleep(0.1)
    p.kill()
    print(p.stdout.read())
    raise SystemExit("server did not start")


def stop(p):
    p.terminate()
    try:
        out, _ = p.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
    return out


def commands():
    try:
        with open(CMD_LOG) as f:
            return [json.loads(l) for l in f if l.strip()]
    except OSError:
        return []


def hears_of(hears):
    """hears list -> {(id, how): snr}"""
    return {(h["id"], h["how"]): h["snr"] for h in hears or []}


def mmsg(e):
    """A mesh event's message record (a notice's "msg" is its text)."""
    m = e.get("msg") if e.get("t") == "mesh" else None
    return m if isinstance(m, dict) else {}


def ack_of(text, state):
    def pred(e):
        r = e.get("update") if e.get("t") == "mesh" else None
        return r is not None and r.get("text") == text and r.get("ack") == state
    return pred


# hears_us on its own: what counts, what doesn't
import mesh as mesh_mod  # noqa: E402
NOW = 1_000_000.0
ME_ID = "!b2a6fd78"
got = mesh_mod.hears_us(ME_ID, {
    "!0000cafe": {"t": NOW - 60, "neighbors": [{"id": ME_ID, "snr": 7.25}]},
    "!0000beef": {"t": NOW - 60, "neighbors": [{"id": "!0000cafe", "snr": 1.0}]},     # hears someone else
    "!000000aa": {"t": NOW - 8 * 86400, "neighbors": [{"id": ME_ID, "snr": 3.0}]},    # older than 7 days
}, {
    "!0000beef": {"to": "!0000beef", "state": "done", "done_t": NOW - 30, "route": ["!0000cafe"], "snr_towards": [6.0, 4.5]},
    "!0000d00d": {"to": "!0000d00d", "state": "done", "done_t": NOW - 20, "route": [], "snr_towards": [-3.5]},
    "!0000f00d": {"to": "!0000f00d", "state": "done", "done_t": NOW - 20, "route": ["?"], "snr_towards": [1.0, 2.0]},
    "!0000dead": {"to": "!0000dead", "state": "failed", "done_t": NOW - 10},
}, [
    {"mine": True, "dm": True, "to": "!0000beef", "ack": "delivered", "t": NOW - 5},
    {"mine": True, "dm": True, "to": "!0000abcd", "ack": "failed", "t": NOW - 5},
    {"mine": True, "dm": False, "to": None, "ack": "relayed", "t": NOW - 5},
], now=NOW)
check("hears_us: evidence, newest first", [(h["id"], h["how"], h["snr"]) for h in got],
      [("!0000beef", "dm", None), ("!0000d00d", "trace", -3.5), ("!0000cafe", "trace", 6.0),
       ("!0000cafe", "neighbor", 7.25)])
check("hears_us: no node id yet -> nothing", mesh_mod.hears_us(None, {"x": {"t": NOW}}, {}, [], now=NOW), [])

server = start()
try:
    page = Page()
    check("hello: config.mesh", page.hello["config"].get("mesh"), True)
    m = page.wait(lambda e: (e.get("msg") or {}).get("text") == "gate is closed")
    check("incoming channel message reaches the page", bool(m), True)
    check("activity: rx from !0000beef", bool(page.wait(lambda e: (e.get("rx") or {}).get("from") == "!0000beef", 3)), True)
    check("  on channel 1, from !0000beef", m and (m["msg"]["ch"], m["msg"]["from"]), (1, "!0000beef"))
    chans = {c["name"]: c for c in (page.latest("info", 3) or {}).get("channels", [])}
    check("channels LongFast and HomeNet", sorted(chans), ["HomeNet", "LongFast"])
    check("HomeNet key matches (key_ok)", chans.get("HomeNet", {}).get("key_ok"), True)
    check("node list: 4 nodes", len(page.latest("nodes", 3) or []), 4)

    page.send({"t": "mesh_send", "text": "hello ranch", "ch": 1})
    check("channel send: shown at once as sending", bool(page.wait(
        lambda e: (e.get("msg") or {}).get("text") == "hello ranch" and e["msg"]["ack"] == "sending")), True)
    check("channel send: delivered", bool(page.wait(ack_of("hello ranch", "delivered"))), True)
    sent = [c for c in commands() if c.get("text") == "hello ranch"]
    check("  bridge got it on channel 1, broadcast", sent and (sent[0]["ch"], sent[0]["to"]), (1, None))

    page.send({"t": "mesh_send", "text": "hi base", "ch": 0, "to": "!0000beef"})
    check("direct message: delivered", bool(page.wait(ack_of("hi base", "delivered"))), True)
    dm = [c for c in commands() if c.get("text") == "hi base"]
    check("  bridge got it addressed to !0000beef", dm and dm[0]["to"], "!0000beef")

    page.send({"t": "mesh_send", "text": "nak", "ch": 1})
    e = page.wait(ack_of("nak", "failed"))
    check("NAK: failed with the reason", e and e["update"].get("why"), "MAX_RETRANSMIT")
    check("  and a time it goes again by itself",
          bool(page.wait(lambda e: ack_of("nak", "failed")(e) and e["update"].get("retry_at"))), True)
    e = page.wait(lambda e: ack_of("nak", "failed")(e) and e["update"].get("tries") == 3
                  and not e["update"].get("retry_at"))
    check("  sent again 3 times, then left failed", bool(e), True)
    check("  the bridge got it 4 times in all", len([c for c in commands() if c.get("text") == "nak"]), 4)

    long_text = " ".join(f"word{i:03d}" for i in range(50))     # 399 bytes: 3 parts
    page.send({"t": "mesh_send", "text": long_text, "ch": 1})
    check("long text: the last part delivered", bool(page.wait(
        lambda e: (e.get("update") or {}).get("part") == [3, 3] and e["update"].get("ack") == "delivered")), True)
    got = [c["text"] for c in commands() if c.get("text", "").startswith("[") and "word0" in c.get("text", "")]
    check("  the bridge got 3 parts, in order", [g[:6] for g in got], ["[1/3] ", "[2/3] ", "[3/3] "])
    check("  nothing lost", " ".join(g[6:] for g in got) == long_text, True)
    page.send({"t": "mesh_send", "text": "fail", "ch": 1})
    check("write error: failed", bool(page.wait(ack_of("fail", "failed"))), True)
    page.send({"t": "mesh_send", "text": "silent", "ch": 1})
    check("sent, then nothing from the node", bool(page.wait(ack_of("silent", "sent"))), True)
    e = page.wait(ack_of("silent", "unknown"), 5)
    check("  after MESH_ACK_WAIT_S: unknown, saying why", e and e["update"].get("why"),
          "no ACK or failure from the node in 1.5 s")
    time.sleep(2)
    check("  a delivered message is not touched by the timer",
          any(ack_of("hello ranch", "unknown")(x) for x in page.events), False)

    before = len(commands())
    for name, msg in [("empty text", {"text": "   ", "ch": 1}),
                      ("over 9 parts", {"text": "x" * (194 * 9 + 1), "ch": 1}),
                      ("a reply over 200 bytes", {"text": "é" * 100 + "x", "ch": 1, "reply_to": 1001}),
                      ("unknown channel 5", {"text": "hi", "ch": 5}),
                      ("bad node id", {"text": "hi", "ch": 0, "to": "beef"})]:
        n = len(page.events)
        page.send({"t": "mesh_send", **msg})
        check(f"refused with a warning: {name}", page.warned(n), True)
    time.sleep(0.3)
    check("  none of them reached the bridge", len(commands()), before)

    # Settings
    cfg = page.latest("config", 3) or {}
    check("settings: sections reach the page", [x["name"] for x in cfg.get("sections", [])],
          ["owner", "device", "lora"])
    check("settings: values reach the page", cfg.get("values", {}).get("lora", {}).get("hop_limit"), 3)
    before = len(commands())
    for name, sec, vals in [("unknown section", "security", {"private_key": "x"}),
                            ("unknown field", "device", {"serial_enabled": False}),
                            ("bool for a number", "lora", {"hop_limit": True}),
                            ("number for a bool", "lora", {"tx_enabled": 1}),
                            ("out of range", "lora", {"hop_limit": 8}),
                            ("not a whole number", "lora", {"hop_limit": 2.5}),
                            ("not a choice", "lora", {"region": "MARS"}),
                            ("empty owner name", "owner", {"long_name": "  "}),
                            ("short name over 4 bytes", "owner", {"short_name": "ABCDE"}),
                            ("nothing to save", "lora", {})]:
        n = len(page.events)
        page.send({"t": "mesh_config", "section": sec, "values": vals})
        check(f"settings refused with a warning: {name}", page.warned(n), True)
    time.sleep(0.3)
    check("  none of them reached the bridge", len(commands()), before)
    page.events.clear()
    page.send({"t": "mesh_config", "section": "lora", "values": {"hop_limit": 4, "region": "EU_868"}})
    e = page.wait(lambda e: (e.get("config") or {}).get("values", {}).get("lora", {}).get("hop_limit") == 4)
    check("settings saved: the page gets the new values", e and e["config"]["values"]["lora"]["region"], "EU_868")
    check("  and a 'saved' notice", bool(page.wait(lambda e: e.get("t") == "notice" and "saved" in e["msg"])), True)
    sets = [c for c in commands() if c.get("cmd") == "set"]
    check("  bridge got only that section's fields", sets and (sets[-1]["section"], sets[-1]["values"]),
          ("lora", {"hop_limit": 4, "region": "EU_868"}))
    n = len(page.events)
    page.send({"t": "mesh_config", "section": "lora", "values": {"hop_limit": 6}})
    check("node refuses a setting: warning", page.warned(n), True)
    page.events.clear()
    page.send({"t": "mesh_favorite", "to": "!0000beef", "fav": True})
    e = page.wait(lambda e: any(x["id"] == "!0000beef" and x["fav"] for x in e.get("nodes") or []))
    check("favorite: node list shows the star", bool(e), True)
    n = len(page.events)
    page.send({"t": "mesh_favorite", "to": "!b2a6fd78", "fav": True})
    check("favorite of this node itself is refused", page.warned(n), True)

    # Traceroute
    page.events.clear()
    page.send({"t": "mesh_trace", "to": "!0000beef"})
    check("traceroute: running", bool(page.wait(lambda e: (e.get("trace") or {}).get("state") == "running")), True)
    n = len(page.events)
    page.send({"t": "mesh_trace", "to": "!0000cafe"})
    check("  a second one while it runs is refused", page.warned(n), True)
    e = page.wait(lambda e: (e.get("trace") or {}).get("state") == "done")
    check("  done, via !0000cafe", e and (e["trace"]["route"], e["trace"]["snr_towards"]), (["!0000cafe"], [6.0, 4.5]))
    check("  route back", e and e["trace"]["route_back"], ["!0000cafe"])
    n = len(page.events)
    page.send({"t": "mesh_trace", "to": "!0000dead"})
    check("  another within 30 s is refused", page.warned(n), True)
    for bad in ("!b2a6fd78", "beef"):
        n = len(page.events)
        page.send({"t": "mesh_trace", "to": bad})
        check(f"  refused: {bad}", page.warned(n), True)
    late = Page()
    tr = {t["to"]: t for t in late.hello["mesh"]["traces"]}
    check("  a page opened later gets the result", tr.get("!0000beef", {}).get("state"), "done")
    late.close()

    # Replies and reactions
    snap = Page()                      # the fake sent these at start: they are in a new page's snapshot
    snap_nodes = {n["id"]: n for n in snap.hello["mesh"]["nodes"]}
    r = next((x for x in snap.hello["mesh"]["messages"] if x["text"] == "thanks, I saw it"), {})
    check("incoming reply carries reply_id", r.get("reply_id"), 1001)
    page.send({"t": "mesh_send", "text": "me too", "ch": 1, "reply_to": 1001})
    check("reply: delivered", bool(page.wait(ack_of("me too", "delivered"))), True)
    c = [c for c in commands() if c.get("text") == "me too"]
    check("  bridge got reply_to 1001", c and (c[0]["reply_to"], c[0]["emoji"]), (1001, False))
    page.send({"t": "mesh_send", "text": "👍", "ch": 1, "reply_to": 1001, "emoji": True})
    check("reaction: delivered", bool(page.wait(ack_of("👍", "delivered"))), True)
    c = [c for c in commands() if c.get("text") == "👍"]
    check("  bridge got it as an emoji reaction", c and (c[0]["reply_to"], c[0]["emoji"]), (1001, True))
    page.send({"t": "mesh_send", "text": "react to me", "ch": 1})
    e = page.wait(lambda e: mmsg(e).get("emoji") is True and not mmsg(e).get("mine"))
    check("incoming reaction: emoji with the reply_id of ours", bool(e and e["msg"]["reply_id"] >= 5000), True)
    before = len(commands())
    for name, msg in [("reply to a message not held", {"text": "hi", "ch": 1, "reply_to": 424242}),
                      ("reply_to not a number", {"text": "hi", "ch": 1, "reply_to": "1001"}),
                      ("reply_to true", {"text": "hi", "ch": 1, "reply_to": True}),
                      ("reaction without a message", {"text": "👍", "ch": 1, "emoji": True}),
                      ("reaction that is a sentence", {"text": "this is far too long to be one emoji", "ch": 1,
                                                       "reply_to": 1001, "emoji": True})]:
        n = len(page.events)
        page.send({"t": "mesh_send", **msg})
        check(f"refused with a warning: {name}", page.warned(n), True)
    time.sleep(0.3)
    check("  none of them reached the bridge", len(commands()), before)

    # Requests, node actions, reboot
    nodes = snap_nodes
    check("node fields: environment", (nodes.get("!0000beef", {}).get("env") or {}).get("temp"), 21.5)
    check("node fields: this node's counters", ((nodes.get("!b2a6fd78") or {}).get("stats") or {}).get("rx"), 900)
    nb = snap.hello["mesh"]["neighbors"]
    snap.close()
    check("NeighborInfo reaches the page", [x["id"] for x in nb.get("!0000beef", {}).get("neighbors", [])], ["!0000cafe"])
    h = hears_of(snap.hello["mesh"]["hears"])
    check("hears us: RDG's NeighborInfo lists us", h.get(("!0000cafe", "neighbor")), 7.25)
    check("  RDG is the traceroute's first hop", h.get(("!0000cafe", "trace")), 6.0)
    check("  BASE ACKed our DM", ("!0000beef", "dm") in h, True)
    check("  BASE is not said to hear us directly", ("!0000beef", "neighbor") in h or ("!0000beef", "trace") in h, False)
    page.events.clear()
    page.send({"t": "mesh_request", "to": "!0000beef", "kind": "position"})
    check("request: running", bool(page.wait(lambda e: (e.get("request") or {}).get("state") == "running")), True)
    n = len(page.events)
    page.send({"t": "mesh_request", "to": "!0000beef", "kind": "telemetry"})
    check("  a second to the same node while one runs is refused", page.warned(n), True)
    e = page.wait(lambda e: (e.get("request") or {}).get("state") == "done")
    check("  answered", e and (e["request"]["to"], e["request"]["kind"]), ("!0000beef", "position"))
    check("  the node list has the new position",
          bool(page.wait(lambda e: any(x["id"] == "!0000beef" and x["lat"] == 40.92 for x in e.get("nodes") or []))), True)
    n = len(page.events)
    page.send({"t": "mesh_request", "to": "!0000beef", "kind": "position"})
    check("  the same kind again within 60 s is refused", page.warned(n), True)
    page.send({"t": "mesh_request", "to": "!0000beef", "kind": "telemetry"})
    check("  another kind is fine", bool(page.wait(
        lambda e: (e.get("request") or {}).get("kind") == "telemetry" and e["request"]["state"] == "done")), True)
    page.send({"t": "mesh_request", "to": "!0000dead", "kind": "nodeinfo"})
    e = page.wait(lambda e: (e.get("request") or {}).get("state") == "failed")
    check("  no answer: failed with the reason", e and e["request"]["error"], "no reply")
    page.events.clear()
    page.send({"t": "mesh_request", "to": "!0000beef", "kind": "neighbors"})
    e = page.wait(lambda e: (e.get("request") or {}).get("kind") == "neighbors" and e["request"]["state"] == "done")
    check("ask for neighbours: answered", bool(e), True)
    e = page.wait(lambda e: e.get("hears") is not None and ("!0000beef", "neighbor") in hears_of(e["hears"]))
    check("  BASE's answer lists us: it hears us directly now", e and hears_of(e["hears"])[("!0000beef", "neighbor")], 4.0)
    n = len(page.events)
    page.send({"t": "mesh_request", "to": "!0000beef", "kind": "neighbors"})
    check("  again within 3 minutes is refused (the firmware answers once per 3 min)", page.warned(n), True)
    w = next((x for x in page.events[n:] if x.get("t") == "notice"), {})
    check("  saying so", "try again in 1" in str(w.get("msg")) or "try again in 2" in str(w.get("msg")), True)
    page.send({"t": "mesh_request", "to": "!0000dead", "kind": "neighbors"})
    e = page.wait(lambda e: (e.get("request") or {}).get("kind") == "neighbors" and e["request"]["state"] == "failed")
    check("  NO_RESPONSE explained", e and e["request"]["error"],
          "NO_RESPONSE: its NeighborInfo module is off, or it answered in the last 3 minutes")
    before = len(commands())
    for name, msg in [("this node", {"to": "!b2a6fd78", "kind": "position"}),
                      ("bad id", {"to": "beef", "kind": "position"}),
                      ("unknown kind", {"to": "!0000cafe", "kind": "admin"})]:
        n = len(page.events)
        page.send({"t": "mesh_request", **msg})
        check(f"request refused: {name}", page.warned(n), True)
    for name, msg in [("this node", {"to": "!b2a6fd78", "action": "ignore"}),
                      ("unknown action", {"to": "!0000cafe", "action": "factory_reset"})]:
        n = len(page.events)
        page.send({"t": "mesh_node", **msg})
        check(f"node action refused: {name}", page.warned(n), True)
    time.sleep(0.3)
    check("  none of them reached the bridge", len(commands()), before)
    late = Page()
    check("  a page opened later gets the requests",
          (late.hello["mesh"]["requests"].get("!0000beef") or {}).get("position", {}).get("state"), "done")
    late.close()
    page.send({"t": "mesh_node", "to": "!0000dead", "action": "ignore"})
    check("ignore: the node list marks it", bool(page.wait(
        lambda e: any(x["id"] == "!0000dead" and x.get("ignored") for x in e.get("nodes") or []))), True)
    page.send({"t": "mesh_node", "to": "!0000dead", "action": "remove"})
    check("remove: gone from the node list", bool(page.wait(
        lambda e: "nodes" in e and not any(x["id"] == "!0000dead" for x in e["nodes"]))), True)
    late = Page()
    check("  but its name is kept (names)", (late.hello["mesh"]["names"].get("!0000dead") or {}).get("short"), "TRK")
    late.close()
    # Conversations: state kept by the station, deletes local with Undo
    page.events.clear()
    page.send({"t": "mesh_conv", "action": "mute", "conv": "c1"})        # muted: new messages leave it archived
    page.send({"t": "mesh_conv", "action": "archive", "conv": "c1"})
    e = page.wait(lambda e: (e.get("convs") or {}).get("convs", {}).get("c1", {}).get("archived"))
    check("conv: archive reaches every page", bool(e), True)
    page.send({"t": "mesh_conv", "action": "read", "conv": "c1", "msg_t": 4102444800.0})
    check("conv: read moves read_t", bool(page.wait(lambda e: (e.get("convs") or {}).get("convs", {}).get("c1", {}).get("read_t") == 4102444800.0)), True)
    n = len(page.events)
    page.send({"t": "mesh_conv", "action": "explode", "conv": "c1"})
    check("conv: an unknown action is refused with a warning", page.warned(n), True)
    n = len(page.events)
    page.send({"t": 12.5, "action": "read"})
    check("a message whose type is not a string is refused", page.warned(n), True)
    page.send({"t": "mesh_send", "text": "delete me", "ch": 1})
    e = page.wait(ack_of("delete me", "delivered"))
    doomed = e and e["update"]["t"]
    page.events.clear()
    page.send({"t": "mesh_conv", "action": "delete", "ts": [doomed]})
    e = page.wait(lambda e: "deleted" in e)
    check("delete: the page gets a ref to undo", bool(e and e["deleted"].get("ref")), True)
    ref = e and e["deleted"]["ref"]
    h = page.wait(lambda e: "history" in e) or {}
    check("  the history no longer has it", any(x.get("text") == "delete me" for x in h.get("history", [])), False)
    page.events.clear()
    page.send({"t": "mesh_conv", "action": "undo", "ref": ref})
    h = page.wait(lambda e: "history" in e) or {}
    check("undo: it is back", any(x.get("text") == "delete me" for x in h.get("history", [])), True)
    n = len(page.events)
    page.send({"t": "mesh_conv", "action": "undo", "ref": ref})
    check("  undo twice: refused", page.warned(n), True)
    page.send({"t": "mesh_conv", "action": "delete", "ts": [doomed]})
    page.wait(lambda e: "deleted" in e)
    page.events.clear()
    page.send({"t": "mesh_reboot"})
    check("reboot: sent, and a notice", bool(page.wait(lambda e: e.get("t") == "notice" and "reboot saved" in e["msg"])), True)

    alltext = "".join(page.raw)
    check("the channel key never reaches the page", FAKE_KEY in alltext, False)

    # the node goes away
    page.send({"t": "mesh_send", "text": "die", "ch": 1})
    check("node lost: page shows disconnected",
          bool(page.wait(lambda e: (e.get("status") or {}).get("connected") is False)), True)
    check("  the send that killed it fails, not hangs", bool(page.wait(ack_of("die", "failed"))), True)
    n = len(page.events)
    page.send({"t": "mesh_send", "text": "while away", "ch": 1})
    check("  sending while disconnected is refused", page.warned(n), True)
    page.events.clear()
    check("  bridge started again: connected",
          bool(page.wait(lambda e: (e.get("status") or {}).get("connected") is True, timeout=12)), True)
    check("  bridge starts counted", len(open(STARTS).read().split()), 2)
    e = page.wait(lambda e: e.get("neighbors") is not None, 3) or {}
    check("  the new bridge's NeighborInfo (BASE only) is merged: RDG's report kept",
          sorted(e.get("neighbors") or {}), ["!0000beef", "!0000cafe"])
    e = page.wait(lambda e: e.get("hears") is not None and "neighbors" not in e, 3) or {}
    check("  hears us: RDG still", hears_of(e.get("hears")).get(("!0000cafe", "neighbor")), 7.25)
    page.close()
finally:
    out = stop(server)

mode = stat.S_IMODE(os.stat(MESH_LOG).st_mode) if os.path.exists(MESH_LOG) else None
check("history file is mode 600", oct(mode) if mode is not None else None, "0o600")
with open(MESH_LOG) as f:
    check("the channel key is not in the history file", FAKE_KEY in f.read(), False)
LINKS = os.path.splitext(MESH_LOG)[0] + "-links.json"
mode = stat.S_IMODE(os.stat(LINKS).st_mode) if os.path.exists(LINKS) else None
check("links file is mode 600", oct(mode) if mode is not None else None, "0o600")
NAMES = os.path.splitext(MESH_LOG)[0] + "-names.json"
mode = stat.S_IMODE(os.stat(NAMES).st_mode) if os.path.exists(NAMES) else None
check("names file is mode 600", oct(mode) if mode is not None else None, "0o600")
for suffix in ("-convs.json", "-trash.jsonl"):
    f = os.path.splitext(MESH_LOG)[0] + suffix
    mode = stat.S_IMODE(os.stat(f).st_mode) if os.path.exists(f) else None
    check(f"{suffix[1:]} is mode 600", oct(mode) if mode is not None else None, "0o600")
check("the channel key is not in the server log", FAKE_KEY in out, False)

server = start(key=BAD_KEY)
try:
    page = Page()
    hist = {r["text"]: r for r in page.hello["mesh"]["messages"] if r.get("mine") or r["text"] != "👍"}
    check("restart: history kept (incoming)", "gate is closed" in hist, True)
    check("restart: our message kept as delivered", hist.get("hello ranch", {}).get("ack"), "delivered")
    check("restart: NAK kept as failed", hist.get("nak", {}).get("ack"), "failed")
    check("restart: a reply keeps its reply_id", hist.get("me too", {}).get("reply_id"), 1001)
    # "hears" needs this node's id, which comes with the bridge's info: a page
    # that connects first gets an empty list in its hello, then a "hears" event.
    h = hears_of(page.hello["mesh"]["hears"])
    if not h:
        e = page.wait(lambda e: bool(e.get("hears")), 5) or {}
        h = hears_of(e.get("hears"))
    check("restart: RDG still hears us (from the links file)", h.get(("!0000cafe", "neighbor")), 7.25)
    check("restart: the traceroute's first hop kept", h.get(("!0000cafe", "trace")), 6.0)
    check("restart: BASE's NeighborInfo kept", [x["id"] for x in page.hello["mesh"]["neighbors"].get("!0000beef", {}).get("neighbors", [])], ["!0000cafe"])
    check("restart: a deleted message stays deleted", "delete me" in hist, False)
    check("restart: a muted conversation stays archived", {k: v for k, v in page.hello["mesh"]["convs"]["convs"].get("c1", {}).items() if k != "read_t"}, {"archived": True, "muted": True})
    check("restart: a removed node's name kept (from the names file)",
          (page.hello["mesh"]["names"].get("!0000dead") or {}).get("long"), "Old Truck")
    check("restart: a reaction keeps emoji", (hist.get("👍", {}).get("emoji"), hist.get("👍", {}).get("reply_id")), (True, 1001))
    chans = {c["name"]: c for c in (page.latest("info", 3) or {}).get("channels", [])}
    check("wrong key: key_ok false", chans.get("HomeNet", {}).get("key_ok"), False)
    page.close()
finally:
    stop(server)

# A node DB too big for asyncio's default 64 KiB line (seen live: 179 nodes, 81 KB)
server = start(env_extra={"FAKE_MESH_NODES_PAD": "400", "MESH_SELF_NEIGHBORS_FIRST_S": "0.5"})
try:
    page = Page()
    got = page.latest("nodes", 10) or []
    check("a node list over 64 KiB on one line arrives", len(got), 404)
    # Our own node's neighbours, asked of itself soon after it connects (no page asked)
    e = page.wait(lambda e: "!b2a6fd78" in (e.get("neighbors") or {}), 8)
    check("own node's NeighborInfo asked of itself, unprompted",
          e and [x["id"] for x in e["neighbors"]["!b2a6fd78"]["neighbors"]], ["!0000cafe", "!0000beef"])
    c = [x for x in commands() if x.get("cmd") == "request" and x.get("to") == "!b2a6fd78"]
    check("  one request, kind neighbors, to this node", [(x["kind"]) for x in c], ["neighbors"])
    n = len(page.events)
    page.send({"t": "mesh_request", "to": "!b2a6fd78", "kind": "position"})
    check("  any other kind to this node is still refused", page.warned(n), True)
    page.send({"t": "mesh_send", "text": "after a big list", "ch": 1})
    check("  and the bridge keeps going (a send is delivered)", bool(page.wait(ack_of("after a big list", "delivered"))), True)
    page.close()
finally:
    stop(server)

# The Health view's counts: a node chatty with position packets at hop limit 7,
# relayed by RDG (its number ends in fe), and BASE heard directly.
server = start(env_extra={"FAKE_MESH_BURST": "1", "MESH_HEALTH_EVERY_S": "0.3"})
try:
    page = Page()
    e = page.wait(lambda e: (e.get("rx") or {}).get("from") == "!0000dead")
    check("health: rx carries what the view counts", e and {k: e["rx"].get(k) for k in ("port", "hops", "hop_start", "relay")},
          {"port": "POSITION_APP", "hops": 2, "hop_start": 7, "relay": 0xfe})
    e = page.wait(lambda e: (e.get("health") or {}).get("total", 0) >= 36)
    h = e and e["health"]
    check("health: an update with every packet heard", h and h["total"], 36)
    kinds = {(f["kind"], f.get("node")) for f in (h or {}).get("findings", [])}
    check("  the chatty node", ("chatty", "!0000dead") in kinds, True)
    check("  its hop limit of 7 is not a finding", ("hop_limit", "!0000dead") in kinds, False)
    check("  RDG as the main relay", ("main_relay", "!0000cafe") in kinds, True)
    check("  status: attention", h and h["status"], "attention")
    check("  top talker first, with its port", h and (h["talkers"][0]["id"], h["talkers"][0]["top_port"]),
          ("!0000dead", "POSITION_APP"))
    check("  our channel load from the node list", h and h["util_now"] and h["util_now"][1], 12.5)
    late = Page()
    check("  a page opened later gets it in its snapshot",
          (late.hello["mesh"].get("health") or {}).get("total", 0) >= 36, True)
    check("  and the packet log", len(late.hello["mesh"].get("packets") or []) >= 36, True)
    late.close()
    page.close()
finally:
    stop(server)

server = start(mesh=False)
try:
    page = Page()
    check("no Mesh add-on: config.mesh false", page.hello["config"].get("mesh"), False)
    n = len(page.events)
    page.send({"t": "mesh_send", "text": "hi", "ch": 0})
    check("no Mesh add-on: mesh_send refused", page.warned(n), True)
    n = len(page.events)
    page.send({"t": "mesh_config", "section": "lora", "values": {"hop_limit": 4}})
    check("no Mesh add-on: mesh_config refused", page.warned(n), True)
    n = len(page.events)
    page.send({"t": "mesh_trace", "to": "!0000beef"})
    check("no Mesh add-on: mesh_trace refused", page.warned(n), True)
    page.close()
finally:
    stop(server)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
