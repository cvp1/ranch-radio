"""Station peers (station_peers.py): one station uses another station's radios.

Runs a simulated G90 behind a station named "linux-station", and a station named
"mac-shack" (with its own simulated QMX and a shared rigctl port) that names
linux-station as a peer, then checks:
  - the host file's station.peers: what is refused (an https peer without a
    pin, a bad url, a name twice or this host's own, an unknown field) and
    what station.json carries
  - a peer's certificate: the pinned one is trusted, any other is not
    (an https server in this process with a certificate made for the test)
  - a peer's token goes up only for this station's operator
  - mac-shack's page lists g90@linux-station, connected, said to be linux-station's, and never
    as its shared rigctl port or a Winlink radio
  - mac-shack's /api/radios lists mac-shack's own radios only (nothing chains)
  - g90@linux-station's page and WebSocket come through mac-shack: a QSY sent there
    moves the G90's dial (read on its own rigctl port), and STOP TX latches it
  - a radio the peer doesn't have, or a peer not named: 404
  - linux-station going away: g90@linux-station stays listed, not connected, saying why
  - a peer that never answers ("ghost"): no radio of its own to list, so the
    page's peer summary says it is away and why, and the Shack's Offline chip
    names "ghost station" (a page in Chrome)

    python3 tests/station_peers_test.py
"""
import asyncio
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

import station_peers  # noqa: E402
import station_peers_config as pc  # noqa: E402
import testkit  # noqa: E402
from cdp_browser import Browser  # noqa: E402
from ranch_radio import render  # noqa: E402

TMP = tempfile.mkdtemp(prefix="station-peers-")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:66} got {got!r}" + ("" if ok else f" want {want!r}"))


def refused(peers, own="mac-shack"):
    try:
        pc.check_peers(peers, own)
    except pc.PeerError as e:
        return str(e)
    return None


# --- the host file's station.peers
PIN = "ab" * 32
check("an https peer with its pin (colons, any case) is kept, the pin normalised",
      pc.check_peers([{"name": "linux-station", "url": "https://linux-station.local:7300/", "pin_sha256": ":".join(["AB"] * 32)}], "mac-shack"),
      [{"name": "linux-station", "url": "https://linux-station.local:7300", "pin_sha256": PIN}])
check("  an https peer without a pin is refused", "needs pin_sha256" in (refused([{"name": "linux-station", "url": "https://linux-station.local:7300"}]) or ""), True)
check("  a pin that isn't 64 hex digits", "64 hex digits" in (refused([{"name": "c", "url": "https://c.local:7300", "pin_sha256": "abc"}]) or ""), True)
check("  a url with a path", "peer station's address" in (refused([{"name": "c", "url": "http://c.local:7300/r/g90"}]) or ""), True)
check("  a url that isn't http(s)", "peer station's address" in (refused([{"name": "c", "url": "ftp://c.local"}]) or ""), True)
check("  a name twice", "named twice" in (refused([{"name": "c", "url": "http://a.local"}, {"name": "c", "url": "http://b.local"}]) or ""), True)
check("  this host's own name", "that is this host" in (refused([{"name": "mac-shack", "url": "http://d.local"}]) or ""), True)
check("  an unknown field", "takes name, url" in (refused([{"name": "c", "url": "http://c.local", "tx": True}]) or ""), True)
check("  a pin on a plain http peer", "for an https url" in (refused([{"name": "c", "url": "http://c.local", "pin_sha256": PIN}]) or ""), True)
host = {"host": "mac-shack", "os": "macos", "repo": "/Users/you/ranch-radio", "python": "/usr/bin/python3",
        "radios": [{"id": "qmx", "name": "QMX", "device": "/dev/cu.usbmodem1", "web": 7301}],
        "station": {"https": True, "peers": [{"name": "linux-station", "url": "https://linux-station.local:7300", "pin_sha256": PIN}]}}
render.validate(host)
check("render: station.json carries the peers", render.station_json(host).get("peers"),
      [{"name": "linux-station", "url": "https://linux-station.local:7300", "pin_sha256": PIN}])
bad = json.loads(json.dumps(host))
bad["station"]["peers"][0].pop("pin_sha256")
try:
    render.validate(bad)
    check("render: an unpinned https peer is refused", False, True)
except render.HostError as e:
    check("render: an unpinned https peer is refused", "needs pin_sha256" in str(e), True)

# --- a peer's token: only for this station's operator
tok = os.path.join(TMP, "linux-station.token")
with open(tok, "w") as f:
    f.write("peer-token-0123456789")
