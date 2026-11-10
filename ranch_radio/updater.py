"""Keeping a host on the latest code, safely: what `ranch-radio update` adds to install.

    status(root)              where the checkout stands against what it tracks (fetches first)
    pull(root) / back(root)   fast-forward; move back to the commit before it
    requirements(root, h)     are requirements.txt's packages there for the host's Python?
                              installs them when that Python is this checkout's own .venv
    wait_serving(h, keys)     did every server that was serving before come back?
    load_state / save_state   ~/.local/state/ranch-radio/update.json
    journal_files / _agents   what an update is about to write and restart (update-journal.json)
    restore_files(home, j)    a rollback: the replaced files back byte for byte, added ones aside
    notify(title, text)       a desktop notification, best effort

Why each exists (2026-10-28): both radio hosts sat 8 commits behind main with
nothing saying so; a dry run pulled the code under the running servers; a
raised floor in requirements.txt (aiohttp>=3.14.4) never reached the Python
that runs the radios; a server that crashed on new code still counted as
restarted (launchd had loaded it); a radio skipped because it was
transmitting kept the old code until someone remembered.

RANCH_RADIO_PIP: a stand-in for `python -m pip` (tests).
RANCH_RADIO_NOTIFY: a stand-in for the notification command (tests); "" is off.
RANCH_RADIO_HEALTH_WAIT: seconds to wait for a restarted server (default 60).
"""

import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import service

STATE = ".local/state/ranch-radio/update.json"


# ---------------------------------------------------------------- git

def git(root, *args, timeout=120):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                           timeout=timeout, env=env)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, "", f"{type(e).__name__}: {e}"


@dataclass
class Status:
    head: str = ""
    upstream: str = ""            # the tracked branch's commit
    tracking: str = ""            # its name, e.g. origin/main
    behind: int = 0
    ahead: int = 0
    commits: list = field(default_factory=list)   # "abc1234 subject", newest first
    error: str = ""


def status(root, fetch=True):
    s = Status()
    if fetch:
        rc, _, err = git(root, "fetch", "-q")
        if rc:
            s.error = f"git fetch failed: {err}"
            return s
    rc, s.head, err = git(root, "rev-parse", "HEAD")
    if rc:
        s.error = f"not a git checkout: {err}"
        return s
    rc, s.tracking, _ = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if rc:
        s.error = "the checkout tracks no branch (detached, or no upstream): update can't tell what's new"
        return s
    _, s.upstream, _ = git(root, "rev-parse", "@{u}")
    rc, counts, _ = git(root, "rev-list", "--left-right", "--count", "HEAD...@{u}")
    if rc == 0 and counts:
        s.ahead, s.behind = (int(x) for x in counts.split())
    if s.behind:
        _, log, _ = git(root, "log", "--format=%h %s", "-n", "20", "HEAD..@{u}")
        s.commits = log.splitlines()
    return s


def pull(root):
    """Fast-forward to the upstream. (ok, error)."""
    rc, out, err = git(root, "pull", "--ff-only", "-q")
    return rc == 0, (err or out)


def back(root, rev):
    """Move the branch back to rev, keeping any local edits (reset --keep
    refuses rather than overwrite one). (ok, error)."""
    rc, out, err = git(root, "reset", "--keep", "-q", rev)
    return rc == 0, (err or out)


def before_pull(root):
    """The commit a fast-forward pull came from (git's ORIG_HEAD), when that
    is plainly what it is: an ancestor of HEAD, and not HEAD. For an update
    pulled by an older update that didn't pass --from. "" when unsure."""
    rc, last, _ = git(root, "reflog", "-1", "--date=unix", "--format=%gd %gs", "HEAD")
    try:
        when, what = last.split(" ", 1)[0], last.split(" ", 1)[1]
        recent = time.time() - int(when.split("{", 1)[1].rstrip("}")) < 900
    except (IndexError, ValueError):
        return ""
    if not (recent and what.startswith("pull")):
        return ""                                  # HEAD's last move wasn't a pull just now
    rc, orig, _ = git(root, "rev-parse", "-q", "--verify", "ORIG_HEAD")
    if rc or not orig or orig == head(root):
        return ""
    rc, _, _ = git(root, "merge-base", "--is-ancestor", orig, "HEAD")
    return orig if rc == 0 else ""


def head(root):
    return git(root, "rev-parse", "HEAD")[1]


def short(rev):
    return (rev or "")[:7]


# ---------------------------------------------------------------- requirements

