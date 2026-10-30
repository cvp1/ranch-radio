"""ranch-radio: the command line. See ranch_radio/__init__.py."""

import argparse
import difflib
import functools
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import catalog, doctor, manifest, modules, paths, render, service, updater
from .render import Artifact, HostError, artifacts, load, same

ROOT = Path(__file__).resolve().parent.parent
HOSTS = ROOT / "hosts"
DEFAULT_STATE = "~/.local/state/radio-remote/addons.json"   # the add-on switches (addons/__init__.py)


def this_host():
    return socket.gethostname().split(".")[0].lower()


def host_files():
    return list(paths.all_hosts(HOSTS).values())


def host_path(name):
    """The host file for name (~/.config/ranch-radio/hosts, then the repo's hosts/)."""
    p = paths.find(HOSTS, name)
    if p is None:
        known = ", ".join(paths.all_hosts(HOSTS)) or "none"
        sys.exit(f"ranch-radio: no host file for {name!r} (known: {known}). "
                 f"On a new machine: ranch-radio init")
    return p


def load_host(name):
    p = host_path(name)
    try:
        return load(p)
    except (HostError, ValueError) as e:
        sys.exit(f"ranch-radio: {paths.shown(p)}: {e}")


def modules_of(h):
    """Catalog names a host runs, in catalog order."""
    on = {r.get("rig") or r["id"] for r in h["radios"]}
    on |= {k for k in ("station", "winlink", "mesh", "packet") if h.get(k)}
    on |= set(h.get("addons") or {}) | set(h.get("integrations") or {})
    if render.updates(h):
        on.add("updates")
    return [m["name"] for m in catalog.MODULES if m["name"] in on]


# ---------------------------------------------------------------- commands

def cmd_modules(a):
    for kind in catalog.KINDS:
        rows = [m for m in catalog.MODULES if m["kind"] == kind]
        print(f"\n{kind.upper()}")
        for m in rows:
            print(f"  {m['name']:<15} {m['title']}")
            print(f"  {'':<15} code:  {m['code']}")
            print(f"  {'':<15} ships: {m['ships']}")
            man = manifest.MANIFESTS.get(m["name"])
            if man:
                needs = [n.get("bin") or n.get("pymod") or n.get("file") or n.get("files") for n in man["needs"]]
                bits = [f"on {'/'.join(man['os'])}"] + ([f"needs {', '.join(needs)}"] if needs else []) \
                    + ([f"after {', '.join(man['requires'])}"] if man.get("requires") else [])
                print(f"  {'':<15} add:   ranch-radio add {m['name']}  ({'; '.join(bits)})")


def cmd_hosts(a):
    hosts = [load_host(f.stem) for f in host_files()]
    names = [m["name"] for m in catalog.MODULES if m["kind"] != "external"]
    runs = {h["host"]: set(modules_of(h)) for h in hosts}
    w = max(len(n) for n in names)
    print(" " * (w + 2) + "  ".join(f"{h['host']:<8}" for h in hosts))
    for n in names:
        if any(n in runs[h["host"]] for h in hosts):
            print(f"{n:<{w}}  " + "  ".join(f"{'yes' if n in runs[h['host']] else '-':<8}" for h in hosts))
    for h in hosts:
        if h.get("note"):
            print(f"\n{h['host']}: {h['note']}")


def cmd_render(a):
    h = load_host(a.host)
    out = Path(a.dir)
    for art in artifacts(h, home=a.home):
        p = out / art.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(art.text())
        print(f"{art.module:<13} {art.path}")


def _states(h, home):
    return render.installed_states(h, home)


def _guard(h, a):
    if a.home is None and h["host"] != this_host():
        sys.exit(f"ranch-radio: hosts/{h['host']}.json is not this machine ({this_host()}). "
                 f"Run it on {h['host']}, or pass --home DIR to work on a copy.")


def cmd_drift(a):
    h = load_host(a.host or this_host())
    _guard(h, a)
    home = Path(a.home or os.path.expanduser("~"))
    bad = 0
    for art, p, state in _states(h, home):
        bad += state != "same"
        print(f"{state:<8} {art.module:<13} {art.path}")
        if state == "differs" and a.diff:
            _diff(art, p)
    print(f"\n{h['host']}: " + ("installed files match hosts/" + h["host"] + ".json" if not bad
                              else f"{bad} file(s) differ from hosts/{h['host']}.json"))
    return 1 if bad else 0


