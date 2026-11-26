"""`ranch-radio vara-backend <xara|mercury>`: switch the modem behind Pat's
varahf on a Mac (plan winlink-mercury 2.6).

One VARA backend runs at a time. Without --apply it prints the plan and
changes nothing. With --apply, in order, stopping at the first failure:

  1. every radio with a Winlink lane is idle (no session armed, nothing
     keyed): the switch never cuts a session; disarm it first
  2. boot out the other backend's agent and move its plist aside, dated,
     to ~/.Trash (never removed), whether or not the host file thought it ran
     (a leftover agent from an earlier switch goes the same way)
  3. wait until that modem's process and every one of its listeners are gone;
     if they are not, stop here, with the new modem not loaded
  4. write winlink.vara_modem into the host file and install what it renders
     (the new agent and INI, the radio server's flags, station.json)
  5. load the new agent, restart the radio servers with a lane
  6. print Pat's two hand edits (varahf.addr, service_codes); ranch-radio
     never writes Pat's config
  7. restart the station, which restarts Pat, so a TNC socket Pat held to the
     old modem cannot outlive the switch; then check none is left

Everything that touches launchd, a process table or a socket goes through
an Ops object, so tests drive a fake one and nothing real is ever loaded.
"""

import json
import os
import shlex
import subprocess
from datetime import date
from pathlib import Path

from . import hostfile, hostlock, render, service

XARA_LABEL = "ranch-radio.xara"
PROC = {"xara": "xara", "mercury": "mercury"}


def _mercury_label():
    return render._feature("mercury_tnc").LABEL


def label_of(h, backend):
    if backend == "mercury":
        return _mercury_label()
    return ((h.get("winlink") or {}).get("xara") or {}).get("label", XARA_LABEL)


def ports_of(h, backend):
    """The TCP ports backend listens on (control, data, broadcast)."""
    b = (h.get("winlink") or {}).get(backend) or {}
    if backend == "mercury":
        mt = render._feature("mercury_tnc")
        return sorted(mt.ports(b.get("port", mt.PORT), b.get("broadcast_port", mt.BROADCAST_PORT)))
    p = b.get("port", 8300)
    return [p, p + 1]


class Ops:
    """The real machine: launchctl, pgrep, lsof (service._cmd's RANCH_RADIO_*
    stand-ins apply). Read-only except bootout/up/restart."""

    def __init__(self, home=None):
        self.home = Path(home or os.path.expanduser("~"))

    def idle(self, h, r):
        return service.idle(h, r)

    def loaded(self, label):
        return service._loaded(label)

    def bootout(self, label, out):
        service._mac_down(label, out)

    def up(self, label, path, out):
        return service._mac_up(label, path, out)

    def restart(self, label, path, out):
        service._mac_down(label, out)
        return service._mac_up(label, path, out)

    def proc_alive(self, name):
        try:
            return subprocess.run([service._cmd("pgrep"), "-x", name], capture_output=True,
                                  timeout=10).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return True                      # can't tell: treat as still there (stop)

    def _lsof(self, port, state):
        try:
            r = subprocess.run([service._cmd("lsof"), "-nP", f"-iTCP:{port}", f"-sTCP:{state}"],
                               capture_output=True, timeout=10)
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return True                      # can't tell: treat as still there

    def listening(self, port):
        return self._lsof(port, "LISTEN")

    def connected(self, port):
        """A TCP connection on port (Pat's TNC socket to the old modem)."""
        return self._lsof(port, "ESTABLISHED")

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
    old = "xara" if to == "mercury" else "mercury"
    new_l, old_l = label_of(h, to), label_of(h, old)
    b = w[to]
    steps = [
        "check every radio with a Winlink lane is idle (no session armed, nothing keyed); stop if not",
        f"launchctl bootout {old_l}, if loaded; move ~/{render.AGENTS}/{old_l}.plist to ~/.Trash/{old_l}-<date>.plist",
        f"wait until no {PROC[old]} process runs and nothing listens on {', '.join(map(str, ports_of(h, old)))}; "
        f"stop if they stay",
        f"write winlink.vara_modem = {to!r} to hosts/{h['host']}.json; install what it renders",
        f"load {new_l}; restart the radio server{'s' if sum(1 for r in h['radios'] if r.get('lane')) > 1 else ''} "
        f"with a Winlink lane",
        f"Pat's config, by hand: varahf.addr = localhost:{b['port']}"
        + ('; service_codes includes "MERCURY" (["PUBLIC", "MERCURY"])' if to == "mercury" else
           "; service_codes as you like (MERCURY lists Mercury gateways only)"),
        f"restart the station (Pat restarts with it); check no connection to {old}'s port "
        f"{ports_of(h, old)[0]} is left",
    ]
    return steps, None


