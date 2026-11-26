"""The Imaging add-on (addons/imaging.py), end to end against a fake radio
server whose /audio streams an SSTV picture (tests/imaging_synth.py).

  - loads, on, in the tools group; in the catalog and the manifest, whose
    need is numpy in the station's Python
  - listens to the radio's /audio with the station's token, and sends it
    nothing (receive only: not one frame back)
  - a Robot 36 picture off the air: saved as a PNG named for its time, mode,
    dial and status (320x240), announced to the pages, first in the snapshot
  - served at /imaging/img/<name> with the token; 401 without; 404 for a
    name that is not a picture's (a path, a dot file) and for one not there
  - while the radio transmits nothing is decoded (our own signal)
  - a picture with no header is found by the scan (on a worker thread) and
    saved as noheader
  - the newest `keep` pictures are kept
  - a radio that isn't up: degraded() says so, and nothing crashes
  - options are checked, each refusal naming its field
  - without numpy: "not loaded", naming numpy and doctor --fix, the station
    runs on

    python3 tests/imaging_addon_test.py
"""
import asyncio
import json
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from aiohttp import WSMsgType, web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import imaging_synth as S  # noqa: E402
import numpy as np  # noqa: E402
import testkit  # noqa: E402
from addons import Addons  # noqa: E402
from ranch_radio import catalog, manifest  # noqa: E402

fails = 0
TOKEN = "t0ken"


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


class Radio:
    def __init__(self, web, present=True):
        self.id, self.name, self.web, self.present = "ic7300", "IC-7300", web, present


class Watch:
    def __init__(self, tx=False):
        self.rig = {"freq": 14_230_000, "mode": "USB", "tx": tx}


class Host:
    def __init__(self, web, tx=False, present=True):
        self.radios = [Radio(web, present)]
        self.watches = {"ic7300": Watch(tx)}
        self.token, self.auth = TOKEN, None
        self.sent = []

    def notice(self, *a):
        pass

    def _broadcast(self, payload):
        self.sent.append(json.loads(payload))

    async def _send(self, ws, payload):
        self.sent.append(json.loads(payload))


class FakeRadio:
    """A radio server's /audio: checks the token, streams `audio` in 20 ms
    frames (faster than real time), counts what the client sends back."""

    def __init__(self, audio, speed=None):
        self.audio, self.tokens, self.back = audio.tobytes(), [], 0
        self.speed = speed               # None: as fast as it goes; N: N x real time
        self.port = testkit.free_port()

    async def start(self):
        app = web.Application()
        app.router.add_get("/audio", self.ws)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()

    async def ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.tokens.append(request.query.get("token"))

        async def reader():
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    self.back += 1
        rd = asyncio.ensure_future(reader())
        for i in range(0, len(self.audio), 640):
            await ws.send_bytes(self.audio[i:i + 640])
            if i % (640 * 50) == 0:
                await asyncio.sleep(1.0 / self.speed if self.speed else 0)   # 50 frames = 1 s of audio
        await asyncio.sleep(0.5)
        rd.cancel()
        await ws.close()
        return ws

    async def stop(self):
        await self.runner.cleanup()


async def until(pred, timeout=30):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return bool(pred())


def png_size(path):
    b = open(path, "rb").read(26)
    return b[:8] == b"\x89PNG\r\n\x1a\n", struct.unpack(">II", b[16:24])