os.chmod(tok, 0o600)
p = station_peers.Peer({"name": "linux-station", "url": "http://127.0.0.1:1", "token_file": tok})
check("the operator's request carries the peer's token; a read-only page's doesn't",
      (p.headers(True).get("Authorization"), "Authorization" in p.headers(False)), ("Bearer peer-token-0123456789", False))
os.chmod(tok, 0o644)
check("  a token file others can read is not used (and the station lives)", "Authorization" in p.headers(True), False)


# --- a peer's certificate: pinned
async def pinning():
    cert, key = os.path.join(TMP, "cert.pem"), os.path.join(TMP, "key.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=peer-test",
                    "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", key, "-out", cert], check=True, capture_output=True)
    der = ssl.PEM_cert_to_DER_cert(open(cert).read())
    import hashlib
    good = hashlib.sha256(der).hexdigest()
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)
    app = web.Application()
    app.router.add_get("/api/radios", lambda r: web.json_response(
        {"radios": [{"id": "g90", "name": "G90", "present": True}, {"id": "../x", "name": "bad"}]}))
    runner = web.AppRunner(app)
    await runner.setup()
    port = testkit.free_port()
    await web.TCPSite(runner, "127.0.0.1", port, ssl_context=ctx).start()
    out = []
    async with aiohttp.ClientSession() as s:
        for pin in (good, "00" * 32):
            peer = station_peers.Peer({"name": "linux-station", "url": f"https://127.0.0.1:{port}", "pin_sha256": pin})
            await peer.poll(s)
            out.append((peer.up, peer.radios, peer.error))
    await runner.cleanup()
    return out


right, wrong = asyncio.run(pinning())
check("the pinned certificate: up, its radios (an id this side can't route is dropped)",
      right, (True, [{"id": "g90", "name": "G90", "present": True}], None))
check("  any other certificate: away, said why", wrong, (False, [], "its certificate is not the pinned one"))

# --- two stations on this machine: mac-shack uses linux-station's G90
B = testkit.free_port()
CAM_WEB, DOG_WEB, DOG_SHARED = testkit.free_port(), testkit.free_port(), testkit.free_port()
G90 = {"web": testkit.free_port(), "rigctl": testkit.free_port()}
QMX = {"web": testkit.free_port(), "rigctl": testkit.free_port()}


