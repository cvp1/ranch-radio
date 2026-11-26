"""How to install packages into a given Python: its own pip, or uv's pip
aimed at it when it is a venv without pip.

A venv made by `uv venv` has no pip (2026-11-26: a station's checkout .venv
was one, so `update` and `doctor --fix` would have failed with "No module
named pip" the first time a requirement changed; the Imaging add-on's numpy
went in by hand with uv). Decided from the files on disk, never by running
that Python: doctor counts its calls to the host's Python
(modules_server_test), and a check must not add one.

    argv(python)        -> ["<py>", "-m", "pip"] | ["env", "UV_PYTHON=<py>", "<uv>", "pip"] | None
    install_args(argv, args) -> args without pip-only flags when argv is uv's

uv takes its target as UV_PYTHON (or --python AFTER the subcommand: `uv pip
--python X install` is refused, measured 2026-11-26), so the prefix stays a
prefix for every caller, argv or shell recipe.
"""
import os
import shlex
import shutil
from pathlib import Path

UV_PLACES = ("~/.local/bin/uv", "~/.cargo/bin/uv", "/opt/homebrew/bin/uv", "/usr/local/bin/uv", "/usr/bin/uv")
PIP_ONLY_FLAGS = ("--disable-pip-version-check",)


def _venv_root(python):
    """The venv a Python lives in, or None (a system or Homebrew Python)."""
    try:
        bindir = Path(os.path.expanduser(str(python))).absolute().parent
    except (OSError, ValueError):
        return None
    root = bindir.parent
    return root if (root / "pyvenv.cfg").is_file() else None


def has_pip(python):
    """Can `python -m pip` work? A venv: only if pip was installed into it (its
    bin/ has pip or pip3). Anything else: assume yes, as before."""
    root = _venv_root(python)
    if root is None:
        return True
    bindir = Path(os.path.expanduser(str(python))).absolute().parent
    if (bindir / "pip").exists() or (bindir / "pip3").exists():
        return True
    return any(root.glob("lib/python*/site-packages/pip/__init__.py"))


def find_uv(path=None):
    found = shutil.which("uv", path=path)
    if found:
        return found
    for p in UV_PLACES:
        p = os.path.expanduser(p)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def argv(python, path=None):
    """The command that installs into `python`, or None when nothing can."""
    py = os.path.expanduser(str(python))
    if has_pip(py):
        return [py, "-m", "pip"]
    uv = find_uv(path)
    return ["env", f"UV_PYTHON={py}", uv, "pip"] if uv else None


def install_args(cmd, args):
    """uv's pip refuses pip's own flags; leave them off when cmd is uv's."""
    if cmd and len(cmd) == 4 and cmd[0] == "env" and os.path.basename(cmd[2]) == "uv":
        return [a for a in args if a not in PIP_ONLY_FLAGS]
    return list(args)


def why_not(python):
    return (f"{python} is a venv without pip and uv isn't installed: add pip to it "
            f"({python} -m ensurepip) or install uv")


def shell(python, path=None):
    """argv() as one shell string, for doctor's printed and auto recipes ("" when none)."""
    cmd = argv(python, path)
    return shlex.join(cmd) if cmd else ""
