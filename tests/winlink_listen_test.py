"""Winlink listen windows (plan winlink-listen, PR 2). Nothing goes on air.

A listen window lets stations that call this one leave traffic: the radio
goes to the listen frequency (UV-PRO add-on), Pat is relaunched with
--listen ax25+agwpe, and the hub's inbound window (A0) opens; at its end the
hub window closes first, Pat drops the flag, and only then is the radio put
back. server.py --sim with tests/fake_pat.py as Pat (it registers MyCall
with the hub over AGWPE when it listens, as Pat does), a real tnc_hub.py on
a fake KISS radio, and tests/fake_uvpro_bridge.py as the radio (it keeps its
state across restarts, as a radio keeps its channel). E-2/E-3 run a real Pat
(/opt/homebrew/bin/pat or PAT=) and skip, not pass, without one.

  B-1   render: the listen block -> both flags (Mac), station.json keeps it;
        absent -> none; bad frequency, hours or method -> HostError naming the field
  B-2   start: record before the QSY, the radio on the frequency, Pat with
        --listen before http, the hub window open, all read back; Stop listening
  B-2b  Stop while the window is still starting: undone, never left listening
  B-3   Pat's listener never comes up: everything undone, the page told
  B-4   refusals change nothing (no QSY, no restart, no hub call)
  B-5   end at `until` (in process, a 3 s window): hub window closed -> Pat without
        the flag -> radio back
  B-5b  the Packet add-on switched off mid-window: the same end path
  B-7   while listening: connect refused, outbox DELETE 409, UV-PRO page writes refused,
        a second Listen refused
  B-8   a clean server stop while listening: everything undone within grace
  B-11  the server SIGKILLed mid-window: the next start undoes it before Pat runs
  B-11b the server SIGKILLed mid outbound packet session: the next start puts the radio back
  B-12  a stale record whose pid is not Pat: that process is not signalled
  B-9   the page: hidden without a plan; two-click confirm text, the state chip,
        Stop listening; a closed page leaves the window open
  E-2   real Pat listening through the hub: active_listeners and MyCall on the hub; both clear after
  E-3   real Pat answers N0XYZ; Pat's dirty disconnect leaves the link; at the end the
        hub link is gone (a DISC on air) before the radio is put back (B-6)

    .venv/bin/python tests/winlink_listen_test.py      (ONLY="B-2 B-5" to pick by id)
"""
import copy
import json
import os
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ["RANCH_RADIO_HOSTS"] = ""          # host files from examples/ only
for _k in ("FAKE_PAT_LOG", "CC_OBS_TOKENS_FILE"):
    os.environ.pop(_k, None)

ONLY = os.environ.get("ONLY", "").split()
if os.path.splitext(os.path.basename(__file__))[0] in ONLY:
    ONLY = []                                  # tests/flake_hunt.sh passes the file's own name
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:70} got {got!r}" + ("" if ok else f" want {want!r}"))
    return ok


def picked(tid):
    return not ONLY or tid in ONLY


# ------------------------------------------------------------ B-1: the host file

if picked("B-1"):
    from ranch_radio import render

    def host(name):
        return json.loads((Path(ROOT) / "examples/hosts" / f"{name}.json").read_text())

    def with_listen(h, block):
        h = copy.deepcopy(h)
        h["winlink"]["listen"] = block
        return h

    def server_argv(h):
        return [a.content["ProgramArguments"] for a in render.artifacts(h, home="/home/t")
                if a.fmt == "plist" and "server.py" in " ".join(a.content.get("ProgramArguments", []))]

    def after(argv, flag):
        return argv[argv.index(flag) + 1] if flag in argv else None

    mac, station = host("mac-shack"), host("linux-station")
    argv = server_argv(with_listen(mac, {"ax25+agwpe": {"mhz": 145.09, "max_hours": 2}}))
    check("B-1 Mac: the listen block -> --winlink-listen ax25+agwpe=145.09",
          [after(a, "--winlink-listen") for a in argv], ["ax25+agwpe=145.09"])
    check("B-1   and --winlink-listen-max-h 2", [after(a, "--winlink-listen-max-h") for a in argv], ["2"])
    argv = server_argv(with_listen(mac, {"ax25+agwpe": {"mhz": 145.09}}))
    check("B-1   max_hours absent -> 4", [after(a, "--winlink-listen-max-h") for a in argv], ["4"])
    check("B-1 Mac: no listen block -> neither flag",
          [("--winlink-listen" in a, "--winlink-listen-max-h" in a) for a in server_argv(mac)], [(False, False)])
    sj = render.station_json(with_listen(station, {"ax25+agwpe": {"mhz": 145.09, "max_hours": 2}}))
    check("B-1 Linux: station.json keeps the block (station.py runs Pat)",
          sj["winlink"].get("listen"), {"ax25+agwpe": {"mhz": 145.09, "max_hours": 2}})
    import listen_plan
    check("B-1   and parses it the same way (one ListenPlan)",
          listen_plan.ListenPlan.from_block(sj["winlink"]["listen"]).flags(),
          ["--winlink-listen", "ax25+agwpe=145.09", "--winlink-listen-max-h", "2"])
    import types
    import winlink as W
    eng = W.PatEngine(types.SimpleNamespace(notice=lambda *a, **k: None), types.SimpleNamespace(
        winlink_pat="pat", winlink_pat_port=1, winlink_session_max=60, winlink_pat_config=None,
        winlink_listen=listen_plan.ListenPlan.from_block(sj["winlink"]["listen"]), addons_state="/tmp/x/addons.json"))
    check("B-1   station.py's engine takes that plan (as station.py builds its args)",
          (eng.listen_plan.hz, eng.listen_plan.max_hours, eng.listen_path), (145_090_000, 2.0, "/tmp/x/winlink-listen.json"))
    eng = W.PatEngine(types.SimpleNamespace(notice=lambda *a, **k: None), types.SimpleNamespace(
        winlink_pat="pat", winlink_pat_port=1, winlink_session_max=60, winlink_pat_config=None))
    check("B-1   no plan in the args: no listening (hidden)", (eng.listen_plan, eng.listen_state()), (None, None))
    check("B-1 Linux: no listen block -> station.json has none", "listen" in render.station_json(station)["winlink"],
          False)
    for name, block, field in (
            ("144.390 (APRS)", {"ax25+agwpe": {"mhz": 144.39}}, "winlink.listen.ax25+agwpe.mhz"),
            ("144.395 (inside the APRS guard)", {"ax25+agwpe": {"mhz": 144.395}}, "winlink.listen.ax25+agwpe.mhz"),
            ("144.050 (below the 2 m data sub-band)", {"ax25+agwpe": {"mhz": 144.05}}, "winlink.listen.ax25+agwpe.mhz"),
            ("7.101 (HF)", {"ax25+agwpe": {"mhz": 7.101}}, "winlink.listen.ax25+agwpe.mhz"),
            ("max_hours 0", {"ax25+agwpe": {"mhz": 145.09, "max_hours": 0}}, "winlink.listen.ax25+agwpe.max_hours"),
            ("max_hours 13", {"ax25+agwpe": {"mhz": 145.09, "max_hours": 13}}, "winlink.listen.ax25+agwpe.max_hours"),
            ("max_hours 0.2", {"ax25+agwpe": {"mhz": 145.09, "max_hours": 0.2}}, "winlink.listen.ax25+agwpe.max_hours"),
            ("an ardop key", {"ardop": {"mhz": 7.101}}, "winlink.listen.ardop"),
            ("a varahf key", {"varahf": {"mhz": 7.101}}, "winlink.listen.varahf"),
            ("two methods", {"ax25+agwpe": {"mhz": 145.09}, "telnet": {}}, "winlink.listen"),
            ("an unknown setting", {"ax25+agwpe": {"mhz": 145.09, "beacon": 600}}, "winlink.listen.ax25+agwpe.beacon"),
            ("mhz as text", {"ax25+agwpe": {"mhz": "145.09"}}, "winlink.listen.ax25+agwpe.mhz")):
        for hname, h in (("Mac", mac), ("Linux", station)):
            try:
                render.artifacts(with_listen(h, block), home="/home/t")
                got = "accepted"
            except render.HostError as e:
                got = f"HostError naming {field}" if f": {field}:" in str(e) else f"HostError: {e}"
            check(f"B-1 {hname}: {name} -> HostError naming the field", got, f"HostError naming {field}")
    for mhz in (145.01, 146.55, 223.4, 441.0):
        try:
            render.validate(with_listen(mac, {"ax25+agwpe": {"mhz": mhz, "max_hours": 12}}))
            got = True
        except render.HostError as e:
            got = str(e)
        check(f"B-1 accepted: {mhz} MHz, 12 h", got, True)
    for mh in (0.25, 12):
        check(f"B-1 server flag check: max-h {mh} accepted",
              listen_plan.ListenPlan.from_flags("ax25+agwpe=145.09", mh).max_hours, float(mh))


