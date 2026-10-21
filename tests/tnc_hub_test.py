"""tnc_hub.py at the wire: the real hub against tests/fake_kiss_radio.py.

Each scenario starts a fake KISS radio on a pseudo-terminal and tnc_hub.py on
it, drives the hub through its AGWPE and KISS ports and its web API, and
asserts on what reached the "air". A simulated station (built on ax25_link,
running in this process) answers connected-mode calls, optionally over a
lossy channel. Nothing here opens real hardware or transmits.

    .venv/bin/python tests/tnc_hub_test.py          (ONLY=<name part> to pick one)
    PAT=/path/to/pat .venv/bin/python tests/tnc_hub_test.py   # adds the real-Pat scenario
"""
import asyncio
import json
import os
import random
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from ax25 import Frame, KissDecoder, U_DM, U_SABM, kiss_wrap  # noqa: E402
from ax25_link import Link  # noqa: E402
from fake_kiss_radio import FakeKissRadio  # noqa: E402

AGW_HDR = struct.Struct("<B3xcxBx10s10sI4x")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))
    return ok


def check_true(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'ok  ' if cond else 'FAIL'}  {name:62} {detail}")
    return cond


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_for(pred, timeout=10.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


# ------------------------------------------------------------ the hub

class HubProc:
    def __init__(self, radio, tx=True, extra=()):
        self.agw, self.kiss, self.http, self.ctrl = free_port(), free_port(), free_port(), free_port()
        self.tmp = tempfile.mkdtemp(prefix="tnchub-")
        cmd = [sys.executable, os.path.join(ROOT, "tnc_hub.py"), "--tnc", radio.path,
               "--mycall", "N9ABC", "--agw-port", str(self.agw), "--kiss-port", str(self.kiss),
               "--http-port", str(self.http), "--http-bind", "127.0.0.1", "--control-port", str(self.ctrl),
               "--frack", "0.4", "--retries", "4", "--baud", "96000",
               # This clock runs ~80x faster than 1200 baud, so the per-minute
               # runaway guard is raised here; scenario_frame_rate_cap tests it.
               "--max-frames-per-min", "100000",
               "--log-dir", self.tmp, *extra]
        if tx:
            cmd.append("--tx")
        self.log = open(os.path.join(self.tmp, "hub.log"), "w+")
        self.p = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT)
        ok = wait_for(lambda: self._up(), 8)
        if not ok:
            self.stop()
            raise RuntimeError("hub did not start:\n" + self.output())

    def _up(self):
        try:
            return self.status()["radio"]["up"]
        except Exception:     # noqa: BLE001
            return False

    def status(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.http}/api/status", timeout=2) as r:
            return json.load(r)

    def set_tx(self, enabled, host="127.0.0.1", **kw):
        return self.post("/api/tx", {"enabled": enabled}, host=host, **kw)

    def post(self, path, body, host="127.0.0.1", csrf=True, origin=None):
        """csrf: send the X-Ranch-Radio header every POST needs (as the
        hub's pages and the UV-PRO add-on do); origin: a browser's Origin."""
        headers = {"content-type": "application/json"}
        if csrf:
            headers["X-Ranch-Radio"] = "1"
        if origin:
            headers["Origin"] = origin
        req = urllib.request.Request(f"http://{host}:{self.http}{path}", method="POST",
                                     data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def output(self):
        self.log.flush()
        self.log.seek(0)
        return self.log.read()

    def stop(self):
        if self.p.poll() is None:
            self.p.send_signal(signal.SIGINT)
            try:
                self.p.wait(5)
            except subprocess.TimeoutExpired:
                self.p.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ------------------------------------------------------- AGWPE client

class Agw:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.frames = []
        self.lock = threading.Lock()
        threading.Thread(target=self._rx, daemon=True).start()

    def send(self, kind, frm="", to="", data=b"", pid=0):
        pad = lambda c: c.encode()[:10].ljust(10, b"\0")   # noqa: E731
        self.s.sendall(AGW_HDR.pack(0, kind.encode(), pid, pad(frm), pad(to), len(data)) + data)

    def _rx(self):
        buf = b""
        try:
            while True:
                d = self.s.recv(65536)
                if not d:
                    return
                buf += d
                while len(buf) >= 36:
                    port, kind, pid, frm, to, n = AGW_HDR.unpack(buf[:36])
                    if len(buf) < 36 + n:
                        break
                    data = buf[36:36 + n]
                    buf = buf[36 + n:]
                    with self.lock:
                        self.frames.append((kind.decode(), frm.split(b"\0")[0].decode(),
                                            to.split(b"\0")[0].decode(), data))
        except OSError:
            return

    def got(self, kind):
        with self.lock:
            return [f for f in self.frames if f[0] == kind]

    def wait(self, kind, timeout=10, n=1):
        return wait_for(lambda: len(self.got(kind)) >= n and self.got(kind), timeout)

    def close(self):
        self.s.close()


class Kiss:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.dec = KissDecoder()
        self.frames = []
        threading.Thread(target=self._rx, daemon=True).start()

    def _rx(self):
        try:
            while True:
                d = self.s.recv(4096)
                if not d:
                    return
                self.frames += [p for _, c, p in self.dec.feed(d) if c == 0]
        except OSError:
            return

    def send(self, raw, cmd=0):
        self.s.sendall(kiss_wrap(raw, cmd=cmd))

    def close(self):
        self.s.close()


# ---------------------------------------------- simulated remote station

class SimStation:
    """A station on the air, running ax25_link in its own event loop thread.

    mode "echo": sends back whatever it receives.
    mode "rms":  a minimal Winlink CMS gateway (banner, answers FF with FQ).
    mode "dm":   refuses every connect with DM.
    mode "deaf": never answers.
    """

    def __init__(self, radio, call="W7TST-10", mode="echo", payload=b""):
        self.radio, self.call, self.mode, self.payload = radio, call, mode, payload
        self.link = None
        self.rx = bytearray()
        self.events = []
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        radio.on_air = lambda raw: self.loop.call_soon_threadsafe(self._heard, raw)

    def _send(self, f):
        self.radio.inject(f.encode())

    def _heard(self, raw):
        try:
            f = Frame.decode(raw)
        except Exception:      # noqa: BLE001
            return
        if str(f.dest) != self.call:
            return
        if self.mode == "deaf":
            return
        if self.mode == "dm" and f.kind == "U" and f.utype == U_SABM:
            self._send(Frame(f.src, f.dest, kind="U", utype=U_DM, pf=f.pf, command=False))
            return
        if self.link is None or self.link.state == "disconnected":
            if f.kind == "U" and f.utype == U_SABM:
                self.link = Link(self.call, str(f.src), self._send, self, frack=0.4, n2=6,
                                 baud=96000, ack_delay=0.05)
                self.link.accept(f)
            return
        self.link.handle(f)

    # Link events
    def on_connected(self, link, inbound):
        self.events.append("connected")
        if self.mode == "rms":
            link.send(b"[WL2K-5.0-B2FWIHJM$]\rCMS via W7TST >\r")
        elif self.payload:
            link.send(self.payload)

    def on_data(self, link, data):
        self.rx += data
        if self.mode == "echo":
            link.send(data)
        elif self.mode == "rms":
            self._rms(link)

    def _rms(self, link):
        rx = bytes(self.rx)
        if "fs" not in self.events and b"\rF>" in rx:
            self.events.append("fs")              # accept every proposal
            n = rx.count(b"FC ")
            link.send(b"FS " + b"+" * n + b"\r")
            self.block_start = len(rx)
        elif "fs" in self.events and "got-block" not in self.events:
            blk = rx[self.block_start:]
            # B2 block: SOH len subject\0 offset\0, STX len data..., EOT checksum
            if len(blk) >= 2 and blk[0] == 0x01:
                i = 2 + blk[1]
                while i < len(blk) and blk[i] == 0x02:
                    i += 2 + (blk[i + 1] or 256)
                if i + 1 < len(blk) and blk[i] == 0x04:
                    self.events.append("got-block")
                    self.block = blk[:i + 2]
                    link.send(b"FF\r")
        elif "fq" not in self.events and b"FF\r" in rx and "fs" not in self.events:
            self.events.append("fq")
            link.send(b"FQ\r")

    def on_disconnected(self, link, reason):
        self.events.append(f"disconnected: {reason}")

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


def aprs(src, info, dest="APRS", digis=()):
    return Frame.ui(src, dest, info.encode(), digis=digis).encode()


# ------------------------------------------------------------ scenarios

def scenario_receive_fanout():
    radio = FakeKissRadio().start()
    hub = HubProc(radio, tx=False)
    try:
        k = Kiss(hub.kiss)
        a = Agw(hub.agw)
        a.send("k")                    # raw monitoring on
        time.sleep(0.3)
        pkt = aprs("KF7ABC-9", "!4049.00N/10510.00W>test", digis=["WIDE1-1"])
        msg = aprs("KF7ABC-9", ":N9ABC-7  :hello from the trail{12")
        radio.inject(pkt)
        radio.inject(msg)
        wait_for(lambda: len(k.frames) >= 2, 5)
        check("KISS client gets both frames, byte for byte", k.frames[:2], [pkt, msg])
        raws = a.wait("K", 5, n=2) or []
        check("AGWPE raw monitor gets both frames", [r[3][1:] for r in raws[:2]], [pkt, msg])
        st = hub.status()
        check("heard list has the station", [h["station"] for h in st["heard"]], ["KF7ABC-9"])
        check("message to N9ABC-7 is surfaced", [(m["to"], m["text"]) for m in st["messages"]],
              [("N9ABC-7", "hello from the trail{12")])
        check("nothing transmitted", radio.transmitted(), [])
        k.close(); a.close()
    finally:
        hub.stop(); radio.stop()


def scenario_receive_only_refuses_everything():
    radio = FakeKissRadio().start()
    hub = HubProc(radio, tx=False)
    try:
        k = Kiss(hub.kiss)
        k.send(aprs("N9ABC-7", ">status"))
        a = Agw(hub.agw)
        a.send("X", "N9ABC-10")
        a.wait("X")
        a.send("M", "N9ABC-10", "APRS", b">status", pid=0xF0)
        a.send("C", "N9ABC-10", "W7TST-10")
        d = a.wait("d", 5)
        check_true("AGWPE connect answered with disconnect", bool(d),
                   repr(d[0][3]) if d else "no 'd' frame")
        check_true("…saying transmit is off", bool(d) and b"transmit is off" in d[0][3])
        time.sleep(0.5)
        check("nothing transmitted (KISS, UI, connect)", radio.transmitted(), [])
        status, body = hub.set_tx(True)
        check("page cannot switch transmit on without --tx", status, 403)
        k.close(); a.close()
    finally:
        hub.stop(); radio.stop()


def scenario_callsign_gate_and_kiss_commands():
    radio = FakeKissRadio().start()
    # N0CALL: a placeholder second call (KK1ABC, the old one, is retired).
    hub = HubProc(radio, tx=True, extra=["--allow-calls", "N9ABC,N0CALL"])
    try:
        k = Kiss(hub.kiss)
        bad = aprs("W1AW", ">not me")
        good = aprs("N9ABC-7", ">status from N9ABC")
        good2 = aprs("N0CALL", ">status from N0CALL")
        k.send(bytes([0x01, 0x02, 0x03]), cmd=1)     # TXDELAY parameter
        k.send(b"\x00garbage")                         # undecodable
        k.send(bad)
        k.send(good)
        k.send(good2)
        wait_for(lambda: len(radio.transmitted()) >= 2, 5)
        time.sleep(0.4)
        check("only the N9ABC and N0CALL frames went out",
              [r for _, r in radio.transmitted()], [good, good2])
        check("KISS parameter command never reached the radio", radio.cmds, [])
        st = hub.status()
        check("refusals counted", st["counts"].get("tx_refused"), 2)
        k.close()
    finally:
        hub.stop(); radio.stop()


def scenario_ui_rate_limit():
    radio = FakeKissRadio().start()
    hub = HubProc(radio, tx=True, extra=["--max-ui-per-min", "3"])
    try:
        k = Kiss(hub.kiss)
        for i in range(6):
            k.send(aprs("N9ABC-7", f">beacon {i}"))
        time.sleep(1.0)
        check("UI frames capped at 3 per minute", len(radio.transmitted()), 3)
        k.close()
    finally:
        hub.stop(); radio.stop()


def scenario_page_kill_switch():
    radio = FakeKissRadio().start()
    hub = HubProc(radio, tx=True, extra=("--http-bind", "0.0.0.0"))   # reachable from the LAN, as deployed
    try:
        status, body = hub.set_tx(False)
        check("page switches transmit off", (status, body.get("enabled")), (200, False))
        k = Kiss(hub.kiss)
        k.send(aprs("N9ABC-7", ">after kill"))
        time.sleep(0.6)
        check("nothing goes out after the kill switch", radio.transmitted(), [])
        status, body = hub.set_tx(True)
        check("loopback can switch it back on (started with --tx)", (status, body.get("enabled")),
              (200, True))
        lan = lan_address()
        if lan:
            # D7 (2026-10-19): any page on the LAN may switch transmit on or
            # off, as radio pages take PTT from the LAN; --tx is the hard switch.
            check("another machine switches it off", hub.set_tx(False, host=lan)[0], 200)
            check("another machine switches it back on", hub.set_tx(True, host=lan), (200, {"enabled": True}))
        else:
            print("skip  transmit on/off from another machine: no network address here")
        k.close()
    finally:
        hub.stop(); radio.stop()


def scenario_cross_site_guard():
    """Every POST needs X-Ranch-Radio: 1, from loopback too (the attacking
    browser may be on this machine), and a browser's Origin must be this hub."""
    radio = FakeKissRadio().start()
    hub = HubProc(radio, tx=True)
    try:
        hub.set_tx(False)
        check("POST without the header: refused", hub.set_tx(True, csrf=False)[0], 403)
        check("  and transmit stayed off", hub.status()["tx"]["enabled"], False)
        check("POST from a foreign page: refused",
              hub.set_tx(True, origin="http://evil.example")[0], 403)
        check("Origin null (sandboxed page): refused", hub.set_tx(True, origin="null")[0], 403)
        check("  and transmit stayed off", hub.status()["tx"]["enabled"], False)
        check("lend without the header: refused",
              hub.post("/api/lend", {"seconds": 5, "to": "x"}, csrf=False)[0], 403)
        check("the hub's own page (same Origin): accepted",
              hub.set_tx(True, origin=f"http://127.0.0.1:{hub.http}"), (200, {"enabled": True}))
        req = urllib.request.Request(f"http://127.0.0.1:{hub.http}/api/tx", method="POST",
                                     data=b'{"enabled": true}',
                                     headers={"Host": f"evil.example:{hub.http}", "X-Ranch-Radio": "1",
                                              "Origin": f"http://evil.example:{hub.http}",
                                              "content-type": "application/json"})
        try:
            code = urllib.request.urlopen(req, timeout=3).status
        except urllib.error.HTTPError as e:
            code = e.code
        check("DNS rebinding (unknown Host, matching Origin): refused", code, 403)
        check("  scheme mismatch in Origin: refused",
              hub.set_tx(True, origin=f"https://127.0.0.1:{hub.http}")[0], 403)
        with urllib.request.urlopen(f"http://127.0.0.1:{hub.http}/static/meshmap.js", timeout=2) as r:
            check("static files served (meshmap.js)", (r.status, b"meshMap" in r.read()), (200, True))
            check("  and never cached stale", r.headers.get("Cache-Control"), "no-cache")
        with urllib.request.urlopen(f"http://127.0.0.1:{hub.http}/", timeout=2) as r:
            check("the page is never cached stale", r.headers.get("Cache-Control"), "no-cache")
    finally:
        hub.stop(); radio.stop()


def scenario_on_needs_tx_at_start():
    radio = FakeKissRadio().start()
    hub = HubProc(radio, tx=False)
    try:
        status, body = hub.set_tx(True)
        check("started without --tx: On refused", (status, body.get("error")), (403, "started without --tx"))
    finally:
        hub.stop(); radio.stop()


def scenario_frame_rate_cap():
    radio = FakeKissRadio().start()
    hub = HubProc(radio, tx=True, extra=["--max-frames-per-min", "5", "--max-ui-per-min", "100"])
    try:
        k = Kiss(hub.kiss)
        for i in range(9):
            k.send(aprs("N9ABC-7", f">burst {i}"))
        time.sleep(1.0)
        check("overall frame cap holds at 5 per minute", len(radio.transmitted()), 5)
        k.close()
    finally:
        hub.stop(); radio.stop()


def _connected_transfer(loss, size, name):
    radio = FakeKissRadio(loss=loss, seed=7).start()
    rng = random.Random(3)
    payload = bytes(rng.randrange(256) for _ in range(size))
    sim = SimStation(radio, mode="echo")
    hub = HubProc(radio, tx=True, extra=["--retries", "12"])
    try:
        a = Agw(hub.agw)
        a.send("X", "N9ABC-10")
        check(f"{name}: callsign registered", (a.wait("X") or [[None] * 4])[0][3], b"\x01")
        t0 = time.monotonic()
        a.send("C", "N9ABC-10", "W7TST-10")
        c = a.wait("C", 20)
        check_true(f"{name}: connected", bool(c) and c[0][3].startswith(b"*** CONNECTED With "),
                   repr(c[0][3]) if c else "no 'C'")
        if not c:
            print(hub.output()[-3000:])
            return
        check(f"{name}: 'C' frame names the remote in From", (c[0][1], c[0][2]),
              ("W7TST-10", "N9ABC-10"))
        for i in range(0, len(payload), 200):
            a.send("D", "N9ABC-10", "W7TST-10", payload[i:i + 200], pid=0xF0)
        got = wait_for(lambda: sum(len(d[3]) for d in a.got("D")) >= size and a.got("D"), 90)
        echoed = b"".join(d[3] for d in a.got("D"))
        check(f"{name}: {size} bytes echoed back intact", echoed == payload, True)
        a.send("Y", "N9ABC-10", "W7TST-10")
        y = a.wait("Y", 5)
        check(f"{name}: outstanding frames drain to 0",
              struct.unpack("<I", y[-1][3])[0] if y else None, 0)
        a.send("d", "N9ABC-10", "W7TST-10")
        d = a.wait("d", 20)
        check_true(f"{name}: disconnect acknowledged", bool(d), repr(d[0][3]) if d else "")
        st = hub.status()
        print(f"      {name}: {time.monotonic() - t0:.1f}s, link stats gone: {st['links'] == []}, "
              f"tx frames {st['counts'].get('tx_frames')}, rx {st['counts'].get('rx_frames')}")
        check(f"{name}: hub link table empty after disconnect", st["links"], [])
        a.close()
        del got
    finally:
        hub.stop(); sim.stop(); radio.stop()


def scenario_connected_clean():
    _connected_transfer(0.0, 3000, "clean channel")


def scenario_connected_lossy():
    _connected_transfer(0.15, 3000, "15% frame loss")


def scenario_connect_no_answer():
    radio = FakeKissRadio().start()
    sim = SimStation(radio, mode="deaf")
    hub = HubProc(radio, tx=True)
    try:
        a = Agw(hub.agw)
        a.send("X", "N9ABC-10")
        a.wait("X")
        a.send("C", "N9ABC-10", "W7TST-10")
        d = a.wait("d", 30)
        check_true("no answer: AGWPE gets RETRYOUT", bool(d) and b"RETRYOUT" in d[0][3],
                   repr(d[0][3]) if d else "no 'd'")
        sabms = [r for _, r in radio.transmitted() if Frame.decode(r).name() == "SABM"]
        check("no answer: SABM sent 1 + N2 times", len(sabms), 5)
        a.close()
    finally:
        hub.stop(); sim.stop(); radio.stop()


def scenario_connect_busy():
    radio = FakeKissRadio().start()
    sim = SimStation(radio, mode="dm")
    hub = HubProc(radio, tx=True)
    try:
        a = Agw(hub.agw)
        a.send("X", "N9ABC-10")
        a.wait("X")
        a.send("C", "N9ABC-10", "W7TST-10")
        d = a.wait("d", 10)
        check_true("DM: AGWPE told busy", bool(d) and b"busy" in d[0][3],
                   repr(d[0][3]) if d else "no 'd'")
        a.close()
    finally:
        hub.stop(); sim.stop(); radio.stop()


def scenario_wrong_source_connect():
    radio = FakeKissRadio().start()
    hub = HubProc(radio, tx=True)
    try:
        a = Agw(hub.agw)
        a.send("X", "W1AW")
        a.wait("X")
        a.send("C", "W1AW", "W7TST-10")
        d = a.wait("d", 5)
        check_true("connect as someone else's call refused", bool(d) and b"not an allowed" in d[0][3],
                   repr(d[0][3]) if d else "no 'd'")
        time.sleep(0.3)
        check("…and nothing transmitted", radio.transmitted(), [])
        a.close()
    finally:
        hub.stop(); radio.stop()


def scenario_radio_drop_ends_link():
    radio = FakeKissRadio().start()
    sim = SimStation(radio, mode="echo")
    hub = HubProc(radio, tx=True)
    try:
        a = Agw(hub.agw)
        a.send("X", "N9ABC-10")
        a.wait("X")
        a.send("C", "N9ABC-10", "W7TST-10")
        if not check_true("radio drop: connected first", bool(a.wait("C", 10))):
            return
        radio.stop()                         # Bluetooth link gone
        d = a.wait("d", 10)
        check_true("radio drop: AGWPE client told the link ended",
                   bool(d) and b"radio" in d[0][3], repr(d[0][3]) if d else "no 'd'")
        st = wait_for(lambda: not hub.status()["radio"]["up"] and hub.status(), 5)
        check("radio drop: status shows radio not linked", bool(st) and st["radio"]["up"], False)
        a.close()
    finally:
        hub.stop(); sim.stop()


def lan_address():
    """This machine's address on its network (not loopback), or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        a = s.getsockname()[0]
        return None if a.startswith("127.") else a
    except OSError:
        return None
    finally:
        s.close()


def scenario_lend_radio():
    """The UV-PRO add-on borrows the radio (its control channel answers only
    while the KISS channel is free) and the hub takes it back."""
    radio = FakeKissRadio().start()
    sim = SimStation(radio, mode="echo")
    hub = HubProc(radio, tx=True, extra=("--http-bind", "0.0.0.0"))
    try:
        a = Agw(hub.agw)
        a.send("X", "N9ABC-10")
        a.wait("X")
        a.send("C", "N9ABC-10", "W7TST-10")
        check_true("lend: a link is up first", bool(a.wait("C", 10)))
        code, body = hub.post("/api/lend", {"seconds": 30, "to": "uvpro"})
        check("lend: refused while a link is up", (code, "link" in body.get("error", "")), (409, True))
        check("lend: the link carries on", hub.status()["radio"]["up"], True)
        a.send("d", "N9ABC-10", "W7TST-10")
        wait_for(lambda: not hub.status()["links"], 10)

        k = Kiss(hub.kiss)
        k.send(aprs("N9ABC-7", ">just sent"))
        wait_for(lambda: radio.transmitted(), 3)
        code, body = hub.post("/api/lend", {"seconds": 30, "to": "uvpro"})
        check("lend: refused just after a transmit", (code, "moment ago" in body.get("error", "")), (409, True))
        time.sleep(5.2)
        n_tx = len(radio.transmitted())

        check("lend: seconds out of range refused", hub.post("/api/lend", {"seconds": 301})[0], 400)
        check("lend: seconds not a number refused", hub.post("/api/lend", {"seconds": "30"})[0], 400)
        lan = lan_address()
        if lan:
            check("lend: refused from another machine", hub.post("/api/lend", {"seconds": 30}, host=lan)[0], 403)
            check("return: refused from another machine", hub.post("/api/return", {}, host=lan)[0], 403)
        else:
            print("skip  lend from another machine: no network address here")

        t0 = time.time()
        code, body = hub.post("/api/lend", {"seconds": 30, "to": "uvpro"})
        check("lend: granted when idle", (code, (body.get("lent") or {}).get("to")), (200, "uvpro"))
        st = hub.status()
        check("lend: radio let go at once (answer came after)", st["radio"]["up"], False)
        check("lend: status says who has it", (st["radio"]["lent"] or {}).get("to"), "uvpro")
        check_true("lend: until about 30 s from now", 29 < st["radio"]["lent"]["until"] - t0 < 40.5)
        time.sleep(2.5)
        check("lend: radio stays let go (no reconnect)", hub.status()["radio"]["up"], False)

        k.send(aprs("N9ABC-7", ">while lent"))
        a.close()
        a = Agw(hub.agw)            # the first one's reader timed out in the quiet above
        a.send("X", "N9ABC-10")
        a.wait("X")
        a.send("C", "N9ABC-10", "W7TST-10")
        d = a.wait("d", 5)
        check_true("lend: a connect is refused, saying why",
                   bool(d) and b"lent to uvpro" in d[0][3], repr(d[0][3]) if d else "no 'd'")
        time.sleep(0.5)
        check("lend: nothing reached the radio while lent", len(radio.transmitted()), n_tx)
        refused = [json.loads(l) for l in open(os.path.join(hub.tmp, "tx.jsonl"))]
        check_true("lend: the refused frame is logged as lent",
                   any(r["dir"] == "refused" and "lent to uvpro" in r["why"] for r in refused))

        code, body = hub.post("/api/lend", {"seconds": 60, "to": "uvpro"})
        check_true("lend: asking again extends it", code == 200 and body["lent"]["until"] - t0 > 59)

        code, _ = hub.post("/api/return", {})
        t1 = time.monotonic()
        up = wait_for(lambda: hub.status()["radio"]["up"], 5)
        check_true("return: radio back without the reconnect wait", bool(up) and time.monotonic() - t1 < 1.5,
                   f"{time.monotonic() - t1:.2f}s")
        check("return: lent cleared", hub.status()["radio"]["lent"], None)
        k.send(aprs("N9ABC-7", ">after return"))
        check_true("return: transmit works again", bool(wait_for(lambda: len(radio.transmitted()) > n_tx, 3)))

        time.sleep(5.2)
        hub.post("/api/lend", {"seconds": 1, "to": "uvpro"})
        check("lend 1 s: let go", hub.status()["radio"]["up"], False)
        up = wait_for(lambda: hub.status()["radio"]["up"], 5)
        check_true("lend 1 s: taken back when it runs out (borrower gone)", bool(up))
        check("lend 1 s: lent cleared", hub.status()["radio"]["lent"], None)
        check("return when not lent: harmless", hub.post("/api/return", {})[0], 200)
        check("…and the radio stays up", hub.status()["radio"]["up"], True)
        a.close(); k.close()
    finally:
        hub.stop(); sim.stop(); radio.stop()


def scenario_real_pat():
    pat = os.environ.get("PAT")
    if not pat:
        print("skip  real Pat scenario (set PAT=/path/to/pat)")
        return
    radio = FakeKissRadio().start()
    sim = SimStation(radio, mode="rms")
    hub = HubProc(radio, tx=True)
    tmp = tempfile.mkdtemp(prefix="pat-")
    try:
        cfg = {"mycall": "N9ABC", "secure_login_password": "", "locator": "FN31",
               "ax25": {"engine": "agwpe", "beacon": {"every": 0}},
               "agwpe": {"addr": f"127.0.0.1:{hub.agw}", "radio_port": 0},
               "connect_aliases": {}, "listen": [], "hamlib_rigs": {}}
        cfgp = os.path.join(tmp, "config.json")
        with open(cfgp, "w") as fh:
            json.dump(cfg, fh)
        env = {**os.environ, "HOME": tmp, "XDG_CONFIG_HOME": tmp, "XDG_DATA_HOME": tmp,
               "XDG_STATE_HOME": tmp}
        r = subprocess.run([pat, "--config", cfgp, "--mbox", os.path.join(tmp, "mbox"),
                            "--log", os.path.join(tmp, "pat.log"),
                            "connect", "ax25+agwpe:///W7TST-10"],
                           env=env, capture_output=True, text=True, timeout=90)
        out = r.stdout + r.stderr
        print("      pat says:", " | ".join(l for l in out.splitlines() if l.strip())[-600:])
        check("real Pat: exit status", r.returncode, 0)
        check_true("real Pat: gateway saw Pat's FF and sent FQ", "fq" in sim.events, str(sim.events))
        check_true("real Pat: Pat sent its SID", b"[PAT-" in bytes(sim.rx).upper(), repr(bytes(sim.rx)[:120]))
        check_true("real Pat: link ended cleanly",
                   any(e.startswith("disconnected") for e in sim.events), str(sim.events))
    finally:
        hub.stop(); sim.stop(); radio.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def scenario_real_pat_sends_message():
    pat = os.environ.get("PAT")
    if not pat:
        print("skip  real Pat message scenario (set PAT=/path/to/pat)")
        return
    radio = FakeKissRadio(loss=float(os.environ.get("PAT_LOSS", "0.10")), seed=int(os.environ.get("PAT_SEED", "11"))).start()
    sim = SimStation(radio, mode="rms")
    hub = HubProc(radio, tx=True, extra=["--retries", "12"])
    tmp = tempfile.mkdtemp(prefix="pat-")
    try:
        cfg = {"mycall": "N9ABC", "secure_login_password": "", "locator": "FN31",
               "ax25": {"engine": "agwpe", "beacon": {"every": 0}},
               "agwpe": {"addr": f"127.0.0.1:{hub.agw}", "radio_port": 0},
               "connect_aliases": {}, "listen": [], "hamlib_rigs": {}}
        cfgp = os.path.join(tmp, "config.json")
        with open(cfgp, "w") as fh:
            json.dump(cfg, fh)
        env = {**os.environ, "HOME": tmp, "XDG_CONFIG_HOME": tmp, "XDG_DATA_HOME": tmp,
               "XDG_STATE_HOME": tmp}
        base = [pat, "--config", cfgp, "--mbox", os.path.join(tmp, "mbox"),
                "--log", os.path.join(tmp, "pat.log")]
        rng = random.Random(5)
        body = "\n".join("line %03d %s" % (i, "".join(rng.choice("abcdefghij ") for _ in range(60)))
                         for i in range(80))
        r = subprocess.run(base + ["compose", "-s", "hub test", "N9ABC"], input=body, env=env,
                           capture_output=True, text=True, timeout=30)
        check("real Pat (message): composed", r.returncode, 0)
        try:
            r = subprocess.run(base + ["connect", "ax25+agwpe:///W7TST-10"], env=env,
                               capture_output=True, text=True, timeout=int(os.environ.get("PAT_TIMEOUT", "180")))
        except subprocess.TimeoutExpired as e:
            print("      pat timed out; its output:", (e.stdout or b"")[-800:], (e.stderr or b"")[-800:])
            print("      gateway events:", sim.events, "rx tail:", bytes(sim.rx)[-300:])
            print("      hub log tail:\n" + hub.output()[-2500:])
            raise
        out = r.stdout + r.stderr
        print("      pat says:", " | ".join(l for l in out.splitlines() if l.strip())[-500:])
        check("real Pat (message): exit status", r.returncode, 0)
        check_true("real Pat (message): gateway received the compressed message block",
                   "got-block" in sim.events, str(sim.events))
        blk = getattr(sim, "block", b"")
        print(f"      block {len(blk)} bytes over a 10%-loss channel; "
              f"hub tx {hub.status()['counts'].get('tx_frames')} frames")
        sent = os.listdir(os.path.join(tmp, "mbox", "N9ABC", "sent")) \
            if os.path.isdir(os.path.join(tmp, "mbox", "N9ABC", "sent")) else []
        check("real Pat (message): Pat moved it to sent", len(sent), 1)
    finally:
        if fails:
            keep = os.path.join(tempfile.gettempdir(), f"tnchub-fail-{os.getpid()}.log")
            with open(keep, "w") as fh:
                fh.write(hub.output())
                fh.write("\n--- gateway link events: %r\n" % (sim.events,))
            print("      hub log kept at", keep)
        hub.stop(); sim.stop(); radio.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def gaia(cmd, body=b"", group=2, reply=False, flags=0):
    data = group.to_bytes(2, "big") + (cmd | (0x8000 if reply else 0)).to_bytes(2, "big") + body
    out = bytes([0xFF, 0x01, flags, len(body)]) + data
    return out + (bytes([sum(out) & 0xFF]) if flags & 1 else b"")


# A GET_DEV_INFO reply as linux-station's UV-PRO sent it on RFCOMM 1 (2026-10-16),
# with a C0 (the KISS frame end) put in its body: a channel's frequency or
# name bytes can hold one; this reply didn't.
DEV_INFO_REPLY = bytes.fromhex("ff01000b0002800400060104010090d0c01e54")


def scenario_split_unit():
    import tnc_hub
    s = tnc_hub.RadioSplit()
    a, b = aprs("KF7ABC-9", ">one"), aprs("KF7ABC-9", ">two")
    stream = kiss_wrap(a) + DEV_INFO_REPLY + kiss_wrap(b)
    check("split: the reply's C0 is inside it", b"\xc0" in DEV_INFO_REPLY[8:], True)
    kiss, frames = s.feed(stream)
    check("split: the Gaia frame out whole", frames, [DEV_INFO_REPLY])
    check("split: the KISS bytes are the two frames", kiss, kiss_wrap(a) + kiss_wrap(b))
    s = tnc_hub.RadioSplit()
    got_k, got_g = b"", []
    for i in range(len(stream)):                     # one byte at a time
        k, g = s.feed(stream[i:i + 1])
        got_k += k
        got_g += g
    check("split: byte by byte, the same", (got_g, got_k), ([DEV_INFO_REPLY], kiss_wrap(a) + kiss_wrap(b)))
    s = tnc_hub.RadioSplit()
    ff_in_frame = kiss_wrap(Frame.ui("KF7ABC-9", "APRS", b"\xff\x01\x00\x05data").encode())
    kiss, frames = s.feed(ff_in_frame)
    check("split: FF 01 inside a KISS frame stays KISS", (kiss, frames), (ff_in_frame, []))
    s = tnc_hub.RadioSplit()
    with_sum = gaia(5, b"\x00\x02", flags=1)
    kiss, frames = s.feed(with_sum + kiss_wrap(a))
    check("split: a frame with a checksum byte", (frames, kiss), ([with_sum], kiss_wrap(a)))
    s = tnc_hub.RadioSplit()
    kiss, frames = s.feed(b"\xff\x02" + kiss_wrap(a))
    check("split: FF not followed by 01 is not Gaia", (frames, kiss), ([], b"\xff\x02" + kiss_wrap(a)))
    r = tnc_hub.GaiaReader()
    req = gaia(4, b"\x03")
    check("reader: frames split across reads", r.feed(req[:3]) + r.feed(req[3:] + req), [req, req])
    try:
        tnc_hub.GaiaReader().feed(b"hello")
        check("reader: junk raises", "no", "raised")
    except ValueError:
        check("reader: junk raises", "raised", "raised")


def scenario_control_port():
    def answer(frame):
        if frame[6:8] == b"\x00\x04":               # GET_DEV_INFO: the reply, between two packets
            return kiss_wrap(aprs("KF7ABC-9", ">before")) + DEV_INFO_REPLY + kiss_wrap(aprs("KF7ABC-9", ">after"))
        return None
    radio = FakeKissRadio(on_control=answer).start()
    hub = HubProc(radio, tx=False)
    try:
        k = Kiss(hub.kiss)
        c = socket.create_connection(("127.0.0.1", hub.ctrl), timeout=3)
        wait_for(lambda: hub.status()["clients"]["control"], 3)
        check("control client listed", len(hub.status()["clients"]["control"]), 1)
        req = gaia(4, b"\x03")
        c.sendall(req)
        got = b""
        end = time.monotonic() + 3
        while len(got) < len(DEV_INFO_REPLY) and time.monotonic() < end:
            try:
                got += c.recv(4096)
            except socket.timeout:
                break
        check("GET_DEV_INFO reached the radio as sent", radio.control, [req])
        check("the reply (with its C0) reached the client whole", got, DEV_INFO_REPLY)
        wait_for(lambda: len(k.frames) >= 2, 3)
        check("packets around it still reach KISS clients", k.frames[:2],
              [aprs("KF7ABC-9", ">before"), aprs("KF7ABC-9", ">after")])
        check("transmit off, and nothing transmitted", radio.transmitted(), [])
        n = len(radio.control)
        for label, f in [("HT_SEND_DATA (packet data past the gate)", gaia(31, b"\x80" + b"x" * 20)),
                         ("DO_PROG_FUNC (could key PTT)", gaia(66, b"\x0d")),
                         ("SET_PF", gaia(56, b"\x00")),
                         ("a reply", gaia(4, b"\x00", reply=True)),
                         ("extended group", gaia(4, b"", group=10))]:
            c.sendall(f)
        time.sleep(0.5)
        check("refused commands never reach the radio", len(radio.control) - n, 0)
        check("  counted", hub.status()["counts"].get("control_refused"), 5)
        c.sendall(gaia(22))                              # GET_VOLUME: allowed
        wait_for(lambda: len(radio.control) > n, 3)
        check("an allowed one still passes after", radio.control[-1], gaia(22))
        c.sendall(b"junk!")
        c.settimeout(3)
        try:
            closed = c.recv(10) == b""
        except OSError:
            closed = True
        check("junk on the control port: client dropped", closed, True)
        c.close()
        c = socket.create_connection(("127.0.0.1", hub.ctrl), timeout=3)
        wait_for(lambda: hub.status()["clients"]["control"], 3)
        st, _ = hub.post("/api/lend", {"seconds": 5, "to": "test"})
        c.settimeout(3)
        try:
            closed = c.recv(10) == b""
        except OSError:
            closed = True
        check("lending the radio closes control clients", (st, closed), (200, True))
        c = socket.create_connection(("127.0.0.1", hub.ctrl), timeout=3)
        c.settimeout(3)
        try:
            closed = c.recv(10) == b""
        except OSError:
            closed = True
        check("  and refuses new ones while lent", closed, True)
        hub.post("/api/return", {})
        k.close()
    finally:
        hub.stop(); radio.stop()


def _get(hub, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{hub.http}{path}", timeout=5) as r:
        return json.load(r)


def scenario_aprs_store():
    """APRS plan M2: heard frames reach the store's API, survive a restart,
    and api/status keeps its shape."""
    logdir = tempfile.mkdtemp(prefix="tnchub-aprs-")
    radio = FakeKissRadio().start()
    try:
        hub = HubProc(radio, tx=False, extra=("--log-dir", logdir, "--lat", "32.2", "--lon", "-105.9"))
        try:
            keys = set(hub.status())
            radio.inject(aprs("KF7ABC-9", "!4049.00N/10510.00W>trail", digis=["WIDE1-1"]))
            radio.inject(aprs("KF7ABC-9", ":N9ABC-7  :hello{12"))
            radio.inject(aprs("K7OWN", ";REPEATER *111111z4049.00N/10510.00Wr146.52"))
            st = wait_for(lambda: [r for r in _get(hub, "/api/aprs/stations?rev=0")["rows"]
                                   if r["call"] == "KF7ABC-9" and r["lat"]], 5)
            check("a heard position is in api/aprs/stations", [(r["call"], r["lat"]) for r in st or []],
                  [("KF7ABC-9", 40.816667)])
            check("api/status keeps its keys, plus aprs", keys >= {"radio", "tx", "heard", "recent", "messages",
                                                                    "counts", "clients", "links", "ports", "aprs"}, True)
            h = hub.status()["heard"][0]
            check("  heard rows keep their fields", sorted(k for k in h if k in
                  ("station", "last", "count", "direct", "text")), ["count", "direct", "last", "station", "text"])
            check("  aprs: me from --lat/--lon", hub.status()["aprs"]["me"], {"lat": 32.2, "lon": -105.9})
            d = _get(hub, "/api/aprs/station/KF7ABC-9")
            check("station detail: packets and the message", (len(d["packets"]), d["direct"]), (2, True))
            check("objects", [o["name"] for o in _get(hub, "/api/aprs/objects?rev=0")["rows"]], ["REPEATER"])
            check("messages with a call", [m["text"] for m in _get(hub, "/api/aprs/messages?with=KF7ABC-9")["messages"]],
                  ["hello"])
            try:
                _get(hub, "/api/aprs/station/NOBODY")
                code = 200
            except urllib.error.HTTPError as e:
                code = e.code
            check("an unknown station: 404", code, 404)
        finally:
            hub.stop()
        hub = HubProc(radio, tx=False, extra=("--log-dir", logdir))
        try:
            rows = _get(hub, "/api/aprs/stations?rev=0")["rows"]
            check("after a restart: the station is still there", "KF7ABC-9" in [r["call"] for r in rows], True)
            check("  the backfill didn't run twice", _get(hub, "/api/aprs/station/KF7ABC-9")["count"], 2)
            st = hub.status()
            check("  heard list and our message reload", ([h["station"] for h in st["heard"]][:1],
                  [m["text"] for m in st["messages"]]), (["K7OWN"], ["hello{12"]))
            check("  nothing transmitted", radio.transmitted(), [])
        finally:
            hub.stop()
    finally:
        radio.stop()
        shutil.rmtree(logdir, ignore_errors=True)


def scenario_aprs_loop_latency():
    """APRS plan M2: the event loop stays responsive (p99 lag < 50 ms, from
    the hub's own ticker) through a backfill of the heard sample and a burst."""
    logdir = tempfile.mkdtemp(prefix="tnchub-lat-")
    sys.path.insert(0, HERE)
    import corpus
    sample = corpus.sample()
    if sample:
        with open(sample) as f:
            lines = [l for l in f if l.strip()]
    else:                                        # volume is what matters here: synthetic packets
        lines = [json.dumps({"t": 1792000000 + i, "text": f"N{i % 10}CALL-{i % 16}>APRS,WIDE2-1:!4053.{i % 60:02d}N/10510.00W-test {i}"}) + "\n"
                 for i in range(429)]
    with open(os.path.join(logdir, "rx.jsonl"), "w") as f:
        for _ in range(15):                      # ~6,000 lines, about a real rolled log
            f.writelines(lines)
    radio = FakeKissRadio().start()
    try:
        hub = HubProc(radio, tx=False, extra=("--log-dir", logdir))
        try:
            st = hub.status()["aprs"]
            check("backfill: every line", st["backfill"]["lines"], 15 * len(lines))
            for i in range(300):
                radio.inject(aprs(f"N{i:04d}", f"!31{i % 60:02d}.00N/10510.00W>burst {i}"), lossy=False)
            wait_for(lambda: hub.status()["aprs"]["ingested"] >= 15 * len(lines) + 300, 15)
            a = hub.status()["aprs"]
            check("  the burst is stored", a["ingested"] >= 15 * len(lines) + 300, True)
            check_true("  event-loop lag p99 under 50 ms", a["loop"]["p99_ms"] < 50,
                       f"p99 {a['loop']['p99_ms']} ms, max {a['loop']['max_ms']} ms over {a['loop']['samples']}")
            check("  nothing dropped", (a["dropped"], a["errors"]), (0, 0))
        finally:
            hub.stop()
    finally:
        radio.stop()
        shutil.rmtree(logdir, ignore_errors=True)


SCENARIOS = [scenario_cross_site_guard, scenario_on_needs_tx_at_start, scenario_split_unit, scenario_control_port, scenario_receive_fanout, scenario_receive_only_refuses_everything,
             scenario_callsign_gate_and_kiss_commands, scenario_ui_rate_limit,
             scenario_page_kill_switch, scenario_frame_rate_cap, scenario_connected_clean, scenario_connected_lossy,
             scenario_connect_no_answer, scenario_connect_busy, scenario_wrong_source_connect,
             scenario_radio_drop_ends_link, scenario_lend_radio, scenario_real_pat,
             scenario_real_pat_sends_message, scenario_aprs_store, scenario_aprs_loop_latency]

if __name__ == "__main__":
    only = os.environ.get("ONLY", "")
    for sc in SCENARIOS:
        if only and only not in sc.__name__:
            continue
        print(f"--- {sc.__name__}")
        try:
            sc()
        except Exception as e:     # noqa: BLE001
            fails += 1
            print(f"FAIL  {sc.__name__} raised {type(e).__name__}: {e}")
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
    sys.exit(1 if fails else 0)