async def main():
    tmp = Path(tempfile.mkdtemp())
    audio, _ = S.sstv("robot36", noise=0.03)
    radio = FakeRadio(audio)
    await radio.start()
    host = Host(radio.port)
    reg = Addons(host, {"imaging": {"enabled": True}}, state_path=str(tmp / "addons.json"))
    d = {x["name"]: x for x in reg.describe()}["imaging"]
    check("loads, on, in the tools group, no error", (d["group"], d["on"], d["error"]), ("tools", True, None))
    await reg.start()
    saved = lambda: [m for m in host.sent if m.get("event") == "saved"]   # noqa: E731
    check("a Robot 36 picture off the air is saved", await until(lambda: saved()), True)
    a = reg.get("imaging")
    folder = tmp / "imaging"
    names = sorted(os.listdir(folder)) if folder.exists() else []
    pics = [n for n in names if n.endswith("-sstv-robot36-14230k-complete.png")]
    check("named for its time, mode, dial and status", len(pics), 1)
    if pics:
        check("a 320x240 PNG", png_size(folder / pics[0]), (True, (320, 240)))
        check("announced to the pages", saved()[0]["image"]["name"], pics[0])
        snap = reg.snapshot()["imaging"]
        check("first in the snapshot, with the dial", (snap["images"][0]["name"], snap["images"][0]["khz"]),
              (pics[0], 14230))
    check("no live picture left behind", [n for n in names if n.startswith("live-")], [])
    check("listened with the station's token", radio.tokens[:1], [TOKEN])
    check("receive only: not one frame sent to the radio", radio.back, 0)
    check("every event carries the decoders' states", all("decoders" in m for m in host.sent if m.get("event")), True)

    # --- served
    app = web.Application()
    a.http(app, lambda r: r.query.get("token") == TOKEN)
    async with TestClient(TestServer(app)) as c:
        if pics:
            r = await c.get(f"/imaging/img/{pics[0]}?token={TOKEN}")
            check("served with the token: 200, image/png", (r.status, r.headers.get("Content-Type")), (200, "image/png"))
            r = await c.get(f"/imaging/img/{pics[0]}")
            check("without the token: 401", r.status, 401)
        for bad in ("..%2Faddons.json", ".hidden.png", "addons.json", "x.png"):
            r = await c.get(f"/imaging/img/{bad}?token={TOKEN}")
            check(f"not a picture's name ({bad}): 404", r.status, 404)
        r = await c.get(f"/imaging/img/live-fax.png?token={TOKEN}")
        check("a picture that isn't there: 404", r.status, 404)
    await reg.stop()
    await radio.stop()

    # --- while the radio transmits (and the control: the same stream, receiving)
    audio, _ = S.sstv("robot36", lines=30)
    for tx in (True, False):
        radio = FakeRadio(audio)
        await radio.start()
        host = Host(radio.port, tx=tx)
        reg = Addons(host, {"imaging": {"enabled": True}}, state_path=str(tmp / f"tx{tx}" / "addons.json"))
        await reg.start()
        await until(lambda: any(m.get("event") == "start" for m in host.sent), timeout=4)
        started = [m["event"] for m in host.sent if m.get("event") in ("start", "progress", "saved")][:1]
        if tx:
            check("while the radio transmits, nothing is decoded", started, [])
        else:
            check("  (control: the same stream, not transmitting, starts a picture)", started, ["start"])
        await reg.stop()
        await radio.stop()

    # --- a picture with no header, found by the scan on the add-on's worker thread
    audio, _ = S.sstv("martin1", noise=0.1, header=False)
    quiet = (np.random.default_rng(5).normal(0, 0.1, 70 * S.RATE) * 32000).astype(np.int16)
    # Paced: live audio comes in real time, so each scan (~0.5 s) ends long
    # before the next is due; streamed flat out, scans fall between.
    radio = FakeRadio(np.concatenate([audio, quiet]), speed=10)
    await radio.start()
    host = Host(radio.port)
    reg = Addons(host, {"imaging": {"enabled": True, "fax": False, "sstv_modes": ["martin1", "pd120"]}},
                 state_path=str(tmp / "nh" / "addons.json"))
    await reg.start()
    nh = lambda: [m for m in host.sent if m.get("event") == "saved"]   # noqa: E731
    await until(nh, timeout=90)
    names = [m["image"]["name"] for m in nh()]
    check("a picture with no header: found by the scan and saved as noheader",
          [n.split("-", 1)[1] for n in names], ["sstv-martin1-14230k-noheader.png"])
    check("  announced as found without its header",
          any("without its header" in m.get("label", "") for m in host.sent if m.get("event") == "start"), True)
    check("  receive only, still", radio.back, 0)
    await reg.stop()
    await radio.stop()

    # --- keep
    host = Host(1)
    reg = Addons(host, {"imaging": {"enabled": True, "keep": 2}}, state_path=str(tmp / "k" / "addons.json"))
    a = reg.get("imaging")
    for s in ("20261101T000000Z", "20261102T000000Z", "20261103T000000Z"):
        a._write(f"{s}-sstv-pd120-14230k-complete.png", np.zeros((4, 4, 3), dtype=np.uint8))
    a._prune()
    check("the newest `keep` pictures are kept", sorted(os.listdir(tmp / "k" / "imaging")),
          ["20261102T000000Z-sstv-pd120-14230k-complete.png", "20261103T000000Z-sstv-pd120-14230k-complete.png"])

    # --- a radio that isn't up
    host = Host(1, present=False)
    reg = Addons(host, {"imaging": {"enabled": True}}, state_path=str(tmp / "d" / "addons.json"))
    await reg.start()
    await until(lambda: reg.degraded(), timeout=3)
    check("a radio that isn't up: degraded() says so", reg.degraded(), ["imaging: IC-7300 isn't up"])
    await reg.stop()

    # --- options
    for opts, word in (({"keep": 0}, "keep"), ({"radio": "g90"}, "radio"), ({"bogus": 1}, "bogus"),
                       ({"sstv": False, "fax": False}, "sstv, fax"), ({"sstv_modes": ["pd121"]}, "sstv_modes"),
                       ({"fax_lpm": 100}, "fax_lpm"), ({"sstv": "yes"}, "sstv"), ({"sstv_scan": "yes"}, "sstv_scan")):
        reg = Addons(Host(1), {"imaging": opts}, state_path=None)
        err = reg.items["imaging"]["error"] or ""
        check(f"refuses {opts}, naming {word}", word in err and err.startswith("not loaded"), True)


asyncio.run(main())

# --- without numpy
code = ("import sys, json; sys.path.insert(0, %r); sys.modules['numpy'] = None\n"
        "from addons import Addons\n"
        "class H:\n"
        "    radios = []\n"
        "    def notice(self, *a): pass\n"
        "    def _broadcast(self, p): pass\n"
        "r = Addons(H(), {'imaging': {}}, state_path=None)\n"
        "print(json.dumps(r.items['imaging']['error']))") % str(ROOT)
out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
err = json.loads(out.stdout.strip() or "null") or ""
check("without numpy: not loaded, naming numpy and doctor --fix", ("numpy" in err, "doctor --fix" in err), (True, True))

check("in the catalog", "imaging" in catalog.by_name(), True)
m = manifest.MANIFESTS.get("imaging") or {}
check("in the manifest, an add-on in the addons block", m.get("block"), ("addons", "imaging"))
check("its need: numpy in the station's Python", [n.get("pymod") for n in m.get("needs", [])], ["numpy"])

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