def wait_port(port, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def rigctl(port, *cmds):
    s = socket.create_connection(("127.0.0.1", port), timeout=3)
    f = s.makefile("r")
    out = []
    for c in cmds:
        s.sendall((c + "\n").encode())
        out.append(f.readline().strip())
    s.close()
    return out


def stop(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def radio(rig, p):
    proc = subprocess.Popen([sys.executable, "server.py", "--sim", "--rig", rig, "--no-audio", "--host", "127.0.0.1",
                             "--listen", str(p["web"]), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(p["rigctl"]),
                             "--stop-state", os.path.join(TMP, f"{rig}-stop.json")],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert wait_port(p["web"]) and wait_port(p["rigctl"]), f"{rig} did not start"
    return proc


def station(name, cfg, listen, *extra):
    path = os.path.join(TMP, f"{name}.json")
    with open(path, "w") as f:
        json.dump({"name": name, **cfg}, f)
    env = dict(os.environ, RR_PEERS_POLL_S="0.5")
    return subprocess.Popen([sys.executable, "station.py", "--config", path, "--listen", str(listen), *extra],
                            cwd=ROOT, env=env, stdout=open(os.path.join(TMP, f"{name}.log"), "w"), stderr=subprocess.STDOUT)


HELLO = {}


async def hello(port):
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"http://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}") as ws:
            while True:
                m = json.loads((await ws.receive(timeout=10)).data)
                if m.get("t") == "hello":
                    HELLO.update(m)
                    return {r["id"]: r for r in m["radios"]}


def radios_until(port, want, timeout=15):
    end = time.time() + timeout
    r = {}
    while time.time() < end:
        r = asyncio.run(hello(port))
        if want(r):
            return r
        time.sleep(0.3)
    return r


g90 = radio("g90", G90)
qmx = radio("qmx", QMX)
cam = station("linux-station", {"radios": [{"id": "g90", "name": "G90", "web": G90["web"], "rigctl": G90["rigctl"]}]}, CAM_WEB,
              "--rigctl-port", "0")
dog = station("mac-shack", {"radios": [{"id": "qmx", "name": "QMX", "web": QMX["web"], "rigctl": QMX["rigctl"]}],
                        "peers": [{"name": "linux-station", "url": f"http://127.0.0.1:{CAM_WEB}"},
                                  {"name": "ghost", "url": f"http://127.0.0.1:{testkit.free_port()}"}]}, DOG_WEB,
              "--rigctl-port", str(DOG_SHARED))
try:
    assert wait_port(CAM_WEB) and wait_port(DOG_WEB), "a station did not start"
    r = radios_until(DOG_WEB, lambda r: r.get("g90@linux-station", {}).get("present") and r.get("qmx", {}).get("present"))
    g = r.get("g90@linux-station", {})
    check("mac-shack lists linux-station's G90, connected, said to be linux-station's",
          (g.get("present"), g.get("name"), g.get("remote")), (True, "G90 (linux-station)", "linux-station"))
    check("  never mac-shack's shared rigctl port, a Winlink radio or a rigctl port of its own",
          (g.get("shared"), g.get("winlink"), g.get("rigctl"), r.get("qmx", {}).get("shared")), (False, False, None, True))
    peers = {p["name"]: p for p in HELLO.get("peers", [])}
    check("the page's peer summary: linux-station up with its radio; ghost away, saying why",
          (peers.get("linux-station", {}).get("up"), peers.get("linux-station", {}).get("radios"),
           peers.get("ghost", {}).get("up"), bool(peers.get("ghost", {}).get("error"))), (True, 1, False, True))
    check("  ghost has no radio in the list", [k for k in r if k.endswith("@ghost")], [])
    CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
    if CHROME:
        b = Browser(CHROME, testkit.free_port(), size="1600,1000")
        try:
            b.goto(f"http://127.0.0.1:{DOG_WEB}/#/home")
            chip = b.wait("[...document.querySelectorAll('#mc-needs .mc-need.off')].map((c) => c.textContent).join(' ') || null")
            check("  the Shack's Offline chip names the ghost station, not linux-station", (
                "ghost station" in (chip or ""), "linux-station" in (chip or "")), (True, False))
        finally:
            b.close()
    else:
        print("skip  no Chrome: the Offline chip")
    rigctl(QMX["rigctl"], "F 14074000")
    check("  mac-shack's shared port is still its own QMX", rigctl(DOG_SHARED, "f"), ["14074000"])

    async def through():
        out = {}
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{DOG_WEB}/api/radios") as rr:
                out["api"] = await rr.json()
            async with s.get(f"http://127.0.0.1:{DOG_WEB}/r/g90@linux-station/") as rr:
                out["page"] = (rr.status, "<html" in (await rr.text()).lower())
            for bad in ("x@linux-station", "g90@nobody", "g90@"):
                async with s.get(f"http://127.0.0.1:{DOG_WEB}/r/{bad}/") as rr:
                    out[bad] = rr.status
            url = f"ws://127.0.0.1:{DOG_WEB}/r/g90%40linux-station/ws"
            async with s.ws_connect(url, origin=f"http://127.0.0.1:{DOG_WEB}") as ws:
                await ws.send_str(json.dumps({"t": "set", "k": "freq", "v": 7074000}))
                end = time.time() + 10
                while time.time() < end and rigctl(G90["rigctl"], "f") != ["7074000"]:
                    await asyncio.sleep(0.2)
                out["qsy"] = rigctl(G90["rigctl"], "f")
                await ws.send_str(json.dumps({"t": "tx_stop"}))
                out["stop"] = None
                while time.time() < end + 5:
                    m = json.loads((await ws.receive(timeout=5)).data)
                    if m.get("t") == "tx_stop":
                        out["stop"] = m.get("stopped")
                        break
        return out

    o = asyncio.run(through())
    check("mac-shack's /api/radios: its own radios only (nothing chains)",
          (o["api"]["station"], [x["id"] for x in o["api"]["radios"]]), ("mac-shack", ["qmx"]))
    check("g90@linux-station's page comes through mac-shack", o["page"], (200, True))
    check("  a radio linux-station doesn't have, a peer not named, no peer: 404",
          (o["x@linux-station"], o["g90@nobody"], o["g90@"]), (404, 404, 404))
    check("  a QSY on its WebSocket through mac-shack moves the G90 (its own rigctl port)", o["qsy"], ["7074000"])
    check("  STOP TX through mac-shack latches the G90 (its latch record: when, and from where)",
          (isinstance(o["stop"], dict), sorted(o["stop"] or {})), (True, ["at", "by"]))

    stop(cam)
    r = radios_until(DOG_WEB, lambda r: r.get("g90@linux-station", {}).get("present") is False)
    g = r.get("g90@linux-station", {})
    check("linux-station away: g90@linux-station still listed, not connected, saying why", (g.get("present"), bool(g.get("away"))), (False, True))
    check("  mac-shack's own QMX is untouched", r.get("qmx", {}).get("present"), True)
finally:
    for proc in (dog, cam, g90, qmx):
        if proc.poll() is None:
            stop(proc)

if fails:
    print(f"\n{fails} FAILED (logs in {TMP})")
    sys.exit(1)
print("\nPASS")
