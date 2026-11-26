"""`ranch-radio vara-backend <xara|mercury>`: switch the modem behind Pat's
varahf on a Mac (plan winlink-mercury 2.6; panel M1 #4, #6-#9).

One VARA backend runs at a time. Without --apply it prints the plan and
changes nothing. With --apply, under the host-file lock for the whole run
(two switches never overlap), stopping at the first failed step:

  0. re-read the host file; validate and render the TARGET host before
     anything changes (a refusal there changes nothing)
  1. fence: the radio servers refuse every new Winlink arm while the fence
     file exists (server.cutover_fenced; checked again just before an arm
     commits); then every radio with a lane must answer and be idle (no
     session, nothing keyed): one that doesn't answer counts as busy
  2. boot out the other backend's agent (it must really unload) and move
     its plist aside, dated, to ~/.Trash (never removed)
  3. wait until that modem's process and every one of its listeners are gone
  4. write winlink.vara_modem and install what it renders
  5. load the new agent; wait for the new modem's listeners (Mercury:
     127.0.0.1 only, on exactly its ports); restart the radio server; check
     the new agent is loaded and the old one is not
  6. Pat's config on disk must already name the new modem (varahf.addr and,
     for Mercury, MERCURY in service_codes). If not: stop here, print the
     two edits and `vara-backend <to> --apply --finish`; VARA arms stay
     fenced (ARDOP may arm) until then. Pat is never restarted on a stale
     config.
  7. restart the station (Pat restarts with it): its process must be a new
     one; then no connection to the old modem's port may be left. Unfence.

Everything that touches launchd, a process table, a socket or Pat's config
goes through an Ops object, so tests drive a fake and nothing real is loaded.
"""

import copy
import json
import os
import re
import shlex
import subprocess
import time
from datetime import date
from pathlib import Path

from . import hostfile, hostlock, render, service

XARA_LABEL = "ranch-radio.xara"
PROC = {"xara": "xara", "mercury": "mercury"}
# The radio servers read the same file (server.CUTOVER_FENCE; the test checks
# they agree): {"scope": "all"|"varahf", "until": epoch s}.
FENCE = ".local/state/radio-remote/vara-cutover.json"
FENCE_SWITCH_S = 900          # a switch that dies leaves arms fenced at most this long
FENCE_PAT_S = 7200            # VARA arms wait this long for Pat's edits and --finish


def _mt():
    return render._feature("mercury_tnc")


def other(to):
    return "xara" if to == "mercury" else "mercury"


def label_of(h, backend):
    if backend == "mercury":
        return _mt().LABEL
    return ((h.get("winlink") or {}).get("xara") or {}).get("label", XARA_LABEL)


def ports_of(h, backend):
    """The TCP ports backend listens on (control, data[, broadcast])."""
    b = (h.get("winlink") or {}).get(backend) or {}
    if backend == "mercury":
        mt = _mt()
        return sorted(mt.ports(b.get("port", mt.PORT), b.get("broadcast_port", mt.BROADCAST_PORT)))
    p = b.get("port", 8300)
    return [p, p + 1]


def pat_refusal(h, to):
    """Why Pat's config on disk does not yet fit backend `to` (None: it does)."""
    import pat_config

    from .doctor import _pat_path
    path = _pat_path(h)
    b = (h.get("winlink") or {}).get(to) or {}
    lane = next((r.get("lane") for r in h["radios"] if r.get("lane")), None)
    why = pat_config.vara_refusal(path, b.get("port"), lane, backend=to)
    if why is None and to == "mercury":
        fields, err = pat_config.read_pat_config(path)
        why = err or pat_config.mercury_service_codes_refusal(fields, path)
    return why


