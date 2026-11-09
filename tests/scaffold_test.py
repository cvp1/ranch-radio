"""`ranch-radio new-addon`: the files a new add-on starts from (ranch_radio/scaffold.py).

  - writes addons/NAME.py, static/addons/NAME.js (and .css with css), tests/NAME_addon_test.py,
    and nothing else; refuses a bad name, a bad group, a name already there, a
    module the catalog or manifest already has
  - the module it writes imports, is an Addon, loads in the registry on, in its
    group, starts and stops, answers ping
  - the test it writes runs: its registry checks pass and its catalog and
    manifest checks fail, naming the files to edit (that is the checklist)
  - the snippets name the module in both entries
  - the CLI subcommand is wired

    python3 tests/scaffold_test.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ranch_radio import scaffold  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


def refused(root, name, group="tools"):
    try:
        scaffold.create(root, name, "X", "x", group=group)
    except ValueError as e:
        return str(e)
    return None


tmp = Path(tempfile.mkdtemp(prefix="rr-scaffold-"))
try:
    # A stand-in checkout: the real addons package and ops package, nothing else.
    (tmp / "addons").mkdir()
    shutil.copy(ROOT / "addons/__init__.py", tmp / "addons/__init__.py")
    os.symlink(ROOT / "ranch_radio", tmp / "ranch_radio")
    (tmp / "tests").mkdir()

    check("a bad name is refused", "not an add-on name" in (refused(tmp, "Bad-Name") or ""), True)
    check("a bad group is refused", "group must be" in (refused(tmp, "rotator", group="widgets") or ""), True)
    check("a module the catalog has is refused", "already has a module" in (refused(tmp, "logbook") or ""), True)

    written = scaffold.create(tmp, "rotator", "Rotator", "Point the beam", group="tools", css=True)
    check("writes the four files", written,
          ["addons/rotator.py", "static/addons/rotator.js", "tests/rotator_addon_test.py", "static/addons/rotator.css"])
    check("  and they exist", all((tmp / w).is_file() for w in written), True)
    check("  nothing else under static/addons", sorted(p.name for p in (tmp / "static/addons").iterdir()),
          ["rotator.css", "rotator.js"])
    check("a second time is refused, naming what's there", "already there" in (refused(tmp, "rotator") or ""), True)

    probe = subprocess.run([sys.executable, "-c", """
import asyncio, sys
sys.path.insert(0, %r)
from addons import Addons, Addon
import addons.rotator as R
class Host:
    notices = []
    def notice(self, msg, level="info", ws=None): self.notices.append(msg)
    def _broadcast(self, payload): self.notices.append(("bcast", payload))
    async def _send(self, ws, payload): self.notices.append(("send", payload))
h = Host()
reg = Addons(h, {"rotator": {"enabled": True}}, state_path=None)
d = {x["name"]: x for x in reg.describe()}["rotator"]
print(issubclass(R.ADDON, Addon), d["label"], d["group"], d["css"], d["on"], d["error"])
async def go():
    await reg.start(); running = reg.items["rotator"]["running"]
    await reg.command("WS", {"t": "addon", "addon": "rotator", "do": "ping"})
    await asyncio.sleep(0.05)
    await reg.stop(); return running, reg.items["rotator"]["running"]
print(asyncio.run(go()))
print([n for n in h.notices if isinstance(n, tuple) and n[0] == "send"])
""" % str(tmp)], capture_output=True, text=True, cwd=tmp)
    lines = probe.stdout.strip().splitlines()
    check("the module imports, is an Addon, loads on in its group with css",
          lines[0] if lines else probe.stderr, "True Rotator tools True True None")
    check("  starts and stops", lines[1] if len(lines) > 1 else probe.stderr, "(True, False)")
    check("  answers ping to the asking page", "pong" in (lines[2] if len(lines) > 2 else ""), True)

    t = subprocess.run([sys.executable, str(tmp / "tests/rotator_addon_test.py")], capture_output=True, text=True, cwd=tmp)
    out = t.stdout
    check("its test runs: registry checks pass", ("ok   loads" in out, "ok   starts" in out, "ok   stops" in out), (True, True, True))
    check("  catalog and manifest checks fail, naming the files",
          ("FAIL in the catalog (ranch_radio/catalog.py)" in out, "FAIL in the manifest" in out, "ranch_radio/manifest.py" in out),
          (True, True, True))
    check("  so the test exits 1 until they are added", t.returncode, 1)

    s = scaffold.snippets("rotator", "Rotator", "Point the beam")
    check("the snippets carry both entries", ('"name": "rotator", "kind": "addon"' in s,
                                              '"block": ("addons", "rotator")' in s, "README.md" in s, "CHANGELOG.md" in s),
          (True, True, True, True))

    cli = subprocess.run([sys.executable, str(ROOT / "ranch-radio"), "new-addon", "--help"], capture_output=True, text=True,
                         env={**os.environ, "RANCH_RADIO_NO_VENV": "1"})
    check("the CLI has new-addon", (cli.returncode, "--group" in cli.stdout, "--css" in cli.stdout), (0, True, True))
    bad = subprocess.run([sys.executable, str(ROOT / "ranch-radio"), "new-addon", "logbook"], capture_output=True, text=True,
                         env={**os.environ, "RANCH_RADIO_NO_VENV": "1"})
    check("  and refuses an existing module without writing", (bad.returncode, "already" in bad.stdout), (1, True))
finally:
    shutil.rmtree(tmp)

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
