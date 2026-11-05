"""MeshCore add-on (addons/meshcore.py), against tests/fake_meshcore_bridge.py.

  - listed as an add-on with a panel; the node connects and the snapshot has
    its info, channels, contacts, battery and the message it heard
  - a channel send reaches the node and settles "sent"; a direct message to
    a contact settles "delivered" on its ACK; a failed send says why
  - refused before the node sees them: empty, too long, a channel the node
    doesn't have, a key that isn't a contact
  - advert: the first is sent, a second inside the gap is refused
  - the node going away fails what was in flight, and the bridge restarts
  - switch off stops the bridge (the port is free); a send while off is
    refused; switch on starts a fresh bridge
  - messages survive a server restart through the log, mode 600

    python3 tests/meshcore_addon_test.py
"""
import asyncio
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
from testkit import free_port  # noqa: E402

WEB = free_port()
TMP = tempfile.mkdtemp(prefix="mcaddon-")
STATE = os.path.join(TMP, "addons-state.json")
CFG = os.path.join(TMP, "addons.json")
CMD_LOG = os.path.join(TMP, "commands.jsonl")
STARTS = os.path.join(TMP, "starts")
MSG_LOG = os.path.join(TMP, "meshcore.jsonl")
MARK = os.path.join(TMP, "port-marker")
ALPHA = "a1" * 32
fails = 0

with open(CFG, "w") as f:
    json.dump({"meshcore": {"port": MARK, "python": sys.executable,
                            "bridge": os.path.join(ROOT, "tests", "fake_meshcore_bridge.py"),
                            "log": MSG_LOG}}, f)


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


class Page:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.events = []
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.run(self._open())

    def run(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _open(self):
        self.sess = aiohttp.ClientSession()
        self.ws = await self.sess.ws_connect(f"http://127.0.0.1:{WEB}/ws")
        self.hello = json.loads((await self.ws.receive()).data)
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                self.events.append(json.loads(msg.data))

    def send(self, m):
        self.run(self.ws.send_str(json.dumps(m)))

    def do(self, verb, **extra):
        self.send({"t": "addon", "addon": "meshcore", "do": verb, **extra})

    def wait(self, pred, timeout=10, since=0):
        end = time.time() + timeout
        while time.time() < end:
            for e in self.events[since:]:
                if pred(e):
                    return e
            time.sleep(0.05)
        return None

    def close(self):
        async def _c():
            await self.ws.close()
            await self.sess.close()
        self.run(_c())


def start():
    env = dict(os.environ, FAKE_MC_LOG=CMD_LOG, FAKE_MC_STARTS=STARTS)
    p = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB),
                          "--rigctl-port", "0", "--addons", CFG, "--addons-state", STATE],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(150):
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
        p.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()


def mc(e):
    return e.get("t") == "addon" and e.get("addon") == "meshcore"


