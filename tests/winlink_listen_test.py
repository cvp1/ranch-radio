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
        --listen before http, the hub window open, all read back
  B-3   Pat's listener never comes up: everything undone, the page told
  B-4   refusals change nothing (no QSY, no restart, no hub call)
  B-5   end at `until`: hub window closed -> Pat without the flag -> radio back
  B-7   while listening: connect refused, outbox DELETE 409, a second Listen refused
  B-8   a clean server stop while listening: everything undone within grace
  B-11  the server SIGKILLed mid-window: the next start undoes it before Pat runs
  B-11b the server SIGKILLed mid outbound packet session: the next start puts the radio back
  B-12  a stale record whose pid is not Pat: that process is not signalled
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


print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
