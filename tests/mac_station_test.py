"""M1: the station in front of one radio, as render emits for a Mac: ardopcf is
its own agent (the host file's "ardopcf" dict), the shared rigctl port is off,
and the radio's lane has ARDOP and VARA HF.

  - the radio is listed, connected and Winlink-able with no sound card of its
    own (ardopcf is external), with both modems; the shared port is off
  - its page comes through /r/<id>/
  - an ARDOP session arms the radio's lane over the remote lane, keys through
    it, ends and disarms; the station started no ardopcf of its own
  - a VARA HF session arms the radio's lane as VARA (the radio's server says
    so), xara keys through its own lane while it is armed, Pat never keys for
    it, and the radio disarms when it ends
  - ardopcf gone: an ARDOP session is refused, naming its port; nothing armed
  - lane_for: VARA on a radio whose lane has no VARA, or with Pat's varahf
    settings wrong, is refused naming what to fix; the external Ardop never
    spawns and says so when its port is closed

    .venv/bin/python tests/mac_station_test.py
"""
import asyncio
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import types

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
import testkit  # noqa: E402
from winlink_test_lib import WebPage, ended  # noqa: E402

RWEB, RRIG, LANE, VLANE, PAT, WEB = (testkit.free_port() for _ in range(6))
TAKEN = {RWEB, RRIG, LANE, VLANE, PAT, WEB}


ARDOP = testkit.pair_port(TAKEN)
TAKEN.update({ARDOP, ARDOP + 1})
VPORT = testkit.pair_port(TAKEN)
XARA_EVENTS = None
TMP = tempfile.mkdtemp(prefix="mac-station-test-")
ARDOP_STARTS = os.path.join(TMP, "ardop-starts.jsonl")
XARA_EVENTS = os.path.join(TMP, "xara-events.jsonl")
PAT_LOG = os.path.join(TMP, "pat.jsonl")
VLOG = os.path.join(TMP, "xara.log")
open(VLOG, "w").close()
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:64} got {got!r}" + ("" if ok else f" want {want!r}"))


def wait_port(port, timeout=60):
    import socket
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def radio_hello():
    async def q():
        async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{RWEB}/ws") as w:
            return json.loads((await w.receive()).data)
    return asyncio.run(q())


def armed_on_radio():
    return radio_hello()["winlink"]["armed"]


def ardop_starts():
    try:
        with open(ARDOP_STARTS) as f:
            return [json.loads(l)["argv"] for l in f if l.strip()]
    except OSError:
        return []


def radio_log_has(needle):
    with open(os.path.join(TMP, "radio.log")) as f:
        return needle in f.read()


# The radio: a simulated IC-7300 with its Winlink lane and xara's VARA lane,
# on loopback behind the station, as render's server_args put it on a Mac.
radio = subprocess.Popen(
    [sys.executable, "server.py", "--sim", "--rig", "ic7300", "--no-audio", "--host", "127.0.0.1",
     "--listen", str(RWEB), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(RRIG),
     "--winlink-rigctl-port", str(LANE), "--winlink-mode", "USB", "--winlink-codec-db", "-38",
     "--winlink-vara-log", VLOG, "--winlink-vara-rigctl-port", str(VLANE), "--winlink-vara-port", str(VPORT),
     "--addons-state", ""],
    cwd=ROOT, stdout=open(os.path.join(TMP, "radio.log"), "w"), stderr=subprocess.STDOUT)