def _diff(art: Artifact, p: Path):
    old = p.read_text(errors="replace").splitlines(keepends=True)
    new = art.text().splitlines(keepends=True)
    sys.stdout.writelines(difflib.unified_diff(old, new, f"installed {p}", f"rendered {art.path}"))


def write_installed(h, home, todo):
    """Write the files in todo [(art, path, state)]; old ones go to a backup
    tree, never beside the live ones (launchd and systemd scan those
    directories, and a stray copy could be loaded). Returns (changed, backups):
    the artifacts whose meaning changed (a linked unit rendering the same only
    becomes a file: a daemon-reload, not a restart)."""
    backups = Path(home) / ".local/state/ranch-radio/backups" / time.strftime("%Y-%m-%d_%H%M%S")
    changed = [art for art, p, s in todo
               if s != "linked" or not p.exists() or not same(art, p.read_bytes())]
    for art, p, s in todo:
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_symlink() or p.exists():
            # A unit linked from the repo (systemctl --user link) is a symlink:
            # move the link itself, never write through it into the repo.
            b = backups / art.path
            b.parent.mkdir(parents=True, exist_ok=True)
            p.rename(b)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(art.text())
        tmp.replace(p)
    return changed, backups


def cmd_install(a):
    h = load_host(a.host or this_host())
    _guard(h, a)
    home = Path(a.home or os.path.expanduser("~"))
    todo = [(art, p, s) for art, p, s in _states(h, home) if s != "same"]
    if not todo:
        print(f"{h['host']}: nothing to install; every file matches.")
        return 0
    for art, p, s in todo:
        print(f"{'write' if a.apply else 'would write'} ({s}) {p}")
        if s == "differs" or (s == "linked" and p.exists() and not same(art, p.read_bytes())):
            _diff(art, p)
    if not a.apply:
        print("\nDry run. Pass --apply to write these files (old ones kept as .bak-<time>).")
        return 0
    changed, backups = write_installed(h, home, todo)
    print(f"\nWritten; old files in {backups}. Nothing was restarted. To load the changes"
          f" (or: ranch-radio update {h['host']} --apply, which restarts only idle radios):")
    if h["os"] == "macos":
        # Over SSH the bootstrap fails ("5: Input/output error") after the
        # bootout has already stopped the agent (mac-shack, 2026-10-18).
        print("  # (outside the Mac's GUI session bootstrap fails; the load fallback then")
        print("  # starts it), or Stop/Start the server from its menu-bar widget:")
    for line in reload_commands(h, changed):
        print("  " + line)
    return 0


def reload_commands(h, arts):
    if h["os"] == "macos":
        labels = {art.content["Label"]: art.path for art in arts if art.fmt == "plist"}
        if any(art.path == render.MAC_ADDONS for art in arts):   # the server reads it at start
            for r in h["radios"]:
                labels.setdefault(r["label"], f"{render.AGENTS}/{r['label']}.plist")
        out = []
        for label, path in labels.items():
            # Quoted: these lines are meant to be pasted into a shell.
            # bootstrap fails outside the Mac's GUI session ("5: Input/output
            # error") after bootout has stopped the agent; the legacy load
            # works there, so it is the fallback (mac-shack, 2026-10-21).
            out += [f"launchctl bootout gui/$(id -u)/{shlex.quote(label)}",
                    f"launchctl bootstrap gui/$(id -u) ~/{shlex.quote(path)} "
                    f"|| launchctl load -w ~/{shlex.quote(path)}"]
        return out
    units = set()
    for art in arts:
        if art.path.endswith("station.json"):
            units.add("station.service")
        elif art.fmt == "systemd":
            name = art.path.split("/")[-1]
            units.add(art.path.split("/")[-2][:-2] if name.endswith(".conf") else name)
    manual = h.get("manual_restart") or {}
    out = ["systemctl --user daemon-reload"]
    if any(art.path == render.WIREPLUMBER_CONF for art in arts):
        # First, so the radio servers restarted below find the pinned names.
        # Every radio's audio drops for a moment: not mid-transmit.
        out.append("systemctl --user restart wireplumber   # radio audio drops briefly; not mid-transmit")
    for u in sorted(units):
        if u in manual:
            # Never printed as a command to paste: the file is written, and the
            # running service keeps its old definition until someone who can
            # reach the device restarts it.
            out.append(f"# not restarted: {u}: {manual[u]}")
        else:
            out.append(f"systemctl --user try-restart {shlex.quote(u)}")
    return out


# ---------------------------------------------------------------- add / remove / doctor

def _print_checks(checks):
    w = max((len(c.module) for c in checks), default=4)
    for c in checks:
        print(f"{c.status:<8} {c.module:<{w}}  {c.what}")
        if c.fix and c.status != doctor.OK:
            print(f"{'':<8} {'':<{w}}  -> {c.fix}")


def cmd_doctor(a):
    name = a.host or this_host()
    h = load_host(name)
    checks = doctor.static(h)
    local = h["host"] == this_host()
    if local:
        checks += doctor.machine(h)
    if getattr(a, "fix", False):
        if not local:
            sys.exit(f"ranch-radio: --fix runs on {h['host']} itself")
        done = doctor.fix(checks)
        if done:
            print("\nChecking again:\n")
            checks = doctor.static(h) + doctor.machine(h)
    _print_checks(checks)
    bad = sum(c.status == doctor.MISSING for c in checks)
    warn = sum(c.status == doctor.WARN for c in checks)
    tail = "" if local else f" (host file only: run doctor on {h['host']} to check the machine)"
    print(f"\n{h['host']}: " + (f"{bad} missing, {warn} to know about" if bad or warn
                              else "everything it runs is in place") + tail)
    return 1 if bad else 0


def _module_or_exit(name):
    if name not in manifest.MANIFESTS:
        hint = ""
        if catalog.by_name().get(name, {}).get("kind") == "radio":
            hint = " Radios are not added this way: give each its own entry under \"radios\"."
        sys.exit(f"ranch-radio: {name!r} is not a module you can add or remove "
                 f"(these are: {', '.join(manifest.MANIFESTS)}).{hint}")
    return manifest.MANIFESTS[name]


def _show_plan(plan, path, apply_it, hname):
    """Print a plan's diff; with --apply write it (the one lock, sha-checked)."""
    sys.stdout.write(plan.diff)
    if not apply_it:
        print("\nDry run. Pass --apply to write the host file.")
        return False
    local = hname == this_host()
    why = modules.apply(path, plan, DEFAULT_STATE if local else None)
    if why:
        sys.exit(f"ranch-radio: hosts/{hname}.json: {why}")
    return True


def cmd_add(a):
    _module_or_exit(a.module)
    hname = a.host or this_host()
    load_host(hname)                                  # exits with the reason if it doesn't load
    path = host_path(hname)
    sets = []
    for s in a.set or []:
        key, eq, val = s.partition("=")
        if not eq:
            sys.exit(f"ranch-radio: --set {s!r}: use FIELD=VALUE")
        sets.append((key, val))
    plan = modules.plan_add(path.read_text(), a.module, sets, source="cli")
    if plan.refusal:
        sys.exit(f"ranch-radio: {plan.refusal}.")
    if plan.errors:
        for key, why in plan.errors.items():
            print(f"ranch-radio: --set {key}: {why}; edit hosts/{hname}.json by hand for other fields",
                  file=sys.stderr)
        sys.exit(f"ranch-radio: --set {next(iter(plan.errors))}: {next(iter(plan.errors.values()))}")
    if plan.asks:
        m = manifest.MANIFESTS[a.module]
        print(f"{a.module} needs these from you before it can go on {hname}:")
        for f in plan.asks:
            print(f"  --set {f}=...   {(m.get('ask') or {}).get(f, '')}")
        return 2
    wrote = _show_plan(plan, path, a.apply, hname)
    for n in plan.notes:
        print(n)
    local = hname == this_host()
    checks = doctor.module_checks(json.loads(plan.new_text), a.module, local)
    if checks:
        print(f"\n{a.module} on {hname}"
              + ("" if local else " (host file only; run doctor there for the machine)") + ":")
        _print_checks(checks)
    if wrote:
        print(f"\nNext: ./ranch-radio install {hname}   (shows the files; --apply writes them)")
    return 0


