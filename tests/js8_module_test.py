"""The `js8` module: rendering the JS8 engine from the host file (the
launcher itself is js8_engine_test).

  - Mac: one agent, the exact argv through the radio's rigctl port (4532
    when the host file names none), KeepAlive, a 10 s throttle
  - Linux: <id>-js8.service per radio, bound to the radio's server, the
    radio's own PipeWire nodes, --pulse
  - a js8_port with no js8 block renders nothing; the examples render no engine
  - refusals: no call, a bad grid, shell words, rigctl 0, a port clash
    (another radio's, Winlink's, xara's, 7300), two engines on a Mac, a Linux
    radio with no sound card, a block with no js8_port
  - add js8: a complete plan from the page, a js8_port handed out by
    free_port, which skips js8 ports after; nothing renders after remove

    python3 tests/js8_module_test.py
"""
import copy
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="rr-xdg-")
os.environ["RANCH_RADIO_HOSTS"] = ""
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ranch_radio import cli, modules, render  # noqa: E402

cli.HOSTS = ROOT / "examples/hosts"
HOME = "/Users/you"
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


def host(name):
    return json.loads((ROOT / "examples/hosts" / f"{name}.json").read_text())


def refused(h, words):
    try:
        render.validate(h)
    except render.HostError as e:
        return words in str(e)
    return False


# ---------------------------------------------------------------- render
JS8_MAC = {"bin": "~/.local/bin/jf8call", "call": "n0call", "grid": "FN31",
           "audio_in": "USB Audio CODEC", "audio_out": "USB Audio CODEC", "pskreporter": False}
mac = host("mac-shack")
mac["js8"] = dict(JS8_MAC)
mac["radios"][0]["js8_port"] = 4537
arts = [a for a in render.artifacts(mac, home=HOME) if a.module == "js8"]
check("mac: one engine agent", [a.path for a in arts], ["Library/LaunchAgents/org.ranch-radio.js8.plist"])
c = arts[0].content if arts else {}
check("mac: the exact argv, through the radio's rigctl (4532 when unnamed)", c.get("ProgramArguments"), [
    f"{HOME}/ranch-radio/.venv/bin/python", f"{HOME}/ranch-radio/js8_engine.py", "run", "--bin", f"{HOME}/.local/bin/jf8call",
    "--config-dir", f"{HOME}/.local/state/radio-remote/js8/{mac['radios'][0]['id']}",
    "--rigctl", f"127.0.0.1:{mac['radios'][0].get('rigctl', 4532)}", "--ws-port", "4537",
    "--call", "N0CALL", "--grid", "FN31", "--audio-in", "USB Audio CODEC", "--audio-out", "USB Audio CODEC"])
check("mac: KeepAlive, RunAtLoad, a 10 s throttle, interactive",
      (c.get("KeepAlive"), c.get("RunAtLoad"), c.get("ThrottleInterval"), c.get("ProcessType")),
      (True, True, 10, "Interactive"))
mac["js8"]["pskreporter"] = True
check("mac: pskreporter adds the flag", [a for a in render.artifacts(mac, home=HOME)
                                         if a.module == "js8"][0].content["ProgramArguments"][-1], "--pskreporter")

lin = host("linux-station")
lin["js8"] = {"bin": "~/.local/bin/jf8call", "call": "N0CALL", "grid": "FN31pr"}
for i, r in enumerate(lin["radios"]):
    r["js8_port"] = 4600 + i
units = {a.path: a.content for a in render.artifacts(lin) if a.module == "js8"}
want = [f".config/systemd/user/{r['id']}-js8.service" for r in lin["radios"]]
check("linux: one unit per radio", sorted(units), sorted(want))
r0 = lin["radios"][0]
u = units.get(f".config/systemd/user/{r0['id']}-js8.service", "")
check("linux: bound to the radio's server", (f"BindsTo={r0['id']}-remote.service" in u,
                                             f"WantedBy={r0['id']}-remote.service" in u), (True, True))
check("linux: the radio's own card and --pulse",
      (f"--audio-in {r0['audio_in']}" in u, f"--audio-out {r0['audio_out']}" in u, " --pulse" in u), (True, True, True))
check("linux: keyed through the radio's rigctl", f"--rigctl 127.0.0.1:{r0['rigctl']}" in u, True)
check("linux: paths in %h", "--bin %h/.local/bin/jf8call" in u, True)

