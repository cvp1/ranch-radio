"""The radio server's cross-site guard (modules plan M0, 2026-10-21).

Before this, server.py checked only an optional token, which no host sets, so
a page from the internet open in a LAN browser could open the radio's
WebSocket (PTT, codec Save, add-ons). Now, as on the station:
  - a WebSocket upgrade with a foreign Origin is refused (403)
  - any request naming an unknown Host is refused (403), against DNS rebinding
  - the radio's own page (same Origin), and apps with no Origin, still connect
  - --allow-host and --tls-name add names

    python3 tests/server_guard_test.py
"""
import asyncio
import os
import random
import socket
import subprocess
import sys
import time

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = 19000 + random.randrange(1000)
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:64} got {got!r}" + ("" if ok else f" want {want!r}"))


def start():
    p = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB),
                          "--rigctl-port", "0", "--addons-state", "",
                          "--allow-host", "radio.example.org", "--tls-name", "shack.example.net"],
                         cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", WEB), timeout=0.2).close()
            return p
        except OSError:
            time.sleep(0.1)
    p.kill()
    print(p.stdout.read())
    raise SystemExit("server did not start")


async def ws_status(headers):
    """The HTTP status of a WebSocket upgrade with these headers (101 = opened)."""
    async with aiohttp.ClientSession() as s:
        try:
            async with s.ws_connect(f"http://127.0.0.1:{WEB}/ws", headers=headers) as ws:
                await ws.receive(timeout=5)          # the hello
                return 101
        except aiohttp.WSServerHandshakeError as e:
            return e.status


async def get_status(headers):
    async with aiohttp.ClientSession() as s:
        async with s.get(f"http://127.0.0.1:{WEB}/", headers=headers) as r:
            return r.status


async def main():
    me = f"127.0.0.1:{WEB}"
    check("no Origin (an app, the station's proxy): opens", await ws_status({}), 101)
    check("the radio's own page (same Origin): opens", await ws_status({"Origin": f"http://{me}"}), 101)
    check("a foreign Origin: refused", await ws_status({"Origin": "https://evil.example"}), 403)
    check("same host, other port as Origin: refused", await ws_status({"Origin": f"http://127.0.0.1:{WEB + 1}"}), 403)
    check("https Origin for an http page: refused", await ws_status({"Origin": f"https://{me}"}), 403)
    check("DNS rebinding (Host evil.example): refused",
          await ws_status({"Host": f"evil.example:{WEB}", "Origin": f"http://evil.example:{WEB}"}), 403)
    check("GET with an unknown Host: refused", await get_status({"Host": "evil.example"}), 403)
    check("GET by a single-label name (mac-shack): served", await get_status({"Host": f"mac-shack:{WEB}"}), 200)
    check("GET by a .local name: served", await get_status({"Host": f"mac-shack.local:{WEB}"}), 200)
    check("--allow-host name: served", await get_status({"Host": "radio.example.org"}), 200)
    check("--tls-name name: served", await get_status({"Host": "shack.example.net"}), 200)
    check("--allow-host page opens its socket",
          await ws_status({"Host": "radio.example.org", "Origin": "http://radio.example.org"}), 101)


server = start()
try:
    asyncio.run(main())
finally:
    server.terminate()
    try:
        server.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
