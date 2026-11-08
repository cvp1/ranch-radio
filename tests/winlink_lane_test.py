"""W1: the Winlink rigctl lane, against a simulated rig.

Pat keys the radio through its own rigctl listener (--winlink-rigctl-port),
never the apps' port. On that lane:
  - it listens on 127.0.0.1 only
  - T 1 is refused (RPRT -11, radio never keyed) unless a Winlink session is armed
  - arming is a browser command and is refused with --no-tx
  - armed, T 1 keys and T 0 unkeys
  - a keyed lane client may stay silent longer than the apps' 5 s (ARDOP frames),
    but is still unkeyed once WINLINK_KEEPALIVE_S passes
  - disarming while keyed unkeys at once
  - the page that armed the session closing disarms it (operator gone)
The apps' port keeps its old behaviour (keys without any arming).

    .venv/bin/python tests/winlink_lane_test.py [rig]
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from rigctl_server import RIGCTL_KEEPALIVE_S, WINLINK_KEEPALIVE_S  # noqa: E402
import testkit  # noqa: E402
TX_OFF_CONFIRM = 1.0     # server.TX_OFF_CONFIRM_S: "not transmitting" must hold this long

RIG = sys.argv[1] if len(sys.argv) > 1 else "ic7300"
APPS = testkit.free_port()
LANE = testkit.free_port()
WEB = testkit.free_port()
TX_TIMEOUT = 60
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:52} got {got!r}" + ("" if ok else f" want {want!r}"))


class Client:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.f = self.s.makefile("r")

    def cmd(self, line, nlines=1):
        self.s.sendall((line + "\n").encode())
        return [self.f.readline().strip() for _ in range(nlines)]

    def close(self):
        self.f.close()
        self.s.close()


def ptt():
    c = Client(APPS)
    v = c.cmd("t")[0]
    c.close()
    return v


class Page:
    """A browser control socket, run on its own event loop thread."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.notices = []
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._open(), self.loop).result(5)

    async def _open(self):
        self.sess = aiohttp.ClientSession()
        self.ws = await self.sess.ws_connect(f"http://127.0.0.1:{WEB}/ws")
        await self.ws.receive()
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                m = json.loads(msg.data)
                if m.get("t") in ("notice", "winlink"):
                    self.notices.append(m)

    def send(self, m):
        asyncio.run_coroutine_threadsafe(self.ws.send_str(json.dumps(m)), self.loop).result(5)
        time.sleep(0.3)

    def close(self):
        async def _c():
            await self.ws.close()
            await self.sess.close()
        asyncio.run_coroutine_threadsafe(_c(), self.loop).result(5)
        time.sleep(0.5)


def start(*extra):
    p = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--rig", RIG, "--no-audio", "--listen", str(WEB),
         "--rigctl-port", str(APPS), "--winlink-rigctl-port", str(LANE),
         "--tx-timeout", str(TX_TIMEOUT), *extra],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(80):
        try:
            socket.create_connection(("127.0.0.1", LANE), timeout=0.2).close()
            socket.create_connection(("127.0.0.1", WEB), timeout=0.2).close()
            return p
        except OSError:
            time.sleep(0.1)
    p.kill()
    print(p.stdout.read())
    raise SystemExit("server did not start")


def stop(p):
    p.terminate()
    try:
        return p.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired:
        p.kill()
        return p.communicate()[0]


def freq():
    c = Client(APPS)
    v = int(c.cmd("f")[0])
    c.close()
    return v


def lan_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
        except OSError:
            return None


check("lane keepalive is longer than the apps'", WINLINK_KEEPALIVE_S > RIGCTL_KEEPALIVE_S, True)

