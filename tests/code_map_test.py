"""CLAUDE.md's "The map" names every root module and every top-level folder,
and names nothing that is gone.

The map is how a newcomer (or an agent) finds where things live in a flat
root. It went stale before this test existed (2026-11-22: 20 of 62 root
modules missing), so it is checked, not trusted:

  - every *.py at the root appears in the map, by name (`civ.py`) or by a
    glob the map uses (`ax25*.py`)
  - every tracked top-level folder appears (`tools/`)
  - every root file, glob or folder the map names still exists

    python3 tests/code_map_test.py
"""
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


def the_map(text):
    m = re.search(r"^## The map\n(.*?)(?=^## )", text, re.S | re.M)
    return m.group(1) if m else ""


def named(section):
    """The backticked names in the map's table rows."""
    rows = [line for line in section.splitlines() if line.startswith("|")]
    return {t for line in rows for t in re.findall(r"`([^`\s]+)`", line)}


def gaps(section, modules, folders):
    """(modules missing, folders missing, named but gone) for one map text."""
    names = named(section)
    globs = [n for n in names if "*" in n and "/" not in n]
    missing = sorted(m for m in modules
                     if m not in names and not any(fnmatch.fnmatch(m, g) for g in globs))
    absent = sorted(f for f in folders if not any(n.startswith(f + "/") for n in names))
    root_names = {n for n in names if "/" not in n and n.endswith(".py")}
    gone = sorted([n for n in root_names if "*" not in n and n not in modules]
                  + [g for g in globs if not any(fnmatch.fnmatch(m, g) for m in modules)]
                  + [n for n in names if re.fullmatch(r"[\w.-]+/", n) and n[:-1] not in folders])
    return missing, absent, gone


tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True,
                         check=True).stdout.split()
modules = {f for f in tracked if "/" not in f and f.endswith(".py")}
folders = {f.split("/")[0] for f in tracked if "/" in f and not f.startswith(".")}
section = the_map((ROOT / "CLAUDE.md").read_text())

check("CLAUDE.md has a '## The map' section with table rows", bool(named(section)), True)
missing, absent, gone = gaps(section, modules, folders)
check("every root module is in the map", missing, [])
check("every top-level folder is in the map", absent, [])
check("everything the map names at the root still exists", gone, [])

# The checks above could pass by finding nothing. Prove each one catches
# its own failure on a doctored copy of the map.
globs = [n for n in named(section) if "*" in n and "/" not in n]
row_mod = sorted(m for m in modules if not any(fnmatch.fnmatch(m, g) for g in globs))[0]
doctored = section.replace(f"`{row_mod}`", "")
m2, _, _ = gaps(doctored, modules, folders)
check(f"negative control: dropping `{row_mod}` from the map is caught", row_mod in m2, True)
_, a2, _ = gaps(re.sub(r"`tools/[^`]*`", "", section), modules, folders)
check("negative control: dropping every `tools/` name is caught", "tools" in a2, True)
_, _, g2 = gaps(section + "\n| X | `no_such_module.py` `gone/` `nope*.py` | x |\n", modules, folders)
check("negative control: a stale module, folder and glob are all caught",
      sorted(g2), ["gone/", "no_such_module.py", "nope*.py"])

sys.exit(1 if fails else 0)
