"""S1: the station front (station.py) with two simulated radios.

Runs two server.py --sim radios (G90 and QMX, each on its own web, rigctl
and Winlink lane ports, as on linux-station), a fake UV-PRO hub, tests/fake_pat.py
as Pat, tests/fake_ardopcf.py as the modem and tests/fake_mesh_bridge.py as
the node, then station.py in front, and checks:
  - the page lists both radios with their own ports; the shared port goes to
    the radio connected longest, and moves only when that radio goes away
  - a connection to the shared port reaches that radio; its own ports reach
    each radio whatever the shared port does
  - each radio's page and its WebSocket come through /r/<id>/
  - an ARDOP session via the QMX: ardopcf starts on the QMX's sound card,
    the QMX's lane is armed (Pat keys through the station's lane port), the
    session ends and the QMX disarms; the G90 is never armed
  - via a radio that isn't connected, or an ARDOP session with no radio
    named while two could take it: refused, nothing armed
  - ARDOP moving to the other radio restarts ardopcf on that radio's card
  - the operator's page closing mid-session disarms the radio at once
  - a packet session (ax25) arms no HF radio
  - the packet hub's page comes through /packet/, but its transmit switch
    can't be turned on from another machine through the station
  - Mesh comes from the station, not from a radio

    python3 tests/station_test.py
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
from aiohttp import web

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
B = 20000 + random.randrange(0, 4000, 40)
WEB, SHARED, LANE, PAT, ARDOP, HUB = B, B + 1, B + 2, B + 3, B + 4, B + 6
G90 = {"web": B + 10, "rigctl": B + 11, "lane": B + 12}
QMX = {"web": B + 20, "rigctl": B + 21, "lane": B + 22}
TMP = tempfile.mkdtemp(prefix="station-test-")
ARDOP_STARTS = os.path.join(TMP, "ardop-starts.jsonl")
PAT_LOG = os.path.join(TMP, "pat.jsonl")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


def wait_port(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def radio(rig, p, *extra):
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--rig", rig, "--no-audio", "--host", "127.0.0.1",
         "--listen", str(p["web"]), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(p["rigctl"]),
         "--winlink-rigctl-port", str(p["lane"]), *extra],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert wait_port(p["web"]) and wait_port(p["rigctl"]), f"{rig} did not start"
    return proc


def stop(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def rigctl(port, *cmds):
    s = socket.create_connection(("127.0.0.1", port), timeout=3)
    f = s.makefile("r")
    out = []
    for c in cmds:
        s.sendall((c + "\n").encode())
        out.append(f.readline().strip())
    s.close()
    return out


def own_addrs():
    """This machine's IPv4 addresses that another LAN machine could be,
    LAN first. Not the default route's: with a VPN up (on a Mac)
    that is the tunnel's own address, and a connection to it never arrives.
    Loopback, link-local and point-to-point (VPN tunnel) addresses are left out."""
    found = []
    try:                                                    # Linux
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True,
                             text=True, timeout=3).stdout
        for line in out.splitlines():
            f = line.split()
            if "inet" in f:
                found.append((f[f.index("inet") + 1].split("/")[0], "peer" in f))
    except (OSError, subprocess.SubprocessError):
        pass
    if not found:
        try:                                                # macOS / BSD
            out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        p2p = False
        for line in out.splitlines():
            if line and not line[0].isspace():
                p2p = "POINTOPOINT" in line
            f = line.split()
            if f[:1] == ["inet"] and len(f) > 1:
                found.append((f[1], p2p or "-->" in f))
    ips = [ip for ip, ptp in found
           if not ptp and not ip.startswith(("127.", "169.254."))]
    rank = lambda ip: (0 if ip.startswith("192.168.")      # noqa: E731
                       else 1 if ip.startswith("10.") or ip.startswith("172.") else 2)
    return sorted(dict.fromkeys(ips), key=rank)


def lan_ip(port):
    """The first of own_addrs() that really accepts a connection on `port`
    (a caller here that the station sees as not-this-machine), or None."""
    for ip in own_addrs():
        try:
            socket.create_connection((ip, port), timeout=1).close()
            return ip
        except OSError:
            continue
    return None


# --- a fake UV-PRO hub: its page, status and transmit switch -------------------

hub_tx = []
hub_lend = []
hub_hdr = []          # (X-Ranch-Radio, Origin) as each POST reached the fake hub


def fake_hub():
    async def index(_):
        return web.Response(text="<html>packet hub</html>", content_type="text/html")

    async def status(_):
        return web.json_response({"link": "up"})

    async def tx(req):
        hub_hdr.append((req.headers.get("X-Ranch-Radio"), req.headers.get("Origin")))
        hub_tx.append((await req.json(), req.remote))
        return web.json_response({"enabled": True})

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    async def lend(req):
        hub_lend.append(req.path)
        return web.json_response({"lent": None})

    app.router.add_post("/api/tx", tx)
    app.router.add_post("/api/lend", lend)
    app.router.add_post("/api/return", lend)
    loop = asyncio.new_event_loop()

    def run():
        asyncio.set_event_loop(loop)
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", HUB).start())
        loop.run_forever()
    threading.Thread(target=run, daemon=True).start()
    assert wait_port(HUB)


class Page:
    """The station page's WebSocket."""

    def __init__(self, host="127.0.0.1"):
        self.loop = asyncio.new_event_loop()
        self.events = []
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.run(self._open(host))

    def run(self, coro, timeout=15):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _open(self, host):
        self.sess = aiohttp.ClientSession()
        self.ws = await self.sess.ws_connect(f"http://{host}:{WEB}/ws")
        self.hello = json.loads((await self.ws.receive()).data)
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                self.events.append(json.loads(msg.data))

    def send(self, m):
        self.run(self.ws.send_str(json.dumps(m)))

    def wait(self, pred, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            for e in list(self.events):
                if pred(e):
                    return e
            time.sleep(0.05)
        return None

    def radios(self):
        e = next((e for e in reversed(self.events) if e.get("t") == "radios"), self.hello)
        return {r["id"]: r for r in e["radios"]}

    def close(self):
        async def _c():
            await self.ws.close()
            await self.sess.close()
        self.run(_c())


def wl(pred):
    return lambda e: e.get("t") == "winlink" and pred(e)


def radio_mode(p):
    async def q():
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(f"http://127.0.0.1:{p['web']}/ws") as w:
                st = json.loads((await w.receive()).data)["state"]
                return st["mode"], bool(st.get("data"))
    return asyncio.run(q())


def armed_on_radio(p):
    """The radio's own view: is its Winlink lane armed? (its page's hello)"""
    async def q():
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(f"http://127.0.0.1:{p['web']}/ws") as w:
                return json.loads((await w.receive()).data)["winlink"]["armed"]
    return asyncio.run(q())


def ardop_starts():
    try:
        with open(ARDOP_STARTS) as f:
            return [json.loads(l)["argv"] for l in f if l.strip()]
    except OSError:
        return []


cfg = {
    "name": "Test Station",
    "radios": [
        {"id": "g90", "name": "G90", **G90, "audio_in": "g90-in", "audio_out": "g90-out"},
        {"id": "qmx", "name": "QMX", **QMX, "audio_in": "qmx-in", "audio_out": "qmx-out"},
    ],
    "packet": {"url": f"http://127.0.0.1:{HUB}", "agwpe": "127.0.0.1:8000"},
    "winlink": {"pat": f"{sys.executable} tests/fake_pat.py", "pat_port": PAT, "lane_port": LANE,
                "ardopcf": f"{sys.executable} tests/fake_ardopcf.py", "ardop_port": ARDOP,
                "ardop_logs": os.path.join(TMP, "ardop")},
    "mesh": {"port": "/dev/null", "python": sys.executable, "bridge": "tests/fake_mesh_bridge.py", "log": ""},
    "addons_state": os.path.join(TMP, "addons.json"),   # never the real ~/.local/state one
}
cfg_path = os.path.join(TMP, "station.json")
with open(cfg_path, "w") as f:
    json.dump(cfg, f)

fake_hub()
g90 = radio("g90", G90)
time.sleep(1.2)                            # the G90 is connected first
qmx = radio("qmx", QMX, "--winlink-mode", "USB")   # ARDOP: USB, never DiGi
env = dict(os.environ, FAKE_PAT_RIGCTL=str(LANE), FAKE_PAT_LOG=PAT_LOG, FAKE_PAT_TX_S="1",
           FAKE_ARDOP_LOG=ARDOP_STARTS)
front = subprocess.Popen([sys.executable, "station.py", "--config", cfg_path, "--listen", str(WEB),
                          "--rigctl-port", str(SHARED)],
                         cwd=ROOT, env=env, stdout=open(os.path.join(TMP, "station.log"), "w"),
                         stderr=subprocess.STDOUT)
try:
    assert wait_port(WEB) and wait_port(SHARED) and wait_port(PAT), "station did not start"
    time.sleep(2.5)                        # presence, Pat's event link

    # --- radios and ports
    page = Page()
    r = page.radios()
    check("both radios listed and connected", (r["g90"]["present"], r["qmx"]["present"]), (True, True))
    check("each radio's own rigctl port", (r["g90"]["rigctl"], r["qmx"]["rigctl"]), (G90["rigctl"], QMX["rigctl"]))
    check("the station first saw both at once: one of them shared",
          [k for k in r if r[k]["shared"]].__len__(), 1)
    shared = next(k for k in r if r[k]["shared"])
    rigctl(G90["rigctl"], "F 7074000")
    rigctl(QMX["rigctl"], "F 14074000")
    want = "7074000" if shared == "g90" else "14074000"
    check("the shared port reaches the shared radio", rigctl(SHARED, "f"), [want])
    check("own ports: G90", rigctl(G90["rigctl"], "f"), ["7074000"])
    check("own ports: QMX", rigctl(QMX["rigctl"], "f"), ["14074000"])
    check("hello: winlink and mesh on", (page.hello["config"]["winlink"], page.hello["config"]["mesh"]), (True, True))
    check("mesh comes from the station", bool(page.hello["mesh"]), True)

    # --- radio pages through /r/<id>/
    async def pages():
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{WEB}/r/qmx", allow_redirects=False) as rr:
                redirect = (rr.status, rr.headers.get("Location"))
            async with s.get(f"http://127.0.0.1:{WEB}/r/qmx/") as rr:
                html = (rr.status, "static/app.js" in await rr.text())
            async with s.ws_connect(f"http://127.0.0.1:{WEB}/r/qmx/ws") as w:
                name = json.loads((await w.receive()).data)["config"]["name"]
            async with s.get(f"http://127.0.0.1:{WEB}/r/nope/") as rr:
                missing = rr.status
            return redirect, html, name, missing
    redirect, html, name, missing = asyncio.run(pages())
    check("/r/qmx redirects to /r/qmx/", redirect, (302, "/r/qmx/"))
    check("/r/qmx/ is the QMX's page", html, (200, True))
    check("/r/qmx/ws is the QMX's socket", "QMX" in name, True)
    check("/r/<unknown>/ is 404", missing, 404)

    # --- ARDOP via the QMX (left in DiGi, USB + DATA, as after FT8)
    rigctl(QMX["rigctl"], "M PKTUSB 0")
    time.sleep(0.5)
    check("QMX in DiGi before the session", radio_mode(QMX), ("USB", True))
    page.events.clear()
    page.send({"t": "winlink_connect", "url": "ardop:///W7XYZ?freq=3588.0", "via": "qmx"})
    e = page.wait(wl(lambda e: e.get("armed") is True))
    check("session via QMX: armed, via QMX", e and e.get("via"), "QMX")
    check("  ardopcf on the QMX's sound card",
          (ardop_starts() or [[None]])[-1][:3], [str(ARDOP), "pipewire:NODE=qmx-in", "pipewire:NODE=qmx-out"])
    check("  the QMX's lane is armed (radio's view)", armed_on_radio(QMX), True)
    check("  the G90 is not", armed_on_radio(G90), False)
    check("  the QMX is in USB (SSB) for ARDOP", radio_mode(QMX), ("USB", False))
    e = page.wait(wl(lambda e: e.get("session") == "ended"), timeout=40)
    check("  session ends ok", e and e.get("ok"), True)
    check("  the QMX disarmed afterwards", armed_on_radio(QMX), False)
    time.sleep(0.5)
    check("  and is back in DiGi", radio_mode(QMX), ("USB", True))
    check("  the page saw armed: false", bool(page.wait(wl(lambda e: e.get("armed") is False))), True)
    check("  the lane refuses PTT again", rigctl(QMX["lane"], "T 1"), ["RPRT -11"])

    # --- refusals
    for label, msg in [("no radio named, two could take it", {"url": "ardop:///W7XYZ?freq=3588.0"}),
                       ("via an unknown radio", {"url": "ardop:///W7XYZ?freq=3588.0", "via": "ic7300"})]:
        page.events.clear()
        page.send({"t": "winlink_connect", **msg})
        e = page.wait(lambda e: e.get("t") == "notice" and e.get("level") == "warn", timeout=5)
        check(f"refused with a warning: {label}", bool(e), True)
        check(f"  nothing armed", (armed_on_radio(G90), armed_on_radio(QMX)), (False, False))

    # --- ARDOP moves to the G90
    page.events.clear()
    n = len(ardop_starts())
    page.send({"t": "winlink_connect", "url": "ardop:///W7XYZ?freq=7101.0", "via": "g90"})
    e = page.wait(wl(lambda e: e.get("session") == "ended"), timeout=50)
    check("session via G90 ends ok", e and e.get("ok"), True)
    check("  ardopcf restarted on the G90's card",
          [a[1] for a in ardop_starts()[n:]], ["pipewire:NODE=g90-in"])

    # --- the operator's page closing mid-session
    p2 = Page()
    p2.send({"t": "winlink_connect", "url": "ardop:///W7XYZ?freq=7101.0", "via": "g90"})
    p2.wait(wl(lambda e: e.get("armed") is True))
    check("second page: G90 armed", armed_on_radio(G90), True)
    p2.close()
    time.sleep(0.6)
    check("  page closed: the G90 disarmed at once", armed_on_radio(G90), False)
    page.wait(wl(lambda e: e.get("session") == "ended"), timeout=30)

    # --- packet: no HF radio armed
    page.events.clear()
    page.send({"t": "winlink_connect", "url": "ax25:///W7XYZ-10", "via": "packet"})
    e = page.wait(wl(lambda e: e.get("armed") is True))
    check("packet session: via UV-PRO", e and e.get("via"), "UV-PRO")
    check("  no HF radio armed", (armed_on_radio(G90), armed_on_radio(QMX)), (False, False))
    page.wait(wl(lambda e: e.get("session") == "ended"), timeout=30)

    # --- packet with the gateway's freq= (Pat's rmslist URL): Pat would QSY and
    # fail ("missing rig reference ... ax25+agwpe"); it gets the URL without it
    page.events.clear()
    page.send({"t": "winlink_connect", "url": "ax25:///N7RCC-10?freq=145010", "via": "packet"})
    e = page.wait(lambda e: e.get("t") == "notice" and "145.010 MHz" in e.get("msg", ""), 5)
    check("packet with freq: page told the radio must be on 145.010", e is not None, True)
    page.wait(wl(lambda e: e.get("session") == "ended"), timeout=30)
    with open(PAT_LOG) as f:
        urls = [json.loads(x).get("url") for x in f if '"connect"' in x]
    check("  Pat got the URL without freq=", urls[-1:], ["ax25:///N7RCC-10"])

    # --- packet hub page and its transmit switch. Any page on the LAN may
    # switch transmit on or off (D7, 2026-10-19); lending stays with this
    # machine; a page from elsewhere may do neither (cross-site guard).
    async def hub():
        out = {}
        async with aiohttp.ClientSession() as s:
            async def post(url, body, **kw):
                async with s.post(url, json=body, **kw) as rr:
                    return rr.status
            async with s.get(f"http://127.0.0.1:{WEB}/packet/") as rr:
                out["page"] = (rr.status, "packet hub" in await rr.text())
                out["page_cache"] = rr.headers.get("Cache-Control")
            local = f"http://127.0.0.1:{WEB}"
            out["local_on"] = await post(f"{local}/packet/api/tx", {"enabled": True},
                                         headers={"Origin": local})
            out["foreign"] = await post(f"{local}/packet/api/tx", {"enabled": True},
                                        headers={"Origin": "http://evil.example"})
            out["foreign_mailbox"] = await post(f"{local}/winlink/api/mailbox/out", {},
                                                headers={"Origin": "http://evil.example"})
            ip = lan_ip(WEB)
            out["ip"] = ip
            if ip:
                lan = f"http://{ip}:{WEB}"
                out["lan_on"] = await post(f"{lan}/packet/api/tx", {"enabled": True}, headers={"Origin": lan})
                out["lan_off"] = await post(f"{lan}/packet/api/tx", {"enabled": False}, headers={"Origin": lan})
                out["lan_lend"] = [await post(f"{lan}/packet/{p}", {"seconds": 60}) for p in ("api/lend", "api/return")]
                # The bypass reproduced on 2026-10-19: aiohttp kept "x/../" in the
                # tail, the lend check missed it, and the hub got /api/lend.
                bypass = ("x/../api/lend", "x/%2e%2e/api/lend", "x/%2E%2E/api/lend", "./api/lend",
                          "x/%252e%252e/api/lend", "x//api/lend", "x\\..\\api/lend",
                          # code review 2026-10-19 (Codex): decoded ?, # and controls
                          "api/lend%3fignored", "api/return%23ignored", "api/lend%09", "api/lend%0d",
                          "api/lend%20")
                out["bypass"] = []
                for p in bypass:
                    url = aiohttp.client.URL(f"{lan}/packet/{p}", encoded=True)
                    out["bypass"].append(await post(url, {"seconds": 60}))
                url = aiohttp.client.URL(f"{lan}/r/g90/x/../ws", encoded=True)
                out["radio_bypass"] = await post(url, {})
            # DNS rebinding (review 2026-10-19, Codex): an internet name pointed
            # at this machine makes its page same-origin; the Host gives it away.
            evil = f"http://evil.example:{WEB}"
            out["rebind_post"] = await post(f"{local}/packet/api/tx", {"enabled": True},
                                            headers={"Host": f"evil.example:{WEB}", "Origin": evil})
            async with s.get(f"{local}/", headers={"Host": f"evil.example:{WEB}"}) as rr:
                out["rebind_get"] = rr.status
            async with s.get(f"{local}/", headers={"Host": f"linux-station:{WEB}"}) as rr:
                out["single_label"] = rr.status
            out["scheme_mismatch"] = await post(f"{local}/packet/api/tx", {"enabled": True},
                                                headers={"Origin": f"https://127.0.0.1:{WEB}"})
            out["local_lend"] = await post(f"{local}/packet/api/lend", {"seconds": 60})
            try:
                async with s.ws_connect(f"{local}/ws", headers={"Origin": "http://evil.example"}) as w:
                    out["ws_foreign"] = "opened"
            except aiohttp.WSServerHandshakeError as e:
                out["ws_foreign"] = e.status
            async with s.ws_connect(f"{local}/ws", headers={"Origin": local}) as w:
                out["ws_own"] = (await w.receive(timeout=3)).type == aiohttp.WSMsgType.TEXT
            try:
                async with s.ws_connect(f"{local}/r/g90/ws", headers={"Origin": "http://evil.example"}) as w:
                    out["ws_radio_foreign"] = "opened"
            except aiohttp.WSServerHandshakeError as e:
                out["ws_radio_foreign"] = e.status
        return out
    n_tx = len(hub_tx)
    o = asyncio.run(hub())
    check("/packet/ is the hub's page", o["page"], (200, True))
    check("  proxied without Cache-Control: the station adds no-cache", o["page_cache"], "no-cache")
    check("  transmit on from this machine: passed to the hub", o["local_on"], 200)
    check("  the hub got the station's header, not the browser's Origin", hub_hdr[-1], ("1", None))
    check("  transmit on from a foreign page: refused here", o["foreign"], 403)
    check("  DNS rebinding: a POST for an unknown host name refused", o["rebind_post"], 403)
    check("  DNS rebinding: the page for an unknown host name refused", o["rebind_get"], 403)
    check("  a single-label name (linux-station) is fine", o["single_label"], 200)
    check("  an Origin with the wrong scheme: refused", o["scheme_mismatch"], 403)
    check("  a Winlink mailbox POST from a foreign page: refused here", o["foreign_mailbox"], 403)
    check("  the station's WebSocket from a foreign page: refused", o["ws_foreign"], 403)
    check("  a radio's WebSocket from a foreign page: refused", o["ws_radio_foreign"], 403)
    check("  the station's WebSocket from its own page: open", o["ws_own"], True)
    if o["ip"] is None:
        print("skip  transmit on/off and lending from another machine (no reachable non-loopback address here)")
    else:
        check("  transmit on from another machine: passed to the hub", o["lan_on"], 200)
        check("  transmit off from another machine: passed to the hub", o["lan_off"], 200)
        check("  the hub saw on, on, off", [t for t, _ in hub_tx[n_tx:]],
              [{"enabled": True}, {"enabled": True}, {"enabled": False}])
        check("  lending the radio from another machine: refused here", o["lan_lend"], [403, 403])
        check("  dot-segment and encoded paths: refused, never forwarded", o["bypass"], [400] * 12)
        check("  the same through a radio's /r/: refused", o["radio_bypass"], 400)
    check("  lending the radio from this machine: passed to the hub", (o["local_lend"], hub_lend), (200, ["/api/lend"]))

    # --- the shared port moves only when its radio goes away
    first = shared
    other = "qmx" if first == "g90" else "g90"
    held = socket.create_connection(("127.0.0.1", SHARED), timeout=3)
    hf = held.makefile("r")
    stop(g90 if first == "g90" else qmx)
    e = page.wait(lambda e: e.get("t") == "radios" and not {r["id"]: r for r in e["radios"]}[first]["present"])
    check(f"{first} unplugged: listed as gone", bool(e), True)
    check(f"  shared port now the {other}", e and {r["id"]: r for r in e["radios"]}[other]["shared"], True)
    held.sendall(b"f\n")
    check("  a connection to the gone radio is closed, not moved", hf.readline(), "")
    held.close()
    want = "14074000" if other == "qmx" else "7074000"
    check(f"  a new shared connection reaches the {other}", rigctl(SHARED, "f"), [want])
    page.close()
finally:
    stop(front)
    out = open(os.path.join(TMP, "station.log")).read()
    for p in (g90, qmx):
        stop(p)
    if fails:
        print(out[-4000:])

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