class Ops:
    """The real machine: launchctl, pgrep, lsof (service._cmd's RANCH_RADIO_*
    stand-ins apply), Pat's config read through pat_config only."""

    def __init__(self, home=None):
        self.home = Path(home or os.path.expanduser("~"))

    def idle(self, h, r):
        """(ok, why): ok only on a positive answer: the server answers, and
        nothing is keyed, tuning or armed. No answer is not idle (M1 #4)."""
        st = service.probe(h, r)
        if not st.get("up"):
            return False, f"{r['name']} did not answer ({st.get('why', '?')}): it can't be shown idle"
        if st.get("tx") or st.get("tuning"):
            return False, f"{r['name']} is transmitting or tuning"
        if st.get("armed") or st.get("session"):
            return False, f"{r['name']} is in a Winlink session"
        return True, "idle"

    def loaded(self, label):
        return service._loaded(label)

    def bootout(self, label, out):
        service._mac_down(label, out)
        return 1 if service._loaded(label) else 0

    def up(self, label, path, out):
        return service._mac_up(label, path, out)

    def pid(self, label):
        """The agent's running pid (launchctl print "pid = N"), or None."""
        try:
            r = subprocess.run([service._cmd("launchctl"), "print", f"gui/{os.getuid()}/{label}"],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        m = re.search(r"^\s*pid = (\d+)", r.stdout, re.M)
        return int(m.group(1)) if r.returncode == 0 and m else None

    def restart(self, label, path, out):
        if self.bootout(label, out):
            out.append((f"{label}: still loaded after bootout", 1, ""))
            return 1
        return self.up(label, path, out)

    def proc_alive(self, name):
        try:
            return subprocess.run([service._cmd("pgrep"), "-x", name], capture_output=True,
                                  timeout=10).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return True                      # can't tell: treat as still there (stop)

    def _lsof_port(self, port, state):
        exe = _mt().lsof_bin()
        if exe is None:
            return True                      # can't tell: treat as still there
        try:
            r = subprocess.run([exe, "-nP", f"-iTCP:{port}", f"-sTCP:{state}"], capture_output=True, timeout=10)
            return r.returncode == 0 or bool(r.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            return True

    def listening(self, port):
        return self._lsof_port(port, "LISTEN")

    def connected(self, port):
        """A TCP connection on port (Pat's TNC socket to the old modem)."""
        return self._lsof_port(port, "ESTABLISHED")

    def modem_refusal(self, h, backend):
        """Why backend's listeners are not up as they must be (None: they are)."""
        if backend == "mercury":
            mt = _mt()
            m = h["winlink"]["mercury"]
            try:
                rows = mt.parse_lsof(mt.run_lsof())
            except OSError as e:
                return str(e)
            return mt.listener_refusal(rows, mt.ports(m["port"], m["broadcast_port"]))
        down = [p for p in ports_of(h, backend) if not self.listening(p)]
        return f"nothing listens on {', '.join(map(str, down))}" if down else None

    def pat_refusal(self, h, to):
        return pat_refusal(h, to)

    def wait(self, pred, timeout):
        return service._until(pred, timeout)


def trash_path(home, label, today=None):
    """~/.Trash/<label>-<YYYY-MM-DD>.plist, never one that exists."""
    stamp = (today or date.today()).isoformat()
    base = Path(home) / ".Trash" / f"{label}-{stamp}.plist"
    n, p = 1, base
    while p.exists():
        n += 1
        p = base.with_name(f"{label}-{stamp}-{n}.plist")
    return p


def _fence(home, scope, seconds):
    p = Path(home) / FENCE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"scope": scope, "until": time.time() + seconds}))
    tmp.replace(p)


def _unfence(home):
    p = Path(home) / FENCE
    if p.exists():
        p.replace(p.with_name(p.name + ".done"))     # moved aside, not removed


def target_of(h, to):
    """h switched to backend `to`, validated and rendered (raises HostError)."""
    t = copy.deepcopy(h)
    t.setdefault("winlink", {})["vara_modem"] = to
    render.validate(t)
    render.artifacts(t)
    return t


def plan(h, to):
    """(steps, refusal): the switch's steps as text, or why it can't be done."""
    if h["os"] != "macos":
        return [], f"{h['host']}: the VARA backend switch is macOS only in this version"
    if to not in render.VARA_MODEMS:
        return [], f"the backend is one of {', '.join(render.VARA_MODEMS)}, not {to!r}"
    w = h.get("winlink") or {}
    if not isinstance(w.get(to), dict):
        what = "ranch-radio add mercury" if to == "mercury" else "ranch-radio add vara"
        return [], f"{h['host']} has no winlink.{to} block ({what} first)"
    try:
        target_of(h, to)
    except (render.HostError, ValueError, KeyError) as e:
        return [], f"the host file with winlink.vara_modem = {to!r} does not render: {e}"
    old = other(to)
    new_l, old_l = label_of(h, to), label_of(h, old)
    b = w[to]
    steps = [
        "fence new Winlink arms on this host; every radio with a Winlink lane must answer and be idle",
        f"launchctl bootout {old_l}, if loaded; move ~/{render.AGENTS}/{old_l}.plist to ~/.Trash/{old_l}-<date>.plist",
        f"wait until no {PROC[old]} process runs and nothing listens on {', '.join(map(str, ports_of(h, old)))}",
        f"write winlink.vara_modem = {to!r} to hosts/{h['host']}.json; install what it renders",
        f"load {new_l} and wait for its listeners; restart the radio server with the Winlink lane",
        f"Pat's config must already say varahf.addr = localhost:{b['port']}"
        + ('; service_codes includes "MERCURY"' if to == "mercury" else "")
        + f"; if not, stop here (VARA arms fenced) until `ranch-radio vara-backend {to} --apply --finish`",
        f"restart the station (Pat with it, a new process); check no connection to {old}'s port "
        f"{ports_of(h, old)[0]} is left; unfence",
    ]
    return steps, None


