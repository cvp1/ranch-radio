"""W3: the codec's output level follows the Winlink session.

ARDOP needs the USB codec well below its factory level (-38 dB keeps ALC near
0 on the IC-7300); voice from the page wants it back. Arming a session sets
--winlink-codec-db; disarming restores what the codec was at before.

Checks:
  - codec_level.AlsaLevel reads and sets a control through amixer
  - the ALSA card is taken from --audio-device when no mixer is named
  - a session sets the data level while keyed and restores the old level after
  - Stop, the page closing and the server stopping mid-session all restore it
  - the codec refusing the data level refuses the session (Pat never connects)
  - without --winlink-codec-db the codec is never touched
  - --sim without a mixer: a simulated level, shown in /winlink/api/status
  - --winlink-codec-db as a per-band table ("default=-38,40m=-40,20 m=-41"):
    the level follows the band of the session URL's freq=, not the radio's
    frequency at arm time; unlisted bands use default; bad tables stop startup
  - arming logs band and level; on disarm a level the operator moved by more
    than 0.5 dB during the session is logged (never kept), then restored

Runs with tests/fake_amixer.py as amixer and tests/fake_pat.py as Pat.

    .venv/bin/python tests/codec_level_test.py
"""
import asyncio
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time

import aiohttp

os.environ["CODEC_LEVEL_BACKEND"] = "alsa"   # before codec_level is imported
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import codec_level  # noqa: E402

APPS = 15000 + random.randrange(1000)
LANE = 16000 + random.randrange(1000)
PATP = 17000 + random.randrange(1000)
WEB = 18000 + random.randrange(1000)
URL = "ardop:///N0DAJ?bw=2000MAX&freq=3588.5"
TMP = tempfile.mkdtemp(prefix="codec-level-")
STATE = os.path.join(TMP, "level")
MIXLOG = os.path.join(TMP, "amixer.jsonl")
fails = 0

with open(os.path.join(TMP, "amixer"), "w") as f:
    f.write(f'#!/bin/sh\nexec "{sys.executable}" "{ROOT}/tests/fake_amixer.py" "$@"\n')
os.chmod(os.path.join(TMP, "amixer"), 0o755)
os.environ["PATH"] = TMP + os.pathsep + os.environ["PATH"]
os.environ["FAKE_AMIXER_STATE"] = STATE
os.environ["FAKE_AMIXER_LOG"] = MIXLOG
os.environ["CODEC_LEVEL_BACKEND"] = "alsa"   # the fake amixer, never CoreAudio (macOS too)


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:56} got {got!r}" + ("" if ok else f" want {want!r}"))


def set_level(db):
    with open(STATE, "w") as f:
        f.write(str(db))


def level():
    with open(STATE) as f:
        return float(f.read())


def mixer_calls():
    try:
        with open(MIXLOG) as f:
            return [json.loads(l) for l in f if l.strip()]
    except OSError:
        return []


def ptt():
    with socket.create_connection(("127.0.0.1", APPS), timeout=3) as s:
        f = s.makefile("r")
        s.sendall(b"t\n")
        v = f.readline().strip()
        f.close()
        return v


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
        json.loads((await self.ws.receive()).data)
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                m = json.loads(msg.data)
                if m.get("t") in ("winlink", "notice"):
                    self.events.append(m)

    def send(self, m):
        self.run(self.ws.send_str(json.dumps(m)))

    def wait(self, pred, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            for e in list(self.events):
                if pred(e):
                    return e
            time.sleep(0.1)
        return None

    def status(self):
        async def go():
            async with self.sess.get(f"http://127.0.0.1:{WEB}/winlink/api/status") as r:
                return await r.json()
        return self.run(go())

    def close(self):
        async def _c():
            await self.ws.close()
            await self.sess.close()
        self.run(_c())
        time.sleep(0.5)


def armed(e):
    return e.get("t") == "winlink" and e.get("armed") is True


def ended(e):
    return e.get("t") == "winlink" and e.get("session") == "ended"


def fake_log(path):
    try:
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]
    except OSError:
        return []


def start(env_extra=None, *extra):
    logf = tempfile.NamedTemporaryFile(prefix="fakepat-", suffix=".jsonl", delete=False).name
    env = dict(os.environ, FAKE_PAT_RIGCTL=str(LANE), FAKE_PAT_LOG=logf, FAKE_PAT_TX_S="2")
    env.update(env_extra or {})
    p = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB),
         "--rigctl-port", str(APPS), "--winlink-rigctl-port", str(LANE),
         "--winlink-pat", f"{sys.executable} tests/fake_pat.py", "--winlink-pat-port", str(PATP),
         *extra],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            for port in (WEB, LANE, PATP):
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            time.sleep(0.5)          # Radio Remote's websocket to Pat
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


