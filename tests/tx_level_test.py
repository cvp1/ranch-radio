"""The page's transmit level: voice on the page's PTT, step, save, guards.

linux-station, 2026-10-17: ardopcf through the Digirig at 0 dB drove the G90 to
~25 W on a 20 W radio, and plain USB keyed it at 0 W. Winlink got a per-band
level (codec_level_test.py); this covers the rest of the page's control.

Checks:
  - the page's PTT puts the codec at the voice level for the band, and back
    after unkeying; the level is set before the key and restored after it
  - an app keying over rigctl (JTDX) never moves the codec
  - the G90 in plain USB: the page's voice is refused with a notice, never keyed
  - a step applies live while voice holds the codec, stops at --codec-min-db
    and at 0 dB, and shows as unsaved; on another band it edits that band only
  - Save without --codec-save is refused; with it, the host file's
    voice_codec_db is rewritten in place (other lines untouched) and the
    radio's unit is installed in the given home
  - a Winlink arm while voice holds the codec is refused, not run at the
    voice level
  - review 2026-10-18 (Sol, Grok): with a slow mixer, an app's rigctl T 1
    during the page's voice preparation is refused (the page owns PTT first),
    and another page's unkey during preparation leaves nothing keyed or
    changed (rigctl T 0 releases only a real transmission, by design); page PTT
    is refused while a Winlink session is armed; Save reloads systemd (Linux)
    without a restart; two Saves at once both land; a failed restore is kept
    and retried instead of forgotten

Runs the simulator (--sim --rig g90) with tests/fake_amixer.py as amixer.
Nothing here touches a radio or the real host file.

    python3 tests/tx_level_test.py
"""
import asyncio
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
TMP = tempfile.mkdtemp(prefix="tx-level-")
STATE = os.path.join(TMP, "level")
MIXLOG = os.path.join(TMP, "amixer.jsonl")
with open(os.path.join(TMP, "amixer"), "w") as f:
    f.write(f'#!/bin/sh\nexec "{sys.executable}" "{ROOT}/tests/fake_amixer.py" "$@"\n')
os.chmod(os.path.join(TMP, "amixer"), 0o755)
ENV = dict(os.environ, PATH=TMP + os.pathsep + os.environ["PATH"], FAKE_AMIXER_STATE=STATE,
           FAKE_AMIXER_LOG=MIXLOG, CODEC_LEVEL_BACKEND="alsa")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


def set_level(db):
    with open(STATE, "w") as f:
        f.write(str(db))


def level():
    with open(STATE) as f:
        return float(f.read())