# ardopcf as launchd runs it on a Mac: already there, on the radio's sound card.
ardop_env = dict(os.environ, FAKE_ARDOP_LOG=ARDOP_STARTS)
ardop = subprocess.Popen([sys.executable, "tests/fake_ardopcf.py", str(ARDOP), "USB Audio CODEC", "USB Audio CODEC",
                          "--logdir", os.path.join(TMP, "ardop")], cwd=ROOT, env=ardop_env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
assert wait_port(RWEB) and wait_port(LANE) and wait_port(VLANE) and wait_port(ARDOP), "radio or ardopcf did not start"
# xara as its launchd agent runs it on a Mac: keys through its own lane on the
# radio's server (started once that lane listens; xara exits on a refused connection).
xara = subprocess.Popen([sys.executable, "tests/fake_xara.py", "--call", "N0CALL", "--hf", "2300",
                         "--ptt", f"rigctld:127.0.0.1:{VLANE}", "--addr", "127.0.0.1", "--port", str(VPORT), "--log", VLOG],
                        cwd=ROOT, env=dict(os.environ, FAKE_XARA_EVENTS=XARA_EVENTS, FAKE_XARA_BURSTS="1",
                                           FAKE_XARA_BURST_S="0.6", FAKE_XARA_CW_S="0.6"),
                        stdout=open(os.path.join(TMP, "xara.err"), "w"), stderr=subprocess.STDOUT)
assert wait_port(VPORT), "xara did not start: " + open(os.path.join(TMP, "xara.err")).read()[-500:]
our_ardop_start = ardop_starts()          # the one start that is not the station's

pat_cmd = testkit.fake_pat_cmd(varahf={"addr": f"localhost:{VPORT}", "rig": "ic7300", "ptt_ctrl": False},
                               hamlib_rigs={"ic7300": {"address": f"localhost:{LANE}", "network": "tcp"}})
cfg = {
    "name": "mac-test",
    "radios": [{"id": "ic7300", "name": "IC-7300", "device": "/dev/null", "web": RWEB, "rigctl": RRIG,
                "lane": LANE, "modems": ["ardop", "varahf"], "vara_port": VPORT}],
    "winlink": {"pat": pat_cmd, "pat_port": PAT, "ardop_logs": os.path.join(TMP, "ardop"),
                "ardopcf": {"label": "org.ranch-radio.ardopcf", "bin": "~/bin/ardopcf", "port": ARDOP,
                            "audio_in": "USB Audio CODEC", "audio_out": "USB Audio CODEC",
                            "hostcommands": "CONSOLELOG 2"}},
    "addons_state": os.path.join(TMP, "addons.json"),
}
cfg_path = os.path.join(TMP, "station.json")
with open(cfg_path, "w") as f:
    json.dump(cfg, f)
env = dict(os.environ, FAKE_PAT_RIGCTL=str(LANE), FAKE_PAT_LOG=PAT_LOG, FAKE_PAT_TX_S="1")
front = subprocess.Popen([sys.executable, "station.py", "--config", cfg_path, "--listen", str(WEB), "--rigctl-port", "0"],
                         cwd=ROOT, env=env, stdout=open(os.path.join(TMP, "station.log"), "w"),
                         stderr=subprocess.STDOUT)
try:
    assert wait_port(WEB) and wait_port(PAT), "station did not start"
    time.sleep(2.5)                        # presence, Pat's event link
    page = WebPage(WEB)
    r = page.hello["radios"][0]
    check("the radio is listed, connected, Winlink-able with no sound card of its own",
          (r["id"], r["present"], r["winlink"]), ("ic7300", True, True))
    check("  with both modems", r["modems"], ["ardop", "varahf"])
    check("  the Winlink panel offers both", sorted(page.hello["winlink"]["modems"]), ["ardop", "varahf"])
    check("the shared rigctl port is off", page.hello["shared_port"], 0)
    st, body = page.http("GET", "/r/ic7300/")
    check("the radio's page comes through /r/<id>/", (st, "<html" in body.lower()), (200, True))

    check("between sessions the panel shows the radio's lane state (codec idle, ID tracked: xara's log)",
          (page.hello["winlink"].get("codec_db"), (page.hello["winlink"].get("id") or {}).get("tracked")), (0.0, True))

    # --- ARDOP over the remote lane, ardopcf external
    page.send({"t": "winlink_connect", "url": "ardop:///N0DAJ?bw=2000MAX&freq=3588.5"})
    check("ARDOP: the radio's lane arms", testkit.wait_for(armed_on_radio, 10), True)
    check("  the panel is told the codec is at the data level",
          page.wait(lambda e: e.get("t") == "winlink" and e.get("codec_db") == -38.0, 5) is not None, True)
    check("  the station started no ardopcf of its own", ardop_starts(), our_ardop_start)
    e = page.wait(ended, 30)
    check("  the session ends", e is not None and e.get("session"), "ended")
    check("  and the radio disarms", testkit.wait_for(lambda: not armed_on_radio(), 15), True)
    check("  the panel is told the codec is back to idle",
          page.wait(lambda e: e.get("t") == "winlink" and e.get("codec_db") == 0.0, 10) is not None, True)
    check("  a fresh page sees the idle codec and the ID ledger",
          (lambda h: (h["winlink"].get("codec_db"), (h["winlink"].get("id") or {}).get("tracked")))(WebPage(WEB).hello), (0.0, True))
    keyed = [json.loads(l) for l in open(PAT_LOG) if '"ptt_on"' in l]
    check("  Pat keyed through the radio's lane", [k.get("reply") for k in keyed], ["RPRT 0"])

    # --- VARA HF: the scheme rides the arm; the radio's server takes it
    page.events.clear()
    page.send({"t": "winlink_connect", "url": "varahf:///N0DAJ?freq=7101"})
    check("VARA: the radio's lane arms", testkit.wait_for(armed_on_radio, 10), True)
    check("  as a VARA session (the radio's log)", testkit.wait_for(lambda: radio_log_has("session armed (VARA HF)"), 5), True)
    e = page.wait(ended, 30)
    check("  the session ends", e is not None and e.get("session"), "ended")
    check("  and the radio disarms", testkit.wait_for(lambda: not armed_on_radio(), 15), True)
    xev = [json.loads(l) for l in open(XARA_EVENTS) if l.strip()] if os.path.exists(XARA_EVENTS) else []
    check("  xara keyed through its own lane while armed", any("key" in json.dumps(x) and "RPRT 0" in json.dumps(x) for x in xev), True)
    check("  Pat never keyed for VARA", [json.loads(l).get("reply") for l in open(PAT_LOG) if '"ptt_on"' in l], ["RPRT 0"])

    # --- ardopcf gone (its agent stopped): ARDOP refused, naming the port
    stop(ardop)
    time.sleep(0.5)
    page.events.clear()
    page.send({"t": "winlink_connect", "url": "ardop:///N0DAJ?freq=3588.5"})
    n = page.wait(lambda e: e.get("t") == "notice" and "ardopcf" in e.get("msg", ""), 10)
    check("ardopcf gone: refused, naming its port", n is not None and str(ARDOP) in n["msg"], True)
    check("  nothing armed", armed_on_radio(), False)
    page.close()
finally:
    stop(front)
    stop(radio)
    stop(ardop)
    stop(xara)

# --- lane_for and the external Ardop, without processes
import station as S  # noqa: E402
from ranch_radio import render  # noqa: E402

# Every example host's rendered station.json builds a Station (the first Mac host's radio
# had no "rigctl" and the station died on it at deploy, 2026-11-10).
for f in sorted((pathlib.Path(ROOT) / "examples/hosts").glob("[!._]*.json")):
    h = render.load(f)
    if not h.get("station"):
        continue
    sj = render.station_json(h)
    st = S.Station(sj, types.SimpleNamespace(token=None, rigctl_port=0))
    check(f"{h['host']}: the rendered station.json builds a Station",
          [(r.id, r.web, r.rigctl) for r in st.radios], [(r["id"], r["web"], r.get("rigctl", 4532)) for r in h["radios"]])

bad_cfg = testkit.pat_config(os.path.join(TMP, "pat-novara.json"))          # no varahf block


class Eng:
    def __init__(self, path):
        self.config_path = path


def make(modems, pat_path):
    st = S.Station({"radios": [{"id": "ic7300", "name": "IC-7300", "web": 1, "rigctl": 2, "lane": LANE,
                                "modems": modems, "vara_port": VPORT}],
                    "winlink": cfg["winlink"]}, types.SimpleNamespace(token=None, rigctl_port=0))
    st.radios[0].present = True
    st.addons = types.SimpleNamespace(engine=lambda name: Eng(pat_path), get=lambda n: None, is_on=lambda n: False)
    return st


st = make(["ardop"], bad_cfg)
check("lane_for: VARA when no radio's lane has it is refused", st.lane_for("varahf:///N0DAJ", None),
      (None, "varahf is not set up on this station."))
check("  ARDOP there is fine", type(st.lane_for("ardop:///N0DAJ", None)[0]).__name__, "RemoteLane")
st = make(["ardop", "varahf"], bad_cfg)
lane, why = st.lane_for("varahf:///N0DAJ", None)
check("  Pat without varahf settings: refused, naming the field", (lane, (why or "").startswith("VARA HF not ready: set varahf.addr")),
      (None, True))
good = testkit.pat_config(os.path.join(TMP, "pat-vara.json"),
                          varahf={"addr": f"localhost:{VPORT}", "rig": "ic7300", "ptt_ctrl": False},
                          hamlib_rigs={"ic7300": {"address": f"localhost:{LANE}", "network": "tcp"}})
st = make(["ardop", "varahf"], good)
check("  Pat set up for xara here: a remote lane", type(st.lane_for("varahf:///N0DAJ", None)[0]).__name__, "RemoteLane")
check("the external Ardop: no command, the agent's port, winlink-able radio",
      (st.ardop.external, st.ardop.cmd, st.ardop.port, st.radios[0].audio_ok), (True, [], ARDOP, True))


async def use_closed():
    try:
        await st.ardop.use(st.radios[0])
    except RuntimeError as e:
        return str(e)
    return None

check("  use() with its port closed says so", (asyncio.run(use_closed()) or "").startswith("ardopcf is not listening on 127.0.0.1:"), True)
check("  stop() never touches it", asyncio.run(st.ardop.stop()), None)

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