server = start()
try:
    ip = lan_ip()
    if ip and ip != "127.0.0.1":
        try:
            socket.create_connection((ip, LANE), timeout=1).close()
            reach = True
        except OSError:
            reach = False
        check(f"lane not reachable on {ip}", reach, False)

    lane = Client(LANE)
    check("unarmed: T 1 refused", lane.cmd("T 1"), ["RPRT -11"])
    check("unarmed: radio not keyed", ptt(), "0")
    check("unarmed: lane reads freq", lane.cmd("f")[0].isdigit(), True)

    apps = Client(APPS)
    check("apps' port keys without arming", apps.cmd("T 1"), ["RPRT 0"])
    check("apps' port unkeys", apps.cmd("T 0"), ["RPRT 0"])
    apps.close()

    page = Page()
    page.send({"t": "winlink_arm", "v": True})
    check("armed: T 1 keys", lane.cmd("T 1"), ["RPRT 0"])
    check("armed: radio keyed", ptt(), "1")
    check("armed: T 0 unkeys", lane.cmd("T 0"), ["RPRT 0"])
    check("armed: radio unkeyed", ptt(), "0")

    check("armed: re-key", lane.cmd("T 1"), ["RPRT 0"])
    time.sleep(RIGCTL_KEEPALIVE_S + 2)            # silent past the apps' keepalive
    check(f"silent {RIGCTL_KEEPALIVE_S + 2:g}s: still keyed", ptt(), "1")
    time.sleep(WINLINK_KEEPALIVE_S - RIGCTL_KEEPALIVE_S)
    check(f"silent past {WINLINK_KEEPALIVE_S:g}s: unkeyed", ptt(), "0")

    check("re-key for disarm", lane.cmd("T 1"), ["RPRT 0"])
    page.send({"t": "winlink_arm", "v": False})
    check("disarm while keyed: unkeyed", ptt(), "0")
    check("disarmed: T 1 refused", lane.cmd("T 1"), ["RPRT -11"])

    page.send({"t": "winlink_arm", "v": True})
    check("re-armed: T 1 keys", lane.cmd("T 1"), ["RPRT 0"])
    page.close()
    check("arming page closed: unkeyed", ptt(), "0")
    check("arming page closed: disarmed", lane.cmd("T 1"), ["RPRT -11"])
    lane.close()

    # --- the lane never sets the hold; an app's hold never moves a Winlink session ---
    page = Page()
    lane = Client(LANE)
    check("lane QSY", lane.cmd("F 3588500"), ["RPRT 0"])
    page.send({"t": "sim_panel", "d": {"freq": 7100000}})   # front-panel dial
    time.sleep(3)
    check("lane only: front-panel change is not undone", freq(), 7100000)

    apps = Client(APPS)
    check("app (JTDX) sets its frequency", apps.cmd("F 14074000"), ["RPRT 0"])
    page.send({"t": "winlink_arm", "v": True})
    check("armed lane QSY with an app holding 14.074", lane.cmd("F 3588500"), ["RPRT 0"])
    time.sleep(3.5)
    check("armed: the app's hold does not pull the radio back", freq(), 3588500)
    page.send({"t": "winlink_arm", "v": False})
    time.sleep(3.5)
    # 2026-10-15: pulling back to the app's band after a session fired an ATU tune carrier
    check("disarmed: the radio stays where Winlink left it", freq(), 3588500)
    check("app's next setting takes the radio again", apps.cmd("F 14074000"), ["RPRT 0"])
    page.send({"t": "sim_panel", "d": {"freq": 14080000}})
    time.sleep(3.5)
    check("and its hold works again", freq(), 14074000)
    apps.close()
    lane.close()
    page.close()
finally:
    out = stop(server)
    for line in out.splitlines():
        if "winlink" in line.lower() or "refused" in line or "unkeyed" in line:
            print("   log:", line)

# --- the lane moves the radio only while armed; unarmed F is answered but ignored ---
# (live 2026-10-15: Pat's post-session revert fired an ATU tune carrier)
server = start("--auto-tune")
try:
    def tunes(page):
        return [n["msg"] for n in page.notices
                if n.get("t") == "notice" and "ATU tuning" in n.get("msg", "")]

    time.sleep(4)                                  # past the auto-tune arming window
    page = Page()
    lane = Client(LANE)
    apps = Client(APPS)
    check("app puts the radio on 20 m", apps.cmd("F 14074000"), ["RPRT 0"])
    time.sleep(3)
    page.notices.clear()

    check("unarmed: lane F answered ok", lane.cmd("F 10136000"), ["RPRT 0"])
    check("unarmed: lane set_freq answered ok", lane.cmd("\\set_freq 10136000"), ["RPRT 0"])
    check("unarmed: lane F VFOA answered ok", lane.cmd("F VFOA 10136000"), ["RPRT 0"])
    check("unarmed: lane F VFOB answered ok", lane.cmd("F VFOB 10136000"), ["RPRT 0"])
    time.sleep(3)
    check("unarmed: lane F did not move the radio", freq(), 14074000)
    check("unarmed: lane F did not auto-tune", tunes(page), [])

    page.send({"t": "winlink_arm", "v": True})
    check("armed: lane F applies", lane.cmd("F 3588500"), ["RPRT 0"])
    check("armed: radio on the session frequency", freq(), 3588500)
    time.sleep(3)
    check("armed: the session's band change still tunes", tunes(page),
          ["ATU tuning for 80 m (band change)"])
    page.send({"t": "winlink_arm", "v": False})
    page.notices.clear()
    check("disarmed: Pat's revert answered ok", lane.cmd("F 10136000"), ["RPRT 0"])
    time.sleep(3)
    check("disarmed: revert did not move the radio", freq(), 3588500)
    check("disarmed: revert did not auto-tune", tunes(page), [])

    check("apps' port still moves the radio", apps.cmd("F 7074000"), ["RPRT 0"])
    check("apps' port: radio moved", freq(), 7074000)
    apps.close()
    lane.close()
    page.close()
finally:
    out = stop(server)