class Page:
    def __init__(self, web):
        self.web, self.events = web, []
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.run(self._open())

    def run(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _open(self):
        self.sess = aiohttp.ClientSession()
        self.ws = await self.sess.ws_connect(f"http://127.0.0.1:{self.web}/ws")
        self.hello = json.loads((await self.ws.receive()).data)
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                self.events.append(json.loads(msg.data))

    def send(self, **m):
        self.run(self.ws.send_str(json.dumps(m)))

    def wait(self, pred, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            for e in list(self.events):
                if pred(e):
                    self.events.remove(e)
                    return e
            time.sleep(0.05)
        return None

    def codec(self, use="voice"):
        self.events = [e for e in self.events if e.get("t") != "codec"]
        self.send(t="codec_get", use=use)
        return self.wait(lambda e: e.get("t") == "codec" and e.get("use") == use)

    def notice(self, has="", timeout=3):
        return self.wait(lambda e: e.get("t") == "notice" and has in e.get("msg", ""), timeout)

    def close(self):
        async def _c():
            await self.ws.close()
            await self.sess.close()
        self.run(_c())


def start(*extra, env=None):
    web, apps = 18000 + random.randrange(1500), 15000 + random.randrange(1500)
    p = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--rig", "g90", "--no-audio", "--listen", str(web),
         "--host", "127.0.0.1", "--rigctl-host", "127.0.0.1", "--rigctl-port", str(apps),
         "--winlink-codec-db", "default=-32,20m=-33", "--winlink-codec-mixer", "CODEC:PCM",
         "--voice-codec-db", "default=-30", "--codec-min-db", "-36", *extra],
        cwd=ROOT, env=env or ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            for port in (web, apps):
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            time.sleep(0.5)
            return p, web, apps
        except OSError:
            time.sleep(0.1)
    p.kill()
    print(p.stdout.read())
    raise SystemExit("server did not start")


def stop(p):
    p.terminate()
    try:
        out = p.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired:
        p.kill()
        out = p.communicate()[0]
    check("no Traceback in the server log", "Traceback" in out, False)
    if "Traceback" in out:
        print(out[-1500:])
    return out


def rigctl(port, line):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        f = s.makefile("r")
        s.sendall(line.encode() + b"\n")
        v = f.readline().strip()
        f.close()
        return v


def mixer_sets():
    try:
        with open(MIXLOG) as f:
            return [json.loads(l) for l in f if "sset" in l]
    except OSError:
        return []


set_level(0.0)
p, web, apps = start()
page = Page(web)
try:
    cfg = page.hello["config"]["codec"]
    check("config: both uses, floor, top, no save", cfg,
          {"uses": ["winlink", "voice"], "min_db": -36.0, "max_db": 0.0, "save": False})
    page.send(t="set", k="freq", v=14_230_000)
    page.send(t="set", k="mode", v="USB")
    page.send(t="set", k="data", v=False)
    time.sleep(0.4)

    print("\n# outside the amateur bands: page PTT refused with the reason")
    page.send(t="set", k="freq", v=14_369_000)        # mac-shack, 2026-10-18: keyed nothing there
    time.sleep(0.3)
    page.send(t="set", k="tx", v=True, mic=True)
    n = page.notice("outside the amateur bands")
    check("refused, names the frequency", bool(n and "14.369 MHz" in n["msg"]), True)
    time.sleep(0.3)
    check("never keyed", rigctl(apps, "t"), "0")
    check("codec untouched", level(), 0.0)
    page.send(t="set", k="freq", v=14_230_000)
    time.sleep(0.3)

    print("\n# the G90 in plain USB: page voice refused")
    page.send(t="set", k="tx", v=True)
    n = page.notice()
    check("refused with a notice naming DATA", bool(n and "DATA" in n["msg"]), True)
    time.sleep(0.3)
    check("never keyed", rigctl(apps, "t"), "0")
    check("codec untouched", level(), 0.0)

    print("\n# USB-D: the page's PTT at the voice level, back after")
    page.send(t="set", k="data", v=True)
    time.sleep(0.4)
    page.send(t="set", k="tx", v=True)
    time.sleep(0.5)
    check("keyed", rigctl(apps, "t"), "1")
    check("codec at the voice level (default)", level(), -30.0)
    snap = page.codec()
    check("snapshot: live on 20 m, default level", (snap["live"], snap["band"], snap["db"], snap["own"]),
          (True, "20 m", -30.0, False))

    print("\n# step while live: applied now, floor and top hold")
    page.send(t="codec_step", use="voice", db=-1)
    time.sleep(0.4)
    check("one step down, live", level(), -31.0)
    snap = page.codec()
    check("20 m now has its own level, unsaved", (snap["db"], snap["own"], snap["unsaved"]), (-31.0, True, True))
    for _ in range(8):
        page.send(t="codec_step", use="voice", db=-1)
    time.sleep(0.8)
    check("stops at the floor", (level(), page.codec()["db"]), (-36.0, -36.0))
    page.send(t="set", k="tx", v=False)
    time.sleep(0.5)
    check("unkeyed", rigctl(apps, "t"), "0")
    check("codec back to its own level", level(), 0.0)
    sets = [a[-1] for a in mixer_sets()]
    check("the last mixer set is the restore, after the unkey", sets[-1:], ["0.00dB"])

    print("\n# the step stays for the next transmission on 20 m only")
    page.send(t="set", k="tx", v=True)
    time.sleep(0.5)
    check("20 m keys at the stepped level", level(), -36.0)
    page.send(t="set", k="tx", v=False)
    time.sleep(0.4)
    page.send(t="set", k="freq", v=7_200_000)
    time.sleep(0.4)
    snap = page.codec()
    check("40 m still the default", (snap["band"], snap["db"], snap["own"]), ("40 m", -30.0, False))
    page.send(t="codec_step", use="voice", db=+40)
    time.sleep(0.4)
    check("a step up stops at 0 dB", page.codec()["db"], 0.0)
    check("not live: the codec itself unchanged", level(), 0.0)

    print("\n# an app keying over rigctl never moves the codec")
    n_before = len(mixer_sets())
    check("rigctl keys", rigctl(apps, "T 1"), "RPRT 0")
    time.sleep(0.4)
    rigctl(apps, "T 0")
    time.sleep(0.3)
    check("no mixer set for an app's PTT", len(mixer_sets()), n_before)

    print("\n# Save without --codec-save")
    page.send(t="codec_save", use="voice")
    n = page.notice("not enabled")
    check("refused with a notice", n is not None, True)
finally:
    page.close()
    stop(p)

print("\n# --codec-max-db: a ceiling below 0 dB for a radio that overdrives there")
set_level(0.0)
p, web, apps = start("--codec-max-db", "-28")
page = Page(web)
try:
    check("config: the ceiling", page.hello["config"]["codec"]["max_db"], -28.0)
    page.send(t="set", k="freq", v=14_230_000)
    time.sleep(0.3)
    page.send(t="codec_step", use="voice", db=+10)
    time.sleep(0.3)
    snap = page.codec()
    check("a step stops at the ceiling", (snap["db"], snap["max_db"]), (-28.0, -28.0))
    check("the snapshot carries the saved level", snap["saved_db"], -30.0)
finally:
    page.close()
    stop(p)

print("\n# a slow mixer: the page owns PTT before it changes anything")
set_level(0.0)
p, web, apps = start(env=dict(ENV, FAKE_AMIXER_SLEEP="1.0"))
page = Page(web)
try:
    page.send(t="set", k="freq", v=14_230_000)
    page.send(t="set", k="data", v=True)
    time.sleep(0.4)
    page.send(t="set", k="tx", v=True, mic=True)
    time.sleep(0.3)                                  # voice is now waiting on the mixer
    check("an app's T 1 during the page's preparation is refused", rigctl(apps, "T 1") != "RPRT 0", True)
    time.sleep(1.5)
    check("the page then keys", rigctl(apps, "t"), "1")
    check("at the voice level", level(), -30.0)
    page.send(t="set", k="tx", v=False)
    time.sleep(1.8)
    check("released: level back", (rigctl(apps, "t"), level()), ("0", 0.0))

    other = Page(web)                                # a second browser: anyone may unkey
    page.send(t="set", k="tx", v=True, mic=True)
    time.sleep(0.3)
    other.send(t="set", k="tx", v=False)             # mid-preparation
    time.sleep(3.0)
    other.close()
    # (rigctl T 0 does NOT cancel it: it releases only a real transmission,
    # and JTDX sends T 0 when idle, so that is right.)
    check("another page's unkey during preparation: never keyed", rigctl(apps, "t"), "0")
    check("another page's unkey during preparation: level back", level(), 0.0)
    snap = page.codec()
    check("another page's unkey during preparation: voice no longer holds the codec", snap["live"], False)
finally:
    page.close()
    stop(p)

print("\n# Save into the host file (a scratch copy of the repo)")
repo = os.path.join(TMP, "repo")
os.makedirs(os.path.join(repo, "hosts"))
shutil.copy(os.path.join(ROOT, "examples/hosts/linux-station.json"), os.path.join(repo, "hosts/linux-station.json"))
home = os.path.join(TMP, "home")
before = open(os.path.join(repo, "hosts/linux-station.json")).read()
set_level(0.0)
LANE = 16000 + random.randrange(1500)
SCTL_LOG = os.path.join(TMP, "systemctl.jsonl")
with open(os.path.join(TMP, "systemctl"), "w") as f:
    f.write(f'#!/bin/sh\nexec "{sys.executable}" "{ROOT}/tests/fake_systemctl.py" "$@"\n')
os.chmod(os.path.join(TMP, "systemctl"), 0o755)
p, web, apps = start("--codec-save", "linux-station:g90", "--winlink-rigctl-port", str(LANE),
                     env=dict(ENV, RANCH_RADIO_REPO=repo, HOME=home, FAKE_SYSTEMCTL_LOG=SCTL_LOG,
                              RANCH_RADIO_SYSTEMCTL=os.path.join(TMP, "systemctl")))
page = Page(web)
try:
    check("config: save on", page.hello["config"]["codec"]["save"], True)
    page.send(t="set", k="freq", v=14_230_000)
    time.sleep(0.3)
    page.send(t="codec_step", use="voice", db=-3)
    time.sleep(0.3)
    page.send(t="codec_save", use="voice")
    n = page.notice()
    check("saved, with a notice", bool(n and "Saved" in n["msg"]), True)
    snap = page.codec()
    check("snapshot: saved", (snap["db"], snap["unsaved"]), (-33.0, False))
    after = open(os.path.join(repo, "hosts/linux-station.json")).read()
    h = json.loads(after)
    g90 = next(r for r in h["radios"] if r["id"] == "g90")
    check("voice_codec_db rewritten with the server's table", g90["voice_codec_db"], "default=-30,20m=-33")
    check("only that line changed", sum(a != b for a, b in zip(before.splitlines(), after.splitlines())), 1)
    unit = os.path.join(home, ".config/systemd/user/g90-remote.service")
    check("the radio's unit installed in that home", os.path.exists(unit), True)
    check("the unit carries the saved table", "--voice-codec-db default=-30,20m=-33" in open(unit).read(), True)
    calls = [json.loads(l) for l in open(SCTL_LOG)] if os.path.exists(SCTL_LOG) else []
    check("systemd reloaded after Save, nothing restarted", calls, [["--user", "daemon-reload"]])

    print("\n# Winlink armed: the page's PTT is refused")
    page.send(t="set", k="data", v=True)
    time.sleep(0.3)
    page.send(t="winlink_arm", v=True, freq=14_105_000)
    check("armed", page.wait(lambda e: e.get("t") == "winlink" and e.get("armed") is True) is not None, True)
    page.events.clear()
    page.send(t="set", k="tx", v=True, mic=True)
    n = page.notice("Winlink")
    check("refused with a notice naming Winlink", n is not None, True)
    time.sleep(0.4)
    check("never keyed", rigctl(apps, "t"), "0")
    check("codec still at the Winlink level", level(), -33.0)
    page.send(t="set", k="tx", v=False)
    page.send(t="winlink_arm", v=False)
    time.sleep(0.5)
    check("disarmed: restored", level(), 0.0)

    print("\n# Winlink: a step while armed applies live, the disarm restores")
    page.send(t="winlink_arm", v=True, freq=14_105_000)
    check("armed", page.wait(lambda e: e.get("t") == "winlink" and e.get("armed") is True) is not None, True)
    time.sleep(0.3)
    check("codec at the 20 m Winlink level", level(), -33.0)
    page.send(t="codec_step", use="winlink", db=-1)
    time.sleep(0.4)
    check("stepped live", level(), -34.0)
    snap = page.codec("winlink")
    check("snapshot: live, unsaved", (snap["live"], snap["db"], snap["unsaved"]), (True, -34.0, True))
    page.send(t="winlink_arm", v=False)
    time.sleep(0.5)
    check("disarmed: restored", level(), 0.0)

    print("\n# Winlink can't arm while voice holds the codec (the page is transmitting)")
    page.send(t="set", k="mode", v="USB")
    page.send(t="set", k="data", v=True)
    time.sleep(0.3)
    page.send(t="set", k="tx", v=True)
    time.sleep(0.4)
    page.send(t="winlink_arm", v=True)
    n = page.notice("Winlink not armed")
    check("arm refused: the page is transmitting", n is not None, True)
    check("not armed", page.wait(lambda e: e.get("t") == "winlink" and e.get("armed") is True, 1), None)
    check("the codec still at the voice level", level(), -33.0)
    page.send(t="set", k="tx", v=False)
    time.sleep(0.4)
    check("restored after", level(), 0.0)
finally:
    page.close()
    stop(p)

print("\n# two Saves at once both land")
sys.path.insert(0, ROOT)
from ranch_radio import levels  # noqa: E402
shutil.copy(os.path.join(ROOT, "examples/hosts/linux-station.json"), os.path.join(repo, "hosts/linux-station.json"))
home2 = os.path.join(TMP, "home2")
os.environ["RANCH_RADIO_SYSTEMCTL"] = os.path.join(TMP, "systemctl")
os.environ["FAKE_SYSTEMCTL_LOG"] = SCTL_LOG
errs = []
_validate = levels.render.validate


def slow_validate(h):          # widen the read-to-write window so a lost update shows
    time.sleep(0.2)
    return _validate(h)


levels.render.validate = slow_validate


def save(use, table):
    try:
        levels.save(repo, "linux-station", "g90", use, table, home=home2)
    except Exception as e:                    # noqa: BLE001 (reported below)
        errs.append(repr(e))


for _ in range(5):
    ts = [threading.Thread(target=save, args=("winlink", {"default": -32.0, "40 m": -31.0})),
          threading.Thread(target=save, args=("voice", {"default": -29.0}))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
g90 = next(r for r in json.load(open(os.path.join(repo, "hosts/linux-station.json")))["radios"] if r["id"] == "g90")
levels.render.validate = _validate
check("no Save failed", errs, [])
check("both tables landed", (g90["codec_db"], g90["voice_codec_db"]), ("default=-32,40m=-31", "default=-29"))

print("\n# a failed restore is kept and retried, not forgotten")
import codec_level  # noqa: E402


class Flaky:
    def __init__(self):
        self.db, self.fail_set = 0.0, False

    async def get(self):
        return self.db

    async def set(self, db):
        if self.fail_set:
            raise codec_level.CodecError("device busy")
        self.db = float(db)


async def flaky():
    m = Flaky()
    c = codec_level.CodecProfile(m, -33.0, voice=-30.0)
    await c.enter("20 m", use="voice")
    m.fail_set = True
    err = await c.leave("voice")
    first = (err is not None, m.db, c.use)
    try:
        await c.enter("20 m", use="winlink")
        blocked = False
    except codec_level.CodecError:
        blocked = True
    m.fail_set = False
    err2 = await c.leave()
    return first, blocked, (err2, m.db, c.use)

first, blocked, second = asyncio.run(flaky())
check("restore failed: reported, the level still held by voice", first, (True, -30.0, "voice"))
check("meanwhile Winlink can't take the codec at the wrong baseline", blocked, True)
check("the retry restores the original 0 dB", second, (None, 0.0, None))

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