def cmd_remove(a):
    _module_or_exit(a.module)
    hname = a.host or this_host()
    load_host(hname)
    path = host_path(hname)
    plan = modules.plan_remove(path.read_text(), a.module)
    if plan.refusal:
        sys.exit(f"ranch-radio: {plan.refusal}.")
    wrote = _show_plan(plan, path, a.apply, hname)
    if plan.orphans:
        print(f"\nNo longer rendered once {a.module} is off; install leaves them in place, so after it:")
        for line in plan.orphans:
            print("  " + line)
    if wrote:
        print(f"\nNext: ./ranch-radio install {hname}   (the rendered files drop {a.module})")
    for n in plan.notes:
        print(n)
    return 0


# ---------------------------------------------------------------- init / start / update

def cmd_init(a):
    from . import init
    return init.run(a, ROOT)


def cmd_where(a):
    """The host file this machine (or HOST) uses; exit 1 if there's none."""
    p = paths.find(HOSTS, a.host or this_host())
    if p is None:
        return 1
    print(p)
    return 0


def _show_service(lines):
    for line, rc, err in lines:
        print(("  ok   " if rc == 0 else f"  FAIL ({rc}) ") + line + (f"\n         {err}" if err else ""))


def cmd_start(a):
    h = load_host(a.host or this_host())
    _guard(h, a)
    lines = service.start(h)
    _show_service(lines)
    url = f"https://{this_host()}:7300/" if (h["radios"] and h["radios"][0].get("https")) or \
        (h.get("station") or {}).get("https") else f"http://{this_host()}:7300/"
    bad = [x for x in lines if x[1]]
    # On a Mac a failed bootstrap or a no-op load is recovered by the next
    # step; only the final verdict ("NOT RUNNING ...") is a failure.
    if h["os"] == "macos":
        bad = [x for x in lines if x[1] and "NOT RUNNING" in x[0]]
    print(f"\n{h['host']}: " + ("started. Open " + url if not bad else f"{len(bad)} step(s) failed"))
    return 1 if bad else 0


def _reexec(h, a, *flags, plain=False, wait=False):
    """Run update again under the code now on disk. plain: only the flags
    every update has ever had (a rollback may land on code older than
    --scheduled and --from). wait: as a child, returning its exit code
    (otherwise this process becomes it)."""
    argv = [sys.executable, str(ROOT / "ranch-radio"), "update", h["host"], "--no-pull", *flags]
    argv += ["--apply"] if a.apply else []
    argv += ["--scheduled"] if a.scheduled and not plain else []
    argv += ["--home", a.home] if a.home else []
    argv += ["--drop-rigctl"] if getattr(a, "drop_rigctl", False) else []
    sys.stdout.flush()
    if wait:
        fd = os.environ.get("RANCH_RADIO_UPDATE_LOCK")
        return subprocess.run(argv, pass_fds=(int(fd),) if fd else ()).returncode
    os.execv(sys.executable, argv)


def _say(h, a, title, text):
    """Print; and from the scheduled job, a desktop notification too."""
    print(text)
    if a.scheduled:
        updater.notify(f"Ranch Radio ({h['host']}): {title}", text)


def cmd_update_check(h, a, st):
    s = updater.status(ROOT)
    if s.error:
        print(f"ranch-radio: {s.error}")
        return 1
    print(f"code: {updater.short(s.head)}, tracking {s.tracking}")
    stale = bool(st["running"]) and st["running"] != s.head
    if stale:
        print(f"the servers run {updater.short(st['running'])}, not the checkout's {updater.short(s.head)} "
              f"(pulled outside an update?)")
    if s.behind:
        print(f"{s.behind} new commit(s):")
        for c in s.commits:
            print(f"  {c}")
        if s.upstream in st["failed"]:
            print(f"  ({updater.short(s.upstream)} failed here: {st['failed'][s.upstream]})")
    elif not stale:
        print("current")
    if s.ahead:
        print(f"{s.ahead} local commit(s) not on {s.tracking}: update can't fast-forward")
    if st["pending"]:
        print(f"still on the old code (were busy at the last update): {', '.join(st['pending'])}")
    todo = s.behind or st["pending"] or stale
    if todo:
        print("To update: ranch-radio update --apply")
    return 2 if todo else 0


def _rollback(h, a, home, before, bad, was_up, why):
    """Put the code back on before, restart on it, and make sure every server
    in was_up serves again (starting any the restart left down: systemd's
    try-restart leaves a failed unit failed). Always returns 1: the update
    didn't happen."""
    print(f"\n{h['host']}: {why}. Rolling back to {updater.short(before)}.")
    st = updater.load_state(home)
    st["failed"][bad] = f"{why} ({time.strftime('%Y-%m-%d %H:%M')})"
    updater.save_state(home, st)
    ok, err = updater.back(ROOT, before)
    if not ok:
        _say(h, a, "update broke a radio",
             f"{why}, and the rollback failed: {err}. Fix the checkout, then: ranch-radio update --apply")
        return 1
    a.apply = True
    rc = _reexec(h, a, "--code-changed", plain=True, wait=True)    # the old code restarts on itself
    down = updater.wait_serving(h, was_up)
    if down:
        _show_service(service.force_start(h, down))
        down = updater.wait_serving(h, down)
    st = updater.load_state(home)                                    # the restart above wrote it
    st["running"] = before
    updater.save_state(home, st)
    tail = (f"every server that was serving is serving again" if not down else
            f"STILL NOT SERVING: {', '.join(down)}; check the server's log")
    _say(h, a, "update rolled back" if not down else "update rolled back; a radio is still down",
         f"{updater.short(bad)}: {why}. Back on {updater.short(before)}; {tail}. "
         f"{updater.short(bad)} won't be tried again until a newer commit lands"
         + ("" if rc == 0 else f" (the rollback's restart exited {rc})") + ".")
    return 1


UPDATE_DEFAULTS = {"apply": False, "check": False, "force": False, "scheduled": False, "no_pull": False,
                   "drop_rigctl": False,
                   "code_changed": False, "from_rev": None, "home": None, "host": None}


def cmd_update(a):
    for k, v in UPDATE_DEFAULTS.items():
        if not hasattr(a, k):
            setattr(a, k, v)
    h = load_host(a.host or this_host())
    _guard(h, a)
    home = Path(a.home or os.path.expanduser("~"))
    st = updater.load_state(home)
    if a.check:
        return cmd_update_check(h, a, st)
    # The job itself, or anything it runs (the rollback runs older code that
    # may not know --scheduled): never restart the update job mid-run.
    job = a.scheduled or bool(os.environ.get("RANCH_RADIO_UPDATE_JOB"))
    if a.scheduled and not a.no_pull:
        u = render.updates(h)
        if u is None:
            print('updates are off in the host file ("updates": false); nothing to do')
            return 0
        if updater.lock(home) is None:
            print("another update is running; this check waits for the next day")
            return 0
        st = updater.load_state(home)
        if not u["apply"]:
            # Check only: say so once per new upstream commit.
            s = updater.status(ROOT)
            if s.error:
                print(f"ranch-radio: {s.error}")
                return 1
            if s.behind and s.upstream != st["notified"]:
                _say(h, a, "update available",
                     f"{s.behind} new commit(s) ({s.commits[0] if s.commits else ''}). "
                     f"To install: ranch-radio update --apply")
                st["notified"] = s.upstream
                updater.save_state(home, st)
            elif not s.behind:
                print(f"code: {updater.short(s.head)} (current)")
            return 0
        a.apply = True
    if a.apply and updater.lock(home) is None:
        _say(h, a, "update skipped", "ranch-radio: another update is running on this machine; try again when it's done")
        return 1
    st = updater.load_state(home, log=lambda *_: None)

    if not a.no_pull:
        s = updater.status(ROOT)
        if s.error:
            _say(h, a, "update failed", f"ranch-radio: {s.error}")
            return 1
        if not s.behind:
            print(f"code: {updater.short(s.head)} (current)")
        elif s.upstream in st["failed"] and not a.force:
            print(f"code: {updater.short(s.head)}; {s.tracking} is at {updater.short(s.upstream)}, which failed here "
                  f"({st['failed'][s.upstream]}). Waiting for a newer commit (or: update --apply --force).")
        elif not a.apply:
            print(f"code: {updater.short(s.head)}; would pull {s.behind} commit(s) from {s.tracking}:")
            for c in s.commits:
                print(f"  {c}")
            print("(the files below are what the current code renders; the new code may change them)")
        else:
            was_up = [k for k, up in updater.serving(h).items() if up]
            if a.scheduled and not was_up:
                # Nothing to check the new code against (just booted, or the
                # radio is off): leave it for a run that can check it.
                print("no server is serving, so an update couldn't be checked; leaving it for the next run")
                return 0
            before = s.head
            ok, err = updater.pull(ROOT)
            if not ok:
                _say(h, a, "update failed", f"ranch-radio: git pull --ff-only failed: {err}. Nothing changed.")
                return 1
            after = updater.head(ROOT)
            print(f"code: {updater.short(before)} -> {updater.short(after)}")
            ok, why = updater.requirements(ROOT, h)
            if not ok:
                okb, errb = updater.back(ROOT, before)
                st["failed"][after] = f"packages: {why}"
                updater.save_state(home, st)
                _say(h, a, "update not installed",
                     f"{why}. " + (f"Moved the code back to {updater.short(before)}; nothing was restarted."
                                   if okb else f"Couldn't move the code back ({errb}); nothing was restarted, "
                                   f"but the checkout is at {updater.short(after)}."))
                return 1
            # The new code renders, so it installs: run the update again under
            # it, as a child. The running servers still hold the old code (and
            # import parts of it lazily), so they restart too, idle ones only.
            # The new code checks itself and rolls back; if it dies before it
            # can (an import error, a flag it doesn't know), this does.
            rc = _reexec(h, a, "--code-changed", "--from", before, wait=True)
            if rc == 0 or updater.head(ROOT) != after:
                return rc
            return _rollback(h, a, home, before, after, was_up,
                             f"the update under {updater.short(after)} failed (exit {rc}) before it could finish")

    h = load_host(h["host"])
    states = list(_states(h, home))
    todo = [(art, p, s) for art, p, s in states if s != "same"]
    new = {art.path for art, p, s in todo if s == "missing"}
    code = a.code_changed
    head_now = updater.head(ROOT)
    if not code and st["running"] and st["running"] != head_now:
        # The checkout moved without an update restarting the servers (a
        # manual pull, an older update's dry run): they run st["running"].
        print(f"the servers run {updater.short(st['running'])}; the checkout is at {updater.short(head_now)}")
        code = True
        if not a.from_rev and updater.git(ROOT, "merge-base", "--is-ancestor", st["running"], "HEAD")[0] == 0:
            a.from_rev = st["running"]
    pending = [m for m in st["pending"] if m]
    if a.scheduled and not code and todo:
        # With no code change, host-file edits wait for a person. (A code
        # update installs everything the host file renders.)
        print(f"{len(todo)} file(s) differ from the host file; the scheduled job leaves them "
              f"(to install: ranch-radio update --apply)")
        todo, new = [], set()
    if not todo and not code and not pending:
        if not st["running"] and a.apply:
            st["running"] = head_now
            updater.save_state(home, st)
        print(f"{h['host']}: nothing to install; every file matches. Nothing restarted.")
        return 0
    for art, p, s in todo:
        print(f"{'write' if a.apply else 'would write'} ({s}) {p}")
    if code:
        print("the code changed: every service restarts on it (radios in use are left for later)")
    elif pending:
        print(f"still on the old code, to restart now if idle: {', '.join(pending)}")
    if not a.apply:
        print("\nDry run. --apply writes these and restarts what changed (idle radios only).")
        return 0

    if code and not a.from_rev:
        # Pulled by an older update that neither checked packages nor said
        # where it came from: find that (ORIG_HEAD).
        a.from_rev = updater.before_pull(ROOT) or None
    if code:
        # Packages, before any server restarts onto code that can't import.
        ok, why = updater.requirements(ROOT, h)
        if not ok:
            okb = False
            if a.from_rev:
                st["failed"][head_now] = f"packages: {why}"
                updater.save_state(home, st)
                okb, _ = updater.back(ROOT, a.from_rev)
            _say(h, a, "update not installed", f"{why}. Nothing was restarted." +
                 (f" Moved the code back to {updater.short(a.from_rev)}." if a.from_rev and okb else ""))
            return 1
    was_up = [k for k, up in updater.serving(h).items() if up]
    changed, backups = [], None
    if todo:
        changed, backups = write_installed(h, home, todo)
        print(f"written; old files in {backups}")
    restart_ids = ({r["id"] for r in h["radios"]} | {"station", "packet"}) if code else set(pending)
    if restart_ids:
        # Restart what runs this repo's Python: the radio servers, the station
        # and the packet hub (manual_restart keeps it out). Not ardopcf or the
        # menu-bar widget: neither runs this code, and ardopcf may be mid-session.
        seen = {art.path for art in changed}
        changed += [art for art in render.artifacts(h, home=str(home))
                    if art.path not in seen and art.module in restart_ids
                    and (art.fmt == "plist" or art.path.endswith((".service", "station.json")))]
    held = []
    check = functools.partial(service.idle, drop_rigctl=bool(getattr(a, "drop_rigctl", False)))
    lines, deferred = service.restart(h, changed, check_idle=check, held=held, new=new,
                                      skip={"updates"} if job else ())
    _show_service(lines)
    for d in deferred:
        print(f"  not restarted: {d}")
    st["pending"] = sorted(set(held))
    updater.save_state(home, st)

    down = updater.wait_serving(h, [k for k in was_up if k not in held])
    if not down:
        st["running"] = head_now
        updater.save_state(home, st)
        msg = (f"updated to {updater.short(head_now)}" if code else "installed") + \
              "; every server that was serving is serving"
        if st["pending"]:
            msg += f"; still on the old code (busy): {', '.join(st['pending'])}, restarted at the next update"
        print(f"\n{h['host']}: {msg}")
        if a.scheduled and code:
            updater.notify(f"Ranch Radio ({h['host']}): updated", msg)
        return 0

    why = f"not serving after the update: {', '.join(down)}"
    if a.from_rev and code:
        return _rollback(h, a, home, a.from_rev, head_now, was_up, why)
    _say(h, a, "a radio isn't serving",
         f"{why}." + (f" Old files in {backups}." if backups else "") +
         " Check the server's log; ranch-radio doctor says what's missing.")
    return 1


# The bar's and JTDX's view of the radios: one line per radio, in the field
# order each script already parses. Read from the INSTALLED station.json (what
# the station runs), not the host file, so a script never names a radio the
# station doesn't serve.
RADIO_FORMATS = {
    "server": (":", ["id", "name", "device", "rigctl"]),
    "jtdx": ("|", ["id", "name", "device", "jtdx_profile", "rigctl", "audio_in", "audio_out"]),
}


def cmd_radios(a):
    import json
    path = Path(os.path.expanduser(a.config))
    try:
        radios = json.loads(path.read_text())["radios"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"ranch-radio radios: {path}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    sep, keys = RADIO_FORMATS[a.format]
    for r in radios:
        vals = [str(r.get(k, "")) for k in keys]
        if any(sep in v for v in vals):
            print(f"ranch-radio radios: {r.get('id')}: a field holds {sep!r}; skipped", file=sys.stderr)
            continue
        print(sep.join(vals))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="ranch-radio", description="Ranch Radio: ship every radio module from hosts/<host>.json")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("modules", help="every module, where its code is and how it ships")
    sub.add_parser("hosts", help="which host runs which module")
    rr = sub.add_parser("radios", help="the installed station's radios, one line each, for the bar and JTDX")
    rr.add_argument("--config", default="~/.config/radio-remote/station.json")
    rr.add_argument("--format", choices=sorted(RADIO_FORMATS), default="server")
    r = sub.add_parser("render", help="write a host's files into DIR (installs nothing)")
    r.add_argument("host")
    r.add_argument("dir")
    r.add_argument("--home", default="/HOME", help="home directory for a Mac's absolute paths")
    for name, fn_help in (("drift", "rendered vs installed on this machine"),
                          ("install", "show what would change; --apply writes it")):
        s = sub.add_parser(name, help=fn_help)
        s.add_argument("host", nargs="?", help="default: this machine")
        s.add_argument("--home", help="treat DIR as the home directory (a copy, for tests)")
        if name == "drift":
            s.add_argument("--diff", action="store_true", help="show the difference for each file that differs")
        else:
            s.add_argument("--apply", action="store_true")
    d = sub.add_parser("doctor", help="what a host's modules need, and whether this machine has it")
    d.add_argument("host", nargs="?", help="default: this machine (another host: its host file only)")
    d.add_argument("--fix", action="store_true",
                   help="run the fixes that need no root (venvs, pip, the ardop build, brew); print the rest")
    it = sub.add_parser("init", help="write this machine's first host file, from the radio plugged in")
    it.add_argument("--rig", help="the radio's profile (default: guessed from its USB names)")
    it.add_argument("--port", help="its serial port (default: found)")
    it.add_argument("--call", help="your callsign; without it the radio is receive-only")
    it.add_argument("--name", help="the radio's name on the page")
    it.add_argument("--host", help="the host file's name (default: this machine's)")
    it.add_argument("--force", action="store_true", help="write even if this host has a host file")
    it.add_argument("--dry-run", action="store_true", help="show it, write nothing")
    w = sub.add_parser("where", help="the host file this machine uses (exit 1 if none)")
    w.add_argument("host", nargs="?")
    st = sub.add_parser("start", help="enable and start everything the host file renders (after install)")
    st.add_argument("host", nargs="?")
    st.add_argument("--home", help=argparse.SUPPRESS)
    up = sub.add_parser("update", help="pull, install and restart what changed (idle radios only); "
                                       "checks they serve, rolls back if not")
    up.add_argument("host", nargs="?")
    up.add_argument("--apply", action="store_true", help="do it (without: show what would change, pull nothing)")
    up.add_argument("--check", action="store_true", help="is there an update? (exit 2 when there is)")
    up.add_argument("--force", action="store_true", help="try a commit that failed here before")
    up.add_argument("--scheduled", action="store_true",
                    help="the daily job: notify, or apply when the host file says \"updates\": {\"apply\": true}")
    up.add_argument("--no-pull", action="store_true", help="skip git pull")
    up.add_argument("--drop-rigctl", action="store_true",
                    help="restart a radio even with JTDX/WSJT-X connected over rigctl (they lose rig control)")
    up.add_argument("--code-changed", action="store_true",
                    help="restart every service (idle ones), as after a pull that changed the code")
    up.add_argument("--from", dest="from_rev", help=argparse.SUPPRESS)      # the commit before the pull
    up.add_argument("--home", help=argparse.SUPPRESS)
    ad = sub.add_parser("add", help="switch a module on in a host file (dry run; --apply writes)")
    ad.add_argument("module")
    ad.add_argument("--host", help="default: this machine")
    ad.add_argument("--set", action="append", metavar="FIELD=VALUE",
                    help="a field of the module's block (dotted for nested; JSON values parse)")
    ad.add_argument("--apply", action="store_true")
    rm = sub.add_parser("remove", help="switch a module off in a host file (dry run; --apply writes)")
    rm.add_argument("module")
    rm.add_argument("--host", help="default: this machine")
    rm.add_argument("--apply", action="store_true")
    a = p.parse_args(argv)
    return {"modules": cmd_modules, "hosts": cmd_hosts, "render": cmd_render, "radios": cmd_radios,
            "drift": cmd_drift, "install": cmd_install, "doctor": cmd_doctor,
            "add": cmd_add, "remove": cmd_remove, "init": cmd_init, "where": cmd_where,
            "start": cmd_start, "update": cmd_update}[a.cmd](a) or 0
