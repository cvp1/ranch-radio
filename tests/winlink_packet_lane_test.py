"""Winlink packet sessions reach the hub through the station.

The station shows the hub (its "packet" block) and its Winlink panel offers
the hub's radio for packet. Against a simulated radio behind the station
(winlink_test_lib), tests/fake_pat.py as Pat and a stand-in hub:
  - an ax25 connect while the hub is up runs through the hub (via the hub's
    name, freq= dropped for Pat), and the radio's own lane stays unarmed:
    Pat's PTT through it is refused, so the radio never keys
  - the hub down (nothing listening): the connect is refused, saying so; back
    up, the next one runs
  - telnet keys no radio either; ardop still arms the radio as before
  - a station without a packet block refuses ax25: no packet radio here

    python3 tests/winlink_packet_lane_test.py
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time

from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testkit  # noqa: E402
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


async def hub_main(port, ready):
    """A stand-in hub that listens while HUB["up"] and not at all otherwise:
    the station's presence poll (a TCP connect) is what "running" means."""
    async def status(request):
        return web.json_response({"radio": {"up": True, "last_rx": time.time()}, "tx": {"enabled": True}})
    app = web.Application()
    app.router.add_get("/api/status", status)
    runner = web.AppRunner(app)
    await runner.setup()
    site = None
    while True:
        if HUB["up"] and site is None:
            site = web.TCPSite(runner, "127.0.0.1", port)
            await site.start()
            ready.set()
        elif not HUB["up"] and site is not None:
            await site.stop()
            site = None
        await asyncio.sleep(0.2)


def hub_seen_up(want):
    """The station's view of the hub, as a fresh page's hello carries it."""
    def pred():
        p = L.Page()
        try:
            return bool((p.hello.get("packet") or {}).get("present")) is want
        finally:
            p.close()
    return pred


def ptt_replies(flog, since):
    return [r.get("reply") for r in L.fake_log(flog)[since:] if r.get("route") == "ptt_on"]


def connect_urls(flog, since):
    return [r.get("url") for r in L.fake_log(flog)[since:] if r.get("route") == "connect"]


def session(page, flog, url, via):
    """Run a session to its end: (armed event, notices, Pat's connect URLs, PTT replies)."""
    n0 = len(L.fake_log(flog))
    page.events.clear()
    page.send({"t": "winlink_connect", "url": url, "via": via})
    armed = page.wait(lambda e: e.get("t") == "winlink" and e.get("armed") is True, 10)
    page.wait(L.ended, 20)
    notices = [e.get("msg", "") for e in page.events if e.get("t") == "notice"]
    return armed, notices, connect_urls(flog, n0), ptt_replies(flog, n0)


hub_port = testkit.free_port()
ready = threading.Event()
loop = asyncio.new_event_loop()
threading.Thread(target=lambda: loop.run_until_complete(hub_main(hub_port, ready)), daemon=True).start()
ready.wait(10)
addons = os.path.join(TMP, "addons.json")
with open(addons, "w") as f:
    json.dump({"packet": {"enabled": True, "url": f"http://127.0.0.1:{hub_port}", "name": "Direwolf"}}, f)

srv, flog = L.start(None, "--addons", addons, "--addons-state", "")
try:
    check("the station sees the hub up", testkit.wait_for(hub_seen_up(True), 15, 0.2), True)
    page = L.Page()
    check("hello: the station shows the hub, by its name",
          (page.hello["packet"].get("configured"), page.hello["packet"].get("name")), (True, "Direwolf"))

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
    check("the hub goes down: the station notices", testkit.wait_for(hub_seen_up(False), 25, 0.2), True)
    armed, notices, urls, ptt = session(page, flog, AX25, "packet")
    check("ax25, hub down: refused, saying so",
          (armed, [m for m in notices if "not running" in m]), (None, ["The Direwolf packet hub is not running."]))
    check("  Pat was not asked", urls, [])

    HUB["up"] = True
    check("the hub comes back: the station notices", testkit.wait_for(hub_seen_up(True), 25, 0.2), True)
    armed, notices, urls, ptt = session(page, flog, AX25, "packet")
    check("ax25 again: the session runs via the hub", armed and armed.get("via"), "Direwolf")
    page.close()
finally:
    srv.terminate()
    srv.wait(10)

srv, flog = L.start(None, "--addons-state", "")       # never the user's own add-on state
try:
    page = L.Page()
    armed, notices, urls, ptt = session(page, flog, AX25, "packet")
    check("no packet block: ax25 is refused, no packet radio here",
          (armed, [m for m in notices if "packet radio" in m]), (None, ["No packet radio is set up on this station."]))
    page.close()
finally:
    srv.terminate()
    srv.wait(10)

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
