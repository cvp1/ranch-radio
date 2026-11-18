"""Wavelog CAT (wavelog_cat.py): what WaveLogGate did, from the station.

Covers:
  - the block: refusals name the field (url, origins with a path, every_s,
    an unknown field); render fills url and key_file from logsync.wavelog,
    and refuses a radios entry naming no radio in the host file
  - push: a simulated IC-7300 behind a station; Wavelog's /api/radio gets
    {key, radio, frequency (Hz), mode, cat_url}; a retune reaches it within
    seconds; CW-R goes as CW
  - qsy: Wavelog's GET <cat_url>/<hz>/<mode> from the configured origin
    retunes the radio (frequency and mode) and answers with CORS for that
    origin; the next push carries the new frequency
  - qsy refused: no Origin (an <img> on any page), a foreign Origin, an
    unknown mode or radio; none of them moves the radio
  - a station token turns qsy into a refusal at load, not a silent risk
  - Wavelog refusing the key: the station's hello shows error "auth" and
    no secret; nothing is retried before the backoff
  - a broken block: the station runs, the hello says "not loaded"

    python3 tests/wavelog_cat_test.py
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web

import testkit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import wavelog_cat  # noqa: E402
from ranch_radio import render  # noqa: E402

fails = 0
LOGS = []                      # each station's log, shown when a check fails
KEY = "wl-test-key-0123"
ORIGIN = "https://192.168.1.21:8087"


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:66} got {got!r}" + ("" if ok else f" want {want!r}"))


def refused(block):
    try:
        wavelog_cat.check_block(block)
        return None
    except ValueError as e:
        return str(e)


# ================================================================ the block

base = {"url": "http://127.0.0.1:8086", "key_file": "/tmp/k"}
check("block: a minimal one is fine", refused(base), None)
check("block: url required", "wavelog_cat.url" in (refused({"key_file": "/tmp/k"}) or ""), True)
check("block: an origin with a path is refused",
      "qsy.origins" in (refused({**base, "qsy": {"station_url": "https://h:7300", "origins": ["https://h:8087/x"]}})
                        or ""), True)
check("block: every_s out of range", "every_s" in (refused({**base, "every_s": 1}) or ""), True)
check("block: an unknown field", "unknown" in (refused({**base, "flrig": 1}) or ""), True)

host = {"host": "mac-shack", "radios": [{"id": "ic7300", "name": "IC-7300"}],
        "logsync": {"wavelog": {"url": "http://127.0.0.1:8086", "station_profile_id": 1,
                                "key_file": "~/.config/ranch-radio/wavelog-key"}},
        "wavelog_cat": {"radios": {"ic7300": "IC-7300"}}}
b = render._wavelog_cat_block(host)
check("render: url and key_file come from logsync.wavelog",
      (b["url"], b["key_file"]), ("http://127.0.0.1:8086", "~/.config/ranch-radio/wavelog-key"))
try:
    render._validate_wavelog_cat({**host, "wavelog_cat": {"radios": {"g90": "G90"}}})
    why = None
except render.HostError as e:
    why = str(e)
check("render: a radios entry naming no radio is refused", "g90" in (why or ""), True)


# ================================================================ a fake Wavelog /api/radio

class FakeRadioApi:
    def __init__(self):
        self.calls = []
        self.key = KEY

    def app(self):
        a = web.Application()
        a.router.add_post("/index.php/api/radio", self.radio)
        return a

    async def radio(self, request):
        body = await request.json()
        ok = body.get("key") == self.key
        self.calls.append({k: v for k, v in body.items() if k != "key"} | {"key_ok": ok})
        if not ok:
            return web.json_response({"status": "failed", "reason": "missing api key"}, status=401)
        return web.json_response({"status": "success"})


async def serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    port = testkit.free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, port


def start(block, token=None):
    tmp = Path(tempfile.mkdtemp(prefix="wavelog-cat-"))
    web_p, radio_web, rig = testkit.free_port(), testkit.free_port(), testkit.free_port()
    radio = subprocess.Popen([sys.executable, "server.py", "--sim", "--rig", "ic7300", "--no-audio", "--host", "127.0.0.1",
                              "--listen", str(radio_web), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(rig)]
                             + (["--token", token] if token else []),
                             cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cfg = {"name": "test", "radios": [{"id": "ic7300", "name": "IC-7300", "web": radio_web, "rigctl": rig}],
           "addons_state": str(tmp / "addons.json"), "wavelog_cat": block}
    (tmp / "station.json").write_text(json.dumps(cfg))
    st = subprocess.Popen([sys.executable, "station.py", "--config", str(tmp / "station.json"), "--host", "127.0.0.1",
                           "--listen", str(web_p), "--rigctl-port", "0"] + (["--token", token] if token else []),
                          cwd=ROOT, stdout=open(tmp / "station.log", "w"), stderr=subprocess.STDOUT)
    LOGS.append(tmp / "station.log")
    return web_p, radio_web, radio, st


def stop(*procs):
    for p in procs:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


async def hello(web_p, token=None):
    url = f"http://127.0.0.1:{web_p}/ws" + (f"?token={token}" if token else "")
    async with aiohttp.ClientSession() as s, s.ws_connect(url) as ws:
        return json.loads((await ws.receive(timeout=10)).data)


async def radio_state(radio_web):
    async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{radio_web}/ws") as ws:
        return json.loads((await ws.receive(timeout=10)).data).get("state") or {}


async def set_radio(radio_web, k, v):
    async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{radio_web}/ws") as ws:
        await ws.receive(timeout=10)
        await ws.send_str(json.dumps({"t": "set", "k": k, "v": v}))
        await asyncio.sleep(0.3)


async def wait_until(pred, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.1)
    return pred()


async def wait_port(port, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            _, w = await asyncio.open_connection("127.0.0.1", port)
            w.close()
            return True
        except OSError:
            await asyncio.sleep(0.2)
    return False


async def main():
    fake = FakeRadioApi()
    runner, wl_port = await serve(fake.app())
    tmp = Path(tempfile.mkdtemp(prefix="wavelog-cat-key-"))
    key = tmp / "wavelog-key"
    key.write_text(KEY + "\n")
    key.chmod(0o600)
    block = {"url": f"http://127.0.0.1:{wl_port}", "key_file": str(key), "radios": {"ic7300": "IC-7300"},
             "qsy": {"station_url": "https://192.168.1.21:7300", "origins": [ORIGIN]}, "every_s": 10}

    # ------------------------------------------------------------ push and qsy
    web_p, radio_web, radio, st = start(block)
    try:
        await wait_port(web_p)
        await wait_port(radio_web)
        got = await wait_until(lambda: any(c.get("frequency") for c in fake.calls))
        check("push: Wavelog hears the radio", got, True)
        first = next((c for c in fake.calls if c.get("frequency")), {})
        check("push: the key, the radio's Wavelog name, Hz, a mode", (first.get("key_ok"), first.get("radio"),
              isinstance(first.get("frequency"), int) and first["frequency"] > 1_000_000, bool(first.get("mode"))),
              (True, "IC-7300", True, True))
        check("push: cat_url is the station's qsy route for this radio", first.get("cat_url"),
              "https://192.168.1.21:7300/wavelog/qsy/ic7300")

        await set_radio(radio_web, "freq", 14_074_000)
        check("push: a retune reaches Wavelog", await wait_until(
            lambda: any(c.get("frequency") == 14_074_000 for c in fake.calls), 10), True)
        await set_radio(radio_web, "mode", "CW-R")
        check("push: CW-R goes as CW", await wait_until(
            lambda: any(c.get("mode") == "CW" for c in fake.calls), 10), True)

        async with aiohttp.ClientSession() as s:
            qsy = f"http://127.0.0.1:{web_p}/wavelog/qsy/ic7300"
            async with s.get(f"{qsy}/7074000/usb") as r:
                check("qsy: no Origin is refused", r.status, 403)
            async with s.get(f"{qsy}/7074000/usb", headers={"Origin": "https://evil.example"}) as r:
                check("qsy: a foreign Origin is refused, without CORS", (r.status, r.headers.get(
                    "Access-Control-Allow-Origin")), (403, None))
            async with s.get(f"{qsy}/7074000/warble", headers={"Origin": ORIGIN}) as r:
                check("qsy: an unknown mode is refused", r.status, 404)
            async with s.get(f"http://127.0.0.1:{web_p}/wavelog/qsy/g90/7074000/usb", headers={"Origin": ORIGIN}) as r:
                check("qsy: an unknown radio is refused", r.status, 404)
            check("qsy: none of those moved the radio", (await radio_state(radio_web)).get("freq"), 14_074_000)

            async with s.get(f"{qsy}/7074000/pktusb", headers={"Origin": ORIGIN}) as r:
                check("qsy: from the configured Wavelog: 200 with CORS for it",
                      (r.status, r.headers.get("Access-Control-Allow-Origin")), (200, ORIGIN))
            state = await radio_state(radio_web)
            check("qsy: the radio is on 7.074 MHz USB, data on",
                  (state.get("freq"), state.get("mode"), bool(state.get("data"))), (7_074_000, "USB", True))
            async with s.get(f"{qsy}/7074000/pktusb", headers={"Origin": ORIGIN}) as r:
                check("qsy: the frequency it is already on: still 200", r.status, 200)
        check("push: the new frequency follows", await wait_until(
            lambda: fake.calls[-1].get("frequency") == 7_074_000, 10), True)

        h = await hello(web_p)
        wc = h.get("wavelog_cat") or {}
        check("hello: loaded, qsy on, no error", (wc.get("loaded"), wc.get("qsy"), wc.get("error")), (True, True, None))
        check("hello: never carries the key", KEY in json.dumps(h), False)
    finally:
        stop(st, radio)

    # ------------------------------------------------------------ a refused key
    fake.calls.clear()
    fake.key = "another-key"
    web_p, radio_web, radio, st = start(block)
    try:
        await wait_port(web_p)
        check("auth: Wavelog is asked", await wait_until(lambda: len(fake.calls) >= 1), True)
        h = {}
        end = time.monotonic() + 10
        while time.monotonic() < end and (h.get("wavelog_cat") or {}).get("error") != "auth":
            h = await hello(web_p)
            await asyncio.sleep(0.3)
        check("auth: the hello says auth", (h.get("wavelog_cat") or {}).get("error"), "auth")
        await set_radio(radio_web, "freq", 21_074_000)
        await asyncio.sleep(2.5)
        check("auth: held, not retried on every change", len(fake.calls), 1)
    finally:
        stop(st, radio)
    fake.key = KEY

    # ------------------------------------------------------------ a station token: qsy refused at load
    web_p, radio_web, radio, st = start(block, token="t0k")
    try:
        await wait_port(web_p)
        h = await hello(web_p, token="t0k")
        wc = h.get("wavelog_cat") or {}
        check("token: not loaded, the reason names the token", (wc.get("loaded"), "token" in (wc.get("error") or "")),
              (False, True))
    finally:
        stop(st, radio)

    # ------------------------------------------------------------ a broken block: the station carries on
    web_p, radio_web, radio, st = start({"url": "ftp://x", "key_file": str(key)})
    try:
        await wait_port(web_p)
        h = await hello(web_p)
        check("broken block: the station runs, the hello says not loaded",
              ((h.get("wavelog_cat") or {}).get("loaded"), h.get("t")), (False, "hello"))
    finally:
        stop(st, radio)
    await runner.cleanup()


asyncio.run(main())
if fails:
    for p in LOGS:
        print(f"--- {p}\n" + "".join(l for l in p.read_text().splitlines(True) if "wavelog" in l)[-3000:])
print("\nall ok" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
