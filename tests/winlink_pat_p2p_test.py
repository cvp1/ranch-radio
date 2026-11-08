"""E-1: P2P between two real Pats over telnet on 127.0.0.1. Nothing on air.

Station A is server.py --sim running a real `pat http`, dialing B as a peer
from the page's WebSocket. Station B is a second real Pat on `--listen
telnet`. Both live in temp dirs: this machine's own Pat is never touched.

  - A's outbox {to B, to B and another, P2P only to B, to an email}: B gets
    exactly the first and third (Pat sends a peer only what is addressed
    solely to it, wl2k-go GetOutbound); A hears "sent 2, 2 left"
  - variant: B with an auxiliary address C; a message solely to C goes too

Skips (does not pass) without `pat` on PATH (or PAT=).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import testkit  # noqa: E402
from testkit import free_port, real_pat_env, wait_for  # noqa: E402

PAT = os.environ.get("PAT") or shutil.which("pat")
if not PAT:
    print("skip  E-1 two real Pats over telnet P2P (no pat on PATH; set PAT=)")
    sys.exit(0)

from winlink_test_lib import WebPage as Page, ended  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:64} got {got!r}" + ("" if ok else f" want {want!r}"))
    return ok


def http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


class PatB:
    """Station B: a real Pat listening for telnet P2P on 127.0.0.1."""

    def __init__(self, call, aux=()):
        self.home = tempfile.mkdtemp(prefix="patB-")
        self.telnet, self.http = free_port(), free_port()
        cfg = testkit.pat_config(os.path.join(self.home, "config.json"), mycall=call,
                                 telnet={"listen_addr": f"127.0.0.1:{self.telnet}", "password": "pw"},
                                 auxiliary_addresses=list(aux))
        self.p = subprocess.Popen(
            [PAT, "--config", cfg, "--mbox", os.path.join(self.home, "mbox"), "--log", os.path.join(self.home, "pat.log"),
             "--listen", "telnet", "http", "--addr", f"127.0.0.1:{self.http}"],
            env=real_pat_env(self.home, call), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok = wait_for(lambda: self._status().get("active_listeners") == ["telnet"], 20)
        check(f"E-1 B ({call}) is up and listening on telnet only", ok, True)

    def _status(self):
        try:
            return http_json(f"http://127.0.0.1:{self.http}/api/status", 1)
        except (OSError, ValueError):
            return {}

    def inbox(self):
        return sorted(m["Subject"] for m in http_json(f"http://127.0.0.1:{self.http}/api/mailbox/in"))

    def stop(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(10)
            except subprocess.TimeoutExpired:
                self.p.kill()
        shutil.rmtree(self.home, ignore_errors=True)


class StationA:
    """Station A: Radio Remote with a real Pat (N9ABC) as its engine."""

    def __init__(self):
        self.home = tempfile.mkdtemp(prefix="patA-")
        self.web, self.apps, self.lane, self.patp = free_port(), free_port(), free_port(), free_port()
        cfg = testkit.pat_config(os.path.join(self.home, "config.json"), mycall="N9ABC")
        pat = f"{PAT} --config {cfg} --mbox {os.path.join(self.home, 'mbox')} --log {os.path.join(self.home, 'pat.log')}"
        env = real_pat_env(self.home, "N9ABC")
        self.p = subprocess.Popen(
            [sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(self.web),
             "--rigctl-port", str(self.apps), "--winlink-rigctl-port", str(self.lane),
             "--winlink-pat", pat, "--winlink-pat-port", str(self.patp),
             "--addons-state", os.path.join(self.home, "addons.json")],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.page = None
        up = wait_for(lambda: self._snap().get("pat_busy") is False, 30)
        check("E-1 A (Radio Remote + real Pat N9ABC) is up", up, True)
        self.page = Page(self.web)
        time.sleep(1.2)                # past Pat's log replay (winlink.REPLAY_S)

    def _snap(self):
        try:
            return http_json(f"http://127.0.0.1:{self.web}/winlink/api/status", 1)
        except (OSError, ValueError):
            return {}

    def compose(self, to, subject, p2ponly=False):
        import aiohttp
        form = aiohttp.FormData()
        for k, v in (("to", to), ("subject", subject), ("body", f"{subject}\r\n")):
            form.add_field(k, v)
        if p2ponly:
            form.add_field("p2ponly", "true")
        return self.page.http("POST", "/winlink/api/mailbox/out", data=form)[0]

    def outbox(self):
        st, body = self.page.http("GET", "/winlink/api/mailbox/out")
        return sorted(m["Subject"] for m in json.loads(body)) if st == 200 else f"HTTP {st}"

    def connect_peer(self, b, call):
        self.page.events.clear()
        self.page.send({"t": "winlink_connect", "via": "telnet", "peer": True,
                        "url": f"telnet://N9ABC:pw@127.0.0.1:{b.telnet}/{call}"})
        return self.page.wait(ended, 90)

    def stop(self):
        if self.page:
            self.page.close()
        self.p.terminate()
        try:
            out = self.p.communicate(timeout=40)[0]
        except subprocess.TimeoutExpired:
            self.p.kill()
            out = self.p.communicate()[0]
        shutil.rmtree(self.home, ignore_errors=True)
        return out


a = StationA()
out = ""
try:
    check("E-1 A composes four messages", [a.compose(to, subject, p2p) for to, subject, p2p in (
        ("N0XYZ", "to B", False), ("N0XYZ K7ABC", "to B and another", False),
        ("N0XYZ", "p2p only to B", True), ("someone@example.com", "to an email", False))], [201] * 4)
    b = PatB("N0XYZ")
    try:
        e = a.connect_peer(b, "N0XYZ")
        check("E-1 the peer session ends ok, sent 2, 2 left, peer, no gateway count",
              e and (e.get("ok"), e.get("sent"), e.get("left"), e.get("unsent"), e.get("peer")),
              (True, 2, 2, None, True))
        check("E-1 B received exactly the first and the third", wait_for(lambda: len(b.inbox()) >= 2, 10) and b.inbox(),
              ["p2p only to B", "to B"])
        check("E-1 A's outbox keeps the other two", a.outbox(), ["to B and another", "to an email"])
    finally:
        b.stop()
    # B advertises an auxiliary address C (N0AUX)
    check("E-1 A composes: to C (B's auxiliary address)", a.compose("N0AUX", "to C"), 201)
    b = PatB("N0XYZ", aux=["N0AUX"])
    try:
        e = a.connect_peer(b, "N0XYZ")
        check("E-1 aux variant: ok, sent 1 (the message to C), 2 left",
              e and (e.get("ok"), e.get("sent"), e.get("left")), (True, 1, 2))
        check("E-1 aux variant: B received the message to C", wait_for(lambda: b.inbox(), 10) and b.inbox(), ["to C"])
    finally:
        b.stop()
finally:
    out = a.stop()
check("E-1 no \"still in the outbox\" warning", "still in the outbox" in out, False)
check("E-1 the telnet password never in Radio Remote's log", ":pw@" in out, False)
if fails:
    print("\n".join("   log: " + line for line in out.splitlines() if "winlink" in line.lower())[-3000:])
print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