def bridge_running():
    r = subprocess.run(["pgrep", "-f", f"fake_meshcore_bridge.py {MARK}"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def lines(path):
    try:
        with open(path) as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def commands(text):
    try:
        with open(CMD_LOG) as f:
            return [c for c in map(json.loads, filter(str.strip, f)) if c.get("text") == text]
    except OSError:
        return []


def settled(text, state, since=0):
    return lambda e: mc(e) and (e.get("update") or {}).get("text") == text and e["update"].get("ack") == state


def warned(words, since=0):
    return lambda e: e.get("t") == "notice" and e.get("level") == "warn" and words in e.get("msg", "")


def snapshot(page):
    return (page.hello.get("addons") or {}).get("meshcore") or {}


def wait_connected(page, since=0):
    if (snapshot(page).get("status") or {}).get("connected"):
        return True
    return page.wait(lambda e: mc(e) and (e.get("status") or {}).get("connected") is True, since=since) is not None


server = start()
try:
    page = Page()
    names = [a["name"] for a in page.hello["config"].get("addons", [])]
    check("listed as an add-on with a panel", "meshcore" in names, True)
    check("the node connects", wait_connected(page), True)
    page.close()
    page = Page()                                  # a page opened now: everything in its snapshot
    s = snapshot(page)
    check("snapshot: the node's name", ((s.get("info") or {}).get("me") or {}).get("name"), "TestNode")
    check("snapshot: radio frequency", ((s.get("info") or {}).get("radio") or {}).get("freq"), 910.525)
    check("snapshot: channels", [c["name"] for c in s.get("channels") or []], ["Public"])
    check("snapshot: contacts, newest first", [c["name"] for c in s.get("contacts") or []], ["Alpha", "Beta Rpt"])
    check("snapshot: battery", s.get("battery_mv"), 4100)
    check("snapshot: the message it heard", [(m["name"], m["text"]) for m in s.get("messages") or []],
          [("Alpha", "hello mesh")])

    n = len(page.events)
    page.do("send", text="hi all", ch=0)
    check("channel send settles sent", page.wait(settled("hi all", "sent"), since=n) is not None, True)
    check("  and reached the node on channel 0", [c.get("ch") for c in commands("hi all")], [0])

    page.do("send", text="hi alpha", to=ALPHA[:12])
    check("direct message settles delivered on its ACK",
          page.wait(settled("hi alpha", "delivered"), since=n) is not None, True)
    check("  sent to the contact's key", [c.get("to") for c in commands("hi alpha")], [ALPHA[:12]])

    page.do("send", text="fail", ch=0)
    e = page.wait(settled("fail", "failed"), since=n)
    check("a failed send says why", (e or {}).get("update", {}).get("why"), "radio busy")

    for label, extra, words in [
            ("empty text is refused", {"text": "  ", "ch": 0}, "Nothing to send"),
            ("too long is refused", {"text": "x" * 141, "ch": 0}, "Too long"),
            ("a channel the node lacks is refused", {"text": "nope", "ch": 5}, "no channel"),
            ("a key that isn't a contact is refused", {"text": "nope", "to": "ff" * 6}, "not one of"),
            ("not a key at all is refused", {"text": "nope", "to": "zz"}, "Not a node key")]:
        m = len(page.events)
        page.do("send", **extra)
        check(label, page.wait(warned(words), since=m, timeout=3) is not None, True)
    check("  and none of them reached the node", commands("nope"), [])

    m = len(page.events)
    page.do("advert", flood=False)
    check("advert: sent", page.wait(lambda e: e.get("t") == "notice" and "advert sent" in e.get("msg", ""),
                                    since=m) is not None, True)
    page.do("advert", flood=False)
    check("  a second inside the gap is refused", page.wait(warned("One advert every"), since=m, timeout=3)
          is not None, True)

    before = lines(STARTS)
    m = len(page.events)
    page.do("send", text="die", ch=0)
    check("the node going away fails the send in flight",
          page.wait(settled("die", "failed"), since=m) is not None, True)
    check("  and the bridge restarts", page.wait(lambda e: mc(e) and (e.get("status") or {}).get("connected")
                                                 is True, since=m, timeout=20) is not None, True)
    check("  (one more start)", lines(STARTS) - before, 1)

    m = len(page.events)
    page.do("power", on=False)
    off = page.wait(lambda e: mc(e) and e.get("on") is False, since=m)
    check("switch off: the page is told", off is not None, True)
    end = time.time() + 5
    while time.time() < end and bridge_running():
        time.sleep(0.1)
    check("  the bridge is stopped (the port is free)", bridge_running(), False)
    m = len(page.events)
    page.do("send", text="while off", ch=0)
    check("a send while off is refused", page.wait(warned("switched off"), since=m, timeout=3) is not None, True)
    check("  and never reaches the node", commands("while off"), [])
    before = lines(STARTS)
    m = len(page.events)
    page.do("power", on=True)
    check("switch on: connects again", page.wait(lambda e: mc(e) and ((e.get("snapshot") or {}).get("status") or
                                                                       e.get("status") or {}).get("connected"),
                                                 since=m) is not None, True)
    check("  a fresh bridge started", lines(STARTS) - before, 1)
    page.close()

    stop(server)
    server = start()
    page = Page()
    texts = [x["text"] for x in snapshot(page).get("messages") or []]
    check("after a restart: our messages are back", all(t in texts for t in ("hi all", "hi alpha")), True)
    check("  with their final state", next((x["ack"] for x in snapshot(page)["messages"] if x["text"] == "hi alpha"),
                                         None), "delivered")
    check("the log is private (mode 600)", stat.S_IMODE(os.stat(MSG_LOG).st_mode), 0o600)
    page.close()
finally:
    stop(server)

print("PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