# ------------------------------------------------------------ the stack

import re                                      # noqa: E402
import shutil                                  # noqa: E402
import signal                                  # noqa: E402
import subprocess                              # noqa: E402
import tempfile                                # noqa: E402
import time                                    # noqa: E402
from datetime import datetime                  # noqa: E402

import testkit                                 # noqa: E402
from testkit import free_port, wait_for        # noqa: E402
from winlink_test_lib import WebPage           # noqa: E402

LISTEN_MHZ = 145.09                            # channel 3 of the fake radio (FAKE_UVPRO_CH3)
PAT = os.environ.get("PAT") or shutil.which("pat") or ("/opt/homebrew/bin/pat"
                                                     if os.path.exists("/opt/homebrew/bin/pat") else None)


def jsonl(path):
    try:
        with open(path) as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def hub_log_times(hub, text):
    """When the hub logged lines containing text (its asctime, ms)."""
    out = []
    for line in hub.output().splitlines():
        if text in line:
            m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3})", line)
            if m:
                out.append(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp() + int(m.group(2)) / 1000)
    return out


class Hub:
    """A real tnc_hub.py on a fake KISS radio (tests/tnc_hub_test.py's harness)."""

    def __init__(self, tx=True, extra=()):
        import tnc_hub_test as T
        self.T = T
        self.radio = T.FakeKissRadio().start()
        self.proc = T.HubProc(self.radio, tx=tx, extra=extra)
        self.url = f"http://127.0.0.1:{self.proc.http}"

    def status(self):
        return self.proc.status()

    def inbound(self):
        return self.status().get("inbound")

    def registered(self):
        return sorted({c for a in self.status()["clients"]["agw"] for c in a["calls"]})

    def opened(self):
        return hub_log_times(self.proc, "inbound window open for")

    def closed(self):
        return hub_log_times(self.proc, "inbound window for N9ABC closed")

    def stop(self):
        self.proc.stop()
        self.radio.stop()