def _write_host(path, to):
    """winlink.vara_modem = to (the caller holds the host-file lock)."""
    text = Path(path).read_text()
    new = hostfile.put(text, ("winlink", "vara_modem"), to)
    h = json.loads(new)
    render.validate(h)
    tmp = f"{path}.{os.getpid()}.tmp"
    Path(tmp).write_text(new)
    os.replace(tmp, path)
    return h


def _default_install(hh, hm):
    from .cli import _states, write_installed
    todo = [(a, p, s) for a, p, s in _states(hh, hm) if s != "same"]
    return write_installed(hh, hm, todo)[0] if todo else []


def _say_out(out, log):
    for line, code, err in out:
        if code:
            log(f"  {line}: exit {code} {err}")


def _finish(h, to, ops, home, log, wait_s):
    """Steps 6-7: Pat's config, the station (Pat) restart, the stale socket."""
    old = other(to)
    why = ops.pat_refusal(h, to)
    b = h["winlink"][to]
    if why:
        _fence(home, "varahf", FENCE_PAT_S)
        log(f"stopped before restarting Pat: {why}.")
        log("Pat's config, by hand (ranch-radio never writes it):")
        log(f"  varahf.addr = \"localhost:{b['port']}\"")
        if to == "mercury":
            log("  service_codes = [\"PUBLIC\", \"MERCURY\"]   (Mercury gateways are listed under MERCURY)")
        log(f"then: ranch-radio vara-backend {to} --apply --finish   "
            f"(until then VARA HF sessions are refused here; ARDOP is not)")
        return 2
    st_l = (h.get("station") or {}).get("label", render.STATION_LABEL)
    before = ops.pid(st_l)
    out = []
    rc = ops.restart(st_l, f"{render.AGENTS}/{st_l}.plist", out)
    _say_out(out, log)
    after = ops.pid(st_l) if not rc else None
    if rc or after is None or after == before:
        log(f"stopped: the station ({st_l}) did not restart (pid {before} -> {after}); Pat may still run "
            f"on its old settings. VARA arms stay fenced; run --finish again")
        _fence(home, "varahf", FENCE_PAT_S)
        return 1
    stale = ports_of(h, old)[0]
    if not ops.wait(lambda: not ops.connected(stale), wait_s):
        log(f"WARNING: a connection to {old}'s port {stale} is still open after the station restart "
            f"(Pat's old TNC socket?): find it with lsof -nP -iTCP:{stale} and stop it, then --finish again")
        _fence(home, "varahf", FENCE_PAT_S)
        return 1
    _unfence(home)
    log(f"restarted the station (pid {before} -> {after}, Pat with it); no connection to {old}'s port {stale}. "
        f"VARA HF is {'Mercury HF' if to == 'mercury' else 'xara'} now")
    return 0


def apply(h, path, to, ops=None, home=None, log=print, install=None, wait_s=10.0, finish=False):
    """Run the switch (plan()'s steps). 0 done; 2 stopped for Pat's edits
    (--finish completes it); 1 stopped on a failure (logged). install(h,
    home) writes the rendered files that differ. h is re-read from path."""
    ops = ops or Ops(home)
    home = Path(home or os.path.expanduser("~"))
    install = install or _default_install
    with hostlock.locked(os.path.dirname(str(path))):
        try:
            h = json.loads(Path(path).read_text())       # re-read under the lock (M1 #7)
            render.validate(h)
        except (OSError, ValueError, render.HostError) as e:
            log(f"refused: the host file does not load: {e}")
            return 1
        _, refusal = plan(h, to)
        if refusal:
            log(f"refused: {refusal}")
            return 1
        if finish:
            if render.vara_modem(h) != to:
                log(f"refused: --finish completes a switch to {to}, but the host file says "
                    f"{render.vara_modem(h)}; run it without --finish")
                return 1
            if not ops.loaded(label_of(h, to)) or ops.loaded(label_of(h, other(to))):
                log(f"refused: {label_of(h, to)} must be loaded and {label_of(h, other(to))} not; "
                    f"run it without --finish")
                return 1
            return _finish(h, to, ops, home, log, wait_s)
        target = target_of(h, to)                        # rendered before any change (M1 #6)
        _fence(home, "all", FENCE_SWITCH_S)
        rc = _switch(h, target, path, to, ops, home, log, install, wait_s)
        if rc:
            _unfence(home)
            return rc
        return _finish(target, to, ops, home, log, wait_s)


