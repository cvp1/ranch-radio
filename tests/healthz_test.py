"""GET /healthz on the radio server, the station and the packet hub (all
simulated or fake, no transmit): 200 with {ok, role, uptime_s, pid,
degraded} plus the role's facts; the radio's tx_stopped follows STOP TX; an
unknown Host is refused (DNS rebinding guard) and a radio with --token wants
it. /healthz is readiness only; service.idle() still decides restarts.

    python3 tests/healthz_test.py
"""
import asyncio
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import aiohttp  # noqa: E402
import testkit  # noqa: E402
import tnc_hub_test as T  # noqa: E402
import winlink_test_lib as L  # noqa: E402

fails = 0
KEYS = {"ok", "role", "uptime_s", "pid", "degraded"}


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


def get(port, path="/healthz", host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read()
            return r.status, (json.loads(body) if r.headers.get_content_type() == "application/json" else None)
    except urllib.error.HTTPError as e:
        return e.code, None
    except OSError:
        return None, None


def radio(*extra):
    web, rig = testkit.free_port(), testkit.free_port()
    p = subprocess.Popen([sys.executable, "server.py", "--sim", "--rig", "ic7300", "--no-audio", "--host", "127.0.0.1",
                          "--listen", str(web), "--rigctl-port", str(rig), *extra], cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    if not testkit.wait_for(lambda: get(web, "/")[0] is not None, 60, 0.2):
        p.kill()
        raise SystemExit("radio server did not start")
    return p, web


async def stop_tx(web):
    async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{web}/ws") as ws:
        await ws.receive()
        await ws.send_str(json.dumps({"t": "tx_stop"}))
        async for m in ws:
            d = json.loads(m.data)
            if d.get("t") == "tx_stop" and d.get("stopped"):
                return True
    return False


procs = []
try:
    p, web = radio()
    procs.append(p)
    testkit.wait_for(lambda: (get(web)[1] or {}).get("rig_connected"), 20, 0.2)
    code, h = get(web)
    check("radio: 200", code, 200)
    check("radio: the common keys", KEYS <= set(h or {}), True)
    check("radio: its role", (h or {}).get("role"), "radio")
    check("radio: the simulated rig is connected, so ok", ((h or {}).get("rig_connected"), (h or {}).get("ok")), (True, True))
    check("radio: not stopped, not keyed, not armed",
          ((h or {}).get("tx_stopped"), (h or {}).get("tx"), (h or {}).get("winlink_armed")), (False, False, False))
    check("radio: STOP TX latches", asyncio.run(stop_tx(web)), True)
    check("radio: and /healthz says so", (get(web)[1] or {}).get("tx_stopped"), True)
    check("radio: an unknown Host is refused", get(web, host="evil.example")[0], 403)
    check("radio: a LAN name is let in", get(web, host="linux-station.local")[0], 200)

    p2, web2 = radio("--token", "s3cret")
    procs.append(p2)
    check("radio with --token: refused without it", get(web2)[0], 401)
    check("  and answered with it", get(web2, "/healthz?token=s3cret")[0], 200)

    fake = T.FakeKissRadio().start()
    hub = T.HubProc(fake, tx=False)
    try:
        code, h = get(hub.http)
        check("hub: 200, its role", (code, (h or {}).get("role")), (200, "hub"))
        check("hub: the radio is up, transmit off", ((h or {}).get("radio_up"), (h or {}).get("tx_enabled")), (True, False))
        check("hub: an unknown Host is refused", get(hub.http, host="evil.example")[0], 403)
    finally:
        hub.stop()
        fake.stop()

    st, _log = L.start(None, winlink=False, wait_pat=False)
    procs.append(st)
    try:
        testkit.wait_for(lambda: get(L.WEB)[0] == 200, 30, 0.2)
        code, h = get(L.WEB)
        check("station: 200, its role, its radio", (code, (h or {}).get("role"), (h or {}).get("radios")), (200, "station", 1))
        check("station: an unknown Host is refused", get(L.WEB, host="evil.example")[0], 403)
    finally:
        L.stop(st)
        procs.remove(st)
finally:
    for p in procs:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