def show(out):
    for line in out.splitlines():
        if "codec" in line.lower() or "winlink" in line.lower() or "Traceback" in line:
            print("   log:", line[:200])


ALSA = ("--winlink-codec-db", "-38", "--winlink-codec-mixer", "CODEC:PCM")

# --- the ALSA backend on its own ---
print("# codec_level.AlsaLevel through amixer")
set_level(-6)
mix = codec_level.AlsaLevel("CODEC:PCM")
check("reads the level", asyncio.run(mix.get()), -6.0)
asyncio.run(mix.set(-38))
check("sets the level", level(), -38.0)
check("reads it back", asyncio.run(mix.get()), -38.0)
check("card from --audio-device", codec_level.alsa_spec("plughw:CARD=CODEC,DEV=0"), "CODEC:PCM")
check("no card in a PipeWire device -> default", codec_level.alsa_spec("pipewire:a,b"), "CODEC:PCM")
try:
    asyncio.run(codec_level.AlsaLevel("NOPE:PCM").get())
    check("unknown card raises CodecError", "no error", "CodecError")
except codec_level.CodecError:
    check("unknown card raises CodecError", "CodecError", "CodecError")

# --- a normal session, Stop, page closing ---
print("\n# sessions move the codec level and put it back")
set_level(-6)
server, flog = start({}, *ALSA)
try:
    page = Page()
    check("idle: codec untouched", level(), -6.0)
    page.send({"t": "winlink_connect", "url": URL})
    page.wait(armed, 5)
    time.sleep(0.8)
    check("session: radio keyed", ptt(), "1")
    check("session: codec at the data level", level(), -38.0)
    check("session: status shows the level", page.status().get("codec_db"), -38.0)
    e = page.wait(ended, 15)
    check("session ended ok", e and e.get("ok"), True)
    time.sleep(0.3)
    check("after session: codec restored", level(), -6.0)

    page.events.clear()
    page.send({"t": "winlink_connect", "url": URL})
    page.wait(armed, 5)
    time.sleep(0.6)
    check("stop: codec at the data level first", level(), -38.0)
    page.send({"t": "winlink_stop"})
    page.wait(ended, 10)
    time.sleep(0.3)
    check("stop: codec restored", level(), -6.0)

    set_level(-12)                      # the operator moved it between sessions
    page.events.clear()
    page.send({"t": "winlink_connect", "url": URL})
    page.wait(armed, 5)
    time.sleep(0.6)
    page.close()
    time.sleep(1.5)
    check("page closed: codec restored to the newer level", level(), -12.0)
finally:
    show(stop(server))

# --- the server stopping mid-session ---
set_level(-6)
server, flog = start({"FAKE_PAT_TX_S": "6"}, *ALSA, "--winlink-session-max", "30")
try:
    page = Page()
    page.send({"t": "winlink_connect", "url": URL})
    page.wait(armed, 5)
    time.sleep(0.8)
    check("server stop: codec at the data level first", level(), -38.0)
finally:
    out = stop(server)
check("server stop: codec restored", level(), -6.0)

# --- the codec refuses: no session ---
print("\n# the codec refusing the data level refuses the session")
set_level(-6)
server, flog = start({"FAKE_AMIXER_FAIL": "set"}, *ALSA)
try:
    page = Page()
    page.send({"t": "winlink_connect", "url": URL})
    e = page.wait(lambda e: e.get("t") == "notice" and "level" in e.get("msg", "").lower(), 5)
    check("refused with a notice about the level", e is not None, True)
    time.sleep(1)
    check("Pat never asked to connect", any(r["route"] == "connect" for r in fake_log(flog)), False)
    check("not armed", page.status().get("armed"), False)
    check("radio never keyed", any(r["route"] == "ptt_on" for r in fake_log(flog)), False)
    page.close()
finally:
    show(stop(server))

# --- no level configured: hands off ---
print("\n# no --winlink-codec-db: the codec is never touched")
open(MIXLOG, "w").close()
server, flog = start({})
try:
    page = Page()
    page.send({"t": "winlink_connect", "url": URL})
    e = page.wait(ended, 15)
    check("session still runs", e and e.get("ok"), True)
    check("amixer never called", mixer_calls(), [])
    check("status: no level", page.status().get("codec_db"), None)
    page.close()
finally:
    stop(server)

