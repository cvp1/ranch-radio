"""The `vara` module (Winlink over VARA HF, xara; plan winlink-vara 2.4, A5, A6).

  - golden: every example host renders exactly as main did before VARA
    (tests/fixtures/render-golden.json, rendered at the merge base)
  - vara on (mac-shack): the xara agent's exact argv, KeepAlive, throttle;
    the server's argv gains the three --winlink-vara-* flags
  - add then remove: back to the golden render, and remove names the
    bootout and move-aside install leaves for the operator
  - Winlink without vara: doctor's static checks are complete (vara's ASK
    fields are its own); vara's unanswered fields are vara's
  - hygiene: shell words in call/label/bin refused, sound card names with
    spaces accepted, a leading "-" refused, hf 1000 and drive_db 3 refused,
    a port clash refused, xara on Linux refused
  - doctor: xara missing -> the pinned build line (no paid-speed option);
    Pat's varahf fields checked by host and port, naming the field, the
    fixture's password never printed; a xara left behind with vara off warns
    with the exact bootout and move-aside commands

    python3 tests/vara_module_test.py
"""
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="rr-xdg-")
os.environ["RANCH_RADIO_HOSTS"] = ""
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from ranch_radio import cli, doctor, manifest, modules, render  # noqa: E402
import testkit  # noqa: E402

cli.HOSTS = ROOT / "examples/hosts"
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
    check(f"golden: {h['host']} renders as before VARA", rendered(h), golden[h["host"]])

# ---------------------------------------------------------------- vara on
XARA = {"audio_in": "USB Audio CODEC", "audio_out": "USB Audio CODEC", "call": "N0CALL"}
text = (ROOT / "examples/hosts/mac-shack.json").read_text()
plan = modules.plan_add(text, "vara", XARA, source="page")
check("add vara from the page: a complete plan", (plan.refusal, plan.errors, plan.asks), ("", {}, []))
on = json.loads(plan.new_text)
arts = {a.path: a for a in render.artifacts(on, home=HOME)}
agent = arts.get("Library/LaunchAgents/ranch-radio.xara.plist")
check("vara on: a xara agent", agent is not None, True)
c = agent.content if agent else {}
check("vara on: its exact argv", c.get("ProgramArguments"), [
    f"{HOME}/.local/bin/xara", "--call", "N0CALL", "--hf", "2300", "--audio", "coreaudio",
    "--capture", "USB Audio CODEC", "--playback", "USB Audio CODEC", "--radio",
    "--ptt", "rigctld:127.0.0.1:4534", "--addr", "127.0.0.1", "--port", "8300", "--kiss-port", "off",
    "--drive-db", "-5", "--log", f"{HOME}/Library/Logs/ranch-radio-xara.log", "--quiet",
    "--state-dir", f"{HOME}/Library/Application Support/ranch-radio/xara"])
check("vara on: KeepAlive, RunAtLoad, a 10 s throttle, interactive",
      (c.get("KeepAlive"), c.get("RunAtLoad"), c.get("ThrottleInterval"), c.get("ProcessType")),
      (True, True, 10, "Interactive"))
server = next(a for a in arts.values() if a.path.endswith("ic7300-remote.plist")).content["ProgramArguments"]
i = server.index("--winlink-vara-log")
check("vara on: the server gets the log, xara's lane and its port",
      server[i:i + 6], ["--winlink-vara-log", f"{HOME}/Library/Logs/ranch-radio-xara.log",
                        "--winlink-vara-rigctl-port", "4534", "--winlink-vara-port", "8300"])
check("vara on: nowhere a paid-speed option", "registered" in json.dumps([a.text() for a in arts.values()]), False)
check("vara on: the other agents as before", sorted(p for p in arts if "xara" not in p and "ic7300-remote" not in p),
      sorted(g["path"] for g in golden["mac-shack"] if "ic7300-remote" not in g["path"]))

# ---------------------------------------------------------------- remove: back to the golden
rm = modules.plan_remove(plan.new_text, "vara")
check("remove vara: a plan", rm.refusal, "")
check("remove vara: the host file as before", rm.new_text, text)
check("remove vara: renders as before VARA", rendered(json.loads(rm.new_text)), golden["mac-shack"])
check("remove vara: names the bootout and move-aside install leaves",
      rm.orphans, ["launchctl bootout gui/$(id -u)/ranch-radio.xara; "
                   "mv ~/Library/LaunchAgents/ranch-radio.xara.plist ~/.Trash/"])
rw = modules.plan_remove(plan.new_text, "winlink")
check("remove winlink while vara is on: refused (vara leans on it)", "vara leans on winlink" in rw.refusal, True)
no_wl = json.loads(text)
del no_wl["winlink"]
check("add vara without winlink: refused", "needs winlink" in
      modules.plan_add(json.dumps(no_wl), "vara", XARA, source="page").refusal, True)

# ---------------------------------------------------------------- doctor static
check("Winlink without vara: doctor static complete",
      [x for x in doctor.static(host("mac-shack")) if x.status == doctor.MISSING], [])
half = copy.deepcopy(on)
half["winlink"]["xara"]["call"] = manifest.ASK
st = [(x.module, x.what) for x in doctor.static(half) if x.status == doctor.MISSING]
check("an unanswered xara field is vara's, once", st, [("vara", f"call is still {manifest.ASK}")])