idle = host("mac-shack")
idle["radios"][0]["js8_port"] = 4537
check("a js8_port with no js8 block: no engine", [a for a in render.artifacts(idle, home=HOME) if a.module == "js8"], [])
for f in sorted((ROOT / "examples/hosts").glob("[!._]*.json")):
    h = render.load(f)
    check(f"{h['host']}: no engine in the example", [a.path for a in render.artifacts(h, home=HOME)
                                                     if a.module == "js8"], [])

# ---------------------------------------------------------------- refusals
def variant(edit, base=mac):
    h = copy.deepcopy(base)
    edit(h)
    return h


mac["js8"]["pskreporter"] = False
check("no call: refused", refused(variant(lambda h: h["js8"].pop("call")), "needs \"call\""), True)
check("the host's call is enough", refused(variant(lambda h: (h["js8"].pop("call"), h.update(call="N0CALL"))),
                                           "call"), False)
check("a bad grid: refused", refused(variant(lambda h: h["js8"].update(grid="ZZ99")), "grid"), True)
check("a shell word in bin: refused", refused(variant(lambda h: h["js8"].update(bin="jf8;rm")), "not allowed"), True)
check("a call with a space: refused", refused(variant(lambda h: h["js8"].update(call="N0CALL --x")), "not allowed"),
      True)
check("rigctl 0: refused", refused(variant(lambda h: h["radios"][0].update(rigctl=0)), "rigctl port is off"), True)
check("on a lane port: refused", refused(variant(lambda h: h["radios"][0].update(js8_port=h["radios"][0]["lane"])),
                                         "port"), True)
if (mac.get("winlink") or {}).get("xara"):
    check("on xara's port: refused", refused(variant(lambda h: h["radios"][0].update(
        js8_port=h["winlink"]["xara"]["port"])), "xara"), True)
check("on 7300: refused", refused(variant(lambda h: h["radios"][0].update(js8_port=7300)), "7300"), True)
check("a block with no js8_port: refused", refused(variant(lambda h: h["radios"][0].pop("js8_port")),
                                                   "no radio has a js8_port"), True)
check("mac without its card: refused", refused(variant(lambda h: h["js8"].pop("audio_out")), "audio_out"), True)
two = variant(lambda h: h["radios"].append(dict(h["radios"][0], id="ic705", name="IC-705", web=7302,
                                                 rigctl=4590, lane=4591, js8_port=4592,
                                                 label="ranch-radio.ic705", device="/dev/cu.usbmodem2")))
check("two engines on a Mac: refused", refused(two, "one engine"), True)
check("a linux radio with no card: refused",
      refused(variant(lambda h: h["radios"][0].pop("audio_in"), lin), "audio_in and audio_out"), True)
check("a valid Linux block passes", refused(lin, ""), False)

# ---------------------------------------------------------------- add / remove
text = (ROOT / "examples/hosts/mac-shack.json").read_text()
plan = modules.plan_add(text, "js8", {"call": "N0CALL", "grid": "FN31", "audio_in": "USB Audio CODEC",
                                      "audio_out": "USB Audio CODEC"}, source="page")
check("add js8 from the page: a complete plan", (plan.refusal, plan.errors, plan.asks), ("", {}, []))
on = json.loads(plan.new_text) if plan.new_text else {"radios": [{}]}
port = on["radios"][0].get("js8_port")
check("add js8: the radio gets a js8_port", isinstance(port, int), True)
check("add js8: TX-safe defaults in the block", on.get("js8", {}).get("pskreporter"), False)
check("free ports skip a js8_port", port in modules.free_ports(on, 3), False)
missing = modules.plan_add(text, "js8", {"call": "N0CALL"}, source="page")
check("add js8 without grid and card: asked for, not guessed", sorted(missing.errors),
      ["audio_in", "audio_out", "grid"])
rm = modules.plan_remove(plan.new_text, "js8") if plan.new_text else None
check("remove js8: a plan", rm.refusal if rm else "no plan", "")
check("remove js8: no engine renders after", [a for a in render.artifacts(json.loads(rm.new_text), home=HOME)
                                              if a.module == "js8"] if rm else None, [])

print(f"\n{fails} failure(s)" if fails else "\nall ok")
sys.exit(1 if fails else 0)