def _switch(h, target, path, to, ops, home, log, install, wait_s):
    old = other(to)
    # 1. positive idle, under the fence
    for r in h["radios"]:
        if r.get("lane"):
            ok, why = ops.idle(h, r)
            if not ok:
                log(f"refused: {why}; end or disarm the session first (the switch never cuts one)")
                return 1
    # 2. the old agent out (for real), its plist aside
    out = []
    old_l = label_of(h, old)
    plist = home / render.AGENTS / f"{old_l}.plist"
    if ops.loaded(old_l):
        if ops.bootout(old_l, out):
            _say_out(out, log)
            log(f"stopped: {old_l} is still loaded after bootout; {to} was NOT loaded")
            return 1
        log(f"booted out {old_l}")
    if plist.exists():
        dest = trash_path(home, old_l)
        dest.parent.mkdir(parents=True, exist_ok=True)
        plist.rename(dest)
        log(f"moved {plist} -> {dest}")
    # 3. gone, for real
    gone = ops.wait(lambda: not ops.loaded(old_l) and not ops.proc_alive(PROC[old])
                    and not any(ops.listening(p) for p in ports_of(h, old)), wait_s)
    if not gone:
        left = [w for w, on in ((f"{old_l} still loaded", ops.loaded(old_l)),
                                (f"a {PROC[old]} process still runs", ops.proc_alive(PROC[old])),
                                *((f"something still listens on {p}", ops.listening(p)) for p in ports_of(h, old)))
                if on]
        log(f"stopped: {old} is not gone after {wait_s:g} s ({'; '.join(left)}); {to} was NOT loaded. "
            f"Find it (lsof -nP -iTCP -sTCP:LISTEN) and stop it, then run this again")
        return 1
    # 4. the host file, and what it renders
    try:
        _write_host(path, to)
    except (render.HostError, hostfile.EditError, ValueError, OSError) as e:
        log(f"stopped: the host file was not changed ({e}); {to} was NOT loaded")
        return 1
    install(target, home)
    log(f"hosts/{target['host']}.json: winlink.vara_modem = {to!r}; rendered files installed")
    # 5. the new agent and its listeners, then the radio server
    new_l = label_of(target, to)
    out = []
    if ops.up(new_l, f"{render.AGENTS}/{new_l}.plist", out):
        _say_out(out, log)
        log(f"stopped: {new_l} did not load; the radio server was not restarted")
        return 1
    if not ops.wait(lambda: ops.modem_refusal(target, to) is None, wait_s):
        log(f"stopped: {new_l} is loaded but {to} is not listening as it must "
            f"({ops.modem_refusal(target, to)}); the radio server was not restarted")
        return 1
    for r in target["radios"]:
        if r.get("lane"):
            out = []
            if ops.restart(r["label"], f"{render.AGENTS}/{r['label']}.plist", out):
                _say_out(out, log)
                log(f"stopped: the radio server {r['label']} did not restart")
                return 1
    if not ops.loaded(new_l) or ops.loaded(old_l):
        log(f"stopped: after the switch {new_l} should be loaded and {old_l} not "
            f"(loaded: {new_l} {ops.loaded(new_l)}, {old_l} {ops.loaded(old_l)})")
        return 1
    log(f"loaded {new_l} ({to} listening); restarted the radio server")
    return 0


def main(h, path, to, do_apply, log=print, ops=None, home=None, finish=False):
    steps, refusal = plan(h, to)
    if refusal:
        log(f"ranch-radio: {refusal}")
        return 1
    log(f"{h['host']}: VARA HF backend -> {to} (now {render.vara_modem(h)})")
    for i, s in enumerate(steps, 1):
        log(f"  {i}. {s}")
    if not do_apply:
        log(f"\nDry run. Pass --apply to run it (on the Mac itself: ranch-radio vara-backend "
            f"{shlex.quote(to)} --apply).")
        return 0
    return apply(h, path, to, ops=ops, home=home, log=log, finish=finish)
