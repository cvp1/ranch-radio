"""Module manifests, `ranch-radio add` / `remove` / `doctor`.

The falsifiers:
- add then remove of every module on every host file gives back the file
  byte for byte (the in-place editor never reformats a hand-written file);
- doctor reports a missing binary as missing (a control that can fail),
  and the same block pointed at a real executable as ok;
- remove refuses to take away a module another one leans on.
Works on copies of hosts/ in a temp directory; touches no radio or service.

    python3 tests/modules_test.py
"""
import contextlib
import io
import json
import os
import re
import shutil
import stat
import sys
import os as _os
# Host files come from examples/hosts/ (or a scratch copy) only, never a
# real ~/.config/ranch-radio/hosts on the machine running the tests.
_os.environ["RANCH_RADIO_HOSTS"] = ""
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ranch_radio import catalog, cli, doctor, hostfile, manifest, render  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


def run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            rc = cli.main(argv)
        except SystemExit as e:
            out.write(str(e.code) if not isinstance(e.code, int) else "")
            rc = e.code if isinstance(e.code, int) else 1
    return rc, out.getvalue()


def filled(block):
    """A template with every ASK answered."""
    out = {}
    for k, v in block.items():
        out[k] = filled(v) if isinstance(v, dict) else ("x" if v == manifest.ASK else v)
    return out


tmp = Path(tempfile.mkdtemp(prefix="rr-modules-"))
hosts = tmp / "hosts"
shutil.copytree(ROOT / "examples/hosts", hosts)
cli.HOSTS = hosts
real_host = cli.this_host

# ---------------------------------------------------------------- manifest
for name, m in manifest.MANIFESTS.items():
    check(f"{name} is in the catalog", name in catalog.by_name(), True)
    for os_name in m["os"]:
        check(f"{name} has a {os_name} template", manifest.template(name, os_name) is not None, True)

# ---------------------------------------------------------------- editor: exact round trips
for f in sorted((ROOT / "examples/hosts").glob("[!._]*.json")):
    text = f.read_text()
    h = json.loads(text)
    present = doctor.on_modules(h)
    for name, m in manifest.MANIFESTS.items():
        if h["os"] not in m["os"]:
            continue
        if name in present:
            block = doctor.block_of(h, name)
            t2 = hostfile.drop(text, m["block"])
            back = hostfile.put(t2, m["block"], block)
            check(f"{f.stem}: drop then put {name} gives the same data", json.loads(back), h)
        else:
            block = filled(manifest.template(name, h["os"], present))
            t2 = hostfile.put(text, m["block"], block)
            check(f"{f.stem}: put {name} lands at {'/'.join(m['block'])}",
                  doctor.block_of(json.loads(t2), name), block)
            check(f"{f.stem}: put then drop {name} is byte-identical", hostfile.drop(t2, m["block"]), text)

# A new top-level block copies its neighbours' layout: its own line, same indent.
t = (ROOT / "examples/hosts" / "linux-laptop.json").read_text()
t2 = hostfile.put(t, ("packet",), {"tnc": "x"})
check("a new block goes on its own line at the file's indent", '},\n  "packet": {"tnc": "x"}\n}' in t2, True)

# A field inside an array element (a radio's lane), compact formatting kept.
t = '{"radios": [\n  {"id": "a", "device": "/dev/x"}\n], "host": "h"}\n'
t2 = hostfile.put(t, ("radios", 0, "lane"), 4537)
check("put a radio field keeps the line", t2, '{"radios": [\n  {"id": "a", "device": "/dev/x", "lane": 4537}\n], "host": "h"}\n')
check("drop it again is byte-identical", hostfile.drop(t2, ("radios", 0, "lane")), t)
check("strings holding braces don't confuse the editor",
      json.loads(hostfile.put('{"a": "}{,\\"", "b": 1}', ("c",), {"d": "]"})), {"a": '}{,"', "b": 1, "c": {"d": "]"}})

# ---------------------------------------------------------------- add
rc, out = run(["add", "packet", "--host", "linux-laptop"])
check("add asks for what only the operator knows (exit 2)", rc, 2)
check("  and names the fields", "--set tnc=" in out and "--set mycall=" in out, True)
rc, out = run(["add", "uvpro", "--host", "mac-shack"])
check("add refuses a module the OS doesn't ship", (rc, "not on mac-shack" in out), (1, True))
rc, out = run(["add", "g90", "--host", "linux-laptop"])
check("add refuses a radio, and says why", (rc, "Radios are not added this way" in out), (1, True))
rc, out = run(["add", "mesh", "--host", "linux-laptop"])
check("add refuses a module already on", (rc, "already on linux-laptop" in out), (1, True))
before = (hosts / "linux-laptop.json").read_text()
rc, out = run(["add", "packet", "--host", "linux-laptop", "--set", "tnc=bt:11:22:33:44:55:66", "--set", "mycall=N0CALL"])
check("add without --apply writes nothing", (rc, (hosts / "linux-laptop.json").read_text()), (0, before))
rc, out = run(["add", "packet", "--host", "linux-laptop", "--set", "tnc=bt:11:22:33:44:55:66",
               "--set", "mycall=N0CALL", "--apply"])
h = render.load(hosts / "linux-laptop.json")
check("add --apply writes the block", (rc, h["packet"]["tnc"], h["packet"]["tx"]), (0, "bt:11:22:33:44:55:66", False))
rc, out = run(["add", "uvpro", "--host", "linux-laptop", "--set", "address=11:22:33:44:55:66", "--set", 'calls=["N0CALL"]',
               "--apply"])
h = render.load(hosts / "linux-laptop.json")
check("uvpro on a host with the hub borrows from it", h["addons"]["uvpro"].get("hub"), "http://127.0.0.1:7310")
check("--set parses JSON values", h["addons"]["uvpro"]["calls"], ["N0CALL"])

