"""Helpers for tests that run a Winlink station: a simulated radio (server.py
--sim, its lane on LANE, its rigctl on APPS, its page on RWEB) behind
station.py (the page on WEB, Pat = tests/fake_pat.py on PATP), as render
puts them on every host. start() takes server.py-style flags and routes each
to the process it belongs to (below); the handle it returns reads as one
process whose log is both logs."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

import aiohttp
import testkit  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPS = testkit.free_port()
LANE = testkit.free_port()
PATP = testkit.free_port()
WEB = testkit.free_port()            # the station's page
RWEB = testkit.free_port()           # the radio's own page, behind it
ARDOP = None                         # ardopcf's stand-in (a Mac's agent); picked at start(), see there
URL = "ardop:///N0DAJ?bw=2000MAX&freq=3588.5"

# Flags that are the station's (station.json), with their field; everything
# else is the radio's. A value flag takes the next argument; a bare one none.
STATION_VALUE_FLAGS = {"--winlink-pat": ("winlink", "pat"), "--winlink-pat-config": ("winlink", "pat_config"),
                       "--winlink-pat-port": ("winlink", "pat_port"), "--winlink-session-max": ("winlink", "session_max"),
                       "--winlink-listen": ("winlink", "_listen"), "--winlink-listen-max-h": ("winlink", "_listen_max_h"),
                       "--addons": (None, "_addons_file"), "--addons-state": (None, "addons_state"),
                       "--host-file": (None, "host_file"),
                       "--mesh-port": ("mesh", "port"), "--mesh-python": ("mesh", "python"), "--mesh-bridge": ("mesh", "bridge"),
                       "--mesh-key": ("mesh", "_key"), "--mesh-log": ("mesh", "log"), "--mesh-feed": ("mesh", "feed"),
                       "--mesh-feed-cred": ("mesh", "feed_cred"),
                       "--p2pdir-url": ("winlink", "_p2pdir_url")}
STATION_BARE_FLAGS = {"--mesh-feed-tx": ("mesh", "feed_tx")}
NUMERIC = {"pat_port": int, "session_max": float, "_listen_max_h": float}


def split_flags(extra):
    """(radio argv, station.json fields) from server.py-style flags."""
    radio, cfg = [], {}
    it = iter(list(extra))
    for a in it:
        if a in STATION_VALUE_FLAGS:
            block, key = STATION_VALUE_FLAGS[a]
            v = next(it)
            v = NUMERIC[key](v) if key in NUMERIC else v
            d = cfg if block is None else cfg.setdefault(block, {})
            if key == "_key":
                d.setdefault("keys", []).append(v)
            else:
                d[key] = v
        elif a in STATION_BARE_FLAGS:
            block, key = STATION_BARE_FLAGS[a]
            cfg.setdefault(block, {})[key] = True
        else:
            radio.append(a)
    return radio, cfg


class Pair:
    """The radio's server and the station, read as one process: poll() is
    the first to die, terminate() ends the station first (it ends a session
    and disarms the radio through the radio's own socket), the output is both
    logs, station last."""

    def __init__(self, radio, station, cfg_path, ardop=None):
        self.radio, self.station, self.cfg_path, self.ardop = radio, station, cfg_path, ardop
        self.pid = station.pid
        self.stdout = self

    def poll(self):
        return self.station.poll() if self.station.poll() is not None else self.radio.poll()

    def terminate(self):
        self.station.terminate()
        try:
            self.station.wait(15)
        except subprocess.TimeoutExpired:
            pass
        self.radio.terminate()

    def kill(self):
        for p in (self.station, self.radio, self.ardop):
            if p is not None:
                p.kill()

    def send_signal(self, sig):
        self.station.send_signal(sig)          # the station owns Pat and the session

    @property
    def returncode(self):
        return self.station.returncode

    def wait(self, timeout=None):
        rc = self.station.wait(timeout)
        if self.radio.poll() is None:            # the radio was never signalled: it goes when the station has
            self.radio.terminate()
        self.radio.wait(timeout)
        return rc

    def communicate(self, timeout=None):
        # The station first (it is what a caller signalled: it ends the session
        # and disarms the radio); then the radio and the modem's stand-in.
        s = self.station.communicate(timeout=timeout)[0] or ""
        if self.radio.poll() is None:
            self.radio.terminate()
        r = self.radio.communicate(timeout=timeout)[0] or ""
        if self.ardop is not None and self.ardop.poll() is None:
            self.ardop.kill()
            self.ardop.wait(5)
        return (r + s, None)

    def read(self):
        return self.communicate()[0]
def ptt():
    with socket.create_connection(("127.0.0.1", APPS), timeout=3) as s:
        f = s.makefile("r")
        s.sendall(b"t\n")
        v = f.readline().strip()
        f.close()
        return v


def lane_ptt_refused():
    with socket.create_connection(("127.0.0.1", LANE), timeout=3) as s:
        f = s.makefile("r")
        s.sendall(b"T 1\n")
        v = f.readline().strip()
        if v == "RPRT 0":
            s.sendall(b"T 0\n")
            f.readline()
        f.close()
        return v == "RPRT -11"


class Page:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.events = []
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.run(self._open())

    def run(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _open(self):
        self.sess = aiohttp.ClientSession()
        self.ws = await self.sess.ws_connect(f"http://127.0.0.1:{WEB}/ws")
        self.hello = json.loads((await self.ws.receive()).data)
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                m = json.loads(msg.data)
                if m.get("t") in ("winlink", "notice", "addon"):
                    self.events.append(m)

    def send(self, m):
        self.run(self.ws.send_str(json.dumps(m)))

    def wait(self, pred, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            for e in list(self.events):
                if pred(e):
                    return e
            time.sleep(0.1)
        return None

    def close(self):
        async def _c():
            await self.ws.close()
            await self.sess.close()
        self.run(_c())
        time.sleep(0.5)


def ended(e):
    return e.get("t") == "winlink" and e.get("session") == "ended"


def fake_log(path):
    try:
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]
    except OSError:
        return []


def _open(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


def start(env_extra=None, *extra, winlink=True, wait_pat=True):
    """The radio and the station. winlink=False: a station with no Pat at
    all (no Winlink panel); wait_pat=False: don't wait for Pat's port (a Pat
    that is meant to fail to start)."""
    global ARDOP
    # Picked now, not at import: a test's own listeners (a stand-in hub) are
    # bound by now, so a pair that is really free can be found.
    ARDOP = testkit.pair_port({APPS, LANE, PATP, WEB, RWEB})
    logf = tempfile.NamedTemporaryFile(prefix="fakepat-", suffix=".jsonl", delete=False).name
    env = dict(os.environ, FAKE_PAT_RIGCTL=str(LANE), FAKE_PAT_LOG=logf)
    env.update(env_extra or {})              # a caller's own FAKE_PAT_LOG (or anything) wins
    logf = env["FAKE_PAT_LOG"]
    radio_extra, cfg = split_flags(extra)
    wl = cfg.setdefault("winlink", {})
    if winlink:
        wl.setdefault("pat", testkit.fake_pat_cmd())
        wl.setdefault("pat_port", PATP)
    # The P2P directory: off unless a test names its own (local) URL, so no
    # test ever fetches the real public file.
    p2pdir_url = wl.pop("_p2pdir_url", None)
    wl.setdefault("directory", {"url": p2pdir_url} if p2pdir_url else False)
    listen = wl.pop("_listen", None)
    max_h = wl.pop("_listen_max_h", None)
    if listen:                                   # METHOD=MHZ -> the host file's block
        method, _, mhz = listen.partition("=")
        wl["listen"] = {method: {"mhz": float(mhz), **({"max_hours": max_h} if max_h is not None else {})}}
    vara = "--winlink-vara-port" in radio_extra

    def radio_flag(name):
        return radio_extra[radio_extra.index(name) + 1] if name in radio_extra else None
    cfg["name"] = "Test"
    cfg["radios"] = [{"id": "ic7300", "name": "IC-7300", "web": RWEB, "rigctl": APPS, "lane": LANE,
                      "modems": ["ardop", "varahf"] if vara else ["ardop"],
                      **({"vara_port": int(radio_flag("--winlink-vara-port")),
                          "vara_log": radio_flag("--winlink-vara-log")} if vara else {})}]
    # ardopcf: external, as a Mac's agent is; its stand-in listens so the station finds it
    wl.setdefault("ardopcf", {"label": "test.ardopcf", "bin": "/nonexistent/ardopcf", "port": ARDOP,
                              "audio_in": "x", "audio_out": "x", "hostcommands": ""})
    ardop = subprocess.Popen([sys.executable, "tests/fake_ardopcf.py", str(ARDOP), "x", "x", "--logdir",
                              tempfile.mkdtemp(prefix="ardop-")], cwd=ROOT,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not winlink:
        cfg.pop("winlink")
    wl.setdefault("ardop_logs", tempfile.mkdtemp(prefix="ardoplogs-"))
    cfg.setdefault("addons_state", "")
    addons_file = cfg.pop("_addons_file", None)
    if addons_file:
        with open(os.path.expanduser(addons_file)) as f:
            adds = json.load(f)
        pk = adds.pop("packet", None)
        if pk:                                   # the station shows the hub itself (no Packet add-on there)
            cfg["packet"] = {"url": pk["url"], "agwpe": pk.get("agwpe", "127.0.0.1:8000"),
                             **{k: pk[k] for k in ("name", "title") if k in pk}}
        if adds:
            cfg["addons"] = {**cfg.get("addons", {}), **adds}
    cfg_dir = tempfile.mkdtemp(prefix="wlstation-")
    cfg_path = os.path.join(cfg_dir, "station.json")
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    radio = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--no-audio", "--host", "127.0.0.1", "--listen", str(RWEB),
         "--rigctl-host", "127.0.0.1", "--rigctl-port", str(APPS), "--winlink-rigctl-port", str(LANE),
         "--addons-state", "", *radio_extra],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    station = None
    # Ceilings, not guesses: CI's two-CPU runners start a Python process slowly.
    for _ in range(400):
        if radio.poll() is not None:
            break
        try:
            for port in (RWEB, LANE):
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        radio.kill()
        print("FAIL  the radio's server did not start; its log:\n" + (radio.stdout.read() or "")[-3000:])
        raise SystemExit("the radio's server did not start")
    if radio.poll() is None and not testkit.wait_for(lambda: _open(ARDOP), 30):
        ardop.kill()
        radio.kill()
        print("FAIL  ardopcf's stand-in did not start (port %d)" % ARDOP)
        raise SystemExit("ardopcf's stand-in did not start")
    if radio.poll() is None:
        station = subprocess.Popen(
            [sys.executable, "station.py", "--config", cfg_path, "--host", "127.0.0.1", "--listen", str(WEB),
             "--rigctl-port", "0"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        pair = Pair(radio, station, cfg_path, ardop)
        for _ in range(600):
            if pair.poll() is not None:
                break
            try:
                for port in (WEB,) + ((PATP,) if winlink and wait_pat else ()):
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                time.sleep(1.2)          # the station's presence poll sees the radio; Pat's event link
                if pair.poll() is None:
                    return pair, logf
            except OSError:
                time.sleep(0.1)
        pair.kill()
        print("FAIL  the station did not start; the logs:\n" + (pair.read() or "")[-4000:])
        raise SystemExit("the station did not start")
    print("FAIL  the radio's server died at start; its log:\n" + (radio.stdout.read() or "")[-3000:])
    raise SystemExit("the radio's server did not start")


def stop(p):
    p.terminate()
    try:
        return p.communicate(timeout=40)[0]
    except subprocess.TimeoutExpired:
        p.kill()
        return p.communicate()[0]


class WebPage:
    """A page's control WebSocket to a server on port `web` (any port), with
    its HTTP routes: winlink and notice events kept in .events."""

    def __init__(self, web):
        self.web = web
        self.loop = asyncio.new_event_loop()
        self.events = []
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.run(self._open())

    def run(self, coro, timeout=20):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _open(self):
        self.sess = aiohttp.ClientSession()
        self.ws = await self.sess.ws_connect(f"http://127.0.0.1:{self.web}/ws")
        self.hello = json.loads((await self.ws.receive()).data)
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                m = json.loads(msg.data)
                if m.get("t") in ("winlink", "notice"):
                    self.events.append(m)

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

    def http(self, method, path, **kw):
        async def go():
            async with self.sess.request(method, f"http://127.0.0.1:{self.web}{path}", **kw) as r:
                return r.status, await r.text()
        return self.run(go())

    def snapshot(self):
        st, body = self.http("GET", "/winlink/api/status")
        return json.loads(body) if st == 200 else {"http": st}

    def close(self):
        async def _c():
            await self.ws.close()
            await self.sess.close()
        self.run(_c())
