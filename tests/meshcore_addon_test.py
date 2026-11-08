"""MeshCore add-on (addons/meshcore.py), against tests/fake_meshcore_bridge.py.

  - snapshot: info, channels, contacts, battery, the heard message
  - sends: channel "sent", direct "delivered" on ACK with tries and round
    trip, failures say why; bad sends refused before the node; advert gap
  - the node going away fails what was in flight and the bridge restarts;
    switch off frees the port and refuses sends; switch on starts afresh
  - messages survive a restart through the log, mode 600
  - settings: sections and presets; a valid write is read back; bad values
    are refused before the node; refusals and Wi-Fi reboots are reported;
    the Wi-Fi password and Bluetooth PIN reach the node and nowhere else
  - other nodes: stats, heard packets, pending nodes; status, telemetry,
    neighbours, path, trace, owner answers; refusals (companion, request
    gap, no route, no admin login); a node that doesn't answer says so
  - login: typed and saved passwords, never in a page message, snapshot,
    message log or server log; remote commands only after an admin login
  - favourite, add, reset path, remove; #hashtag channels join and leave;
    reboot

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
from testkit import free_port, wait_for  # noqa: E402

WEB = free_port()
TMP = tempfile.mkdtemp(prefix="mcaddon-")
STATE = os.path.join(TMP, "addons-state.json")
CFG = os.path.join(TMP, "addons.json")
CMD_LOG = os.path.join(TMP, "commands.jsonl")
STARTS = os.path.join(TMP, "starts")
MSG_LOG = os.path.join(TMP, "meshcore.jsonl")
MARK = os.path.join(TMP, "port-marker")
ALPHA = "a1" * 32
BETA = "b2" * 32
GAMMA = "d4" * 32
DELTA = "e5" * 32
PWD = "ranch-pa55-Wd!"
SAVED_PW, TYPED_PW = "Sav3d-Pw", "Typ3d-Wr0ng"
SAVED_FILE = os.path.join(TMP, "gamma_admin_password")
with open(SAVED_FILE, "w") as f:
    f.write(SAVED_PW + "\n")
fails = 0

with open(CFG, "w") as f:
    json.dump({"meshcore": {"port": MARK, "python": sys.executable,
                            "bridge": os.path.join(ROOT, "tests", "fake_meshcore_bridge.py"),
                            "log": MSG_LOG, "logins": {GAMMA[:12]: SAVED_FILE},
                            "presets": [{"label": "Local", "freq": 908.205, "bw": 62.5, "sf": 9, "cr": 8}]}}, f)


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


SERVER_OUT = []


def stop(p):
    p.terminate()
    try:
        out, _ = p.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
    SERVER_OUT.append(out or "")


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


def commands_cmd(cmd):
    try:
        with open(CMD_LOG) as f:
            return [c for c in map(json.loads, filter(str.strip, f)) if c.get("cmd") == cmd]
    except OSError:
        return []


def settled(text, state):
    return lambda e: mc(e) and (e.get("update") or {}).get("text") == text and e["update"].get("ack") == state


def warned(words):
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
    check("snapshot: contacts, newest first", [c["name"] for c in s.get("contacts") or []], ["Alpha", "Beta Rpt", "Gamma Rpt"])
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
    check("  the bridge is stopped (the port is free)", wait_for(lambda: not bridge_running(), 5), True)
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

    # --- settings
    page = Page()
    s = snapshot(page)
    check("settings: the node's sections", [x["name"] for x in (s.get("config") or {}).get("sections") or []],
          ["node", "radio", "wifi", "bluetooth"])
    check("  presets: the host's, then the built-in", [p["label"] for p in s.get("presets") or []],
          ["Local", "USA/Canada (recommended)"])

    def cfg_event(pred, since):
        return page.wait(lambda e: mc(e) and e.get("config") and pred(e["config"]), since=since)

    def saved(words, since):
        return page.wait(lambda e: e.get("t") == "notice" and words in e.get("msg", ""), since=since)

    m = len(page.events)
    page.do("set", section="radio", values={"freq": 908.205, "sf": 9, "cr": 8})
    check("a radio write: the page gets the read-back",
          cfg_event(lambda c: c["values"]["radio"]["sf"] == 9, m) is not None, True)
    check("  it reached the node", [c.get("values") for c in commands_cmd("set")][-1:],
          [{"freq": 908.205, "sf": 9, "cr": 8}])
    check("  with a saved notice", saved("Radio settings saved", m) is not None, True)

    before = len(commands_cmd("set"))
    for label, sec, vals, words in [
            ("SF 13 is refused", "radio", {"sf": 13}, "outside"),
            ("a bandwidth not on the list is refused", "radio", {"bw": 63}, "not one of"),
            ("a frequency out of range is refused", "radio", {"freq": 1000.0}, "outside"),
            ("SF as text is refused", "radio", {"sf": "9"}, "not a whole number"),
            ("a 32-byte name is refused", "node", {"name": "x" * 32}, "longer than"),
            ("a name over two lines is refused", "node", {"name": "a\nb"}, "one line"),
            ("an empty name is refused", "node", {"name": "  "}, "can't be empty"),
            ("share location as text is refused", "node", {"share_location": "yes"}, "not on/off"),
            ("a short Wi-Fi password is refused", "wifi", {"password": "short"}, "at least 8"),
            ("an unknown setting is refused", "radio", {"bogus": 1}, "has no setting"),
            ("an unknown section is refused", "lora", {"sf": 9}, "no settings section"),
            ("only an empty password: nothing to save", "wifi", {"password": ""}, "Nothing to save"),
            ("a 5-digit Bluetooth PIN is refused", "bluetooth", {"pin": 12345}, "6 digits"),
            ("a 7-digit Bluetooth PIN is refused", "bluetooth", {"pin": 1234567}, "outside")]:
        m = len(page.events)
        page.do("set", section=sec, values=vals)
        check(label, page.wait(warned(words), since=m, timeout=3) is not None, True)
    check("  and none of them reached the node", len(commands_cmd("set")), before)

    m = len(page.events)
    page.do("set", section="node", values={"name": "refuse"})
    check("a write the node refuses says why", saved("not saved: RuntimeError: the node refused", m) is not None, True)

    m = len(page.events)
    page.do("set", section="wifi", values={"enabled": True, "ssid": "RanchNet", "password": PWD})
    check("a Wi-Fi write says to reboot", saved("Reboot the node to apply", m) is not None, True)
    check("  the password reached the node (after the read-back)", [c["values"].get("password") for c in commands_cmd("set")][-1], PWD)
    check("  the read-back has the network, not the password",
          (cfg_event(lambda c: c["values"]["wifi"]["ssid"] == "RanchNet", m) or {}).get("config", {})
          .get("values", {}).get("wifi", {}).get("password"), "")
    check("  the password is in no page message", any(PWD in json.dumps(e) for e in page.events), False)

    m = len(page.events)
    page.do("set", section="bluetooth", values={"pin": 482913})
    check("a 6-digit PIN is written", saved("Bluetooth settings saved", m) is not None, True)
    check("  and is in no page message", any("482913" in json.dumps(e) for e in page.events), False)

    # --- other nodes
    page.close()
    page = Page()
    s = snapshot(page)
    check("snapshot: the node's radio stats", (s.get("stats") or {}).get("noise_floor"), -118)
    check("  a packet heard on the air, via Gamma", ((s.get("air") or {}).get("last") or {}).get("last"), "d4")
    check("  the node heard but not added", [c["name"] for c in s.get("pending") or []], ["Delta"])
    check("  which repeaters have a saved login (prefixes only)", s.get("logins"), [GAMMA[:12]])
    check("  and not the saved password", SAVED_PW in json.dumps(page.hello), False)
    gap = lambda: time.sleep(2.1)        # one request on the air every 2 s

    def result_of(kind, key, since):
        return page.wait(lambda e: mc(e) and (e.get("result") or {}).get("kind") == kind
                         and (e["result"].get("key") or "").startswith(key[:12]), since=since)

    for kind, probe in [("status", lambda d: d["noise_floor"]), ("telemetry", lambda d: d["lpp"][0]["type"]),
                        ("neighbours", lambda d: d["neighbours"][0]["snr"]), ("path", lambda d: d["out"]),
                        ("trace", lambda d: d["hops"][0]["snr"]), ("owner", lambda d: d["owner"])]:
        m = len(page.events)
        page.do(kind, key=GAMMA[:12])
        r = (result_of(kind, GAMMA, m) or {}).get("result") or {}
        want = {"status": -117, "telemetry": "voltage", "neighbours": 6.25, "path": ["d4"], "trace": 8.0,
                "owner": "N0CALL"}[kind]
        check(f"{kind}: the answer reaches the page", r.get("ok") and probe(r.get("data") or {}), want)
        gap()
    m = len(page.events)
    page.do("status", key=BETA[:12])
    r = (result_of("status", BETA, m) or {}).get("result") or {}
    check("a node that doesn't answer says so", (r.get("ok"), "no answer" in (r.get("error") or "")), (False, True))
    check("  with a notice", page.wait(warned("status failed: no answer"), since=m) is not None, True)
    page.close()
    page = Page()
    check("answers are in a later page's snapshot",
          ((snapshot(page).get("results") or {}).get(GAMMA) or {}).get("status", {}).get("ok"), True)
    gap()
    before = len(commands_cmd("status"))
    for label, verb, extra, words in [
            ("status from a companion is refused", "status", {"key": ALPHA[:12]}, "Only a repeater"),
            ("a key that isn't a contact is refused", "status", {"key": "ff" * 6}, "not one of"),
            ("a remote command before an admin login is refused", "cli", {"key": GAMMA[:12], "text": "ver"},
             "Log in as admin first"),
            ("setting the clock before an admin login is refused", "clock", {"key": GAMMA[:12]},
             "Log in as admin first"),
            ("a 16-character password is refused", "login", {"key": BETA[:12], "password": "x" * 16},
             "at most 15"),
            ("a login with no password and none saved is refused", "login", {"key": BETA[:12]},
             "none is saved")]:
        m = len(page.events)
        page.do(verb, **extra)
        check(label, page.wait(warned(words), since=m, timeout=3) is not None, True)
    check("  and none of them reached the node", len(commands_cmd("status")) + len(commands_cmd("cli"))
          + len(commands_cmd("clock")), before)
    m = len(page.events)
    page.do("status", key=GAMMA[:12])
    result_of("status", GAMMA, m)
    page.do("telemetry", key=GAMMA[:12])
    check("a second request inside the gap is refused", page.wait(warned("One request every"), since=m, timeout=3)
          is not None, True)
    gap()
    m = len(page.events)
    page.do("reset_path", key=GAMMA[:12])          # nothing on the air: no gap
    page.wait(lambda e: mc(e) and e.get("contacts") and next(c for c in e["contacts"] if c["key"] == GAMMA)["hops"]
              is None, since=m)
    m = len(page.events)
    page.do("trace", key=GAMMA[:12])
    check("a trace with no route known is refused", page.wait(warned("find the path first"), since=m, timeout=3)
          is not None, True)

    m = len(page.events)
    page.do("login", key=GAMMA[:12], password=TYPED_PW)
    r = (result_of("login", GAMMA, m) or {}).get("result") or {}
    check("login: a wrong password fails", (r.get("ok"), r.get("error")), (False, "wrong password"))
    check("  the typed password reached the node", any(c.get("password") == TYPED_PW for c in commands_cmd("login")),
          True)
    gap()
    m = len(page.events)
    page.do("login", key=GAMMA[:12])
    r = (result_of("login", GAMMA, m) or {}).get("result") or {}
    check("  the saved password logs in as admin", (r.get("ok"), (r.get("data") or {}).get("admin")), (True, True))
    check("  (it was read from its file)", any(c.get("password") == SAVED_PW for c in commands_cmd("login")), True)
    check("  the page is told it is logged in",
          page.wait(lambda e: mc(e) and ((e.get("logged_in") or {}).get(GAMMA) or {}).get("admin"), since=m)
          is not None, True)
    check("  no password is in a page message",
          any(SAVED_PW in json.dumps(e) or TYPED_PW in json.dumps(e) for e in page.events), False)
    gap()
    m = len(page.events)
    page.do("cli", key=GAMMA[:12], text="get radio")
    check("a remote command: shown as sent",
          page.wait(lambda e: mc(e) and (e.get("cli") or {}).get("way") == "out"
                    and e["cli"]["text"] == "get radio", since=m) is not None, True)
    check("  and the repeater's reply reaches the page",
          page.wait(lambda e: mc(e) and (e.get("cli") or {}).get("way") == "in"
                    and e["cli"]["text"] == "echo: get radio" and e["cli"]["key"] == GAMMA, since=m) is not None, True)
    gap()
    m = len(page.events)
    page.do("clock", key=GAMMA[:12])
    check("set clock: the repeater confirms", page.wait(lambda e: mc(e) and (e.get("cli") or {}).get("text")
                                                        == "OK - clock set", since=m) is not None, True)
    m = len(page.events)
    page.do("cli", key=GAMMA[:12], text="a\nb")
    check("a two-line remote command is refused", page.wait(warned("one line"), since=m, timeout=3) is not None, True)

    m = len(page.events)
    page.do("fav", key=ALPHA[:12], fav=True)
    check("favourite: the contact list follows",
          page.wait(lambda e: mc(e) and e.get("contacts") and next(c for c in e["contacts"] if c["key"] == ALPHA)["fav"],
                    since=m) is not None, True)
    m = len(page.events)
    page.do("add", key=DELTA[:12])
    check("a pending node is added",
          page.wait(lambda e: mc(e) and e.get("contacts") and any(c["key"] == DELTA for c in e["contacts"]), since=m)
          is not None, True)
    check("  and leaves the pending list", page.wait(lambda e: mc(e) and e.get("pending") == [], since=m) is not None,
          True)
    m = len(page.events)
    page.do("remove", key=DELTA[:12])
    check("remove: the contact goes",
          page.wait(lambda e: mc(e) and e.get("contacts") is not None and not any(c["key"] == DELTA for c in e["contacts"]),
                    since=m) is not None, True)
    m = len(page.events)
    page.do("send", text="tries", to=ALPHA[:12])
    e = page.wait(settled("tries", "delivered"), since=m) or {}
    check("a delivered message has its tries and round trip",
          ((e.get("update") or {}).get("tries"), (e.get("update") or {}).get("trip_ms")), (1, 1800))

    m = len(page.events)
    page.do("channel_join", name="#Ranch-Net")
    check("channels: a #hashtag is joined (lower case)",
          page.wait(lambda e: mc(e) and any(c["name"] == "#ranch-net" for c in e.get("channels") or []), since=m)
          is not None, True)
    for bad in ("#", "#two words", "#" + "x" * 31):
        m = len(page.events)
        page.do("channel_join", name=bad)
        check(f"  a bad name is refused: {bad[:12]!r}", page.wait(warned("hashtag channel is"), since=m, timeout=3)
              is not None, True)
    m = len(page.events)
    page.do("channel_leave", index=1)
    check("  and left", page.wait(lambda e: mc(e) and e.get("channels") == [{"index": 0, "name": "Public"}], since=m)
          is not None, True)
    m = len(page.events)
    page.do("channel_leave", index=7)
    check("  leaving a channel the node lacks is refused", page.wait(warned("no channel"), since=m, timeout=3)
          is not None, True)

    m = len(page.events)
    page.do("reboot")
    check("reboot: the page is told", saved("rebooting", m) is not None, True)
    check("  and the node comes back", page.wait(lambda e: mc(e) and (e.get("status") or {}).get("connected")
                                                 is True, since=m, timeout=20) is not None, True)
    page.close()

    stop(server)
    server = start()
    page = Page()
    check("the password is in no snapshot", PWD in json.dumps(page.hello), False)
    texts = [x["text"] for x in snapshot(page).get("messages") or []]
    check("after a restart: our messages are back", all(t in texts for t in ("hi all", "hi alpha")), True)
    check("  with their final state", next((x["ack"] for x in snapshot(page)["messages"] if x["text"] == "hi alpha"),
                                         None), "delivered")
    check("the log is private (mode 600)", stat.S_IMODE(os.stat(MSG_LOG).st_mode), 0o600)
    with open(MSG_LOG) as f:
        log_text = f.read()
    check("the password is not in the message log", PWD in log_text, False)
    check("no repeater password is in the message log", SAVED_PW in log_text or TYPED_PW in log_text, False)
    check("remote commands are not in the message log", "get radio" in log_text, False)
    page.close()
finally:
    stop(server)
check("the password is not in the server's log", any(PWD in o for o in SERVER_OUT), False)
check("no repeater password is in the server's log", any(SAVED_PW in o or TYPED_PW in o for o in SERVER_OUT), False)
check("  (its requests were logged, by name)", any(f"MeshCore: login for {GAMMA[:12]}" in o for o in SERVER_OUT), True)
check("  (the server's log was captured)", any("MeshCore: writing Wi-Fi settings" in o for o in SERVER_OUT), True)

print("PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
