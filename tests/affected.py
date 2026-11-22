#!/usr/bin/env python3
"""Which test runs a change touches, from the import graph and the file names
the tests mention. Nothing here runs a test unless --run.

    python3 tests/affected.py                       changed vs main: committed, staged, unstaged, untracked
    python3 tests/affected.py --base origin/main
    python3 tests/affected.py addons/logbook.py     these files, whatever git says
    python3 tests/affected.py --why                 each run with the file that pulled it in
    python3 tests/affected.py --run                 RR_ONLY=<runs> bash tests/run_all.sh

A run is picked when a changed file is
  - imported by its test, directly or through anything that test imports (ast);
  - named as a path in the test's source, or a tests/ helper's ("server.py",
    "tests/fake_civ.py": the servers, bridges and fakes a test spawns); a path
    in a source file's text (the catalog's prose) is documentation, not an edge;
  - the module the test is named after (foo.py -> foo_test, foo_*_test;
    static/addons/foo.js -> the same): the module with the LONGEST stem the
    test's name starts with, so station_id_test is station_id.py's alone and
    a change to station.py or static/station.css does not pick it by name
    (station_page_test, with no station_page module, is still station's);
  - under static/ (a page asset): every *_page_test, plus the above.
A change to the harness itself (tests/run_all.sh, tests/ci/, requirements.txt,
constraints.txt, install.sh, .github/) picks every run, except the CI split's own files
(tests/ci/shard.py, tests/ci/durations.json): they decide which shard a run
lands on, never whether it passes, so they pick ci_shard_test alone. Deleted
files count as changed.

    import affected; affected.select(["civ.py"]) -> {"civ_safety_test": "civ.py", ...}
"""
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ("tests/run_all.sh", "tests/ci/", "requirements.txt", "constraints.txt", "install.sh", ".github/")
# 2026-11-20: a durations refresh picked all 136 runs through the
# tests/ci/ prefix. The split cannot change a result; ci_shard_test proves it
# still places every run exactly once.
SPLIT_ONLY = {"tests/ci/shard.py": "ci_shard_test", "tests/ci/durations.json": "ci_shard_test"}
PACKAGES = ("addons", "ranch_radio")
PATH_TOKEN = re.compile(r"[\w./-]+\.(?:py|js|css|html|sh|json|csv|bin|c|swift|qml|conf|rules|plist)\b")


def rel(p):
    return str(Path(p)).replace(os.sep, "/")


# ---------------------------------------------------------------- the runs

def runs():
    """[(name, test path)] in tests/run_all.sh order, from the runner itself."""
    text = (ROOT / "tests/run_all.sh").read_text()
    out = [(p.stem, rel(p.relative_to(ROOT))) for p in sorted((ROOT / "tests").glob("*_test.py"))]
    for m in re.finditer(r"^run\s+(\S+)\s+\S+\s+(tests/\S+)", text, re.M):
        out.append((m.group(1), m.group(2)))
    return out


# ---------------------------------------------------------------- the graph

def _index():
    """module name -> file, for every Python file a test could import."""
    idx = {}
    for p in ROOT.glob("*.py"):
        idx[p.stem] = rel(p.relative_to(ROOT))
    for pkg in PACKAGES:
        for p in (ROOT / pkg).glob("*.py"):
            idx[pkg if p.stem == "__init__" else f"{pkg}.{p.stem}"] = rel(p.relative_to(ROOT))
    for p in (ROOT / "tests").glob("*.py"):           # tests import helpers as top-level names
        idx.setdefault(p.stem, rel(p.relative_to(ROOT)))
    for p in (ROOT / "tests/ci").glob("*.py"):
        idx.setdefault(f"ci.{p.stem}", rel(p.relative_to(ROOT)))
    return idx


def _imports(path, idx):
    """Files this Python file imports, by name, among idx."""
    try:
        tree = ast.parse((ROOT / path).read_text(), filename=path)
    except (SyntaxError, UnicodeDecodeError, OSError):
        return set()
    pkg = path.split("/")[0] if "/" in path and path.split("/")[0] in PACKAGES else ""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:                              # from . import x / from .render import y
                base = pkg if pkg else ""
                mod = f"{base}.{node.module}" if node.module and base else (node.module or base)
            else:
                mod = node.module or ""
            if mod:
                names.add(mod)
            names.update(f"{mod}.{a.name}" if mod else a.name for a in node.names)   # a submodule, maybe
    return {idx[n] for n in names if n in idx}


def _literals(path):
    """Repo files this file names as a path string."""
    try:
        text = (ROOT / path).read_text()
    except (UnicodeDecodeError, OSError):
        return set()
    out = set()
    for tok in set(PATH_TOKEN.findall(text)):
        tok = tok.lstrip("./")
        for cand in (tok, f"tests/{tok}"):
            cand = rel(cand)
            if (ROOT / cand).is_file() and cand != path and not _is_test_or_harness(cand):
                out.add(cand)
    return out


def _is_test_or_harness(path):
    """A test naming another test ("see x_test.py") or the runner is prose, not a
    dependency; only source files, fakes and fixtures are."""
    return path.endswith("_test.py") or path in ("tests/run_all.sh", "tests/flake_hunt.sh", "tests/affected.py") \
        or path.startswith("tests/ci/")


_EDGES = {}


def edges(path, idx):
    """What path reaches in one step (cached: the graph is read once per process)."""
    if path not in _EDGES:
        e = _imports(path, idx) if path.endswith(".py") else set()
        if path.startswith("tests/"):
            e |= _literals(path)
        _EDGES[path] = e
    return _EDGES[path]


def deps(path, idx, memo):
    """Everything path pulls in, transitively: imports and named files.
    memo: {path: {dep: the file it was reached through}}, for trace()."""
    if path in memo:
        return memo[path]
    via = {path: None}
    stack = [path]
    while stack:
        p = stack.pop()
        for d in edges(p, idx):
            if d not in via:
                via[d] = p
                stack.append(d)
    memo[path] = via
    return via


def trace(test, target, idx=None, memo=None):
    """The chain of files from test to target, or [] when it doesn't reach it."""
    via = deps(test, idx or _index(), memo if memo is not None else {})
    if target not in via:
        return []
    chain, p = [], target
    while p is not None:
        chain.append(p)
        p = via[p]
    return chain[::-1]


# ---------------------------------------------------------------- selection

def _stem(path):
    """The name a file's tests start with: addons/uvpro.py, static/addons/uvpro.js -> uvpro."""
    p = Path(path)
    if p.parts[0] in ("static", "addons", "ranch_radio") or len(p.parts) == 1:
        return p.stem
    return None


def _module_stems():
    """Every stem a module file gives (_stem): top-level *.py, addons/, ranch_radio/, static/."""
    paths = list(ROOT.glob("*.py")) + [p for pkg in PACKAGES for p in (ROOT / pkg).glob("*.py")]
    paths += [p for p in (ROOT / "static").rglob("*") if p.is_file()]
    return {p.stem for p in paths}


def _owner(name, stems):
    """The module a run is named after: the LONGEST stem its name starts with.
    station_id_test is station_id.py's, not station.py's (2026-11-19: the
    shorter stem also claimed it, so a station.css change ran station_id_test)."""
    hits = [s for s in stems if name == f"{s}_test" or name.startswith(f"{s}_")]
    return max(hits, key=len, default=None)


def select(changed):
    """{run name: the changed file that picked it} ("*" when the harness changed)."""
    changed = [rel(c) for c in changed]
    all_runs = runs()
    for c in changed:
        if c not in SPLIT_ONLY and any(c == h or c.startswith(h) for h in HARNESS):
            return {name: c for name, _ in all_runs}
    idx = _index()
    memo = {}
    picked = {SPLIT_ONLY[c]: c for c in changed if c in SPLIT_ONLY}
    page_tests = [name for name, _ in all_runs if name.endswith("_page_test")]
    # A changed module that is gone (or new) still names its tests.
    stems = _module_stems() | {s for s in map(_stem, changed) if s}
    owner = {name: _owner(name, stems) for name, _ in all_runs}
    for c in changed:
        stem = _stem(c)
        for name, test in all_runs:
            if name in picked:
                continue
            if c in deps(test, idx, memo):
                picked[name] = c
            elif stem and owner[name] == stem:
                picked[name] = c
            elif c.startswith("static/") and name in page_tests:
                picked[name] = c
    return {name: picked[name] for name, _ in all_runs if name in picked}


def changed_files(base):
    def git(*a):
        return subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True).stdout.split("\n")
    files = set()
    merge_base = git("merge-base", base, "HEAD")[0].strip()
    if merge_base:
        files.update(git("diff", "--name-only", merge_base, "HEAD"))
    files.update(git("diff", "--name-only", "HEAD"))
    files.update(git("ls-files", "--others", "--exclude-standard"))
    return sorted(f for f in files if f)


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("files", nargs="*", help="changed files (default: what git says changed vs --base)")
    p.add_argument("--base", default="main")
    p.add_argument("--why", action="store_true", help="show the file that picked each run")
    p.add_argument("--trace", metavar="RUN", help="the chain of files from that run's test to each changed file")
    p.add_argument("--run", action="store_true", help="run them with tests/run_all.sh")
    a = p.parse_args(argv)
    files = a.files or changed_files(a.base)
    if not files:
        print("nothing changed", file=sys.stderr)
        return 0
    if a.trace:
        test = dict(runs()).get(a.trace)
        if not test:
            print(f"no run {a.trace!r}", file=sys.stderr)
            return 2
        for f in files:
            chain = trace(test, rel(f))
            print(" -> ".join(chain) if chain else f"{test} does not reach {f}")
        return 0
    picked = select(files)
    if not picked:
        print(f"no test runs touch: {' '.join(files)}", file=sys.stderr)
        return 0
    everything = len(picked) == len(runs())
    for name, why in picked.items():
        print(f"{name}  <- {why}" if a.why else name)
    if a.run:
        env = dict(os.environ)
        if not everything:
            env["RR_ONLY"] = " ".join(picked)
        print(f"-- running {'everything' if everything else str(len(picked)) + ' run(s)'}", file=sys.stderr)
        return subprocess.run(["bash", "tests/run_all.sh"], cwd=ROOT, env=env).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
