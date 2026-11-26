"""Installing into a venv without pip (ranch_radio/pip.py): a `uv venv` has
none, and a station's checkout .venv was one (2026-11-26), so `update` and
`doctor --fix` failed the moment a requirement changed. Offline; fake venvs
and a fake uv that records how it was called.

  - a venv without pip: uv's pip aimed at it; with pip: its own; a system
    Python (not a venv): its own, as before; no pip and no uv: None, and a
    reason that names both ways out
  - uv is not handed pip's own flags (it refuses --disable-pip-version-check)
  - update's requirements step installs through uv into a pip-less checkout
    venv, with the same -r requirements.txt -c constraints.txt; with nothing
    to install with, it says why instead of failing on "No module named pip"
  - doctor's numpy recipe (the Imaging add-on) uses uv's pip for that venv,
    and is left out when nothing can install
  - decided from the venv's files, not by running its Python (doctor
    counts those calls)

    python3 tests/pip_fallback_test.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ranch_radio import doctor, manifest, updater  # noqa: E402
from ranch_radio import pip as pipmod  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


pipmod.UV_PLACES = ()                       # only the PATH each check gives (this Mac has a real uv)
tmp = Path(tempfile.mkdtemp(prefix="pipless-"))


def venv(name, with_pip):
    root = tmp / name
    (root / "bin").mkdir(parents=True)
    (root / "pyvenv.cfg").write_text(f"home = {Path(sys.executable).parent}\n")
    os.symlink(sys.executable, root / "bin" / "python")
    if with_pip:
        (root / "bin" / "pip").write_text("#!/bin/sh\n")
    return root


bare, piped = venv("bare", False), venv("piped", True)
uvdir = tmp / "uvbin"
uvdir.mkdir()
log = tmp / "uv-calls"
uv = uvdir / "uv"
# Strict like the real uv (2026-11-26, live): --python before the subcommand
# is refused; the target comes as UV_PYTHON, logged first.
uv.write_text(f'#!/bin/sh\ncase "$2" in --python*) echo "unexpected argument" >&2; exit 2;; esac\n'
              f'echo "UV_PYTHON=$UV_PYTHON $@" >> {log}\ntouch {tmp}/installed\nexit 0\n')
uv.chmod(0o755)
nouv = tmp / "empty"
nouv.mkdir()

bpy, ppy = str(bare / "bin" / "python"), str(piped / "bin" / "python")
check("a venv without pip: uv's pip aimed at it", pipmod.argv(bpy, path=str(uvdir)), ["env", f"UV_PYTHON={bpy}", str(uv), "pip"])
check("a venv with pip: its own", pipmod.argv(ppy, path=str(uvdir)), [ppy, "-m", "pip"])
check("a system Python (not a venv): its own, as before", pipmod.argv("/usr/bin/python3", path=str(nouv)),
      ["/usr/bin/python3", "-m", "pip"])
check("no pip and no uv: None", pipmod.argv(bpy, path=str(nouv)), None)
check("  and the reason names both ways out", all(w in pipmod.why_not(bpy) for w in ("ensurepip", "uv")), True)
args = ["install", "-q", "--disable-pip-version-check", "-r", "r.txt"]
check("uv is not handed pip's own flags", pipmod.install_args(["env", f"UV_PYTHON={bpy}", str(uv), "pip"], args),
      ["install", "-q", "-r", "r.txt"])
check("  pip keeps them", pipmod.install_args([ppy, "-m", "pip"], args), args)

# --- update's requirements step, into a pip-less checkout venv
repo = tmp / "repo"
repo.mkdir()
(repo / "requirements.txt").write_text("aiohttp>=3\n")
(repo / "constraints.txt").write_text("aiohttp==3.14.5\n")
os.symlink(bare, repo / ".venv")
h = {"python": str(repo / ".venv" / "bin" / "python")}
real_unmet = updater.unmet
updater.unmet = lambda root, h: [] if (tmp / "installed").exists() else ["aiohttp>=3"]
old_path = os.environ.get("PATH", "")
os.environ.pop("RANCH_RADIO_PIP", None)
os.environ["PATH"] = f"{uvdir}{os.pathsep}{old_path}"
ok, why = updater.requirements(str(repo), h, log=lambda *a: None)
calls = log.read_text().split() if log.exists() else []
check("update installs through uv into the pip-less venv", (ok, why), (True, ""))
check("  with -r requirements.txt -c constraints.txt, aimed at that venv",
      (f"UV_PYTHON={h['python']}" in calls and calls[1:4] == ["pip", "install", "-q"] and str(repo / "requirements.txt") in calls
       and str(repo / "constraints.txt") in calls), True)
check("  and without pip's own flags", "--disable-pip-version-check" in calls, False)
(tmp / "installed").unlink()
os.environ["PATH"] = str(nouv)
ok, why = updater.requirements(str(repo), h, log=lambda *a: None)
check("with nothing to install with, update says why (not 'No module named pip')",
      (ok, "ensurepip" in why), (False, True))
os.environ["PATH"] = old_path
updater.unmet = real_unmet

# --- doctor's numpy recipe (the Imaging add-on's need)
need = manifest.MANIFESTS["imaging"]["needs"][0]
os.environ["PATH"] = f"{uvdir}{os.pathsep}{old_path}"
c = doctor._need("imaging", need, {}, bpy, path=str(uvdir), host_os="macos")[0]
check("doctor: numpy missing in the pip-less venv", c.status, doctor.MISSING)
check("  its recipe installs through uv's pip, aimed at that venv",
      c.recipe.startswith(f"env UV_PYTHON={bpy} {uv} pip install") and "numpy" in c.recipe, True)
os.environ["PATH"] = str(nouv)
c = doctor._need("imaging", need, {}, bpy, path=str(nouv), host_os="macos")[0]
check("  with nothing to install with, no recipe (the fix text still says what to do)", (c.recipe, bool(c.fix)), ("", True))
os.environ["PATH"] = old_path

# --- decided from files: these fake venvs' python is this test's own, which has pip
check("decided from the venv's files, not by running its Python", (pipmod.has_pip(bpy), pipmod.has_pip(ppy)), (False, True))

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