lane_logs = [ln for ln in out.splitlines() if "winlink lane" in ln and "set_freq" in ln]
check("lane F logged: ignored, unarmed",
      sum("10136000 Hz ignored (no session armed)" in ln for ln in lane_logs), 5)
check("lane F logged: applied, armed",
      sum("3588500 Hz applied (armed)" in ln for ln in lane_logs), 1)
for ln in lane_logs:
    print("   log:", ln)

# --- T 0 is the sender's own; every end of a lane key reaches the ID ledger once ---
# (plan winlink-vara A2: another client's T 0 used to cut Pat's frame, and a cut CW ID read as sent)
def released(out):
    """The kinds of every "lane PTT released" line, in order."""
    return [ln.rsplit("(", 1)[-1].rstrip(")") for ln in out.splitlines() if "lane PTT released" in ln]


server = start()
try:
    page = Page()
    page.send({"t": "winlink_arm", "v": True})
    a, b = Client(LANE), Client(LANE)
    check("two lane clients: A keys", a.cmd("T 1"), ["RPRT 0"])
    check("B (not keying) sends T 0: answered ok", b.cmd("T 0"), ["RPRT 0"])
    check("B's T 0 leaves A on the air", ptt(), "1")
    check("B's T 1 while A keys: refused", b.cmd("T 1"), ["RPRT -11"])
    check("A's own T 0 unkeys", a.cmd("T 0"), ["RPRT 0"])          # 1: normal
    check("A unkeyed", ptt(), "0")
    check("A's second T 0: ok, nothing to release", a.cmd("T 0"), ["RPRT 0"])
    check("A keys again", a.cmd("T 1"), ["RPRT 0"])
    a.close()                                                        # 2: forced (disconnect)
    time.sleep(0.5)
    check("A disconnected while keyed: unkeyed", ptt(), "0")
    check("B keys", b.cmd("T 1"), ["RPRT 0"])
    page.send({"t": "winlink_arm", "v": False})                      # 3: forced (disarm)
    check("disarm while B keys: unkeyed", ptt(), "0")
    page.send({"t": "winlink_arm", "v": True})
    check("B keys for the radio's own unkey", b.cmd("T 1"), ["RPRT 0"])
    time.sleep(1.5)                                                  # past the 1 s stale-reply grace
    page.send({"t": "sim_panel", "d": {"tx": False}})                # 4: forced (unkeyed at the radio)
    time.sleep(TX_OFF_CONFIRM + 0.8)
    check("unkeyed at the radio: released", ptt(), "0")
    check("B's T 0 after the radio let go: ok", b.cmd("T 0"), ["RPRT 0"])
    check("B keys for the keepalive", b.cmd("T 1"), ["RPRT 0"])
    time.sleep(WINLINK_KEEPALIVE_S + 1.5)                            # 5: forced (went quiet)
    check(f"B silent past {WINLINK_KEEPALIVE_S:g}s: unkeyed", ptt(), "0")
    b.close()

    apps_a, apps_b = Client(APPS), Client(APPS)
    check("apps' port: A keys", apps_a.cmd("T 1"), ["RPRT 0"])
    check("apps' port: B's T 0 is ok", apps_b.cmd("T 0"), ["RPRT 0"])
    check("apps' port: B's T 0 leaves A on the air", ptt(), "1")
    check("apps' port: A's T 0 unkeys", apps_a.cmd("T 0"), ["RPRT 0"])
    check("apps' port: unkeyed", ptt(), "0")
    apps_a.close()
    apps_b.close()
    page.close()
finally:
    out = stop(server)
check("each end of a lane key reaches the ledger once, with how it ended",
      released(out), ["normal", "forced", "forced", "forced", "forced"])

# the transmit time limit is a forced release too
server = start("--tx-timeout", "3")
try:
    page = Page()
    page.send({"t": "winlink_arm", "v": True})
    lane = Client(LANE)
    check("tx cap: lane keys", lane.cmd("T 1"), ["RPRT 0"])
    for _ in range(5):                                # talking, so the keepalive never fires
        time.sleep(1.0)
        lane.cmd("t")
    check("tx cap: unkeyed by the 3 s limit", ptt(), "0")
    check("tx cap: the owner's T 0 afterwards is ok", lane.cmd("T 0"), ["RPRT 0"])
    lane.close()
    page.close()
finally:
    out = stop(server)
check("tx cap: one forced release", released(out), ["forced"])
check("tx cap: it was the time limit", any("3s transmit time limit" in ln for ln in out.splitlines()), True)

server = start("--no-tx")
try:
    page = Page()
    page.send({"t": "winlink_arm", "v": True})
    warned = any(n.get("t") == "notice" and n.get("level") == "warn" for n in page.notices)
    check("--no-tx: arming refused with a warning", warned, True)
    lane = Client(LANE)
    check("--no-tx: T 1 refused", lane.cmd("T 1"), ["RPRT -11"])
    lane.close()
    page.close()
finally:
    stop(server)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
