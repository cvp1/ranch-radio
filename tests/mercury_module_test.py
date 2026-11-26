"""The `mercury` module (Winlink over Mercury HF, plan winlink-mercury M1).
Nothing here runs Mercury, opens a sound card, keys a radio, runs lsof or
launchctl for real, or reads this machine's Pat config: every probe is faked.

  - golden: every example host renders exactly as the fixture says (no
    example has Mercury: hosts without the block are unchanged)
  - add from the page: a complete plan; with vara_modem unset (xara) nothing
    Mercury renders; remove gives the host file back
  - vara_modem mercury: the agent's exact argv (no -G, -U or -Q), its
    MERCURY_BIND_ADDR=127.0.0.1, the pinned INI (max_keydown_s 16, no UI,
    hamlib PTT on 4535), the server's --winlink-vara-modem/--winlink-mercury-*
    flags, station.json's vara_port 8340 and vara_backend; with xara too: no
    xara agent while Mercury is selected (its lane flags stay), and back
  - render refuses: every port overlap (xara and its data port, ardopcf and
    +1, both lanes, the station, Pat, the packet hub, JS8, its own derived
    and broadcast ports), -p 65535 (Mercury would move to 8300 silently),
    a bad vara_modem, mercury without its block, Linux, an INI outside ~/,
    a VARA listen channel with Mercury selected
  - listener check (mercury_tnc): loopback on exactly its ports passes;
    a wildcard, 0.0.0.0, [::], an unexpected port (a -U websocket), another
    process on one of its ports, a missing listener, nothing at all: refused
  - arm-time (server.Station, in process): a wildcard listener -> not armed,
    a warn; the probe raising -> not armed; good listeners -> armed; good
    listeners but no ID path (no audio) -> not armed; the test-listeners
    flag is refused without --sim
  - vara_refusal per backend: Mercury's port and name, xara's as before
  - doctor (faked probes): all good -> ok; no patch, wrong version, a
    wildcard listener -> missing; agent not loaded, xara still loaded, no
    MERCURY in service_codes -> warn; Mercury loaded with xara selected -> warn
  - vara-backend (fake Ops): the plan printed, nothing touched without
    --apply; to mercury and back to xara, each with the old plist moved to
    ~/.Trash dated (never removed) and the host file switched; a busy radio
    -> refused, nothing touched; a leftover agent that won't go -> stopped,
    the new agent not loaded, the host file unchanged; a stale Pat socket
    that survives the station restart -> a warning and exit 1; one the
    restart clears -> done

    python3 tests/mercury_module_test.py
"""
import argparse
import asyncio
import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="rr-xdg-")
os.environ["RANCH_RADIO_HOSTS"] = ""
os.environ.pop("CC_OBS_TOKENS_FILE", None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from ranch_radio import doctor, manifest, modules, render, vara_backend  # noqa: E402
import mercury_tnc  # noqa: E402
import pat_config  # noqa: E402
import testkit  # noqa: E402

HOME = "/Users/you"
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


def rendered(h):
    return [{"path": a.path, "fmt": a.fmt, "text": a.text()} for a in render.artifacts(h, home=HOME)]


def host(name):
    return json.loads((ROOT / "examples/hosts" / f"{name}.json").read_text())


# ---------------------------------------------------------------- golden
golden = json.loads((ROOT / "tests/fixtures/render-golden.json").read_text())
for f in sorted((ROOT / "examples/hosts").glob("[!._]*.json")):
    h = render.load(f)
    check(f"golden: {h['host']} renders as the fixture says", rendered(h), golden[h["host"]])

# ---------------------------------------------------------------- add, xara selected: nothing Mercury
MERC = {"audio_in": "BuiltInCodecUID", "audio_out": "BuiltInCodecUID", "call": "N0CALL"}
text = (ROOT / "examples/hosts/mac-shack.json").read_text()
plan = modules.plan_add(text, "mercury", MERC, source="page")
check("add mercury from the page: a complete plan", (plan.refusal, plan.errors, plan.asks), ("", {}, []))
on = json.loads(plan.new_text)
check("added: the template's ports", {k: on["winlink"]["mercury"][k] for k in ("port", "broadcast_port", "lane_port")},
      {"port": 8340, "broadcast_port": 8342, "lane_port": 4535})
check("added, vara_modem unset (xara): no Mercury file renders",
      [a.path for a in render.artifacts(on, home=HOME) if "mercury" in a.path], [])
check("added, xara selected: the server has no Mercury flag",
      any("mercury" in x for a in render.artifacts(on, home=HOME) if a.fmt == "plist"
          for x in a.content.get("ProgramArguments", [])), False)
rm = modules.plan_remove(plan.new_text, "mercury")
check("remove mercury: the host file as before", (rm.refusal, rm.new_text), ("", text))
check("free ports skip Mercury's", {8340, 8341, 8342, 4535} & set(modules.free_ports(on, 6, start=4535)), set())

# ---------------------------------------------------------------- vara_modem mercury
mon = copy.deepcopy(on)
mon["winlink"]["vara_modem"] = "mercury"
arts = {a.path: a for a in render.artifacts(mon, home=HOME)}
agent = arts.get("Library/LaunchAgents/ranch-radio.mercury.plist")
check("mercury on: an agent", agent is not None, True)
c = agent.content if agent else {}
argv = c.get("ProgramArguments", [])
check("mercury on: its exact argv", argv, [
    f"{HOME}/.local/bin/mercury", "-C", f"{HOME}/Library/Application Support/ranch-radio/mercury/mercury.ini",
    "-x", "coreaudio", "-i", "BuiltInCodecUID", "-o", "BuiltInCodecUID", "-p", "8340", "-b", "8342",
    "-P", "hamlib", "-R", "2", "-A", "127.0.0.1:4535", "-L", f"{HOME}/Library/Logs/ranch-radio-mercury.log"])
check("mercury on: no -G, -U, -Q, -T anywhere in its argv", [a for a in argv if a in ("-G", "-U", "-Q", "-T")], [])
check("mercury on: MERCURY_BIND_ADDR=127.0.0.1", c.get("EnvironmentVariables"), {"MERCURY_BIND_ADDR": "127.0.0.1"})
check("mercury on: KeepAlive, RunAtLoad, 10 s throttle",
      (c.get("KeepAlive"), c.get("RunAtLoad"), c.get("ThrottleInterval")), (True, True, 10))
ini = arts.get("Library/Application Support/ranch-radio/mercury/mercury.ini")
lines = set((ini.content if ini else "").splitlines())
check("mercury on: the pinned INI", {"ui_enabled = false", "max_keydown_s = 16", "method = hamlib",
                                     "hamlib_model = 2", "device = 127.0.0.1:4535", "arq_tcp_base_port = 8340",
                                     "broadcast_tcp_port = 8342", "callsign = N0CALL"} <= lines, True)
server = next(a for a in arts.values() if a.path.endswith("ic7300-remote.plist")).content["ProgramArguments"]
i = server.index("--winlink-vara-modem")
check("mercury on: the server's flags", server[i:i + 10], [
    "--winlink-vara-modem", "mercury", "--winlink-mercury-rigctl-port", "4535", "--winlink-mercury-call", "N0CALL",
    "--winlink-mercury-port", "8340", "--winlink-mercury-broadcast-port", "8342"])
sj = arts[render.STATION_JSON].content["radios"][0]
check("mercury on: station.json names Mercury's port and the backend, no xara log",
      (sj["modems"], sj.get("vara_port"), sj.get("vara_backend"), "vara_log" in sj),
      (["ardop", "varahf"], 8340, "mercury", False))

both = copy.deepcopy(mon)
both["winlink"]["xara"] = manifest.template("vara", "macos", ["winlink"])
both["winlink"]["xara"].update(audio_in="USB Audio CODEC", audio_out="USB Audio CODEC", call="N0CALL")
paths = [a.path for a in render.artifacts(both, home=HOME)]
check("xara and Mercury, Mercury selected: no xara agent", any("xara" in p for p in paths), False)
srv = next(a for a in render.artifacts(both, home=HOME) if a.path.endswith("ic7300-remote.plist")).content
check("  xara's lane flags stay (its lane refuses a Mercury session)",
      "--winlink-vara-rigctl-port" in srv["ProgramArguments"], True)
back = copy.deepcopy(both)
back["winlink"]["vara_modem"] = "xara"
pb = [a.path for a in render.artifacts(back, home=HOME)]
check("xara selected again: the xara agent, no Mercury file",
      (any("xara" in p for p in pb), any("mercury" in p for p in pb)), (True, False))
sjx = next(a for a in render.artifacts(back, home=HOME) if a.path == render.STATION_JSON).content["radios"][0]
check("  station.json: xara's port, backend xara (the page says VARA HF (xara))",
      (sjx.get("vara_port"), sjx.get("vara_backend")), (8300, "xara"))

# ---------------------------------------------------------------- render refuses


def refused(name, base, mutate):
    h = copy.deepcopy(base)
    mutate(h)
    try:
        render.validate(h)
        check(f"refused: {name}", "accepted", "refused")
    except render.HostError as e:
        check(f"refused: {name}", "refused", "refused")
        return str(e)


def m(k, v):
    return lambda h: h["winlink"]["mercury"].__setitem__(k, v)


refused("port on xara's command port", both, m("port", 8300))
refused("data port (+1) on xara's command port", both, m("port", 8299))
refused("broadcast on xara's data port", both, m("broadcast_port", 8301))
refused("lane on xara's lane", both, m("lane_port", 4534))
refused("port on ardopcf's", mon, m("port", 8515))
refused("broadcast on ardopcf's +1", mon, m("broadcast_port", 8516))
refused("lane on the radio's ARDOP lane", mon, m("lane_port", 4533))
refused("broadcast on the radio's rigctl port", mon, m("broadcast_port", 4532))
refused("port on the station's 7300", mon, m("port", 7300))
refused("port on the radio's page (7301)", mon, m("port", 7301))
refused("data port (+1) on the station's 7300", mon, m("port", 7299))
refused("broadcast on Pat's HTTP (8095)", mon, m("broadcast_port", 8095))
refused("broadcast equal to its own data port", mon, m("broadcast_port", 8341))
refused("lane equal to its broadcast", mon, m("lane_port", 8342))
refused("port 65535 (Mercury would listen on 8300, silently)", mon, m("port", 65535))
refused("broadcast 0", mon, m("broadcast_port", 0))
refused("an unknown key (ui_port)", mon, m("ui_port", 10000))
refused("vara_modem not a backend", mon, lambda h: h["winlink"].__setitem__("vara_modem", "vara"))
refused("vara_modem mercury without the block", host("mac-shack"),
        lambda h: h["winlink"].__setitem__("vara_modem", "mercury"))
refused("mercury on Linux", mon, lambda h: h.update(os="linux"))
refused("INI outside ~/", mon, m("ini", "/etc/mercury/mercury.ini"))
refused("call with a shell character", mon, m("call", "N0CALL;id"))
aioc = host("mac-aioc")
if "winlink" in aioc and any(r.get("lane") for r in aioc["radios"]):
    aioc["winlink"]["mercury"] = copy.deepcopy(on["winlink"]["mercury"])
    refused("port on the packet hub's KISS port (8001)", aioc, m("port", 8001))
    refused("broadcast on the packet hub's HTTP (7310)", aioc, m("broadcast_port", 7310))
else:
    pk = copy.deepcopy(mon)
    pk["packet"] = {"tnc": "tcp:127.0.0.1:8099", "mycall": "N0CALL", "url": "http://127.0.0.1:7310",
                    "agwpe": "127.0.0.1:8000"}
    refused("port on the packet hub's KISS port (8001)", pk, m("port", 8001))
    refused("broadcast on the packet hub's HTTP (7310)", pk, m("broadcast_port", 7310))
js = copy.deepcopy(mon)
js["radios"][0]["js8_port"] = 8342
refused("broadcast on a radio's js8_port", js, lambda h: None)
lh = copy.deepcopy(mon)
lh["winlink"]["listen_hf"] = {"channels": [{"khz": 14108, "modem": "varahf"}]}
why = refused("a VARA listen channel with Mercury selected", lh, lambda h: None)
check("  refused for being Mercury (not a malformed plan)", "is Mercury on this host" in (why or ""), True)
ok = copy.deepcopy(mon)
ok["winlink"]["mercury"].update(port=8350, broadcast_port=8352, lane_port=4536)
try:
    render.validate(ok)
    check("accepted: other free ports", True, True)
except render.HostError as e:
    check("accepted: other free ports", str(e), True)

# ---------------------------------------------------------------- the listener check
EXP = mercury_tnc.ports(8340, 8342)


def lsof(*rows):
    out = []
    for i, (cmd, addrs) in enumerate(rows):
        out += [f"p{100 + i}", f"c{cmd}"] + [f"f{3 + j}\nn{a}" for j, a in enumerate(addrs)]
    return "\n".join(out) + "\n"


GOOD = lsof(("mercury", ["127.0.0.1:8340", "127.0.0.1:8341", "127.0.0.1:8342"]),
            ("Python", ["127.0.0.1:7301", "*:4532"]))


def lr(text):
    return mercury_tnc.listener_refusal(mercury_tnc.parse_lsof(text), EXP)


check("listeners: loopback, exactly its ports -> ok", lr(GOOD), None)
for name, text, part in [
        ("a wildcard control port", lsof(("mercury", ["*:8340", "127.0.0.1:8341", "127.0.0.1:8342"])),
         "every interface"),
        ("0.0.0.0 on the data port", lsof(("mercury", ["127.0.0.1:8340", "0.0.0.0:8341", "127.0.0.1:8342"])),
         "every interface"),
        ("[::] on broadcast", lsof(("mercury", ["127.0.0.1:8340", "127.0.0.1:8341", "[::]:8342"])), "every interface"),
        ("a LAN address", lsof(("mercury", ["192.168.1.20:8340", "127.0.0.1:8341", "127.0.0.1:8342"])),
         "192.168.1.20"),
        ("an unexpected port (a -U websocket)",
         lsof(("mercury", ["127.0.0.1:8340", "127.0.0.1:8341", "127.0.0.1:8342", "127.0.0.1:10000"])), "10000"),
        ("Mercury on the stock ports (8300)", lsof(("mercury", ["127.0.0.1:8300", "127.0.0.1:8301",
                                                               "127.0.0.1:8100"])), "8300"),
        ("another process on its data port",
         lsof(("mercury", ["127.0.0.1:8340", "127.0.0.1:8342"]), ("xara", ["127.0.0.1:8341"])), "held by xara"),
        ("no broadcast listener", lsof(("mercury", ["127.0.0.1:8340", "127.0.0.1:8341"])), "broadcast 8342"),
        ("nothing listening", "", "not listening")]:
    w = lr(text)
    check(f"listeners: {name} -> refused, saying {part!r}", (w is not None, part in (w or "")), (True, True))

# ---------------------------------------------------------------- arm-time, in process
import rigs  # noqa: E402
import server  # noqa: E402
from sim import SimRadio  # noqa: E402


class Args(argparse.Namespace):
    def __getattr__(self, k):
        return None


class Audio:
    async def play(self, pcm):
        return True


class WS:
    closed = False

    def __init__(self):
        self.sent = []

    async def send_str(self, msg):
        self.sent.append(json.loads(msg))

    async def send_bytes(self, b):
        pass


def make(audio=True, probe=None):
    a = Args(no_tx=False, sim=True, tx_timeout=60, tune_timeout=10, winlink_rigctl_port=1,
             winlink_vara_modem="mercury", winlink_mercury_rigctl_port=2, winlink_mercury_call="N0CALL",
             winlink_mercury_port=8340, winlink_mercury_broadcast_port=8342)
    st = server.Station(SimRadio(rigs.get("ic7300")), Audio() if audio else None, a)
    if not audio:
        st.args.sim = False                 # the ID path's "no audio" refusal is for a real radio
    calls = []

    async def fake():
        calls.append(1)
        if isinstance(probe, Exception):
            raise probe
        return probe
    st.mercury_listen_probe = fake
    return st, calls


async def arm_case(probe, audio=True):
    st, calls = make(audio, probe)
    await st.radio.start()
    ws = WS()
    try:
        await st.winlink_arm(ws, True, scheme="varahf")
        await asyncio.sleep(0.05)           # notices go out as tasks
        armed = st.winlink_armed and st.winlink_lane_modem == "mercury"
        warn = [m["msg"] for m in ws.sent if m.get("t") == "notice"]
        return armed, warn, len(calls)
    finally:
        await st.winlink_disarm("test over")
        await st.radio.stop()


async def arms():
    bad = lsof(("mercury", ["*:8340", "*:8341", "*:8342"]))
    armed, warn, n = await arm_case(bad)
    check("arm: a wildcard Mercury listener -> not armed", armed, False)
    check("arm:   the page is told why", any("every interface" in w for w in warn), True)
    check("arm:   the listeners were read once", n, 1)
    armed, warn, _ = await arm_case(OSError("lsof: not found"))
    check("arm: the listeners can't be read -> not armed", (armed, any("could not be read" in w for w in warn)),
          (False, True))
    armed, warn, _ = await arm_case(GOOD)
    check("arm: loopback-only listeners -> armed (Mercury's lane)", armed, True)
    armed, warn, _ = await arm_case(GOOD, audio=False)
    check("arm: good listeners but no ID path (no audio) -> not armed",
          (armed, any("CW ID" in w for w in warn)), (False, True))

asyncio.run(arms())
r = subprocess.run([sys.executable, "server.py", "--winlink-mercury-test-listeners", "/dev/null",
                    "--listen", "0"], cwd=ROOT, capture_output=True, text=True, timeout=60)
check("server: --winlink-mercury-test-listeners without --sim is refused",
      (r.returncode, "needs --sim" in r.stderr), (2, True))

# ---------------------------------------------------------------- vara_refusal per backend
tmp = Path(tempfile.mkdtemp(prefix="rr-mercury-"))
PW = "s3cret-fake-pw"


def patcfg(addr="localhost:8340", codes=None):
    over = {"varahf": {"addr": addr, "rig": "ic7300", "ptt_ctrl": False},
            "hamlib_rigs": {"ic7300": {"address": "localhost:4533"}}, "secure_login_password": PW}
    if codes is not None:
        over["service_codes"] = codes
    return testkit.pat_config(str(tmp / "pat.json"), **over)


p = patcfg("localhost:8300")
w = pat_config.vara_refusal(p, 8340, 4533, backend="mercury")
check("vara_refusal, Mercury: varahf.addr on xara's port -> names 8340 and Mercury HF",
      ("localhost:8340" in (w or ""), "Mercury HF" in (w or "")), (True, True))
check("vara_refusal, xara: as before", "xara on this machine" in (pat_config.vara_refusal(p, 8301, 4533) or ""), True)
check("vara_refusal, Mercury: its port -> none", pat_config.vara_refusal(patcfg(), 8340, 4533, backend="mercury"), None)

# ---------------------------------------------------------------- doctor, faked probes
dh = copy.deepcopy(mon)
dh["winlink"]["pat_config"] = patcfg(codes=["PUBLIC", "MERCURY"])


def probe(**over):
    d = {"version": "Rhizomatica Mercury Version 1.9.25 (git 33efac4e)", "patched": True,
         "loaded": {"ranch-radio.mercury": True, "ranch-radio.xara": False}, "lsof": GOOD}
    d.update(over)
    return lambda h: d


def statuses(h, **over):
    return [(c.status, c.what) for c in doctor.mercury_checks(h, probe=probe(**over))]


good = statuses(dh)
check("doctor: all good -> every check ok", {s for s, _ in good}, {doctor.OK})
check("doctor: six checks (version, patch, agent, listeners, Pat, service codes)", len(good), 6)
for name, over, want, part in [
        ("no bind patch", {"patched": False}, doctor.MISSING, "no bind patch"),
        ("another version", {"version": "Rhizomatica Mercury Version 1.9.24"}, doctor.MISSING, "1.9.24"),
        ("a wildcard listener", {"lsof": lsof(("mercury", ["*:8340", "*:8341", "*:8342"]))}, doctor.MISSING,
         "every interface"),
        ("agent not loaded", {"loaded": {"ranch-radio.mercury": False}}, doctor.WARN, "not loaded"),
        ("xara still loaded", {"loaded": {"ranch-radio.mercury": True, "ranch-radio.xara": True}}, doctor.WARN,
         "xara")]:
    got = [s for s, what in statuses(dh, **over) if part in what]
    check(f"doctor: {name} -> {want}", got[:1], [want])
nc = copy.deepcopy(dh)
nc["winlink"]["pat_config"] = patcfg(codes=None)
check("doctor: no MERCURY in service_codes (PUBLIC by default) -> warn",
      [s for s, what in statuses(nc) if "service_codes" in what or "Mercury gateways" in what], [doctor.WARN])
wa = copy.deepcopy(dh)
wa["winlink"]["pat_config"] = patcfg(addr="localhost:8300", codes=["PUBLIC", "MERCURY"])
check("doctor: Pat's varahf.addr on xara's port with Mercury selected -> warn",
      [s for s, what in statuses(wa) if "varahf.addr" in what], [doctor.WARN])
check("doctor: the Pat password never printed",
      PW in json.dumps([c.what + c.fix for c in doctor.mercury_checks(wa, probe=probe())]), False)
xs = copy.deepcopy(dh)
xs["winlink"]["vara_modem"] = "xara"
check("doctor: xara selected, Mercury's agent loaded -> warn",
      [s for s, what in statuses(xs) if "loaded" in what], [doctor.WARN])

# ---------------------------------------------------------------- vara-backend cutover, fake Ops


class FakeOps:
    """launchd, processes and sockets as a dict. stuck: labels bootout can't
    stop; stale: a connection to the old modem's port the station restart
    may or may not clear."""

    def __init__(self, loaded, procs, listening, busy=False, stuck=(), stale=False, stale_clears=True):
        self.loaded_set, self.procs, self.listen = set(loaded), set(procs), set(listening)
        self.busy, self.stuck = busy, set(stuck)
        self.stale, self.stale_clears = stale, stale_clears
        self.calls = []

    def idle(self, h, r):
        self.calls.append(("idle", r["id"]))
        return (False, "the IC-7300 is in a Winlink session") if self.busy else (True, "idle")

    def loaded(self, label):
        return label in self.loaded_set

    def bootout(self, label, out):
        self.calls.append(("bootout", label))
        if label not in self.stuck:
            self.loaded_set.discard(label)
            name = "mercury" if "mercury" in label else "xara"
            self.procs.discard(name)
            self.listen -= set(PORTS[name])

    def up(self, label, path, out):
        self.calls.append(("up", label))
        self.loaded_set.add(label)
        return 0

    def restart(self, label, path, out):
        self.calls.append(("restart", label))
        if label.endswith("station") and self.stale_clears:
            self.stale = False
        return 0

    def proc_alive(self, name):
        return name in self.procs

    def listening(self, port):
        return port in self.listen

    def connected(self, port):
        return self.stale

    def wait(self, pred, timeout):
        return pred()


PORTS = {"xara": [8300, 8301], "mercury": [8340, 8341, 8342]}


def cutover(h, to, ops, plists=()):
    home = Path(tempfile.mkdtemp(prefix="rr-home-"))
    agents = home / render.AGENTS
    agents.mkdir(parents=True)
    for label in plists:
        (agents / f"{label}.plist").write_text("<plist/>")
    hosts = home / "hosts"
    hosts.mkdir()
    path = hosts / f"{h['host']}.json"
    path.write_text(json.dumps(h, indent=2))
    installed, said = [], []

    def install(hh, hm):
        installed.append(render.vara_modem(hh))
        return []
    rc = vara_backend.apply(h, path, to, ops=ops, home=home, log=said.append, install=install)
    return rc, home, json.loads(path.read_text()), installed, "\n".join(said)


said = []
ops = FakeOps({"ranch-radio.xara"}, {"xara"}, {8300, 8301})
check("vara-backend dry run: exit 0", vara_backend.main(both, Path("/nonexistent"), "mercury", False,
                                                           log=said.append, ops=ops), 0)
check("vara-backend dry run: prints the plan, touches nothing",
      (any("bootout ranch-radio.xara" in s for s in said), any("varahf.addr = localhost:8340" in s for s in said),
       ops.calls), (True, True, []))

xh = copy.deepcopy(both)
xh["winlink"]["vara_modem"] = "xara"
ops = FakeOps({"ranch-radio.xara"}, {"xara"}, {8300, 8301}, stale=True)
rc, home, newh, inst, out = cutover(xh, "mercury", ops, plists=["ranch-radio.xara"])
check("to mercury: done", rc, 0)
check("to mercury: idle checked, xara out, Mercury up, the radio server then the station restarted",
      ops.calls, [("idle", "ic7300"), ("bootout", "ranch-radio.xara"), ("up", "ranch-radio.mercury"),
                  ("restart", xh["radios"][0]["label"]), ("restart", "org.ranch-radio.station")])
trash = sorted(p.name for p in (home / ".Trash").iterdir())
check("to mercury: xara's plist moved to ~/.Trash, dated (not removed)",
      (len(trash), trash[0].startswith("ranch-radio.xara-2") if trash else False,
       (home / render.AGENTS / "ranch-radio.xara.plist").exists()), (1, True, False))
check("to mercury: the host file says mercury; install ran with it", (newh["winlink"]["vara_modem"], inst),
      ("mercury", ["mercury"]))
check("to mercury: Pat's two hand edits printed",
      ('varahf.addr = "localhost:8340"' in out, '"MERCURY"' in out), (True, True))
check("to mercury: the stale Pat socket to 8300 cleared by the station restart", "no connection to xara" in out, True)

ops = FakeOps({"ranch-radio.mercury"}, {"mercury"}, {8340, 8341, 8342})
rc, home, newh, inst, out = cutover(newh, "xara", ops, plists=["ranch-radio.mercury"])
check("back to xara: done, Mercury out and its plist in the Trash, xara up",
      (rc, ("bootout", "ranch-radio.mercury") in ops.calls, ("up", "ranch-radio.xara") in ops.calls,
       [p.name.startswith("ranch-radio.mercury-") for p in (home / ".Trash").iterdir()], newh["winlink"]["vara_modem"]),
      (0, True, True, [True], "xara"))

ops = FakeOps({"ranch-radio.xara"}, {"xara"}, {8300, 8301}, busy=True)
rc, home, newh, inst, out = cutover(xh, "mercury", ops, plists=["ranch-radio.xara"])
check("busy radio: refused, nothing booted out, host file and plists untouched",
      (rc, ops.calls, newh.get("winlink", {}).get("vara_modem"), (home / render.AGENTS / "ranch-radio.xara.plist").exists(),
       inst), (1, [("idle", "ic7300")], "xara", True, []))

ops = FakeOps({"ranch-radio.xara"}, {"xara"}, {8300, 8301}, stuck={"ranch-radio.xara"})
rc, home, newh, inst, out = cutover(xh, "mercury", ops)
check("leftover agent that won't go (no plist): stopped, Mercury NOT loaded, host file unchanged",
      (rc, any(c[0] == "up" for c in ops.calls), newh["winlink"]["vara_modem"], inst, "NOT loaded" in out),
      (1, False, "xara", [], True))
ops = FakeOps(set(), {"xara"}, {8301})
rc, home, newh, inst, out = cutover(xh, "mercury", ops)
check("a xara process still listening with no agent: stopped, says what is left",
      (rc, "something still listens on 8301" in out, any(c[0] == "up" for c in ops.calls)), (1, True, False))

ops = FakeOps({"ranch-radio.xara"}, {"xara"}, {8300, 8301}, stale=True, stale_clears=False)
rc, home, newh, inst, out = cutover(xh, "mercury", ops)
check("stale Pat socket that survives the station restart: a warning, exit 1",
      (rc, "WARNING" in out and "8300" in out), (1, True))
check("vara-backend on Linux: refused", vara_backend.plan(dict(xh, os="linux"), "mercury")[1] is not None, True)
check("vara-backend to mercury without its block: refused",
      "no winlink.mercury" in (vara_backend.plan(host("mac-shack"), "mercury")[1] or ""), True)

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
