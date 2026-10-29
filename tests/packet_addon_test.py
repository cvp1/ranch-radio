"""The Packet add-on: the packet hub as a tab on a Mac's radio page.

  - a Mac host file with a packet block (and a radio) renders the add-on,
    on, pointed at the hub; a hub-only Mac (no radio server) doesn't; a host
    file that lists "packet" itself keeps its own entry
  - against a stand-in hub and a simulated radio server: the hub's page and
    API come through /packet/; dot segments, encoded "?" and websockets are
    refused; a cross-site POST (the transmit switch) never reaches the hub;
    the hub gets the X-Ranch-Radio header; switched off, the proxy answers 503

    python3 tests/packet_addon_test.py
"""
import asyncio
import json
import random
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
import yarl
from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ranch_radio import render  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="packet-addon-"))
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


# ---------------------------------------------------------------- rendering
mac = json.loads((ROOT / "examples/hosts/mac-shack.json").read_text())
mac["packet"] = {"url": "http://127.0.0.1:7310", "agwpe": "127.0.0.1:8000", "tnc": "direwolf", "mycall": "N9ABC",
                 "name": "Direwolf", "direwolf": {"usb": "1209:7388", "audio": "AIOC Audio"}}
adds = render.mac_addons(mac)
check("a Mac with a packet hub runs the Packet add-on, on, pointed at the hub",
      adds.get("packet"), {"enabled": True, "url": "http://127.0.0.1:7310", "name": "Direwolf"})
check("  beside its own add-ons", "opengd77" in adds, True)
arts = {a.path: a for a in render.artifacts(mac, home="/H")}
check("  addons.json carries it", "packet" in arts[render.MAC_ADDONS].content, True)
radio = next(a for a in arts.values() if a.module == "ic7300")
check("  and the radio server is given addons.json", "--addons" in radio.content["ProgramArguments"], True)
hub_only = json.loads((ROOT / "examples/hosts/mac-aioc.json").read_text())
check("a hub-only Mac (no radio server to show it): no add-on", "packet" in render.mac_addons(hub_only), False)
own = dict(mac, addons=dict(mac["addons"], packet={"enabled": False, "url": "http://127.0.0.1:7399"}))
check("a host file's own packet entry is kept", render.mac_addons(own)["packet"]["url"], "http://127.0.0.1:7399")


# ---------------------------------------------------------------- against a stand-in hub
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


seen = []                                         # (method, path, X-Ranch-Radio) the hub was sent


async def hub_main(port, ready):
    async def any_(request):
        seen.append((request.method, request.path, request.headers.get("X-Ranch-Radio")))
        if request.path == "/api/status":
            return web.json_response({"radio": {"up": True, "last_rx": time.time()}, "tx": {"enabled": False}})
        if request.path == "/":
            return web.Response(text="<html>hub page</html>", content_type="text/html")
        return web.json_response({"ok": True})
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", any_)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    ready.set()
    await asyncio.Event().wait()


hub_port, web_port = free_port(), free_port()
loop = asyncio.new_event_loop()
import threading  # noqa: E402
ready = threading.Event()
threading.Thread(target=lambda: loop.run_until_complete(hub_main(hub_port, ready)), daemon=True).start()
ready.wait(10)

(TMP / "addons.json").write_text(json.dumps({"packet": {"enabled": True, "url": f"http://127.0.0.1:{hub_port}"}}))
srv = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(web_port),
                        "--rigctl-port", "0", "--addons", str(TMP / "addons.json"), "--addons-state", ""],
                       cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
base = f"http://127.0.0.1:{web_port}"


async def client():
    out = {}
    async with aiohttp.ClientSession() as s:
        for _ in range(100):
            try:
                async with s.get(base + "/"):
                    break
            except aiohttp.ClientError:
                await asyncio.sleep(0.1)
        async with s.get(base + "/packet/api/status") as r:
            out["status"] = (r.status, (await r.json()).get("radio", {}).get("up"))
        async with s.get(base + "/packet/") as r:
            out["page"] = (r.status, "hub page" in await r.text())
        async with s.get(base + "/packet", allow_redirects=False) as r:
            out["slash"] = (r.status, r.headers.get("Location"))
        # encoded=True: send the path as written (the client would tidy x/../ away itself)
        async with s.get(yarl.URL(base + "/packet/x/../api/lend", encoded=True), allow_redirects=False) as r:
            out["dots"] = r.status
        async with s.get(base + "/packet/api/lend%3fx") as r:
            out["encoded"] = r.status
        async with s.get(base + "/packet/ws", headers={"Upgrade": "websocket", "Connection": "Upgrade"}) as r:
            out["ws"] = r.status
        n = len(seen)
        async with s.post(base + "/packet/api/tx", json={"enabled": True},
                          headers={"Origin": "http://evil.example"}) as r:
            out["xsite"] = (r.status, len(seen) - n)
        async with s.post(base + "/packet/api/tx", json={"enabled": False},
                          headers={"Origin": base}) as r:
            out["same_site"] = r.status
        out["header"] = [h for m, p, h in seen if p == "/api/tx"]
        async with s.ws_connect(base + "/ws") as ws:
            await ws.send_str(json.dumps({"t": "addon", "addon": "packet", "do": "power", "on": False}))
            await asyncio.sleep(0.5)
        async with s.get(base + "/packet/api/status") as r:
            out["off"] = r.status
    return out


try:
    o = asyncio.run(client())
finally:
    srv.terminate()
    srv.wait(10)

check("the hub's API comes through /packet/", o["status"], (200, True))
check("  and its page", o["page"], (200, True))
check("  /packet redirects to /packet/", o["slash"], (302, "/packet/"))
check("refused: a dot segment (x/../api/lend)", o["dots"], 400)
check("refused: an encoded '?' (api/lend%3fx)", o["encoded"], 400)
check("refused: a websocket (the hub has none)", o["ws"], 400)
check("a cross-site POST never reaches the hub", (o["xsite"][0] >= 400, o["xsite"][1]), (True, 0))
check("a same-site POST does, with the hub's X-Ranch-Radio header", (o["same_site"], o["header"]), (200, ["1"]))
check("switched off: the proxy answers 503", o["off"], 503)

subprocess.run(["rm", "-rf", str(TMP)])
print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
