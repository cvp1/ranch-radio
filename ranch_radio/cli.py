"""ranch-radio: the command line. See ranch_radio/__init__.py."""

import argparse
import difflib
import subprocess
import json
import os
import shlex
import socket
import sys
import time
from pathlib import Path

from . import catalog, doctor, manifest, modules, paths, render, service
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


def cmd_update(a):
    h = load_host(a.host or this_host())
    _guard(h, a)
    home = Path(a.home or os.path.expanduser("~"))
    if not a.no_pull:
        before = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
        r = subprocess.run(["git", "-C", str(ROOT), "pull", "--ff-only", "-q"], capture_output=True, text=True)
        if r.returncode:
            sys.exit(f"ranch-radio: git pull --ff-only failed: {(r.stderr or r.stdout).strip()}")
        after = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                               capture_output=True, text=True).stdout.strip()
        print(f"code: {before}" + (f" -> {after}" if after != before else " (already current)"))
        if after != before:
            # The new code renders: run the update again under it. The running
            # servers still hold the old code (and import parts of it lazily),
            # so they are restarted too, idle ones only (--code-changed).
            os.execv(sys.executable, [sys.executable, str(ROOT / "ranch-radio"), "update", h["host"], "--no-pull",
                                      "--code-changed"]
                     + (["--apply"] if a.apply else []) + (["--home", a.home] if a.home else []))
    h = load_host(h["host"])
    todo = [(art, p, s) for art, p, s in _states(h, home) if s != "same"]
    code = a.code_changed
    if not todo and not code:
        print(f"{h['host']}: nothing to install; every file matches. Nothing restarted.")
        return 0
    for art, p, s in todo:
        print(f"{'write' if a.apply else 'would write'} ({s}) {p}")
    if code:
        print("the code changed: every service restarts on it (radios in use are left for later)")
    if not a.apply:
        print("\nDry run. --apply writes these and restarts what changed (idle radios only).")
        return 0
    changed = []
    if todo:
        changed, backups = write_installed(h, home, todo)
        print(f"written; old files in {backups}")
    if code:
        # Restart what runs this repo's Python: the radio servers, the station
        # and the packet hub (manual_restart keeps it out). Not ardopcf or the
        # menu-bar widget: neither runs this code, and ardopcf may be mid-session.
        ours = {r["id"] for r in h["radios"]} | {"station", "packet"}
        seen = {art.path for art in changed}
        changed += [art for art in render.artifacts(h, home=str(home))
                    if art.path not in seen and art.module in ours
                    and (art.fmt == "plist" or art.path.endswith((".service", "station.json")))]
    lines, deferred = service.restart(h, changed)
    _show_service(lines)
    for d in deferred:
        print(f"  not restarted: {d}")
    return 0


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
    up = sub.add_parser("update", help="git pull, then install and restart what changed (idle radios only)")
    up.add_argument("host", nargs="?")
    up.add_argument("--apply", action="store_true")
    up.add_argument("--no-pull", action="store_true", help="skip git pull")
    up.add_argument("--code-changed", action="store_true",
                    help="restart every service (idle ones), as after a pull that changed the code")
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
