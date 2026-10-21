"""Check a host file against what its modules need.

    static(h)  -> [Check]   from the host file alone (any machine)
    machine(h) -> [Check]   on this machine: python, binaries, devices, files

Read-only: it runs the host's own pythons only to try an import, looks files
up, and never opens a radio, a port or a key file's contents.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass

import rigs

from .manifest import ASK, CORE, MANIFESTS, asked, template

OK, WARN, MISSING = "ok", "warn", "missing"


@dataclass
class Check:
    module: str
    status: str          # ok | warn | missing
    what: str
    fix: str = ""
    recipe: str = ""     # a shell command `doctor --fix` may run (never as root)


def on_modules(h):
    """Switchable modules present in the host file."""
    out = []
    for name, m in MANIFESTS.items():
        cur = h
        for k in m["block"]:
            cur = cur.get(k) if isinstance(cur, dict) else None
        if cur is not None:
            out.append(name)
    return out


def block_of(h, name):
    cur = h
    for k in MANIFESTS[name]["block"]:
        cur = (cur or {}).get(k)
    return cur


# ---------------------------------------------------------------- static

def static(h):
    out = []
    for r in h["radios"]:
        rig = r.get("rig", "ic7300")
        p = rigs.PROFILES.get(rig)
        if p is None:
            out.append(Check(r["id"], MISSING, f"rig {rig!r} has no profile",
                             f"pick one of: {', '.join(rigs.PROFILES)}"))
        elif p.get("tested"):
            out.append(Check(r["id"], OK, f"{p['name']}: profile tested on real hardware"))
        else:
            out.append(Check(r["id"], WARN, f"{p['name']}: profile built from the references, "
                                             "not yet run on a real radio"))
    present = on_modules(h)
    for name in present:
        m = MANIFESTS[name]
        if h["os"] not in m["os"]:
            out.append(Check(name, MISSING, f"not shipped on {h['os']} (only {', '.join(m['os'])})"))
        for dep in m.get("requires", []):
            if dep not in present:
                out.append(Check(name, MISSING, f"needs the {dep} module", f"ranch-radio add {dep}"))
        block = block_of(h, name)
        if isinstance(block, dict):
            for f in asked(block):
                out.append(Check(name, MISSING, f"{f} is still {ASK}", (m.get("ask") or {}).get(f, "")))
        for field in m.get("radios", {}):
            for r in h["radios"]:
                if not r.get(field):
                    out.append(Check(name, MISSING, f"radio {r['id']} has no {field!r}",
                                     f"set it in hosts/{h['host']}.json"))
    return out


# ---------------------------------------------------------------- machine

def _expand(p):
    return os.path.expanduser(str(p))


def _which(p, path=None):
    p = _expand(p)
    if "/" in p:
        return p if os.path.isfile(p) and os.access(p, os.X_OK) else None
    return shutil.which(p, path=path)


def search_path(h):
    """The PATH the host's services run with: a Mac agent's own "path" when
    the host file sets one (launchd doesn't read the shell's), else ours."""
    for r in h["radios"]:
        if r.get("path"):
            return r["path"]
    return None


def _imports(python, mod):
    """(ok, detail): can python import mod?"""
    py = _which(python)
    if not py:
        return False, f"{python} not found"
    try:
        r = subprocess.run([py, "-I", "-c", f"import {mod}"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"{type(e).__name__}"
    if r.returncode == 0:
        return True, ""
    last = (r.stderr.strip().splitlines() or ["import failed"])[-1]
    return False, last


def _value(block, key, default):
    v = (block or {}).get(key, default) if key else default
    if isinstance(v, dict):               # a Mac's ardopcf block names its binary
        v = v.get("bin", default)
    return v


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _recipe(n, host_os, ctx):
    auto = n.get("auto")
    if isinstance(auto, dict):
        auto = auto.get(host_os)
    if not auto:
        return ""
    try:
        return auto.format(repo=REPO, **ctx)
    except (KeyError, IndexError):
        return ""


def _need(name, n, block, host_python, path=None, host_os=None):
    level = WARN if n.get("level") == "warn" else MISSING
    fix = n.get("fix", "")
    if "bin" in n:
        exe = _value(block, n.get("key"), n["default"])
        found = _which(exe, path)
        full = _expand(exe) if "/" in str(exe) else os.path.expanduser(f"~/.local/bin/{exe}")
        recipe = _recipe(n, host_os, {"path": full, "dir": os.path.dirname(full), "python": host_python,
                                      "venv": "", "host_python": _expand(host_python)})
        return [Check(name, OK, f"{n['bin']}: {found}")] if found else \
            [Check(name, level, f"{n['bin']} not found ({exe})", fix, recipe)]
    if "pymod" in n:
        py = _value(block, n.get("key"), n.get("default", host_python))
        ok, why = _imports(py, n["pymod"])
        venv = os.path.dirname(os.path.dirname(_expand(py)))
        recipe = _recipe(n, host_os, {"python": _expand(py), "venv": venv, "path": _expand(py),
                                      "dir": venv, "host_python": _expand(host_python)})
        return [Check(name, OK, f"{n['pymod']} imports in {py}")] if ok else \
            [Check(name, level, f"{n['pymod']} does not import in {py}: {why}", fix, recipe)]
    if "file" in n or "files" in n:
        if "files" in n:
            paths = [str(k).partition("=")[2] or k for k in (block or {}).get(n["files"], [])]
        else:
            paths = [_value(block, n.get("key"), n["file"])]
        out = []
        for p in paths:
            out.append(Check(name, OK, f"{p} present") if os.path.exists(_expand(p))
                       else Check(name, level, f"{p} not present", fix))
        return out
    return []


UNCHECKED = "unchecked"


def machine(h, budget_s=None, include_available=False):
    """Every machine check for h, in order. budget_s caps the whole run: each
    check can take up to 30 s (a venv's python), so once the budget is spent the
    rest are reported as not checked rather than run (plan G6).
    include_available: also check what each module not on yet would need here
    (its template's defaults), for the page's Available rows."""
    import time
    deadline = time.monotonic() + budget_s if budget_s else None
    out = []

    def late(module, what):
        if deadline is not None and time.monotonic() > deadline:
            out.append(Check(module, UNCHECKED, f"{what}: not checked (out of time; Refresh to try again)"))
            return True
        return False

    py = h["python"]
    want = CORE["python"]
    found = _which(py)
    if not found:
        out.append(Check("core", MISSING, f"python {py} not found", "set \"python\" in the host file"))
    else:
        try:
            r = subprocess.run([found, "-I", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                               capture_output=True, text=True, timeout=30)
            ver = r.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            ver = ""
        try:
            good = tuple(int(x) for x in ver.split(".")) >= want
        except ValueError:
            good = False
        out.append(Check("core", OK if good else MISSING, f"python {ver or '?'} ({found})",
                         "" if good else f"needs Python {want[0]}.{want[1]}+"))
        for mod in CORE["pymods"]:
            if late("core", mod):
                continue
            ok, why = _imports(py, mod)
            in_venv = os.path.exists(os.path.join(os.path.dirname(os.path.dirname(_expand(py))), "pyvenv.cfg"))
            out.append(Check("core", OK, f"{mod} imports") if ok else
                       Check("core", MISSING, f"{mod} does not import: {why}", f"{py} -m pip install {mod}",
                             f"{_expand(py)} -m pip install -q {mod}" if in_venv else ""))
    sp = search_path(h)
    core_needs = list(CORE["audio"][h["os"]])
    if any(r.get("https") for r in h["radios"]) or (h.get("station") or {}).get("https"):
        core_needs.append(CORE["https"])
    for n in core_needs:
        if not late("core", n.get("bin") or n.get("pymod")):
            out += _need("core", n, None, py, sp, h["os"])
    for r in h["radios"]:
        dev = r["device"]
        if os.path.exists(dev) and not os.access(dev, os.R_OK | os.W_OK):
            try:
                import grp
                group = grp.getgrgid(os.stat(dev).st_gid).gr_name
            except (KeyError, OSError):
                group = "dialout"
            out.append(Check(r["id"], MISSING, f"{dev} present, but you can't open it",
                             f"sudo usermod -aG {group} $USER, then log out and back in"))
        elif os.path.exists(dev):
            out.append(Check(r["id"], OK, f"{dev} present"))
        else:
            out.append(Check(r["id"], WARN, f"{dev} not present (radio unplugged or off?)",
                             "plug the radio in, or fix \"device\""))
    present = on_modules(h)
    names = [n for n in MANIFESTS if n in present or include_available]
    for name in names:
        if h["os"] not in MANIFESTS[name]["os"]:
            continue
        block = block_of(h, name) if name in present else template(name, h["os"], present)
        for n in MANIFESTS[name]["needs"]:
            if n.get("os") and n["os"] != h["os"]:
                continue
            if name not in present and "files" in n:
                continue                    # nothing to look for until the operator names them
            if not late(name, n.get("bin") or n.get("pymod") or n.get("file") or n.get("files")):
                out += _need(name, n, block, py, sp, h["os"])
    return out


def module_checks(h, name, local):
    """The checks for one module only (add's report)."""
    checks = [c for c in static(h) if c.module == name]
    if local:
        block = block_of(h, name)
        for n in MANIFESTS[name]["needs"]:
            if n.get("os") and n["os"] != h["os"]:
                continue
            checks += _need(name, n, block, h["python"], search_path(h), h["os"])
    return checks


def fix(checks, log=print):
    """Run the recipe of every missing check that has one, in order, as the
    current user (never sudo). Returns [(check, rc)]. What has no recipe, or
    needs root, is left for the caller to print."""
    done, seen = [], set()
    for c in checks:
        if c.status != MISSING or not c.recipe or c.recipe in seen:
            continue
        seen.add(c.recipe)
        if "sudo " in c.recipe:
            continue
        log(f"fixing {c.module}: {c.what}\n  $ {c.recipe}")
        try:
            r = subprocess.run(["/bin/sh", "-c", c.recipe], timeout=1800)
            rc = r.returncode
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"  failed: {type(e).__name__}: {e}")
            rc = 1
        log("  done" if rc == 0 else f"  failed (exit {rc})")
        done.append((c, rc))
    return done
