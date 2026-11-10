"""The station's /audio: where a radio's own server had it before the station
stood in front of the radio. A remote listener still asks there (2026-11-10:
one asked every 5 s and got 404 from the deploy on).

Runs a simulated IC-7300 (server.py --sim, with its simulated audio) and
station.py in front of it, and checks:
  - a station of one radio: /audio streams that radio's audio, as
    /r/<id>/audio does (the positive control)
  - a station of two radios: /audio is 404 and names each radio's /r/<id>/audio

    python3 tests/station_audio_test.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import testkit  # noqa: E402

RWEB, RRIG, WEB1, WEB2, OTHER = (testkit.free_port() for _ in range(5))
TMP = tempfile.mkdtemp(prefix="station-audio-test-")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:64} got {got!r}" + ("" if ok else f" want {want!r}"))


def wait_port(port, timeout=60):
    import socket
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def station(name, radios, port):
    cfg = {"name": name, "radios": radios, "addons_state": os.path.join(TMP, f"{name}-addons.json")}
    path = os.path.join(TMP, f"{name}.json")
    with open(path, "w") as f:
        json.dump(cfg, f)
    return subprocess.Popen([sys.executable, "station.py", "--config", path, "--listen", str(port),
                             "--rigctl-port", "0"],
                            cwd=ROOT, stdout=open(os.path.join(TMP, f"{name}.log"), "w"),
                            stderr=subprocess.STDOUT)


async def first_frame(url):
    """The first binary frame from a WebSocket (its size), or why there was none."""
    try:
        async with aiohttp.ClientSession() as s, s.ws_connect(url) as w:
            m = await w.receive(timeout=15)
            return len(m.data) if m.type == aiohttp.WSMsgType.BINARY else f"no audio: {m.type.name}"
    except aiohttp.WSServerHandshakeError as e:
        return f"refused: {e.status}"
    except asyncio.TimeoutError:
        return "no audio in 15 s"


async def get(url):
    async with aiohttp.ClientSession() as s, s.get(url) as r:
        return r.status, await r.text()


IC7300 = {"id": "ic7300", "name": "IC-7300", "device": "/dev/null", "web": RWEB, "rigctl": RRIG}
G90 = {"id": "g90", "name": "G90", "device": "/dev/null", "web": OTHER, "rigctl": testkit.free_port()}

radio = subprocess.Popen(
    [sys.executable, "server.py", "--sim", "--rig", "ic7300", "--host", "127.0.0.1", "--listen", str(RWEB),
     "--rigctl-host", "127.0.0.1", "--rigctl-port", str(RRIG), "--addons-state", ""],
    cwd=ROOT, stdout=open(os.path.join(TMP, "radio.log"), "w"), stderr=subprocess.STDOUT)
one = two = None
try:
    assert wait_port(RWEB), "radio did not start: " + open(os.path.join(TMP, "radio.log")).read()[-800:]
    one = station("one", [IC7300], WEB1)
    two = station("two", [IC7300, G90], WEB2)
    assert wait_port(WEB1) and wait_port(WEB2), "a station did not start"

    got = asyncio.run(first_frame(f"http://127.0.0.1:{WEB1}/r/ic7300/audio"))
    check("positive control: /r/ic7300/audio streams the radio's audio", isinstance(got, int) and got > 0, True)
    got = asyncio.run(first_frame(f"http://127.0.0.1:{WEB1}/audio"))
    check("one radio: /audio streams that radio's audio", (isinstance(got, int) and got > 0, got), (True, got))

    status, body = asyncio.run(get(f"http://127.0.0.1:{WEB2}/audio"))
    check("two radios: /audio is 404", status, 404)
    check("  and names each radio's /r/<id>/audio",
          "/r/ic7300/audio" in body and "/r/g90/audio" in body, True)
finally:
    for p in (one, two, radio):
        if p is not None:
            stop(p)

if fails:
    for n in ("radio", "one", "two"):
        p = os.path.join(TMP, f"{n}.log")
        if os.path.exists(p):
            print(f"--- {n}.log\n" + open(p).read()[-1500:])
print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