# A radio without a lane gets a free one when Winlink goes on.
lane_less = json.loads((hosts / "linux-laptop.json").read_text())
lane_less["host"] = "lanetest"
del lane_less["radios"][0]["lane"]
(hosts / "lanetest.json").write_text(json.dumps(lane_less, indent=2) + "\n")
rc, out = run(["add", "winlink", "--host", "lanetest", "--apply"])
h = render.load(hosts / "lanetest.json")
ports = [h["radios"][0][k] for k in ("web", "rigctl", "lane")]
check("winlink gives a lane-less radio a lane", (rc, "lane" in h["radios"][0]), (0, True))
check("  on a port nothing else uses", len(set(ports)) == 3 and h["radios"][0]["lane"] not in h["winlink"].values(), True)
check("  and the host still renders", bool(render.artifacts(h)), True)

# ---------------------------------------------------------------- remove
rc, out = run(["remove", "packet", "--host", "linux-laptop", "--apply"])
check("remove refuses the hub while uvpro borrows from it", (rc, "leans on packet" in out), (1, True))
rc, _ = run(["remove", "uvpro", "--host", "linux-laptop", "--apply"])
rc2, _ = run(["remove", "packet", "--host", "linux-laptop", "--apply"])
check("remove uvpro, then packet: back to the original file", ((hosts / "linux-laptop.json").read_text(), rc, rc2), (before, 0, 0))
rc, out = run(["remove", "winlink", "--host", "mac-shack"])
check("remove on a Mac names the agent install leaves behind",
      "launchctl bootout gui/$(id -u)/org.ranch-radio.ardopcf" in out and "~/.Trash/" in out, True)
check("  and keeps the lane", "keeps its lane port" in out, True)
rc, out = run(["remove", "packet", "--host", "mac-shack"])
check("remove refuses a module that isn't on", (rc, "not on mac-shack" in out), (1, True))

# ---------------------------------------------------------------- doctor: a control that can fail
exe = tmp / "bin" / "fakepat"
exe.parent.mkdir()
exe.write_text("#!/bin/sh\n")
exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
mac = json.loads((ROOT / "examples/hosts" / "mac-shack.json").read_text())


def winlink_checks(pat):
    h = json.loads(json.dumps(mac))
    h["winlink"]["pat"] = pat
    h["winlink"]["ardopcf"]["bin"] = str(exe)
    return {c.what.split(":")[0].split(" ")[0]: c.status
            for c in doctor.module_checks(h, "winlink", local=True)}


check("doctor: a pat that isn't there is missing", winlink_checks(str(tmp / "nope" / "pat"))["pat"], doctor.MISSING)
check("doctor: the same block at a real executable is ok", winlink_checks(str(exe))["pat"], doctor.OK)
h = json.loads(json.dumps(mac))
h["mesh"]["python"] = str(tmp / "no-python")
c = [c for c in doctor.module_checks(h, "mesh", local=True) if "meshtastic" in c.what][0]
check("doctor: a mesh python that isn't there is missing", c.status, doctor.MISSING)
h = json.loads(json.dumps(mac))
h["mesh"]["python"] = sys.executable
h["mesh"]["keys"] = [f"Ch={tmp}/no-key"]
c = [c for c in doctor.module_checks(h, "mesh", local=True) if "no-key" in c.what][0]
check("doctor: a missing channel key is worth knowing, not blocking", c.status, doctor.WARN)
h = json.loads(json.dumps(mac))
h["winlink"]["ardopcf"]["audio_in"] = manifest.ASK
c = [c for c in doctor.static(h) if c.module == "winlink"]
check("doctor: an unanswered field is missing", [x.status for x in c], [doctor.MISSING])
h = json.loads(json.dumps(mac))
h["radios"][0]["rig"] = "nosuchrig"
check("doctor: an unknown rig is missing", doctor.static(h)[0].status, doctor.MISSING)
h["radios"][0]["rig"] = "x6100"
check("doctor: an untested profile says so", doctor.static(h)[0].status, doctor.WARN)

# doctor's exit code follows "missing", on a host taken for this machine.
bad = json.loads(json.dumps(mac))
bad["host"] = "doctest"
bad["python"] = sys.executable
bad["radios"][0]["device"] = "/dev/null"
bad["winlink"]["pat"] = str(tmp / "nope" / "pat")
(hosts / "doctest.json").write_text(json.dumps(bad))
cli.this_host = lambda: "doctest"
rc, out = run(["doctor"])
check("doctor exits 1 when something is missing", (rc, "missing" in out.splitlines()[-1]), (1, True))
cli.this_host = real_host
rc, out = run(["doctor", "doctest"])
check("doctor for another host checks the host file only", "host file only" in out, True)

# One benlink: the doctor's install, its printed fix, CI and the flake-hunt
# image all pin the commit the UV-PRO bridge is tested against (a printed fix
# of plain "pip install benlink" pulled a release the bridge can't parse).
pin = re.search(r"@([0-9a-f]{40})", manifest.BENLINK_PIP).group(1)
need = manifest.MANIFESTS["uvpro"]["needs"][0]
check("benlink: doctor's install and its printed fix use the pin",
      (manifest.BENLINK_PIP in need["auto"], manifest.BENLINK_PIP in need["fix"]), (True, True))
for f in (".github/workflows/ci.yml", "tests/ci/Dockerfile"):
    check(f"benlink: {f} pins the same commit",
          re.findall(r"benlink @ git\+https://github.com/khusmann/benlink@([0-9a-f]{40})", (ROOT / f).read_text()), [pin])

shutil.rmtree(tmp)
print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