def _set_modem(path, to):
    """winlink.vara_modem = to in the host file, under the one lock."""
    path = str(path)
    with hostlock.locked(os.path.dirname(path)):
        text = Path(path).read_text()
        new = hostfile.put(text, ("winlink", "vara_modem"), to)
        h = json.loads(new)
        render.validate(h)
        render.artifacts(h)
        tmp = f"{path}.{os.getpid()}.tmp"
        Path(tmp).write_text(new)
        os.replace(tmp, path)
    return h


def apply(h, path, to, ops=None, home=None, log=print, install=None, wait_s=10.0):
    """Run the switch (plan()'s steps). Returns 0 when done, else 1 with the
    reason logged. install(h, home) writes the rendered files that differ
    and returns them (cli.write_installed by default)."""
    ops = ops or Ops(home)
    home = Path(home or os.path.expanduser("~"))
    _, refusal = plan(h, to)
    if refusal:
        log(f"refused: {refusal}")
        return 1
    old = "xara" if to == "mercury" else "mercury"
    # 1. idle
    for r in h["radios"]:
        if r.get("lane"):
            ok, why = ops.idle(h, r)
            if not ok:
                log(f"refused: {why}; end or disarm the session first (the switch never cuts one)")
                return 1
    # 2. the old agent out, its plist aside (dated, never removed)
    out = []
    old_l = label_of(h, old)
    plist = home / render.AGENTS / f"{old_l}.plist"
    if ops.loaded(old_l):
        ops.bootout(old_l, out)
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
        h2 = _set_modem(path, to)
    except (render.HostError, hostfile.EditError, ValueError, OSError) as e:
        log(f"stopped: the host file was not changed ({e}); {to} was NOT loaded")
        return 1
    if install is None:
        from .cli import _states, write_installed

        def install(hh, hm):
            todo = [(a, p, s) for a, p, s in _states(hh, hm) if s != "same"]
            return write_installed(hh, hm, todo)[0] if todo else []
    install(h2, home)
    log(f"hosts/{h2['host']}.json: winlink.vara_modem = {to!r}; rendered files installed")
    # 5. the new agent, the radio servers
    new_l = label_of(h2, to)
    out = []
    rc = ops.up(new_l, f"{render.AGENTS}/{new_l}.plist", out)
    for r in h2["radios"]:
        if r.get("lane"):
            rc |= ops.restart(r["label"], f"{render.AGENTS}/{r['label']}.plist", out)
    for line, code, err in out:
        if code:
            log(f"  {line}: exit {code} {err}")
    if rc:
        log(f"stopped: {new_l} or a radio server did not come up (above)")
        return 1
    log(f"loaded {new_l}; restarted the radio server(s)")
    # 6. Pat's config is the operator's
    b = h2["winlink"][to]
    log("Pat's config, by hand (ranch-radio never writes it):")
    log(f"  varahf.addr = \"localhost:{b['port']}\"")
    if to == "mercury":
        log("  service_codes = [\"PUBLIC\", \"MERCURY\"]   (Mercury gateways are listed under MERCURY)")
    # 7. Pat restarts with the station; no socket to the old modem survives
    st_l = (h2.get("station") or {}).get("label", render.STATION_LABEL)
    out = []
    rc = ops.restart(st_l, f"{render.AGENTS}/{st_l}.plist", out)
    if rc:
        log(f"stopped: the station ({st_l}) did not come back; Pat may still hold a socket to {old}")
        return 1
    stale = ports_of(h, old)[0]
    if not ops.wait(lambda: not ops.connected(stale), wait_s):
        log(f"WARNING: a connection to {old}'s port {stale} is still open after the station restart "
            f"(Pat's old TNC socket?): find it with lsof -nP -iTCP:{stale} and stop it")
        return 1
    log(f"restarted the station (Pat with it); no connection to {old}'s port {stale} is left. "
        f"VARA HF is {'Mercury HF' if to == 'mercury' else 'xara'} now")
    return 0


def main(h, path, to, do_apply, log=print, ops=None, home=None):
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
    return apply(h, path, to, ops=ops, home=home, log=log)