class Stack:
    """server.py --sim with the Packet and UV-PRO add-ons and Winlink (fake
    Pat unless pat_cmd), a listen plan on 145.090, on a Hub of its own."""

    def __init__(self, hub=None, env=None, listen=f"ax25+agwpe={LISTEN_MHZ:g}", max_h="4", pat_cmd=None,
                 uvpro=True, tmp=None, ports=None, state=True, wait=True, pat_home=None, packet_status=True):
        self.hub = hub or Hub()
        self.own_hub = hub is None
        self.tmp = tmp or tempfile.mkdtemp(prefix="wllisten-")
        self.web, self.apps, self.lane, self.patp = ports or (free_port(), free_port(), free_port(), free_port())
        self.flog = os.path.join(self.tmp, "fakepat.jsonl")
        self.blog = os.path.join(self.tmp, "bridge.jsonl")
        self.radio_file = os.path.join(self.tmp, "radio.json")
        self.state_dir = os.path.join(self.tmp, "state")
        self.record = os.path.join(self.state_dir, "winlink-listen.json")
        self.qsy_record = os.path.join(self.state_dir, "uvpro-qsy.json")
        self.cfg = testkit.pat_config(os.path.join(self.tmp, "patcfg.json"),
                                      agwpe={"addr": f"127.0.0.1:{self.hub.proc.agw}", "radio_port": 0})
        self.pat_cmd = pat_cmd or f"{sys.executable} tests/fake_pat.py --config {self.cfg}"
        cfg = {"packet": {"enabled": True, "url": self.hub.url, "name": "UV-PRO"}}
        if uvpro:
            cfg["uvpro"] = {"enabled": True, "address": "00:11:22:33:44:55", "python": sys.executable,
                            "bridge": os.path.join(HERE, "fake_uvpro_bridge.py"),
                            "control": f"tcp:127.0.0.1:{self.hub.proc.ctrl}",
                            "calls": ["N9ABC"], "restore_wait_s": 3}
            if packet_status:          # the tuner's own "no link up" check (off: only the engine's guards)
                cfg["uvpro"]["packet_status"] = f"{self.hub.url}/api/status"
            else:                      # the hub still holds APRS while tuned (plan 2026-11-07, 3.2)
                cfg["uvpro"].update(hub=self.hub.url, packet_status=None)
        self.addons = os.path.join(self.tmp, "addons.json")
        with open(self.addons, "w") as f:
            json.dump(cfg, f)
        # Real Pat's own HOME/XDG first; then this stack's fakes, whatever the
        # environment already holds (the in-process B-5 sets some for itself).
        self.env = testkit.real_pat_env(pat_home, "N9ABC") if pat_home else dict(os.environ)
        self.env.update(FAKE_PAT_RIGCTL=str(self.lane), FAKE_PAT_LOG=self.flog, FAKE_PAT_NO_PTT="1",
                        FAKE_UVPRO_LOG=self.blog, FAKE_UVPRO_STATE=self.radio_file, FAKE_UVPRO_CH3=f"{LISTEN_MHZ:g}",
                        FAKE_UVPRO_PROBE=os.path.join(self.tmp, "state", "winlink-listen.json"), FAKE_UVPRO_LOG_T="1")
        self.env.update(env or {})
        self.listen, self.max_h, self.use_state, self.has_uvpro = listen, max_h, state, uvpro
        self.page = None
        self.start(wait)

    def start(self, wait=True):
        extra = ["--winlink-listen", self.listen, "--winlink-listen-max-h", self.max_h] if self.listen else []
        state = os.path.join(self.state_dir, "addons.json") if self.use_state else ""
        self.p = subprocess.Popen(
            [sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(self.web),
             "--rigctl-port", str(self.apps), "--winlink-rigctl-port", str(self.lane),
             "--winlink-pat", self.pat_cmd, "--winlink-pat-port", str(self.patp),
             "--addons", self.addons, "--addons-state", state, *extra],
            cwd=ROOT, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.out = None
        if not wait_for(lambda: self._open(self.web), 30):
            self.p.kill()
            raise SystemExit("server did not start:\n" + self.p.stdout.read()[-3000:])
        self.page = WebPage(self.web)
        if wait and not wait_for(self.ready, 40, 0.3):
            print("      snapshot:", self.snap())
            raise SystemExit("the stack did not come up:\n" + self.stop()[-3000:])

    @staticmethod
    def _open(port):
        import socket
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return True
        except OSError:
            return False

    def snap(self):
        return self.page.snapshot()

    def addon_snap(self):
        p = WebPage(self.web)
        try:
            return p.hello.get("addons") or {}
        finally:
            p.close()

    def ready(self):
        s = self.snap()
        if s.get("pat_busy") is not False or not s.get("mycall"):
            return False
        a = self.addon_snap()
        uv, pk = a.get("uvpro") or {}, a.get("packet") or {}
        return bool(pk.get("up") and (not self.has_uvpro or ((uv.get("status") or {}).get("connected")
                                                              and uv.get("settings") and uv.get("memories"))))

    def radio(self):
        """(channel_a, double_channel) as the fake radio holds them."""
        try:
            with open(self.radio_file) as f:
                st = json.load(f)["settings"]
            return st["channel_a"], st["double_channel"]
        except (OSError, ValueError, KeyError):
            return (0, 1)              # never written: as it started

    def writes(self, since=0):
        return [(c["cmd"], c.get("values")) for c in jsonl(self.blog)[since:] if c.get("cmd") in ("settings", "channel")]

    def write_times(self, since=0):
        return [c["t"] for c in jsonl(self.blog)[since:] if c.get("cmd") in ("settings", "channel")]

    def starts(self, since=0):
        return [r for r in jsonl(self.flog) if r.get("route") == "start"][since:]

    def mark(self):
        return len(jsonl(self.blog)), len(self.starts()), len(self.hub.opened())

    def listen_on(self, hours=0.25):
        self.page.events.clear()
        self.page.send({"t": "winlink_listen", "hours": hours})

    def window(self):
        return ((self.snap().get("listen") or {}).get("window"))

    def listening(self, timeout=20):
        return wait_for(lambda: (self.snap().get("listen") or {}).get("window") is not None
                        and (self.snap().get("listen") or {}).get("busy") is None, timeout)

    def idle(self, timeout=40):
        return wait_for(lambda: (self.snap().get("listen") or {}).get("window") is None
                        and (self.snap().get("listen") or {}).get("busy") is None, timeout)

    def notices(self, level=None):
        return [e.get("msg", "") for e in list(self.page.events)
                if e.get("t") == "notice" and (level is None or e.get("level") == level)]

    def stop(self, sig=signal.SIGTERM):
        try:
            if self.page:
                self.page.close()
        except Exception:              # noqa: BLE001 - the server may be gone already
            pass
        self.page = None
        if self.p.poll() is None:
            self.p.send_signal(sig)
        try:
            self.out = self.p.communicate(timeout=60)[0]
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.out = self.p.communicate()[0]
        return self.out

    def close(self):
        if self.p.poll() is None:
            self.stop()
        for r in self.starts():          # no Pat outlives a test
            if alive(r["pid"]):
                os.kill(r["pid"], signal.SIGKILL)
        if self.own_hub:
            self.hub.stop()


def has(msgs, *parts):
    return any(all(p in m for p in parts) for m in msgs)


# ------------------------------------------------------------ B-2, B-7: start, and while listening

if picked("B-2") or picked("B-7"):
    s = Stack()
    try:
        b0, p0, h0 = s.mark()
        t_click = time.time()
        s.listen_on(0.25)
        ok = s.listening()
        check("B-2 listening (page snapshot: a window, nothing in progress)", bool(ok), True)
        w = s.window() or {}
        check("B-2   the window: method, frequency, call", (w.get("method"), w.get("hz"), w.get("call")),
              ("ax25+agwpe", 145_090_000, "N9ABC"))
        check("B-2   until = 15 min from the click", 0.25 * 3600 - 30 < (w.get("until") or 0) - t_click < 0.25 * 3600 + 5,
              True)
        check("B-2 the record was on disk when the radio got its first write",
              [c.get("probe") for c in jsonl(s.blog)[b0:] if c.get("cmd") in ("settings", "channel")][:1], [True])
        check("B-2 the radio tuned to channel 3 (145.090), dual watch off",
              (s.writes(b0), s.radio()), ([("settings", {"channel_a": 2, "double_channel": 0})], (2, 0)))
        st = s.starts(p0)
        argv = st[-1]["argv"] if st else []
        check("B-2 Pat relaunched once, listening on ax25+agwpe", [r.get("listen") for r in st], [["ax25+agwpe"]])
        check("B-2   --listen is a global flag, before http",
              "--listen" in argv and argv.index("--listen") < argv.index("http"), True)
        check("B-2   the old Pat is gone", alive(s.starts()[p0 - 1]["pid"]) if p0 else None, False)
        ib = s.hub.inbound() or {}
        check("B-2 the hub's inbound window is open for N9ABC", ib.get("calls"), ["N9ABC"])
        check("B-2   ending with ours (within 5 s)", abs((ib.get("until") or 0) - (w.get("until") or 0)) <= 5, True)
        check("B-2 the hub lists N9ABC registered (Pat's listener)", s.hub.registered(), ["N9ABC"])
        check("B-2 Pat's status lists the listener", (s.snap().get("status") or {}).get("active_listeners"),
              ["ax25+agwpe"])
        rec = jsonl(s.record)[0] if os.path.exists(s.record) else {}
        check("B-2 the record: stage listening, the listening Pat's pid",
              (rec.get("stage"), rec.get("pid") == (st[-1]["pid"] if st else None)), ("listening", True))
        check("B-2 the page is told (frequency, APRS and iGate off-frequency)",
              has(s.notices("info"), "Listening for calls", "145.090", "APRS"), True)

        if picked("B-7"):
            # compose still works; the outbox can't be changed; connect and a second window refused
            form = __import__("aiohttp").FormData()
            for k, v in (("to", "N0XYZ"), ("subject", "while listening"), ("body", "b")):
                form.add_field(k, v)
            check("B-7 compose while listening -> 201", s.page.http("POST", "/winlink/api/mailbox/out", data=form)[0], 201)
            st_, body = s.page.http("DELETE", "/winlink/api/mailbox/out/OUT0")
            check("B-7 outbox DELETE while listening -> 409, saying why", (st_, "Listening" in body), (409, True))
            st_, _ = s.page.http("POST", "/winlink/api/mailbox/archive", headers={"X-Pat-SourcePath": "/api/mailbox/out/OUT0"})
            check("B-7 outbox move while listening -> 409", st_, 409)
            n_conn = len([r for r in jsonl(s.flog) if r.get("route") == "connect"])
            s.page.events.clear()
            s.page.send({"t": "winlink_connect", "url": "telnet://N9ABC:pw@127.0.0.1:9/wl2k"})
            check("B-7 connect while listening: refused, saying why",
                  bool(s.page.wait(lambda e: e.get("t") == "notice" and "stop listening" in e.get("msg", ""), 5)), True)
            time.sleep(1.0)            # negative check: a window for a dial to reach Pat
            check("B-7   nothing dialed", len([r for r in jsonl(s.flog) if r.get("route") == "connect"]), n_conn)
            b1, p1, h1 = s.mark()
            s.page.events.clear()
            s.page.send({"t": "addon", "addon": "uvpro", "do": "settings", "values": {"squelch_level": 5}})
            check("B-7 a UV-PRO page write while listening: refused (the radio is the window's)",
                  bool(s.page.wait(lambda e: e.get("t") == "notice" and "using the radio" in e.get("msg", ""), 5)), True)
            time.sleep(0.5)            # negative check
            check("B-7   nothing written to the radio", s.writes(b1), [])
            s.listen_on(0.25)
            check("B-7 a second Listen: refused",
                  bool(s.page.wait(lambda e: e.get("t") == "notice" and "Not listening" in e.get("msg", ""), 5)), True)
            time.sleep(1.0)            # negative check
            check("B-7   still one window: no QSY, no restart, no second hub call",
                  (s.writes(b1), len(s.starts()) - p1, len(s.hub.opened()) - h1), ([], 0, 0))
        # Stop listening from the page: B4
        b1, p1, _ = s.mark()
        s.page.events.clear()
        s.page.send({"t": "winlink_listen_stop"})
        check("B-2 Stop listening: back to idle", bool(s.idle()), True)
        check("B-2   Pat relaunched without the flag", [r.get("listen") for r in s.starts(p1)], [[]])
        check("B-2   hub window closed, N9ABC no longer registered", (s.hub.inbound(), s.hub.registered()), (None, []))
        check("B-2   the radio back on channel 1, dual watch A", s.radio(), (0, 1))
        check("B-2   records gone", (os.path.exists(s.record), os.path.exists(s.qsy_record)), (False, False))
        if picked("B-7"):
            form = __import__("aiohttp").FormData()
            for k, v in (("to", "N0XYZ"), ("subject", "after"), ("body", "b")):
                form.add_field(k, v)
            s.page.http("POST", "/winlink/api/mailbox/out", data=form)
            mid = json.loads(s.page.http("GET", "/winlink/api/mailbox/out")[1])[-1]["MID"]
            check("B-7 after: outbox DELETE -> 200", s.page.http("DELETE", f"/winlink/api/mailbox/out/{mid}")[0], 200)
    finally:
        out = s.stop()
        s.close()
    if fails:
        print("\n".join("   | " + l for l in out.splitlines() if "winlink" in l or "UV-PRO" in l)[-4000:])


def end_order(s, b1, p1, h_closed0=0):
    """(hub window closed, Pat started without the flag, the radio's restore
    write) as times, from the hub's log, fake Pat's log and the bridge's log."""
    closed = s.hub.closed()[h_closed0:] + hub_log_times(s.hub.proc, "inbound window for N9ABC ran out")
    plain = [r["t"] for r in s.starts(p1) if "--listen" not in r.get("argv", [])]
    back = [c["t"] for c in jsonl(s.blog)[b1:] if c.get("cmd") == "settings"
            and (c.get("values") or {}).get("channel_a") == 0]
    return (min(closed) if closed else None), (plain[0] if plain else None), (back[0] if back else None)


def in_order(ts):
    return all(t is not None for t in ts) and list(ts) == sorted(ts)


# ------------------------------------------------------------ B-3: Pat's listener never comes up

if picked("B-3"):
    s = Stack(env={"FAKE_PAT_LISTEN_FAIL": "1"})
    try:
        b0, p0, h0 = s.mark()
        s.listen_on(0.25)
        told = s.page.wait(lambda e: e.get("t") == "notice" and "Not listening" in e.get("msg", "")
                           and "put back" in e.get("msg", ""), 30)
        check("B-3 listener missing: the page is told, saying why", bool(told and "listening on []" in told["msg"]), True)
        check("B-3   back to idle", bool(s.idle()), True)
        check("B-3   Pat had the flag, then not", ["--listen" in r["argv"] for r in s.starts(p0)], [True, False])
        check("B-3   the hub window was opened, then closed",
              (len(s.hub.opened()) - h0, len(s.hub.closed()), s.hub.inbound()), (1, 1, None))
        check("B-3   the radio back on channel 1", s.radio(), (0, 1))
        check("B-3   records gone", (os.path.exists(s.record), os.path.exists(s.qsy_record)), (False, False))
        check("B-3   undone in order: hub window, Pat, radio", in_order(end_order(s, b0, p0)), True) or print("      ", end_order(s, b0, p0))
    finally:
        s.stop()
        s.close()


# ------------------------------------------------------------ B-2b: Stop while the window is starting

if picked("B-2b"):
    s = Stack()
    try:
        b0, p0, h0 = s.mark()
        s.listen_on(0.25)
        wait_for(lambda: (s.snap().get("listen") or {}).get("busy") == "starting", 5, 0.02)
        s.page.send({"t": "winlink_listen_stop"})
        check("B-2b Stop while starting: undone, back to idle", bool(s.idle()), True)
        check("B-2b   never left listening: no hub window, Pat without the flag, radio back, no records",
              (s.hub.inbound(), ["--listen" in r["argv"] for r in s.starts(p0)][-1:], s.radio(),
               os.path.exists(s.record), os.path.exists(s.qsy_record)), (None, [False], (0, 1), False, False))
        time.sleep(2.0)                # negative check: nothing reopens it
        check("B-2b   still idle 2 s later", (s.window(), s.hub.inbound()), (None, None))
    finally:
        s.stop()
        s.close()


# ------------------------------------------------------------ B-4: refusals change nothing

if picked("B-4"):
    def nothing(s, m0, label, needle, act=None):
        """Listen refused (a warn naming needle), and no QSY, no Pat restart, no hub window."""
        b0, p0, h0 = m0
        s.page.events.clear()
        (act or (lambda: s.listen_on(0.25)))()
        told = s.page.wait(lambda e: e.get("t") == "notice" and e.get("level") == "warn"
                           and "Not listening" in e.get("msg", ""), 10)
        time.sleep(1.0)                # negative check: a window for any change to show
        check(f"B-4 {label}: refused, saying why", bool(told and needle in told["msg"]),
              True) or print("       ", told)
        check(f"B-4 {label}: no QSY, no restart, no hub window, no record",
              (s.writes(b0), len(s.starts()) - p0, len(s.hub.opened()) - h0, s.hub.inbound(), os.path.exists(s.record)),
              ([], 0, 0, None, False))

    s = Stack(env={"FAKE_PAT_TX_S": "6"})
    try:
        # a session running (telnet: keys nothing)
        s.page.send({"t": "winlink_connect", "url": "telnet://N9ABC:pw@127.0.0.1:9/wl2k"})
        s.page.wait(lambda e: e.get("t") == "winlink" and e.get("session") == "started", 10)
        nothing(s, s.mark(), "a session running", "session")
        s.page.wait(lambda e: e.get("t") == "winlink" and e.get("session") == "ended", 20)
        wait_for(lambda: s.snap().get("pat_busy") is False, 10)
        # Pat in an exchange another station started
        s.page.run(s.page.sess.post(f"http://127.0.0.1:{s.patp}/fake/inbound", json={"remote": "AX.25:N0XYZ"}))
        wait_for(lambda: (s.snap().get("inbound") or {}).get("call") == "N0XYZ", 10)
        nothing(s, s.mark(), "Pat busy (an inbound exchange)", "N0XYZ")
        s.page.run(s.page.sess.post(f"http://127.0.0.1:{s.patp}/fake/inbound/end", json={}))
        wait_for(lambda: s.snap().get("inbound") is None and s.snap().get("pat_busy") is False, 10)
        # hub transmit off
        s.hub.proc.set_tx(False)
        nothing(s, s.mark(), "hub transmit off", "transmit is off")
        s.hub.proc.set_tx(True)
        # Pat's config would listen on its own (preflight)
        with open(s.cfg) as f:
            good = f.read()
        testkit.pat_config(s.cfg, listen=["telnet"], agwpe={"addr": f"127.0.0.1:{s.hub.proc.agw}", "radio_port": 0})
        nothing(s, s.mark(), "preflight refuses Pat's config", "listen")
        with open(s.cfg, "w") as f:
            f.write(good)
        # a window longer than the plan allows
        nothing(s, s.mark(), "hours over max_hours", "0.25 to 4 hours", lambda: s.listen_on(5))
        # the Packet add-on off
        s.page.send({"t": "addon", "addon": "packet", "do": "power", "on": False})
        wait_for(lambda: not (s.addon_snap().get("packet") or {}).get("on"), 10)
        nothing(s, s.mark(), "the Packet add-on off", "Packet add-on is switched off")
        s.page.send({"t": "addon", "addon": "packet", "do": "power", "on": True})
        wait_for(lambda: (s.addon_snap().get("packet") or {}).get("up"), 15)
        # the UV-PRO add-on off: nothing could tune the radio and put it back
        s.page.send({"t": "addon", "addon": "uvpro", "do": "power", "on": False})
        wait_for(lambda: not (s.addon_snap().get("uvpro") or {}).get("on"), 10)
        nothing(s, s.mark(), "the UV-PRO add-on off", "UV-PRO add-on is off")
    finally:
        s.stop()
        s.close()
    # the tuner refuses: no channel on 145.090, no VFO, no qsy_channel
    s = Stack(env={"FAKE_UVPRO_CH3": "", "FAKE_UVPRO_NO_VFO": "1"})
    try:
        nothing(s, s.mark(), "the tuner refuses (no channel on 145.090)", "no FM simplex channel on 145.090")
    finally:
        s.stop()
        s.close()
    # no state directory: a crash could not be undone
    s = Stack(state=False)
    try:
        nothing(s, s.mark(), "no --addons-state", "keeps no state")
    finally:
        s.stop()
        s.close()
    # a server without a listen plan: the feature is hidden
    s = Stack(listen=None)
    try:
        check("B-4 no plan: the snapshot has no listen (hidden on the page)", s.snap().get("listen"), None)
        nothing(s, s.mark(), "no plan", "not set up")
    finally:
        s.stop()
        s.close()


# ------------------------------------------------------------ B-5 and B-4 (admission), in process

if picked("B-5") or picked("B-4"):
    import asyncio
    import types
    import winlink as W
    import logging
    from addons import uvpro as UV

    logging.getLogger().addHandler(logging.NullHandler())   # the engine's warnings are expected here
    hub = Hub()
    tmp = tempfile.mkdtemp(prefix="wllisten-ip-")
    state_dir = os.path.join(tmp, "state")
    blog, flog, radio_file = (os.path.join(tmp, n) for n in ("bridge.jsonl", "fakepat.jsonl", "radio.json"))
    saved_env = dict(os.environ)
    os.environ.update(FAKE_UVPRO_STATE=radio_file, FAKE_UVPRO_CH3=f"{LISTEN_MHZ:g}", FAKE_UVPRO_LOG=blog, FAKE_UVPRO_LOG_T="1",
                      FAKE_PAT_LOG=flog, FAKE_PAT_NO_PTT="1", FAKE_PAT_RIGCTL="0")

    class Host:
        def __init__(self):
            self.notes, self.tuner = [], None

        def notice(self, msg, level="info", ws=None):
            self.notes.append((level, msg))

        async def _send(self, ws, payload):
            pass

        def _broadcast(self, payload):
            pass

        def band_of(self, hz):
            return None

        def winlink_listen_target(self, need_hub=True, need_up=True):
            return {"hub": hub.url, "name": "UV-PRO", "tuner": self.tuner}, None

    async def until(pred, t=20.0):
        end = time.monotonic() + t
        while time.monotonic() < end:
            if pred():
                return True
            await asyncio.sleep(0.05)
        return bool(pred())

    def radio_now():
        try:
            with open(radio_file) as f:
                st = json.load(f)["settings"]
            return st["channel_a"], st["double_channel"]
        except (OSError, ValueError, KeyError):
            return (0, 1)

    async def in_process():
        res = {}
        host = Host()
        host.tuner = UV.UvproAddon(host, {"address": "00:11:22:33:44:55", "control": "tcp:127.0.0.1:1",
                                          "python": sys.executable, "bridge": os.path.join(HERE, "fake_uvpro_bridge.py"),
                                          "packet_status": f"{hub.url}/api/status", "restore_wait_s": 3})
        host.tuner.state_dir = state_dir
        await host.tuner.start()
        await until(lambda: host.tuner._bridge_up() and host.tuner.settings is not None and host.tuner.channels)
        cfg = testkit.pat_config(os.path.join(tmp, "patcfg.json"),
                                 agwpe={"addr": f"127.0.0.1:{hub.proc.agw}", "radio_port": 0})
        args = types.SimpleNamespace(winlink_pat=f"{sys.executable} tests/fake_pat.py --config {cfg}",
                                     winlink_pat_port=free_port(), winlink_session_max=60, winlink_pat_config=None,
                                     winlink_listen=f"ax25+agwpe={LISTEN_MHZ:g}", winlink_listen_max_h=4,
                                     addons_state=os.path.join(state_dir, "addons.json"))
        eng = W.PatEngine(host, args)
        await eng.start()
        try:
            await until(lambda: eng.pat_ws is not None and eng._status_live and eng.mycall
                        and (eng.live_at or 1e18) <= time.monotonic(), 15)
            starts = lambda: [r for r in jsonl(flog) if r.get("route") == "start"]   # noqa: E731
            if picked("B-4"):
                eng._admitting = True          # a connect being admitted (arming its lane)
                n0, b0 = len(starts()), len(jsonl(blog))
                await eng.listen_start(None, 0.25)
                await asyncio.sleep(1.0)       # negative check
                res["admission"] = ([m for lvl, m in host.notes if lvl == "warn"][-1:],
                                    len(starts()) - n0, len(jsonl(blog)) - b0, hub.inbound())
                eng._admitting = False
            if picked("B-5"):
                b0, n0 = len(jsonl(blog)), len(starts())
                W.MIN_HOURS = 0                # a 3-second window (the shortest a page offers is 15 min)
                await eng.listen_start(None, 3 / 3600)
                res["listening"] = (eng.listening is not None, radio_now(), (hub.inbound() or {}).get("calls"))
                t_until = (eng.listening or {}).get("until")
                await until(lambda: eng.listening is None and eng._listen_busy is None, 40)
                t_done = time.time()
                closed = hub.closed() + hub_log_times(hub.proc, "inbound window for N9ABC ran out")
                plain = [r["t"] for r in starts()[n0:] if "--listen" not in r.get("argv", [])]
                back = [c["t"] for c in jsonl(blog)[b0:] if c.get("cmd") == "settings"
                        and (c.get("values") or {}).get("channel_a") == 0]
                res["order"] = (min(closed) if closed else None, plain[0] if plain else None, back[0] if back else None)
                res["until"] = (t_until, t_done)
                res["after"] = (radio_now(), hub.inbound(), eng.status.get("active_listeners"),
                                os.path.exists(os.path.join(state_dir, "winlink-listen.json")))
                res["notes"] = [m for _, m in host.notes if "Listening ended" in m]
        finally:
            await eng.stop()
            await host.tuner.stop()
        return res

    try:
        r = asyncio.run(in_process())
    except Exception as e:                     # noqa: BLE001 - a failed check, not an abort
        import traceback
        traceback.print_exc()
        r = {"error": repr(e)}
    finally:
        hub.stop()
        os.environ.clear()
        os.environ.update(saved_env)
    if picked("B-4"):
        a = r.get("admission") or ([], None, None, None)
        check("B-4 a connect being admitted: refused, saying why", bool(a[0] and "session is running" in a[0][0]), True)
        check("B-4 a connect being admitted: no restart, no QSY, no hub window", a[1:], (0, 0, None))
    if picked("B-5"):
        check("B-5 precondition: a 3 s window open, radio on channel 3, hub window for N9ABC",
              r.get("listening"), (True, (2, 0), ["N9ABC"]))
        t_until, t_done = r.get("until") or (None, None)
        check("B-5 ended at until (within 10 s of it)", bool(t_until and t_until <= t_done <= t_until + 10), True)
        o = r.get("order") or (None, None, None)
        check("B-5 order: hub window closed -> Pat without the flag -> radio back", in_order(o), True) or print("      ", o)
        check("B-5 after: channel 1, no hub window, Pat lists no listener, no record", r.get("after"),
              ((0, 1), None, [], False))
        check("B-5 the page told why", r.get("notes"), ["Listening ended: the window ran out."])


# ------------------------------------------------------------ B-5b: what a window stands on goes away

if picked("B-5b"):
    s = Stack()
    try:
        b0, p0, h0 = s.mark()
        s.listen_on(0.25)
        check("B-5b precondition: listening", bool(s.listening()), True)
        s.page.events.clear()
        s.page.send({"t": "addon", "addon": "packet", "do": "power", "on": False})
        check("B-5b the Packet add-on switched off: the window ends, saying why",
              bool(s.page.wait(lambda e: e.get("t") == "notice" and "Listening ended" in e.get("msg", "")
                               and "Packet add-on" in e.get("msg", ""), 20)), True)
        check("B-5b   idle, hub window closed, Pat without the flag, radio back, no records",
              (bool(s.idle()), s.hub.inbound(), ["--listen" in r["argv"] for r in s.starts(p0)][-1:], s.radio(),
               os.path.exists(s.record)), (True, None, [False], (0, 1), False))
        check("B-5b   in order: hub window, Pat, radio", in_order(end_order(s, b0, p0)), True)
    finally:
        s.stop()
        s.close()


# ------------------------------------------------------------ B-8: a clean stop while listening

if picked("B-8"):
    s = Stack()
    try:
        b0, p0, h0 = s.mark()
        s.listen_on(0.25)
        check("B-8 precondition: listening, radio on channel 3", (bool(s.listening()), s.radio()), (True, (2, 0)))
        pids = [r["pid"] for r in s.starts()]
        t0 = time.time()
        out = s.stop()
        took = time.time() - t0
        check("B-8 the server exited within grace (40 s)", (s.p.returncode is not None, took < 40), (True, True))
        print(f"      took {took:.1f}s")
        check("B-8   the hub window closed (not just run out)", (len(s.hub.closed()), s.hub.inbound()), (1, None))
        check("B-8   no Pat left", [p for p in pids if alive(p)], [])
        check("B-8   N9ABC no longer registered on the hub", wait_for(lambda: s.hub.registered() == [], 5), True)
        check("B-8   the radio back on channel 1", s.radio(), (0, 1))
        check("B-8   records gone", (os.path.exists(s.record), os.path.exists(s.qsy_record)), (False, False))
        b = end_order(s, b0, p0)
        check("B-8   the radio went back after the hub window closed", in_order((b[0], b[2])), True)
        check("B-8   logged", "listen window ended (the server is stopping)" in out, True)
    finally:
        s.close()


# ------------------------------------------------------------ B-11, B-12: crash recovery

def restart_same(s, **kw):
    """A new server on the same ports, state and hub (as launchd would restart it)."""
    s.start(**kw)


if picked("B-11"):
    s = Stack()
    try:
        b0, p0, h0 = s.mark()
        s.listen_on(0.25)
        check("B-11 precondition: listening", bool(s.listening()), True)
        listener = s.starts()[-1]
        check("B-11 precondition: the listening Pat runs, the hub window is open, the radio on channel 3",
              (listener.get("listen"), alive(listener["pid"]), (s.hub.inbound() or {}).get("calls"), s.radio()),
              (["ax25+agwpe"], True, ["N9ABC"], (2, 0)))
        s.stop(signal.SIGKILL)         # the crash: nothing of B4 runs
        time.sleep(1.0)
        check("B-11 after SIGKILL: the listening Pat outlived it, the window still open, the radio still off",
              (alive(listener["pid"]), (s.hub.inbound() or {}).get("calls"), s.radio(), os.path.exists(s.record)),
              (True, ["N9ABC"], (2, 0), True))
        p1, b1 = len(s.starts()), len(jsonl(s.blog))
        restart_same(s)
        check("B-11 the next start: the stale listening Pat is gone", wait_for(lambda: not alive(listener["pid"]), 15),
              True)
        check("B-11   the hub window closed (by the server, not run out)", (len(s.hub.closed()), s.hub.inbound()),
              (1, None))
        new = s.starts(p1)
        sig = [r["t"] for r in jsonl(s.flog) if r.get("route") == "sigterm" and r["t"] > listener["t"]]
        check("B-11   before any new Pat launched (hub closed, old Pat signalled, then the start)",
              bool(new and sig and s.hub.closed() and s.hub.closed()[0] < new[0]["t"] and sig[0] < new[0]["t"]), True)
        check("B-11   the new Pat does not listen", [r.get("listen") for r in new], [[]])
        check("B-11   the radio back on channel 1", wait_for(lambda: s.radio() == (0, 1), 30) and s.radio(), (0, 1))
        check("B-11   not listening, records gone",
              (s.window(), wait_for(lambda: not os.path.exists(s.record) and not os.path.exists(s.qsy_record), 10)),
              (None, True))
        check("B-11   N9ABC no longer registered on the hub", wait_for(lambda: s.hub.registered() == [], 5), True)
    finally:
        out = s.stop()
        s.close()
    check("B-11   logged: the old window ended, never resumed", "was left by a server that did not end it" in out, True)

if picked("B-11b"):
    # The tuner's own record (F16): a crash mid outbound packet session.
    s = Stack(env={"FAKE_PAT_TX_S": "60"}, listen=None)
    try:
        s.page.send({"t": "winlink_connect", "url": "ax25+agwpe:///N0DAJ?freq=145090", "via": "packet"})
        ok = wait_for(lambda: s.radio() == (2, 0) and any(r.get("route") == "connect" for r in jsonl(s.flog)), 20)
        check("B-11b precondition: an outbound packet session, the radio tuned to channel 3", bool(ok), True)
        old = [r["pid"] for r in s.starts()]
        s.stop(signal.SIGKILL)
        for pid in old:                # its Pat would hold the port; not this test's subject
            if alive(pid):
                os.kill(pid, signal.SIGKILL)
        restart_same(s)
        check("B-11b the next start puts the radio back on channel 1",
              wait_for(lambda: s.radio() == (0, 1), 30) and s.radio(), (0, 1))
        check("B-11b   the tuner's record is gone", wait_for(lambda: not os.path.exists(s.qsy_record), 10), True)
    finally:
        s.stop()
        s.close()

if picked("B-12"):
    s = Stack(wait=False)
    s.stop()
    os.makedirs(s.state_dir, exist_ok=True)
    others = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"]),
              # a pat-like command line, but not on this server's port
              subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "--listen", "ax25+agwpe",
                                "http", "--addr", f"127.0.0.1:{free_port()}"])]
    try:
        for i, o in enumerate(others):
            with open(s.record, "w") as f:
                json.dump({"stage": "listening", "pid": o.pid, "pat_port": s.patp, "hub": s.hub.url, "call": "N9ABC",
                           "method": "ax25+agwpe", "hz": 145_090_000, "until": time.time() + 900}, f)
            restart_same(s)
            what = ("a non-Pat process", "a pat-like process on another port")[i]
            check(f"B-12 stale record, pid is {what}: not signalled", (o.poll(), alive(o.pid)), (None, True))
            check(f"B-12   the record is gone, Pat started without the flag",
                  (wait_for(lambda: not os.path.exists(s.record), 10), [r.get("listen") for r in s.starts()][-1:]),
                  (True, [[]]))
            out = s.stop()
            check(f"B-12   logged as left alone", "is not the old listening Pat" in out, True)
    finally:
        for o in others:
            o.kill()
        s.close()


if picked("B-13"):
    # A record written by ANOTHER server that is still running (a second server
    # on the same state: every test server once read the live one) is that
    # server's window: its listening Pat, the hub's inbound window and the
    # record are left alone. Once that owner is gone, the same record is stale.
    s = Stack(wait=False)
    s.stop()
    os.makedirs(s.state_dir, exist_ok=True)
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "server.py"])
    pat = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "--listen", "ax25+agwpe",
                            "http", "--addr", f"127.0.0.1:{s.patp}"])
    rec = {"stage": "listening", "pid": pat.pid, "pat_port": s.patp, "hub": s.hub.url, "call": "N9ABC",
           "method": "ax25+agwpe", "hz": 145_090_000, "until": time.time() + 900, "server_pid": owner.pid}
    def open_window():
        import urllib.request
        req = urllib.request.Request(f"{s.hub.url}/api/inbound", method="POST",
                                     data=json.dumps({"calls": ["N9ABC"], "seconds": 900}).encode(),
                                     headers={"Content-Type": "application/json", "X-Ranch-Radio": "1"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)["inbound"]["calls"]

    try:
        check("B-13 precondition: the owner's window is open on the hub", open_window(), ["N9ABC"])
        with open(s.record, "w") as f:
            json.dump(rec, f)
        restart_same(s)
        time.sleep(2.0)
        check("B-13 owner alive: its listening Pat not signalled", (pat.poll(), alive(pat.pid)), (None, True))
        check("B-13   the hub's inbound window still open", (s.hub.inbound() or {}).get("calls"), ["N9ABC"])
        check("B-13   the record kept", os.path.exists(s.record), True)
        out = s.stop()
        check("B-13   logged as another running server's", "belongs to a running server" in out, True)
        owner.kill()
        owner.wait()
        with open(s.record, "w") as f:
            json.dump(rec, f)
        restart_same(s)
        check("B-13 positive control, owner gone: the stale listening Pat is ended",
              wait_for(lambda: pat.poll() is not None, 15), True)
        check("B-13   the hub window closed by the server", wait_for(lambda: len(s.hub.closed()) == 1, 10)
              and s.hub.inbound(), None)
        check("B-13   the record gone", wait_for(lambda: not os.path.exists(s.record), 10), True)
        s.stop()
    finally:
        for o in (owner, pat):
            if o.poll() is None:
                o.kill()
        s.close()


# ------------------------------------------------------------ B-9: the page

if picked("B-9"):
    CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
    if not CHROME:
        print("skip  B-9 the listen row on the page (no Chromium; set CHROME=)")
    else:
        from cdp_browser import Browser
        TEXT = "(s) => (document.querySelector(s)?.textContent || '')"
        VISIBLE = ("(s) => { const e = document.querySelector(s); "
                   "return !!e && !e.closest('[hidden]') && e.offsetParent !== null; }")
        b = Browser(CHROME, free_port())
        try:
            s = Stack(listen=None)
            try:
                b.goto(f"http://127.0.0.1:{s.web}/?v=noplan#/winlink")
                b.wait(f"({TEXT})('#wl-mycall').includes('N9ABC')")
                check("B-9 no plan: the listen row is hidden", b.js(f"({VISIBLE})('#wl-listen')"), False)
            finally:
                s.stop()
                s.close()
            s = Stack()
            try:
                b.goto(f"http://127.0.0.1:{s.web}/?v=listen#/winlink")
                b.wait(f"({TEXT})('#wl-mycall').includes('N9ABC')")
                b.js("window.__errs = []; window.addEventListener('error', (e) => window.__errs.push(e.message))")
                check("B-9 a plan: the listen row shows, Listen and the hours", (
                    b.wait(f"({VISIBLE})('#wl-listen-btn')", 10), b.js(f"({VISIBLE})('#wl-listen-stop')")), (True, False))
                check("B-9   hours offered up to max_hours (4), defaulting to it",
                      (b.js("[...document.querySelectorAll('#wl-listen-hours option')].map((o) => o.textContent)"),
                       b.js("document.querySelector('#wl-listen-hours').value")),
                      (["15 min", "30 min", "1 h", "2 h", "4 h"], "4"))
                b.js("document.querySelector('#wl-listen-hours').value = '0.25'")
                b.js("document.querySelector('#wl-listen-btn').click()")
                check("B-9 the first click asks to confirm: frequency, hours, APRS and iGate off-frequency",
                      b.js(f"({TEXT})('#wl-listen-btn')"),
                      "Confirm: answer calls on 145.090 MHz for 15 min · APRS and iGate off-frequency meanwhile")
                time.sleep(1.0)        # negative check: one click starts nothing
                check("B-9   one click starts nothing", (s.window(), s.writes()), (None, []))
                b.js("document.querySelector('#wl-listen-btn').click()")
                chip = b.wait(f"({TEXT})('#wl-state').startsWith('Listening on 145.090 MHz until')", 30)
                check("B-9 confirmed: the state chip says Listening, until when, for which call",
                      bool(chip) and "answering calls to N9ABC" in b.js(f"({TEXT})('#wl-state')"), True)
                check("B-9   Stop listening shows, Listen hides, Connect disabled",
                      (b.js(f"({VISIBLE})('#wl-listen-stop')"), b.js(f"({VISIBLE})('#wl-listen-btn')"),
                       b.js("document.querySelector('#wl-connect').disabled")), (True, False, True))
                check("B-9   the server is listening", (s.window() or {}).get("hz"), 145_090_000)
                check("B-9 no JS errors (the first page)", b.js("window.__errs"), [])
                # Closing the page does not end a window (unattended VHF data is lawful, 97.221(b))
                b.goto("about:blank")
                time.sleep(2.0)        # negative check: a page gone is not an end
                check("B-9 the page closed: still listening", ((s.window() or {}).get("hz"), s.radio()),
                      (145_090_000, (2, 0)))
                b.goto(f"http://127.0.0.1:{s.web}/?v=listen2#/winlink")
                b.js("window.__errs2 = []; window.addEventListener('error', (e) => window.__errs2.push(e.message))")
                check("B-9 a page opened later shows the window",
                      bool(b.wait(f"({TEXT})('#wl-state').startsWith('Listening on 145.090 MHz')", 15)), True)
                b.js("document.querySelector('#wl-listen-stop').click()")
                check("B-9 Stop listening: Idle, Listen back", (
                    bool(b.wait(f"({TEXT})('#wl-state') === 'Idle'", 40)), b.wait(f"({VISIBLE})('#wl-listen-btn')", 5)),
                    (True, True))
                check("B-9   the radio is back", s.radio(), (0, 1))
                check("B-9 no JS errors (the second page)", b.js("window.__errs2"), [])
            finally:
                s.stop()
                s.close()
        finally:
            b.close()


# ------------------------------------------------------------ E-2, E-3: real Pat (B-6)

class Peer:
    """A station on the air that calls N9ABC (ax25_link in its own loop
    thread): it acknowledges what it hears at the link layer and never
    sends a byte of its own ("the peer goes silent"). ignore_disc: it stops
    answering a DISC, so the hub retries until it gives up."""

    def __init__(self, radio, call="N0XYZ", to="N9ABC"):
        import asyncio
        import threading
        self.radio, self.me, self.to = radio, call, to
        self.link, self.events, self.rx = None, [], bytearray()
        self.ignore_disc = False
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        radio.on_air = lambda raw: self.loop.call_soon_threadsafe(self._heard, raw)

    def call(self):
        self.loop.call_soon_threadsafe(self._call)

    def _call(self):
        from ax25_link import Link
        self.link = Link(self.me, self.to, self._send, self, frack=1.0, n2=10, baud=96000, ack_delay=0.05)
        self.link.connect()

    def _send(self, f):
        self.radio.inject(f.encode(), lossy=False)

    def _heard(self, raw):
        from ax25 import U_DISC, Frame
        try:
            f = Frame.decode(raw)
        except Exception:              # noqa: BLE001
            return
        if str(f.dest) != self.me:
            return
        if self.ignore_disc and f.kind == "U" and f.utype == U_DISC:
            self.events.append("DISC ignored")
            return
        if self.link is not None:
            self.link.handle(f)

    def on_connected(self, link, inbound):
        self.events.append("connected")

    def on_data(self, link, data):
        self.rx += data

    def on_disconnected(self, link, reason):
        self.events.append(f"disconnected: {reason}")

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


def frames_to(radio, call, since=0):
    from ax25 import Frame
    out = []
    for t, raw in radio.transmitted()[since:]:
        try:
            f = Frame.decode(raw)
        except Exception:              # noqa: BLE001
            continue
        if str(f.dest) == call:
            out.append((t, f.name()))
    return out


if picked("E-2") or picked("E-3"):
    if not PAT:
        print("skip  E-2/E-3 real Pat listening through the hub (no pat on PATH; set PAT=)")
    else:
        import threading
        home = tempfile.mkdtemp(prefix="wlpat-")
        hub = Hub()
        cfg = testkit.pat_config(os.path.join(home, "config.json"),
                                 agwpe={"addr": f"127.0.0.1:{hub.proc.agw}", "radio_port": 0})
        patlog = os.path.join(home, "pat.log")
        # packet_status off: the UV-PRO add-on can't see the hub's links, so
        # only the engine's own wait for the link to clear keeps the radio put (B-6).
        s = Stack(hub=hub, pat_home=home, packet_status=False,
                  pat_cmd=f"{PAT} --config {cfg} --mbox {os.path.join(home, 'mbox')} --log {patlog}")
        peer = None
        try:
            # E-2: the window opens on a real Pat
            s.listen_on(0.25)
            ok = s.listening(40)
            check("E-2 real Pat: listening (read back by the server)", bool(ok), True)
            check("E-2   Pat's own status: active_listeners [ax25+agwpe]",
                  (s.snap().get("status") or {}).get("active_listeners"), ["ax25+agwpe"])
            check("E-2   the hub lists N9ABC registered", s.hub.registered(), ["N9ABC"])
            if picked("E-3"):
                # E-3: N0XYZ calls in; the hub answers, Pat accepts
                peer = Peer(hub.radio)
                n_tx = len(hub.radio.transmitted())
                peer.call()
                check("E-3 the hub answers N0XYZ's SABM with UA",
                      bool(wait_for(lambda: "UA" in [n for _, n in frames_to(hub.radio, "N0XYZ", n_tx)], 10)), True)
                check("E-3 Pat accepts: 'Got connect (ax25+agwpe:N0XYZ)' in its log",
                      bool(wait_for(lambda: "Got connect (ax25+agwpe:N0XYZ)" in open(patlog, errors="replace").read(), 15)),
                      True)
                check("E-3 the page is told Pat is answering N0XYZ",
                      bool(wait_for(lambda: (s.snap().get("inbound") or {}).get("call") == "N0XYZ", 10)), True)
                links = lambda: [l for l in hub.status()["links"] if l["local"] == "N9ABC"]   # noqa: E731
                check("E-3 the hub holds the link", [l["remote"] for l in links()], ["N0XYZ"])
                # Pat's dirty disconnect alone does nothing on AX.25 (F9)
                import urllib.request
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{s.patp}/api/disconnect?dirty=true", timeout=5).read()
                except OSError:
                    pass
                time.sleep(3.0)        # negative check: longer than the hub's DISC retries (0.4 s x 4)
                check("E-3 Pat's dirty disconnect alone: the hub link is still up", [l["remote"] for l in links()],
                      ["N0XYZ"])
                # B-6: end the window with the exchange up; the peer now ignores DISC
                peer.ignore_disc = True
                seen = []
                stop_poll = threading.Event()

                def poll():
                    while not stop_poll.is_set():
                        try:
                            seen.append((time.time(), bool(links())))
                        except Exception:  # noqa: BLE001
                            pass
                        time.sleep(0.05)
                threading.Thread(target=poll, daemon=True).start()
                b1, p1, _ = s.mark()
                off = time.time() - time.monotonic()      # the fake radio stamps with monotonic
                s.page.send({"t": "winlink_listen_stop"})
                check("E-3 Stop listening with the exchange up: back to idle", bool(s.idle(60)), True)
                stop_poll.set()
                discs = [t + off for t, n in frames_to(hub.radio, "N0XYZ", n_tx) if n == "DISC"]
                gone = next((t for t, up in seen if not up and all(not u for _, u in seen if _ >= t)), None)
                back = [c["t"] for c in jsonl(s.blog)[b1:] if c.get("cmd") == "settings"
                        and (c.get("values") or {}).get("channel_a") == 0]
                check("E-3 the hub sent N0XYZ a DISC (Pat's graceful close), the peer ignored it",
                      (bool(discs), "DISC ignored" in peer.events), (True, True))
                check("B-6 order: DISC on air -> hub link gone -> radio put back",
                      bool(discs and gone and back and discs[0] < gone < back[0]), True) or print(
                          "      ", discs[:1], gone, back[:1], b1, jsonl(s.blog)[-3:])
                check("E-3   the hub window closed", s.hub.inbound(), None)
                check("E-3   the radio back on channel 1", s.radio(), (0, 1))
            # E-2: after the window, both clear
            if not picked("E-3"):
                s.page.send({"t": "winlink_listen_stop"})
                s.idle(40)
            check("E-2 after: Pat lists no listener", wait_for(
                lambda: (s.snap().get("status") or {}).get("active_listeners") == [], 15), True)
            check("E-2 after: N9ABC no longer registered on the hub", wait_for(lambda: s.hub.registered() == [], 15), True)
        finally:
            if peer:
                peer.stop()
            out = s.stop()
            s.close()
            hub_out = hub.proc.output()
            hub.stop()
        for line in out.splitlines():
            if "listen window ending" in line or "the session with" in line:
                print("      server:", line.split(" winlink ", 1)[-1])
        started = [("listening on ax25+agwpe" in l) for l in out.splitlines() if "Pat started (pid" in l]
        check("E-2 Pat's launches as the server logged them: plain, listening, plain", started, [False, True, False])
        if fails:
            print("      hub log tail:\n" + "\n".join("      h| " + l for l in hub_out.splitlines()[-25:]))
            try:
                print("      pat.log tail:\n" + open(patlog, errors="replace").read()[-2500:])
            except OSError:
                pass
            print("\n".join("   | " + l for l in out.splitlines() if "winlink" in l or "UV-PRO" in l)[-3000:])


print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
