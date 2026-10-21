"""M1: the Mesh panel's server side, against tests/fake_mesh_bridge.py.

Runs server.py --sim with the fake bridge as the Meshtastic node and checks:
  - the page is told mesh is on, and gets the node's info, channels, nodes
    and an incoming message
  - the channel key check comes through as key_ok only; the key itself never
    reaches a page; a wrong key shows as key_ok false
  - a channel message and a direct message reach the bridge as sent, and each
    goes sending -> sent -> delivered; a NAK and a failed write show as failed
  - empty, too-long, unknown-channel and bad-node-id sends are refused with a
    warning and never reach the bridge
  - the node going away shows as disconnected, the bridge is started again
    and a send waiting on it fails rather than hanging
  - message history (with ACK states) survives a server restart, in a file
    only the user can read
  - the node's settings reach the page; a change is checked against the
    schema (unknown section or field, wrong type, out of range, bad choice,
    empty name) and refused with a warning before the bridge sees it; a good
    one is written and the page gets the new values; a node refusal warns
  - a favorite reaches the bridge and the node list shows the star
  - a traceroute goes running -> done with the route and SNRs, a second one
    while it runs or within 30 s is refused, a NAK ends it as failed, and the
    result is in a later page's snapshot; packets heard show as rx activity
  - without --mesh-port there is no mesh and mesh_send is refused

    python3 tests/mesh_test.py
"""
import asyncio
import base64
import json
import os
import random
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
from fake_mesh_bridge import FAKE_KEY  # noqa: E402

WEB = 18000 + random.randrange(1000)
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


def start(*extra, mesh=True, key=GOOD_KEY):
    env = dict(os.environ, FAKE_MESH_LOG=CMD_LOG, FAKE_MESH_STARTS=STARTS)
    args = [sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB),
            "--rigctl-port", "0", "--addons-state", os.path.join(TMP, "addons.json"), *extra]
    if mesh:
        args += ["--mesh-port", "/dev/null", "--mesh-python", sys.executable,
                 "--mesh-bridge", "tests/fake_mesh_bridge.py",
                 "--mesh-key", f"HomeNet={key}", "--mesh-log", MESH_LOG]
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


def ack_of(text, state):
    def pred(e):
        r = e.get("update") if e.get("t") == "mesh" else None
        return r is not None and r.get("text") == text and r.get("ack") == state
    return pred


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
    page.send({"t": "mesh_send", "text": "fail", "ch": 1})
    check("write error: failed", bool(page.wait(ack_of("fail", "failed"))), True)

    before = len(commands())
    for name, msg in [("empty text", {"text": "   ", "ch": 1}),
                      ("201 bytes", {"text": "x" * 201, "ch": 1}),
                      ("100 two-byte chars (200 B ok) + 1", {"text": "é" * 100 + "x", "ch": 1}),
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

    alltext = "".join(page.raw)
    check("the channel key never reaches the page", FAKE_KEY in alltext, False)

    # The node goes away: disconnected, a fresh bridge, connected again.
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
    page.close()
finally:
    out = stop(server)

mode = stat.S_IMODE(os.stat(MESH_LOG).st_mode) if os.path.exists(MESH_LOG) else None
check("history file is mode 600", oct(mode) if mode is not None else None, "0o600")
with open(MESH_LOG) as f:
    check("the channel key is not in the history file", FAKE_KEY in f.read(), False)
check("the channel key is not in the server log", FAKE_KEY in out, False)

server = start(key=BAD_KEY)
try:
    page = Page()
    hist = {r["text"]: r for r in page.hello["mesh"]["messages"]}
    check("restart: history kept (incoming)", "gate is closed" in hist, True)
    check("restart: our message kept as delivered", hist.get("hello ranch", {}).get("ack"), "delivered")
    check("restart: NAK kept as failed", hist.get("nak", {}).get("ack"), "failed")
    chans = {c["name"]: c for c in (page.latest("info", 3) or {}).get("channels", [])}
    check("wrong key: key_ok false", chans.get("HomeNet", {}).get("key_ok"), False)
    page.close()
finally:
    stop(server)

server = start(mesh=False)
try:
    page = Page()
    check("no --mesh-port: config.mesh false", page.hello["config"].get("mesh"), False)
    n = len(page.events)
    page.send({"t": "mesh_send", "text": "hi", "ch": 0})
    check("no --mesh-port: mesh_send refused", page.warned(n), True)
    n = len(page.events)
    page.send({"t": "mesh_config", "section": "lora", "values": {"hop_limit": 4}})
    check("no --mesh-port: mesh_config refused", page.warned(n), True)
    n = len(page.events)
    page.send({"t": "mesh_trace", "to": "!0000beef"})
    check("no --mesh-port: mesh_trace refused", page.warned(n), True)
    page.close()
finally:
    stop(server)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
