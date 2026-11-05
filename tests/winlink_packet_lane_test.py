"""Winlink on a radio's own page (server.py) reaches the packet hub.

A Mac has no station page: its one radio server runs the Packet add-on,
and its Winlink panel must offer the hub's radio for packet the way the
station page does. Against server.py --sim with tests/fake_pat.py as Pat, a
stand-in hub and the Packet add-on pointed at it:
  - an ax25 connect while the hub is up runs through the hub (via the hub's
    name, freq= dropped for Pat), and this radio's own lane stays unarmed:
    Pat's PTT through it is refused, so the radio never keys
  - the hub down: the connect is refused, saying so
  - the Packet add-on switched off: refused, saying so
  - telnet keys no radio either; ardop still arms this radio as before
  - a server without the Packet add-on refuses ax25: no packet radio here

    python3 tests/winlink_packet_lane_test.py
"""
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time

from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import winlink_test_lib as L  # noqa: E402

TMP = tempfile.mkdtemp(prefix="wlpacket-")
fails = 0
HUB = {"up": True}
AX25 = "ax25:///N0DAJ?freq=145050"
TELNET = "telnet://N9ABC:CMSTelnet@cms.winlink.org:8772/wl2k"


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:66} got {got!r}" + ("" if ok else f" want {want!r}"))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def hub_main(port, ready):
    async def status(request):
        return web.json_response({"radio": {"up": HUB["up"], "last_rx": time.time()}, "tx": {"enabled": True}})
    app = web.Application()
    app.router.add_get("/api/status", status)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    ready.set()
    await asyncio.Event().wait()


def wait_for(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def hub_seen_up(want):
    """The Packet add-on's view of the hub, as a fresh page's hello carries it."""
    def pred():
        p = L.Page()
        try:
            return bool(((p.hello.get("addons") or {}).get("packet") or {}).get("up")) is want
        finally:
            p.close()
    return pred


def ptt_replies(flog, since):
    return [r.get("reply") for r in L.fake_log(flog)[since:] if r.get("route") == "ptt_on"]


def connect_urls(flog, since):
    return [r.get("url") for r in L.fake_log(flog)[since:] if r.get("route") == "connect"]


def session(page, flog, url, via):
    """Start a session and wait for its end; returns (armed event, notices, Pat's connect URLs, PTT replies)."""
    n0 = len(L.fake_log(flog))
    page.events.clear()
    page.send({"t": "winlink_connect", "url": url, "via": via})
    armed = page.wait(lambda e: e.get("t") == "winlink" and e.get("armed") is True, 10)
    page.wait(L.ended, 20)
    notices = [e.get("msg", "") for e in page.events if e.get("t") == "notice"]
    return armed, notices, connect_urls(flog, n0), ptt_replies(flog, n0)


hub_port = free_port()
ready = threading.Event()
loop = asyncio.new_event_loop()
threading.Thread(target=lambda: loop.run_until_complete(hub_main(hub_port, ready)), daemon=True).start()
ready.wait(10)
addons = os.path.join(TMP, "addons.json")
with open(addons, "w") as f:
    json.dump({"packet": {"enabled": True, "url": f"http://127.0.0.1:{hub_port}", "name": "Direwolf"}}, f)

srv, flog = L.start(None, "--addons", addons, "--addons-state", "")
try:
    check("the Packet add-on polled the hub up", wait_for(hub_seen_up(True), 15), True)
    page = L.Page()
    check("hello: the radio server runs the Packet add-on",
          "packet" in [a["name"] for a in page.hello["config"]["addons"]], True)

    armed, notices, urls, ptt = session(page, flog, AX25, "packet")
    check("ax25, hub up: the session runs via the hub, by its name", armed and armed.get("via"), "Direwolf")
    check("  Pat got the URL without freq= (no QSY through the hub)", urls, ["ax25:///N0DAJ"])
    check("  the page is told the radio must already be on 145.050 MHz",
          any("145.050" in m for m in notices), True)
    check("  this radio's lane refused Pat's PTT: the radio never keyed", ptt, ["RPRT -11"])
    check("  and stays disarmed afterwards", L.lane_ptt_refused(), True)

    armed, notices, urls, ptt = session(page, flog, TELNET, "telnet")
    check("telnet: a session that keys no radio", (armed and armed.get("via"), ptt), ("telnet", ["RPRT -11"]))

    armed, notices, urls, ptt = session(page, flog, L.URL, "radio")
    check("ardop: this radio is armed and keys, as before", (armed is not None, ptt), (True, ["RPRT 0"]))
    check("  and is disarmed after the session", L.lane_ptt_refused(), True)

    HUB["up"] = False
    check("the hub goes down: the add-on notices", wait_for(hub_seen_up(False), 25), True)
    armed, notices, urls, ptt = session(page, flog, AX25, "packet")
    check("ax25, hub down: refused, saying so",
          (armed, [m for m in notices if "not running" in m]), (None, ["The Direwolf packet hub is not running."]))
    check("  Pat was not asked", urls, [])

    HUB["up"] = True
    page.send({"t": "addon", "addon": "packet", "do": "power", "on": False})
    time.sleep(0.5)
    armed, notices, urls, ptt = session(page, flog, AX25, "packet")
    check("Packet add-on off: refused, saying so",
          (armed, [m for m in notices if "switched off" in m]), (None, ["The Packet add-on is switched off."]))
    page.close()
finally:
    srv.terminate()
    srv.wait(10)

srv, flog = L.start()
try:
    page = L.Page()
    armed, notices, urls, ptt = session(page, flog, AX25, "packet")
    check("no Packet add-on: ax25 is refused, no packet radio here",
          (armed, [m for m in notices if "packet radio" in m]), (None, ["No packet radio is set up on this station."]))
    page.close()
finally:
    srv.terminate()
    srv.wait(10)

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