# --- --sim with no mixer named: a simulated codec ---
print("\n# --sim, no mixer: a simulated codec level")
open(MIXLOG, "w").close()
server, flog = start({}, "--winlink-codec-db", "-38")
try:
    page = Page()
    check("sim idle: 0 dB", page.status().get("codec_db"), 0.0)
    page.send({"t": "winlink_connect", "url": URL})
    page.wait(armed, 5)
    time.sleep(0.6)
    check("sim session: -38 dB", page.status().get("codec_db"), -38.0)
    page.wait(ended, 15)
    time.sleep(0.3)
    check("sim after: 0 dB", page.status().get("codec_db"), 0.0)
    check("sim: amixer never called", mixer_calls(), [])
    page.close()
finally:
    stop(server)

# --- per-band table: parsing ---
print("\n# --winlink-codec-db per-band table: parsing")
BANDS = ["160 m", "80 m", "40 m", "20 m", "15 m"]
check("single number -> default only", codec_level.parse_levels("-38", BANDS), {"default": -38.0})
check("table, both band spellings",
      codec_level.parse_levels("default=-38, 80m=-38,40 m=-40,20M=-41.5", BANDS),
      {"default": -38.0, "80 m": -38.0, "40 m": -40.0, "20 m": -41.5})
for bad in ("", "abc", "80m=-38", "default=-38,99m=-1", "default=x", "default=-38,40m=-40,40 m=-41",
            "default=-38,40m", "default=nan", "default=-38,,40m=-40", "=-38"):
    try:
        codec_level.parse_levels(bad, BANDS)
        check(f"refused: {bad!r}", "accepted", "ValueError")
    except ValueError:
        check(f"refused: {bad!r}", "ValueError", "ValueError")


def startup_fails(spec):
    r = subprocess.run([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB),
                        "--winlink-codec-db", spec], cwd=ROOT, capture_output=True, text=True,
                       timeout=15)
    return r.returncode != 0 and "--winlink-codec-db" in r.stderr, r.stderr.strip().splitlines()[-1:]


for bad in ("default=-38,99m=-1", "40m=-40", "loud"):
    ok, tail = startup_fails(bad)
    check(f"server refuses to start: {bad!r}", ok, True)
    if not ok:
        print("   stderr:", tail)

# --- per-band table: sessions ---
print("\n# per-band table: level from the session's band")
TABLE = ("--winlink-codec-db", "default=-38,80m=-38,40m=-40,20 m=-41",
         "--winlink-codec-mixer", "CODEC:PCM")


def session_level(page, url, during=None):
    """Run one session to the end; the codec level seen while keyed."""
    page.events.clear()
    page.send({"t": "winlink_connect", "url": url})
    page.wait(armed, 5)
    time.sleep(0.6)
    seen = level()
    if during is not None:
        set_level(during)               # the operator moves the macOS slider mid-session
    page.wait(ended, 15)
    time.sleep(0.3)
    return seen


set_level(-6)
server, flog = start({}, *TABLE)        # sim radio sits on 14.074 MHz (20 m)
try:
    page = Page()
    check("80 m session: -38", session_level(page, "ardop:///N0DAJ?bw=2000MAX&freq=3588.5"), -38.0)
    check("after: restored", level(), -6.0)
    check("40 m URL, radio still on 20 m: -40",
          session_level(page, "ardop:///KM7N?bw=2000MAX&freq=7101.5"), -40.0)
    check("20 m session: -41", session_level(page, "ardop:///N0DAJ?bw=2000MAX&freq=14101.5"), -41.0)
    check("15 m (unlisted): default -38",
          session_level(page, "ardop:///KB2PCN-5?bw=2000MAX&freq=21101.0"), -38.0)
    check("20 m, operator moves to -44 mid-session",
          session_level(page, "ardop:///N0DAJ?bw=2000MAX&freq=14101.5", during=-44), -41.0)
    check("after operator move: prior level restored", level(), -6.0)
    check("40 m, operator nudges 0.3 dB (within 0.5)",
          session_level(page, "ardop:///KM7N?bw=2000MAX&freq=7101.5", during=-40.3), -40.0)
    check("after nudge: prior level restored", level(), -6.0)
    page.close()
finally:
    out = stop(server)
show(out)
check("arm log: band and level (80 m)",
      "codec: 80 m: -38 dB for Winlink (was -6 dB)" in out, True)
check("arm log: band and level (40 m)",
      "codec: 40 m: -40 dB for Winlink (was -6 dB)" in out, True)
check("arm log: unlisted band says default",
      "codec: 15 m (default): -38 dB for Winlink (was -6 dB)" in out, True)
check("operator adjustment logged",
      "codec: operator adjusted to -44 dB on 20 m during session (profile -41 dB)" in out, True)
check("adjustment within 0.5 dB not logged", "operator adjusted to -40.3" in out, False)
check("only one adjustment line", out.count("operator adjusted"), 1)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