# Run under the host's Python: which of requirements.txt's packages are
# missing or too old for it. Stdlib only (the system Python may have no pip).
# Understands what requirements.txt uses: name[extras] (op version, ...)
# with an optional `; sys_platform ==/!= "x"` marker. A line it can't read is
# reported as unreadable, never as satisfied.
CHECK = r"""
import json, re, sys
from importlib import metadata
def ver(v):
    out = []
    for p in v.split("."):
        m = re.match(r"\d+", p)
        out.append(int(m.group()) if m else 0)
        if not m: break
    return tuple(out)
OPS = {">=": lambda a, b: a >= b, "==": lambda a, b: a == b, ">": lambda a, b: a > b,
       "<=": lambda a, b: a <= b, "<": lambda a, b: a < b, "!=": lambda a, b: a != b,
       "~=": lambda a, b: a >= b and a[:max(len(b) - 1, 1)] == b[:max(len(b) - 1, 1)]}
bad = []
for line in open(sys.argv[1]):
    line = line.split("#", 1)[0].strip()
    if not line:
        continue
    req, _, marker = line.partition(";")
    m = re.fullmatch(r'\s*sys_platform\s*(==|!=)\s*["\']([^"\']+)["\']\s*', marker) if marker.strip() else None
    if marker.strip() and not m:
        bad.append(f"{line} (marker not understood)"); continue
    if m and (sys.platform == m.group(2)) != (m.group(1) == "=="):
        continue
    m = re.fullmatch(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)", req)
    if not m:
        bad.append(f"{line} (not understood)"); continue
    name, specs = m.group(1), [s.strip() for s in m.group(3).split(",") if s.strip()]
    try:
        have = metadata.version(name)
    except metadata.PackageNotFoundError:
        bad.append(f"{req.strip()} (not installed)"); continue
    for s in specs:
        sm = re.fullmatch(r"(>=|==|>|<=|<|!=|~=)\s*([0-9][0-9A-Za-z.]*)", s)
        if not sm:
            bad.append(f"{req.strip()} (specifier {s!r} not understood)"); break
        if not OPS[sm.group(1)](ver(have), ver(sm.group(2))):
            bad.append(f"{req.strip()} (have {have})"); break
print(json.dumps(bad))
"""


def _python(h):
    return os.path.expanduser(h["python"])


def unmet(root, h):
    """Requirements the host's Python doesn't meet ([] when all are met)."""
    req = Path(root) / "requirements.txt"
    if not req.exists():
        return []
    try:
        r = subprocess.run([_python(h), "-c", CHECK, str(req)], capture_output=True, text=True, timeout=60)
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError) as e:
        return [f"couldn't check with {_python(h)}: {type(e).__name__}"]


def own_venv(root, h):
    """Is the host's Python this checkout's .venv (the only one update pips into)?"""
    venv = (Path(root) / ".venv").resolve()
    try:
        py = Path(_python(h)).absolute()
        return py.parent.parent.resolve() == venv or venv in py.resolve().parents
    except OSError:
        return False


def requirements(root, h, log=print):
    """Make sure the host's Python has what requirements.txt asks. (ok, why)."""
    missing = unmet(root, h)
    if not missing:
        return True, ""
    if not own_venv(root, h) and not os.environ.get("RANCH_RADIO_PIP"):
        return False, (f"{_python(h)} lacks {'; '.join(missing)}. It is not this checkout's .venv, so update "
                       f"won't pip into it: install those for it (on Linux, the system package manager), "
                       f"then run the update again")
    log(f"packages: installing {'; '.join(missing)}")
    pip = shlex.split(os.environ["RANCH_RADIO_PIP"]) if os.environ.get("RANCH_RADIO_PIP") else \
        [_python(h), "-m", "pip"]
    try:
        r = subprocess.run(pip + ["install", "-q", "--disable-pip-version-check", "-r",
                                  str(Path(root) / "requirements.txt")],
                           capture_output=True, text=True, timeout=900)
        err = (r.stderr or r.stdout).strip()[-600:]
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"pip failed: {type(e).__name__}: {e}"
    if r.returncode:
        return False, f"pip failed: {err}"
    still = unmet(root, h)
    if still:
        return False, f"after pip, still missing: {'; '.join(still)}"
    return True, ""


# ---------------------------------------------------------------- health

def serving(h):
    """{key: up} for every server the host runs."""
    return {k: bool(service.probe(h, r).get("up")) for k, r in service.servers(h).items()}


def health_wait():
    try:
        return float(os.environ.get("RANCH_RADIO_HEALTH_WAIT", "60"))
    except ValueError:
        return 60.0


def wait_serving(h, keys, wait=None, settle=None):
    """The keys (of servers(h)) not serving after up to wait seconds. A server
    must answer twice, settle seconds apart: one that binds and then dies on
    its first import doesn't pass."""
    wait = health_wait() if wait is None else wait
    settle = min(5.0, wait / 4) if settle is None else settle
    srv = service.servers(h)
    keys = [k for k in keys if k in srv]
    end = time.monotonic() + wait
    left = list(keys)
    while left and time.monotonic() < end:
        left = [k for k in left if not service.probe(h, srv[k]).get("up")]
        if left:
            time.sleep(1)
    if left:
        return left
    time.sleep(settle)
    return [k for k in keys if not service.probe(h, srv[k]).get("up")]


# ---------------------------------------------------------------- state

def state_path(home):
    return Path(home) / STATE


