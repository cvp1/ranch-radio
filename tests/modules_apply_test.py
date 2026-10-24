"""Adding and removing a module from the page (modules plan M3/M4).

Falsifiers from plan revision 2:
  M3  a plan writes nothing; the page is refused a path, a bool (tx), a label,
      a key not in the template, and an ASK value that fails its rule; a valid
      calls list is accepted
  M4  apply writes the host file and nothing else; another page can't apply
      my plan; apply refuses when the file changed underneath; no codec table
      is lost when Save runs second; the one lock keeps the CLI, Save and apply
      apart; the radio's loop keeps answering while apply waits on that lock
Against a station (Linux host file) and a simulated radio server (Mac host
file), each on scratch copies (RANCH_RADIO_REPO / RANCH_RADIO_HOME).

    python3 tests/modules_apply_test.py
"""
import asyncio
import fcntl
import hashlib
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import os as _os
# Host files come from examples/hosts/ (or a scratch copy) only, never a
# real ~/.config/ranch-radio/hosts on the machine running the tests.
_os.environ["RANCH_RADIO_HOSTS"] = ""
import tempfile
import threading
import time

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ranch_radio import hostlock, levels, modules  # noqa: E402

TMP = tempfile.mkdtemp(prefix="modapply-")
REPO, HOME = os.path.join(TMP, "repo"), os.path.join(TMP, "home")
os.makedirs(os.path.join(REPO, "hosts"))
os.makedirs(HOME)
STATE = os.path.join(TMP, "addons-state.json")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:70} got {got!r}" + ("" if ok else f" want {want!r}"))


def hpath(name):
    return os.path.join(REPO, "hosts", f"{name}.json")


def write_host(name, h):
    with open(hpath(name), "w") as f:
        json.dump(h, f, indent=2)
        f.write("\n")


def digest(name):
    with open(hpath(name), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


write_host("testlinux", {"host": "testlinux", "os": "linux", "repo": "~/ranch-radio", "python": sys.executable,
                         "radios": [], "station": {"https": False}})
write_host("testmac", {"host": "testmac", "os": "macos", "repo": "~/ranch-radio", "python": sys.executable,
                       "radios": [{"id": "ic7300", "name": "IC-7300", "label": "t.radio", "device": "/dev/null",
                                   "codec_db": "default=-38", "codec_save": True}],
                       "addons": {"opengd77": {"enabled": True, "port": "auto", "label": "MD-UV390", "poll": 2,
                                               "dmrconf": "/opt/homebrew/bin/dmrconf"}}})


def wait_port(p, port):
    for _ in range(600):                  # 60 s ceiling; returns once the port answers
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return p
        except OSError:
            time.sleep(0.1)
    p.kill()
    print(p.stdout.read())
    raise SystemExit("did not start")


def station(port):
    cfg = os.path.join(TMP, "station.json")
    with open(cfg, "w") as f:
        json.dump({"name": "Test", "radios": [], "host_file": "testlinux", "addons_state": STATE}, f)
    env = dict(os.environ, RANCH_RADIO_REPO=REPO, RANCH_RADIO_HOME=HOME)
    return wait_port(subprocess.Popen([sys.executable, "station.py", "--config", cfg, "--listen", str(port),
                                       "--rigctl-port", "0"], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True), port)


def server(port):
    env = dict(os.environ, RANCH_RADIO_REPO=REPO, RANCH_RADIO_HOME=HOME)
    return wait_port(subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(port), "--rigctl-port", "0",
         "--addons-state", STATE, "--host-file", "testmac"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True), port)


def stop(p):
    p.terminate()
    try:
        p.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()


class Page:
    def __init__(self, port):
        self.port, self.events = port, []

    async def open(self):
        self.sess = aiohttp.ClientSession()
        self.ws = await self.sess.ws_connect(f"http://127.0.0.1:{self.port}/ws")
        self.hello = json.loads((await self.ws.receive(timeout=10)).data)
        self.reader = asyncio.ensure_future(self._read())
        return self

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                self.events.append(json.loads(msg.data))

    async def ask(self, m, t, timeout=10):
        n = len(self.events)
        await self.ws.send_str(json.dumps(m))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for e in self.events[n:]:
                if e.get("t") == t:
                    return e
            await asyncio.sleep(0.02)
        return None

    async def close(self):
        await self.ws.close()
        await self.sess.close()


async def linux_station(port):
    a, b = await Page(port).open(), await Page(port).open()
    before = digest("testlinux")

    async def plan(sets, module="packet"):
        return await a.ask({"t": "module_plan", "do": "add", "module": module, "set": sets}, "module_plan")

    good = {"tnc": "bt:00:11:22:33:44:55", "mycall": "n9abc"}
    for name, sets, field in (
            ("a path (url)", {**good, "url": "http://evil:1"}, "url"),
            ("a bool (tx)", {**good, "tx": True}, "tx"),
            ("a key not in the template (python)", {**good, "python": "/tmp/evil"}, "python"),
            ("an ASK value that fails its rule (mycall 'N9ABC --tx')", {**good, "mycall": "N9ABC --tx"}, "mycall"),
            ("a TNC with a space", {**good, "tnc": "bt:00:11:22:33:44:55 --http-bind 0.0.0.0"}, "tnc")):
        p = await plan(sets)
        check(f"M3 refused from the page: {name}", bool(p and field in (p.get("errors") or {})), True)
    p = await plan({"tnc": "bt:00:11:22:33:44:55"})
    check("M3 a missing ASK field is required", (p or {}).get("errors", {}).get("mycall"), "required")
    check("M3 refused plans write nothing", digest("testlinux"), before)

    p = await plan(good)
    check("M3 a good plan: no errors, a diff", (not p.get("errors"), '"packet"' in p.get("diff", "")), (True, True))
    check("  callsign and address upper-cased", ('"N9ABC"' in p["diff"], "bt:00:11:22:33:44:55" in p["diff"]), (True, True))
    check("  transmit stays off (the template's default)", '"tx": false' in p["diff"], True)
    check("M3 planning writes nothing", digest("testlinux"), before)

    r = await b.ask({"t": "module_apply", "plan": p["id"]}, "module_applied")
    check("M4 another page can't apply my plan", (r or {}).get("ok"), False)
    check("  and the file is unchanged", digest("testlinux"), before)

    p = await plan(good)                      # b's attempt consumed nothing of a's? plan again to be sure
    r = await a.ask({"t": "module_apply", "plan": p["id"]}, "module_applied")
    with open(hpath("testlinux")) as f:
        h = json.load(f)
    check("M4 apply writes the block", (r.get("ok"), h.get("packet", {}).get("mycall")), (True, "N9ABC"))
    check("  says what to run next: install first, never drift",
          [x.split(" ")[1] for x in r.get("next", [])[:2]], ["install", "install"])
    check("  installs nothing (no unit in the scratch home)",
          os.path.exists(os.path.join(HOME, ".config/systemd/user/uvpro-hub.service")), False)
    rep = next((e for e in reversed(a.events) if e.get("t") == "modules"), None)
    packet = next((i for i in (rep or {}).get("items", []) if i["name"] == "packet"), {})
    check("  every page gets the new inventory; packet reads install pending", packet.get("state"), "install_pending")

    # uvpro: a calls list fits callsign_list; with the hub on the host it borrows from it
    p = await a.ask({"t": "module_plan", "do": "add", "module": "uvpro",
                     "set": {"address": "00:11:22:33:44:55", "calls": "n9abc, n9abc-9"}}, "module_plan")
    check("M3 a valid calls list is accepted", (not p.get("errors"), '"N9ABC-9"' in p.get("diff", "")), (True, True))
    check("  and borrows the radio from the hub", '"hub"' in p.get("diff", ""), True)
    # the file changes underneath (the CLI, a hand edit): apply refuses
    with open(hpath("testlinux"), "a") as f:
        f.write(" ")
    r = await a.ask({"t": "module_apply", "plan": p["id"]}, "module_applied")
    check("M4 apply refuses when the file changed since the plan", (r.get("ok"), "changed" in (r.get("refusal") or "")), (False, True))

    # remove: packet is leaned on by nothing yet (uvpro wasn't applied)
    p = await a.ask({"t": "module_plan", "do": "remove", "module": "packet"}, "module_plan")
    r = await a.ask({"t": "module_apply", "plan": p["id"]}, "module_applied")
    with open(hpath("testlinux")) as f:
        h = json.load(f)
    check("M4 remove from the page", (r.get("ok"), "packet" in h), (True, False))
    await a.close()
    await b.close()


async def mac_server(port):
    a = await Page(port).open()
    # a stale run-time "off" for mesh in the state file
    with open(STATE, "w") as f:
        json.dump({"mesh": False, "opengd77": True}, f)
    p = await a.ask({"t": "module_plan", "do": "add", "module": "mesh",
                     "set": {"port": "ble:NODEM_7720"}}, "module_plan")
    check("M3 mesh plan on the Mac", (not p.get("errors"), not p.get("refusal")), (True, True))
    r = await a.ask({"t": "module_apply", "plan": p["id"]}, "module_applied")
    with open(STATE) as f:
        st = json.load(f)
    check("M4 add drops the stale run-time off for that add-on only", (r.get("ok"), "mesh" in st, st.get("opengd77")),
          (True, False, True))
    p = await a.ask({"t": "module_plan", "do": "add", "module": "winlink",
                     "set": {"ardopcf.audio_in": "USB Audio CODEC", "ardopcf.audio_out": "USB Audio CODEC",
                             "ardopcf.bin": "/tmp/evil"}}, "module_plan")
    check("M3 refused from the page: a nested path (ardopcf.bin)", "ardopcf.bin" in (p.get("errors") or {}), True)
    p = await a.ask({"t": "module_plan", "do": "add", "module": "winlink",
                     "set": {"ardopcf.audio_in": "USB Audio CODEC", "ardopcf.audio_out": "USB Audio CODEC",
                             "ardopcf.label": "x; reboot"}}, "module_plan")
    check("M3 refused from the page: a label", "ardopcf.label" in (p.get("errors") or {}), True)
    p = await a.ask({"t": "module_plan", "do": "add", "module": "winlink",
                     "set": {"ardopcf.audio_in": "USB Audio CODEC", "ardopcf.audio_out": "USB Audio CODEC"}},
                    "module_plan")
    check("M3 winlink on the Mac: the server assigns the radio's lane", ('"lane": 4537' in p.get("diff", "")), True)

    # The radio's loop keeps answering while apply waits on the host-file lock.
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl,sys,time; f=open(sys.argv[1],'w'); fcntl.flock(f, fcntl.LOCK_EX); "
        "print('held', flush=True); time.sleep(3)"), os.path.join(REPO, "hosts", ".host.lock")],
        stdout=subprocess.PIPE, text=True)
    holder.stdout.readline()
    t0 = time.monotonic()
    await a.ws.send_str(json.dumps({"t": "module_apply", "plan": p["id"]}))
    worst = 0.0
    for i in range(8):
        s = time.monotonic()
        e = await a.ask({"t": "ping", "ts": i}, "pong", timeout=5)
        worst = max(worst, time.monotonic() - s if e else 99)
        await asyncio.sleep(0.2)
    done = None
    end = time.monotonic() + 10
    while time.monotonic() < end and done is None:
        done = next((e for e in a.events if e.get("t") == "module_applied" and e.get("module") == "winlink"), None)
        await asyncio.sleep(0.05)
    holder.wait()
    check("M4 pings answer within 0.5 s while apply waits on the lock", worst < 0.5, True)
    check("  and apply waited for the lock, then landed", (bool(done and done["ok"]), time.monotonic() - t0 >= 2.5),
          (True, True))
    await a.close()


def lock_and_save_race():
    """No codec table lost when Save runs second; the one lock keeps them apart."""
    write_host("racemac", {"host": "racemac", "os": "macos", "repo": "~/ranch-radio", "python": sys.executable,
                           "radios": [{"id": "ic7300", "name": "IC-7300", "label": "t.radio",
                                       "device": "/dev/null", "codec_db": "default=-38", "codec_save": True}]})
    with open(hpath("racemac")) as f:
        text = f.read()
    plan = modules.plan_add(text, "mesh", {"port": "ble:NODEM_7720"}, source="page")
    levels.save(REPO, "racemac", "ic7300", "winlink", {"default": -41.0}, home=HOME)     # Save first
    why = modules.apply(hpath("racemac"), plan)
    with open(hpath("racemac")) as f:
        h = json.load(f)
    check("M4 Save first, then a stale apply: apply refuses", why is not None and "changed" in why, True)
    check("  and the codec table Save wrote is still there", h["radios"][0]["codec_db"], "default=-41")
    with open(hpath("racemac")) as f:
        plan = modules.plan_add(f.read(), "mesh", {"port": "ble:NODEM_7720"}, source="page")
    check("  plan again, apply: mesh lands", modules.apply(hpath("racemac"), plan), None)
    levels.save(REPO, "racemac", "ic7300", "winlink", {"default": -43.0}, home=HOME)     # Save second
    with open(hpath("racemac")) as f:
        h = json.load(f)
    check("M4 apply then Save: both land", (h["radios"][0]["codec_db"], "mesh" in h), ("default=-43", True))

    # Save waits while the lock is held (as the CLI or the page would hold it).
    took = {}

    def saver():
        s = time.monotonic()
        levels.save(REPO, "racemac", "ic7300", "winlink", {"default": -44.0}, home=HOME)
        took["s"] = time.monotonic() - s

    with hostlock.locked(os.path.join(REPO, "hosts")):
        t = threading.Thread(target=saver)
        t.start()
        time.sleep(1.0)
        check("M4 Save waits while the host-file lock is held", "s" in took, False)
    t.join(10)
    check("  and runs as soon as it's free", took.get("s", 0) >= 0.9, True)


lock_and_save_race()
p1 = 19000 + random.randrange(1000)
st = station(p1)
try:
    asyncio.run(linux_station(p1))
finally:
    stop(st)
p2 = p1 + 1
srv = server(p2)
try:
    asyncio.run(mac_server(p2))
finally:
    stop(srv)
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