# ---------------------------------------------------------------- hygiene


def refused(name, mutate):
    h = copy.deepcopy(on)
    mutate(h)
    try:
        render.validate(h)
        check(f"refused: {name}", "accepted", "refused")
    except render.HostError:
        check(f"refused: {name}", "refused", "refused")


def x(k, v):
    return lambda h: h["winlink"]["xara"].__setitem__(k, v)


refused("call with an option", x("call", "N0CALL --tx"))
refused("call with a shell character", x("call", "N0CALL;id"))
refused("label with a shell character", x("label", "ranch-radio.xara;id"))
refused("bin with a space", x("bin", "~/bin/xara --tx"))
refused("audio_in starting with -", x("audio_in", "-x"))
refused("audio_in with a newline", x("audio_in", "USB\nAudio"))
refused("hf 1000", x("hf", 1000))
refused("drive_db 3 (above the codec)", x("drive_db", 3))
refused("drive_db not a number", x("drive_db", "-5"))
refused("lane_port on the radio's lane", x("lane_port", 4533))
refused("xara's data port on ardopcf's", x("port", 8514))
refused("lane_port equal to xara's own data port", x("lane_port", 8301))
refused("xara on Linux", lambda h: h.update(os="linux"))
ok = copy.deepcopy(on)
ok["winlink"]["xara"].update(audio_in="USB Audio CODEC (2)", hf=500, drive_db=-12.5)
try:
    render.validate(ok)
    check("accepted: a sound card name with spaces and brackets, hf 500, drive_db -12.5", True, True)
except render.HostError as e:
    check("accepted: a sound card name with spaces and brackets, hf 500, drive_db -12.5", str(e), True)
check("page rule: a callsign for call", manifest.check_ask("callsign", "N0CALL;id") is not None, True)
check("free ports skip xara's", 4534 in modules.free_ports(on, 3) or 8300 in modules.free_ports(on, 3), False)

# ---------------------------------------------------------------- doctor machine
tmp = Path(tempfile.mkdtemp(prefix="rr-vara-"))
h = copy.deepcopy(on)
h["winlink"]["xara"]["bin"] = str(tmp / "nope" / "xara")
miss = [c for c in doctor.module_checks(h, "vara", local=True) if c.what.startswith("xara")]
check("doctor: xara missing", miss and miss[0].status, doctor.MISSING)
check("doctor: its fix is the pinned build", miss and miss[0].fix,
      "brew install rust && cargo install --locked --git https://github.com/x0rro/xara --tag v0.1.3 --root ~/.local xara")
check("doctor: no recipe (the toolchain is the operator's call)", miss and miss[0].recipe, "")
check("doctor: no paid-speed option anywhere in the module", "registered" in json.dumps(manifest.MANIFESTS["vara"]), False)

PW = "s3cret-fake-pw"


def cfg_check(**varahf):
    rigs = varahf.pop("_rigs", {"ic7300": {"address": "localhost:4533"}})
    v = {"addr": "localhost:8300", "rig": "ic7300", "ptt_ctrl": False, **varahf}
    path = testkit.pat_config(str(tmp / "pat.json"), varahf=v, hamlib_rigs=rigs, secure_login_password=PW)
    hh = copy.deepcopy(on)
    hh["winlink"]["pat_config"] = path
    return doctor.vara_config_check(hh)


good = cfg_check()
check("doctor: Pat's VARA settings right -> ok", good.status, doctor.OK)
for what, over, field in [("varahf.rig empty", {"rig": ""}, "varahf.rig"),
                          ("varahf.addr elsewhere", {"addr": "192.168.1.50:8300"}, "varahf.addr"),
                          ("the rig's address elsewhere", {"_rigs": {"ic7300": {"address": "192.168.1.50:4533"}}},
                           "hamlib_rigs.ic7300.address"),
                          ("ptt_ctrl true", {"ptt_ctrl": True}, "varahf.ptt_ctrl")]:
    c = cfg_check(**over)
    check(f"doctor: {what} -> warn naming {field}", (c.status, field in c.what), (doctor.WARN, True))
    check(f"doctor: {what}: the password is never printed", PW in c.what + c.fix, False)

out = io.StringIO()
with contextlib.redirect_stdout(out):
    for c in doctor.machine(on):
        print(c.module, c.status, c.what, c.fix)
check("doctor run: the fixture password never in its output", PW in out.getvalue(), False)

left = doctor.xara_leftover_checks(host("mac-shack"), probe=lambda label, port: (True, None, True, False))
check("doctor: vara off but xara still loaded -> warn", [c.status for c in left], [doctor.WARN])
check("doctor:   with the exact bootout and move-aside",
      left and left[0].fix, "launchctl bootout gui/$(id -u)/ranch-radio.xara; "
                            "mv ~/Library/LaunchAgents/ranch-radio.xara.plist ~/.Trash/ranch-radio.xara.plist")
check("doctor: nothing left -> nothing said",
      doctor.xara_leftover_checks(host("mac-shack"), probe=lambda label, port: (False, None, False, False)), [])

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
