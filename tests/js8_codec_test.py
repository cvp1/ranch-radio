"""The codec's JS8 level (codec_level "js8" use, server.py js8_codec): jf8call
writes full-scale audio and has no level of its own, so the station's JS8
bridge holds the codec at --js8-codec-db while an arm lasts.

  CodecProfile, a simulated level:
  - enter js8 sets the band's level and remembers the one before; leave puts it back
  - follow: the dial moved to another band while JS8 holds it -> that band's
    level; the same band, or another use holding it, or none: nothing
  - follow failing (the mixer refuses): the old band's level is kept, still held
  - JS8 holding it: Winlink and voice are refused; their leave() can't release it

  server.py --sim, a socket acting as the bridge:
  - on: answered ok with the radio's band level; a second socket is refused
  - the dial moves to another band: the level follows; back again: it follows back
  - off: the level from before is back; on, then the socket closes: back too
  - on while the radio is transmitting (keyed over rigctl): refused, untouched
  - a radio with no --js8-codec-db: refused, configured false, untouched
  - a malformed request (on not a boolean): rejected

    python3 tests/js8_codec_test.py
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import aiohttp  # noqa: E402

import codec_level  # noqa: E402
import testkit  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"), flush=True)


# ------------------------------------------------------------ CodecProfile
class Flaky(codec_level.SimLevel):
    def __init__(self, db=0.0):
        super().__init__(db)
        self.fail = False

    async def set(self, db):
        if self.fail:
            raise codec_level.CodecError("the mixer refused")
        await super().set(db)


async def unit():
    mix = Flaky(-3.0)
    c = codec_level.CodecProfile(mix, -38, voice=-30, js8={"default": -40, "20 m": -50, "40 m": -44})
    check("profile: js8 is a use", (c.has("js8"), codec_level.LABEL["js8"]), (True, "JS8"))
    await c.enter("20 m", use="js8")
    check("enter js8: the band's level", (mix.db, c.use, c.applied), (-50.0, "js8", ("20 m", -50.0)))
    check("follow, same band: nothing", (await c.follow("20 m", "js8"), mix.db), (None, -50.0))
    check("follow, another use named: nothing", (await c.follow("40 m", "voice"), mix.db), (None, -50.0))
    check("follow to 40 m: 40 m's level", (await c.follow("40 m", "js8"), mix.db, c.applied),
          (None, -44.0, ("40 m", -44.0)))
    check("follow to an unlisted band: the default", (await c.follow("15 m", "js8"), mix.db), (None, -40.0))
    mix.fail = True
    err = await c.follow("20 m", "js8")
    check("follow failing: says why, keeps the old band's level, still held",
          (err, mix.db, c.applied, c.use), ("the mixer refused", -40.0, ("15 m", -40.0), "js8"))
    mix.fail = False
    for use in ("winlink", "voice"):
        try:
            await c.enter("20 m", use=use)
            got = "entered"
        except codec_level.CodecError as e:
            got = str(e)
        check(f"js8 holding it: {use} refused", got, "the codec is at the JS8 level right now")
    check("js8 holding it: winlink's leave() can't release it", (await c.leave("winlink"), c.use, mix.db),
          (None, "js8", -40.0))
    check("leave js8: the level from before", (await c.leave("js8"), c.use, mix.db), (None, None, -3.0))
    check("follow with nothing held: nothing", (await c.follow("40 m", "js8"), mix.db), (None, -3.0))


asyncio.run(unit())


# ------------------------------------------------------------ the server
def radio(*extra):
    web, rig = testkit.free_port(), testkit.free_port()
    p = subprocess.Popen([sys.executable, "server.py", "--sim", "--rig", "ic7300", "--no-audio", "--host", "127.0.0.1",
                          "--listen", str(web), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(rig), *extra],
                         cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env={**os.environ, "CODEC_LEVEL_BACKEND": "alsa"})
    return p, web, rig


def stop(p):
    p.terminate()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()


def rigctl(port, line):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        f = s.makefile("r")
        s.sendall(line.encode() + b"\n")
        v = f.readline().strip()
        f.close()
        return v


class Sock:
    """One WebSocket to the radio's server, every text message kept."""

    async def open(self, web):
        self.s = aiohttp.ClientSession()
        end = time.monotonic() + 20
        while True:
            try:
                self.ws = await self.s.ws_connect(f"http://127.0.0.1:{web}/ws")
                break
            except aiohttp.ClientError:
                if time.monotonic() > end:
                    raise
                await asyncio.sleep(0.2)
        self.msgs = []
        self.reader = asyncio.ensure_future(self._read())
        return self

    async def _read(self):
        async for m in self.ws:
            if m.type == aiohttp.WSMsgType.TEXT:
                self.msgs.append(json.loads(m.data))

    async def send(self, **m):
        await self.ws.send_str(json.dumps(m))

    async def until(self, pred, timeout=8, since=0):
        end = time.monotonic() + timeout
        seen = since
        while time.monotonic() < end:
            for m in self.msgs[seen:]:
                if pred(m):
                    return m
            seen = len(self.msgs)
            await asyncio.sleep(0.05)
        return None

    async def ask(self, on):
        n = len(self.msgs)
        await self.send(t="js8_codec", on=on)
        return await self.until(lambda m: m.get("t") == "js8_codec", since=n) or {}

    async def level(self):
        n = len(self.msgs)
        await self.send(t="codec_get", use="js8")
        m = await self.until(lambda m: m.get("t") == "codec" and m.get("use") == "js8", since=n)
        return (m or {}).get("current")

    async def level_is(self, want, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if await self.level() == want:
                return True
            await asyncio.sleep(0.1)
        return False

    async def set_freq(self, hz):
        n = len(self.msgs)
        await self.send(t="set", k="freq", v=hz)
        return bool(await self.until(lambda m: m.get("t") == "state" and (m.get("d") or {}).get("freq") == hz, since=n))

    async def close(self):
        await self.ws.close()
        await self.s.close()


async def server():
    p, web, rig = radio("--js8-codec-db", "default=-40,20m=-50,40m=-44")
    try:
        a = await Sock().open(web)
        b = await Sock().open(web)
        check("the radio on 20 m", await a.set_freq(14_078_000), True)
        check("before: the codec at its own level (0 dB)", await a.level(), 0.0)
        m = await a.ask(True)
        check("on: ok, 20 m's level", (m.get("ok"), m.get("on"), m.get("db"), m.get("band")), (True, True, -50.0, "20 m"))
        m = await b.ask(True)
        check("a second socket: refused", (m.get("ok"), m.get("why")), (False, "another JS8 bridge holds the codec"))
        check("the dial to 40 m: the level follows", (await a.set_freq(7_078_000), await a.level_is(-44.0)), (True, True))
        check("the dial back to 20 m: it follows back", (await a.set_freq(14_078_000), await a.level_is(-50.0)), (True, True))
        m = await a.ask(False)
        check("off: answered, not held", (m.get("ok"), m.get("on")), (True, False))
        check("off: the level from before is back", await a.level_is(0.0), True)
        m = await b.ask(True)
        check("now the other socket may hold it", (m.get("ok"), m.get("db")), (True, -50.0))
        await b.close()
        check("its socket closes: the level from before is back", await a.level_is(0.0), True)

        n = len(a.msgs)
        app = socket.create_connection(("127.0.0.1", rig), timeout=3)     # held open: closing it unkeys
        af = app.makefile("r")
        app.sendall(b"T 1\n")
        check("keyed over rigctl", af.readline().strip(), "RPRT 0")
        check("  the radio says it is transmitting",
              bool(await a.until(lambda m: m.get("t") == "state" and (m.get("d") or {}).get("tx") is True, since=n)), True)
        m = await a.ask(True)
        check("on while transmitting: refused", (m.get("ok"), "mid-transmission" in (m.get("why") or "")), (False, True))
        check("on while transmitting: the level untouched", await a.level(), 0.0)
        app.sendall(b"T 0\n")
        af.readline()
        af.close()
        app.close()
        n = len(a.msgs)
        await a.send(t="js8_codec", on="yes")
        m = await a.until(lambda m: m.get("t") == "notice" and "Rejected" in (m.get("msg") or ""), since=n)
        check("on not a boolean: rejected", bool(m) and not any(x.get("t") == "js8_codec" for x in a.msgs[n:]), True)
        await a.close()
    finally:
        stop(p)

    p, web, rig = radio()
    try:
        a = await Sock().open(web)
        m = await a.ask(True)
        check("no --js8-codec-db: refused, configured false",
              (m.get("ok"), m.get("configured"), m.get("on")), (False, False, False))
        await a.close()
    finally:
        stop(p)


asyncio.run(server())
print(f"\n{fails} failure(s)" if fails else "\nall ok")
sys.exit(1 if fails else 0)