def load_state(home, log=print):
    """{"pending": [module ids to restart when idle], "failed": {rev: why},
    "notified": rev, "running": the commit the servers were last restarted on}. A state file that can't be read is said so, and reads
    as empty: the worst case is retrying a commit that failed here, which
    fails its health check again and is rolled back again."""
    blank = {"pending": [], "failed": {}, "notified": "", "running": ""}
    p = state_path(home)
    if not p.exists():
        return blank
    try:
        st = json.loads(p.read_text())
        if not isinstance(st, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as e:
        log(f"warning: {p}: unreadable ({e}); starting from an empty update state")
        return blank
    return {"pending": list(st.get("pending") or []), "failed": dict(st.get("failed") or {}),
            "notified": str(st.get("notified") or ""), "running": str(st.get("running") or "")}


def save_state(home, st):
    p = state_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(st, indent=2, sort_keys=True) + "\n")
    tmp.replace(p)


# ---------------------------------------------------------------- what an update changed, to undo it

JOURNAL = ".local/state/ranch-radio/update-journal.json"


def journal_path(home):
    return Path(home) / JOURNAL


def load_journal(home, rev, log=print):
    """The journal of the update to rev: {"rev", "files": [{"path", "backup"}],
    "agents": [...]}; None when there is none for rev (the update never got
    as far as writing a file or restarting a server)."""
    p = journal_path(home)
    if not p.exists():
        return None
    try:
        j = json.loads(p.read_text())
        if not isinstance(j, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as e:
        log(f"warning: {p}: unreadable ({e}); the rollback can't put files back")
        return None
    if j.get("rev") != rev:
        return None
    return {"rev": rev, "files": list(j.get("files") or []), "agents": list(j.get("agents") or [])}


def _save_journal(home, j):
    p = journal_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(j, indent=2, sort_keys=True) + "\n")
    tmp.replace(p)


def journal_files(home, rev, entries):
    """Before the update to rev writes its files: [(path relative to home,
    where its old file goes, "" when there is none)]. Written first, so an
    update that dies halfway can still be undone. A retry of the same rev
    keeps the first backup of each file: that one is what ran before."""
    j = load_journal(home, rev, log=lambda *_: None) or {"rev": rev, "files": [], "agents": []}
    have = {f["path"] for f in j["files"]}
    j["files"] += [{"path": path, "backup": backup} for path, backup in entries if path not in have]
    _save_journal(home, j)


def journal_agents(home, rev, agents):
    """Before the update to rev restarts anything: the agents (Mac) or units
    (Linux) it may restart or start, as service.touched() lists them."""
    j = load_journal(home, rev, log=lambda *_: None) or {"rev": rev, "files": [], "agents": []}
    key = lambda e: e.get("label") or e.get("unit")
    have = {key(e) for e in j["agents"]}
    j["agents"] += [e for e in agents if key(e) not in have]
    _save_journal(home, j)


def clear_journal(home, keep_in=None):
    """The update landed (or was undone): drop its journal, or keep it in
    keep_in beside the files it describes."""
    p = journal_path(home)
    if not p.exists():
        return
    if keep_in:
        Path(keep_in).mkdir(parents=True, exist_ok=True)
        p.rename(Path(keep_in) / "update-journal.json")
    else:
        p.unlink()


def _there(p):
    return p.is_symlink() or p.exists()


def restore_files(home, j, aside):
    """Put every file the update replaced back as it was, byte for byte (a
    symlink as the link it was), and move every file it added into aside.
    Never re-renders: the host file may have been edited for the new code,
    and the old code's reading of it is not what ran before (2026-11-10).
    Returns (restored, removed): paths relative to home."""
    home, aside = Path(home), Path(aside)
    restored, removed = [], []

    def move_aside(p, rel):
        a = aside / rel
        a.parent.mkdir(parents=True, exist_ok=True)
        p.rename(a)

    for f in j["files"]:
        rel = f["path"]
        p = home / rel
        if f.get("backup"):
            b = Path(f["backup"])
            if not _there(b):
                continue          # the update died before moving it: p is still the old file
            if _there(p):
                move_aside(p, rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            b.rename(p)
            restored.append(rel)
        elif _there(p):
            move_aside(p, rel)
            removed.append(rel)
    return restored, removed


def lock(home):
    """Hold the update lock for this process and the updates it runs (the
    re-exec under new code, the rollback): None when another update holds it.
    The descriptor is inherited, and RANCH_RADIO_UPDATE_LOCK tells a child
    it already holds it."""
    import fcntl
    if os.environ.get("RANCH_RADIO_UPDATE_LOCK"):
        return int(os.environ["RANCH_RADIO_UPDATE_LOCK"])
    p = state_path(home).with_name("update.lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.set_inheritable(fd, True)
    os.environ["RANCH_RADIO_UPDATE_LOCK"] = str(fd)
    return fd


# ---------------------------------------------------------------- telling someone

def notify(title, text):
    """A desktop notification. Never raises; a host with no desktop just logs."""
    stand_in = os.environ.get("RANCH_RADIO_NOTIFY")
    if stand_in is not None:
        argv = shlex.split(stand_in) + [title, text] if stand_in else None
    elif sys.platform == "darwin":
        script = f"display notification {json.dumps(text, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)}"
        argv = ["osascript", "-e", script]
    else:
        argv = ["notify-send", "--app-name=Ranch Radio", title, text]
    if not argv:
        return
    try:
        subprocess.run(argv, capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass
