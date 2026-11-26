"""`ranch-radio vara-backend <xara|mercury>`: switch the modem behind Pat's
varahf on a Mac (plan winlink-mercury 2.6; panel M1 #4, #6-#9).

One VARA backend runs at a time. Without --apply it prints the plan and
changes nothing. With --apply, under the host-file lock for the whole run
(two switches never overlap), stopping at the first failed step:

  0. re-read the host file; validate and render the TARGET host before
     anything changes (a refusal there changes nothing)
  1. fence: while the fence file exists the radio server refuses every new
     Winlink arm, key and ATU tune (server.cutover_fenced; arms check it
     again at commit); then every radio with a lane must answer and be idle
     as service.idle means it (nothing keyed or tuning, no session, no
     unsaved STOP TX, no rigctl client): no answer counts as busy
  2. boot out the other backend's agent (it must really unload) and move
     its plist aside, dated, to ~/.Trash (never removed)
  3. wait until that modem's process and every one of its listeners are
     gone ("absent"; a probe that can't be read is not gone)
  4. write winlink.vara_modem and install what it renders
  5. restart the radio server and wait for the new modem's lane, owned by
     it (Mercury exits at start when it can't reach its lane); then load the
     new agent and wait for its listeners, owned by its process (Mercury:
     127.0.0.1 only, on exactly its ports); check the agents
  6. with the station and the lane radio idle (an ARDOP session is not
     cut): Pat's config on disk must already name the new modem. If not,
     stop: print the two edits and `vara-backend <to> --apply --finish`.
     VARA HF stays refused (a durable pending state that does not expire;
     ARDOP may run) until --finish succeeds. Pat is never restarted on a
     stale config.
  7. the modem is still listening; restart the station (Pat restarts with
     it): a new pid, and its Pat running; no connection to the old modem's
     port left. Lift the fence and the pending state.

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
# The radio servers read the same files (server.CUTOVER_FENCE; the test checks
# they agree): the switch's fence {"until"} and the durable pending-Pat state.
FENCE = ".local/state/radio-remote/vara-cutover.json"
PENDING_SUFFIX = ".pending"   # server.PENDING_SUFFIX: VARA refused until --finish, no expiry
FENCE_SWITCH_S = 900          # a switch that dies leaves the radio fenced at most this long


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
    stand-ins apply), Pat's config read through pat_config only. Socket
    probes answer "present", "absent" or "unreadable"; unreadable never
    counts as ready or as gone (panel M1 r2 C5)."""

    def __init__(self, home=None):
        self.home = Path(home or os.path.expanduser("~"))

    def idle(self, h, r):
        """(ok, why) for a radio (r) or the station (r None), decided on ONE
        hello (panel M1 r3 R1): restart_blocker() on that answer. No answer
        is busy, except an agent that is not loaded and refuses connections
        (it isn't running, so it can't transmit: R6's retry after a failed
        restart)."""
        st = service.probe(h, r)
        name = r["name"] if r else "the station"
        if not st.get("up"):
            label = r["label"] if r else (h.get("station") or {}).get("label", render.STATION_LABEL)
            if st.get("why") in service.NOT_LISTENING and not self.loaded(label):
                return True, f"{name} is not running ({label} not loaded)"
            return False, f"{name} did not answer ({st.get('why', '?')}): it can't be shown idle"
        why = restart_blocker(st, name)
        return (False, why) if why else (True, "idle")

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

    def _port(self, port, state):
        """("present", {pids}) | ("absent", set()) | ("unreadable", set())."""
        exe = _mt().lsof_bin()
        if exe is None:
            return "unreadable", set()
        try:
            r = subprocess.run([exe, "-nP", f"-iTCP:{port}", f"-sTCP:{state}", "-F", "p"], capture_output=True,
                               text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return "unreadable", set()
        out, err = (r.stdout or "").strip(), (r.stderr or "").strip()
        if r.returncode == 0 and out:
            return "present", {int(x[1:]) for x in out.splitlines() if x[:1] == "p" and x[1:].isdigit()}
        if r.returncode == 1 and not out and not err:
            return "absent", set()
        return "unreadable", set()

    def listening(self, port):
        return self._port(port, "LISTEN")[0]

    def connected(self, port):
        """A TCP connection on port (Pat's TNC socket to the old modem)."""
        return self._port(port, "ESTABLISHED")[0]

    def owned_refusal(self, ports, label):
        """Why `ports` are not all listened on by the process of agent `label`."""
        pid = self.pid(label)
        if pid is None:
            return f"{label} has no running process"
        for p in ports:
            state, pids = self._port(p, "LISTEN")
            if state != "present":
                return f"port {p}: {state}"
            if pids != {pid}:
                return f"port {p} is held by pid {', '.join(map(str, sorted(pids)))}, not {label} (pid {pid})"
        return None

    def modem_refusal(self, h, backend):
        """Why backend's listeners are not up as they must be, owned by its
        agent's process (None: they are)."""
        label = label_of(h, backend)
        if backend == "mercury":
            mt = _mt()
            m = h["winlink"]["mercury"]
            try:
                rows = mt.parse_lsof(mt.run_lsof())
            except OSError as e:
                return str(e)
            why, info = mt.snapshot(rows, mt.ports(m["port"], m["broadcast_port"]))
            if why:
                return why
            pid = self.pid(label)
            return None if info["pid"] == pid else f"Mercury's ports are held by pid {info['pid']}, not {label} ({pid})"
        return self.owned_refusal(ports_of(h, backend), label)

    def lane_refusal(self, h, r, backend):
        """Why the radio server r is not listening on backend's lane (the
        port its modem keys through: Mercury exits at start without it)."""
        return self.owned_refusal([h["winlink"][backend]["lane_port"]], r["label"])

    def pat_ready(self, h):
        """The station answers and its Pat is running with a status."""
        st = service.probe(h, None)
        return bool(st.get("up") and st.get("pat_ready"))

    def pat_refusal(self, h, to):
        return pat_refusal(h, to)

    def wait(self, pred, timeout):
        return service._until(pred, timeout)


def restart_blocker(st, name):
    """Why a server that answered `st` (one hello) may not be restarted now
    (None: it may): everything service.idle refuses for, a reserved or held
    PTT, and the station's Pat exchanges (M1 r3 R1-R3)."""
    if st.get("tx") or st.get("tuning"):
        return f"{name} is {'tuning (ATU)' if st.get('tuning') and not st.get('tx') else 'transmitting'}"
    if st.get("ptt"):
        return f"{name} has its PTT held or reserved (a page's voice key may be getting ready)"
    if st.get("armed") or st.get("session"):
        return f"{name} is in a Winlink session"
    if st.get("inbound") or st.get("pat_busy"):
        return f"{name}'s Pat is in an exchange (inbound or busy)"
    if st.get("stopped") and not st.get("stop_saved"):
        return f"{name} has STOP TX latched but not saved; a restart would clear it"
    n = int(st.get("rigctl") or 0)
    if n:
        return f"{name} has {n} rigctl client{'s' if n != 1 else ''} connected (JTDX, WSJT-X...)"
    return None


def trash_path(home, label, today=None):
    """~/.Trash/<label>-<YYYY-MM-DD>.plist, never one that exists."""
    stamp = (today or date.today()).isoformat()
    base = Path(home) / ".Trash" / f"{label}-{stamp}.plist"
    n, p = 1, base
    while p.exists():
        n += 1
        p = base.with_name(f"{label}-{stamp}-{n}.plist")
    return p


def _fence(home, seconds=None):
    """The switch's fence: nothing arms, keys or tunes on the radio while it
    lasts (server.cutover_fenced); it expires, so a switch that dies does not
    wedge the radio."""
    p = Path(home) / FENCE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"until": time.time() + (seconds or FENCE_SWITCH_S)}))
    tmp.replace(p)


def _unfence(home):
    p = Path(home) / FENCE
    if p.exists():
        p.replace(p.with_name(p.name + ".done"))     # moved aside, not removed


def _pend(home, to):
    """Durable: VARA HF stays refused until --finish succeeds (no expiry)."""
    p = Path(home) / (FENCE + PENDING_SUFFIX)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"to": to, "since": time.time()}))
    tmp.replace(p)


def _unpend(home):
    p = Path(home) / (FENCE + PENDING_SUFFIX)
    if p.exists():
        p.replace(p.with_name(p.name + ".done"))


def pending_of(home):
    p = Path(home) / (FENCE + PENDING_SUFFIX)
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {"to": "?"} if p.exists() else None


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
        "fence the radio (no new session, key or tune); every radio with a Winlink lane must answer and be idle",
        f"launchctl bootout {old_l}, if loaded; move ~/{render.AGENTS}/{old_l}.plist to ~/.Trash/{old_l}-<date>.plist",
        f"wait until no {PROC[old]} process runs and nothing listens on {', '.join(map(str, ports_of(h, old)))}",
        f"write winlink.vara_modem = {to!r} to hosts/{h['host']}.json; install what it renders",
        f"restart the radio server and wait for {to}'s lane; then load {new_l} and wait for its listeners",
        f"Pat's config must already say varahf.addr = localhost:{b['port']}"
        + ('; service_codes includes "MERCURY"' if to == "mercury" else "")
        + f"; if not, stop here (VARA arms fenced) until `ranch-radio vara-backend {to} --apply --finish`",
        f"with the station and the radio idle: restart the station (Pat with it, a new process, Pat ready); "
        f"check no connection to {old}'s port {ports_of(h, old)[0]} is left; lift the fence",
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
    """Steps 6-7, under the switch's fence: everything idle (the station and
    the lane radio: an ARDOP session the pending state let through is not
    cut, C3), Pat's config, the modem still listening (C6), the station (Pat)
    restart, a new pid, Pat ready (C6), no stale socket. Only full success
    lifts the pending state (C7)."""
    old = other(to)
    b = h["winlink"][to]
    _fence(home)

    def stop(rc, msg):
        _pend(home, to)
        _unfence(home)
        log(msg)
        return rc
    for r in [None] + [r for r in h["radios"] if r.get("lane")]:
        ok, why = ops.idle(h, r)
        if not ok:
            return stop(1, f"stopped before restarting Pat: {why}; nothing was restarted. "
                           f"Run `ranch-radio vara-backend {to} --apply --finish` when it is idle")
    why = ops.pat_refusal(h, to)
    if why:
        _pend(home, to)
        _unfence(home)
        log(f"stopped before restarting Pat: {why}.")
        log("Pat's config, by hand (ranch-radio never writes it):")
        log(f"  varahf.addr = \"localhost:{b['port']}\"")
        if to == "mercury":
            log("  service_codes = [\"PUBLIC\", \"MERCURY\"]   (Mercury gateways are listed under MERCURY)")
        log(f"then: ranch-radio vara-backend {to} --apply --finish   "
            f"(until then VARA HF sessions are refused here; ARDOP is not)")
        return 2
    why = ops.modem_refusal(h, to)
    if why:
        return stop(1, f"stopped before restarting Pat: {to} is not listening as it must ({why}). "
                       f"Run `ranch-radio vara-backend {to} --apply` again")
    st_l = (h.get("station") or {}).get("label", render.STATION_LABEL)
    before = ops.pid(st_l)
    out = []
    rc = ops.restart(st_l, f"{render.AGENTS}/{st_l}.plist", out)
    _say_out(out, log)
    after = ops.pid(st_l) if not rc else None
    if rc or after is None or after == before:
        return stop(1, f"stopped: the station ({st_l}) did not restart (pid {before} -> {after}); Pat may still "
                       f"run on its old settings. VARA stays refused; run --finish again")
    if not ops.wait(lambda: ops.pat_ready(h), wait_s):
        return stop(1, f"stopped: the station restarted (pid {after}) but its Pat is not running "
                       f"(ranch-radio logs station; doctor). VARA stays refused; run --finish again")
    stale = ports_of(h, old)[0]
    if not ops.wait(lambda: ops.connected(stale) == "absent", wait_s):
        return stop(1, f"WARNING: a connection to {old}'s port {stale} is still open (or can't be read: "
                       f"{ops.connected(stale)}) after the station restart (Pat's old TNC socket?): find it with "
                       f"lsof -nP -iTCP:{stale} and stop it, then --finish again")
    _unpend(home)
    _unfence(home)
    log(f"restarted the station (pid {before} -> {after}, Pat ready); no connection to {old}'s port {stale}. "
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
        _fence(home)
        try:
            rc = _switch(h, target, path, to, ops, home, log, install, wait_s)
        except BaseException:
            _unfence(home)               # the pending state, if the switch got that far, stays
            raise
        if rc:
            _unfence(home)               # the switch's lockout only; pending stays if it was set (C7, R5)
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
    # The first disruptive step is next: from here VARA stays refused until
    # --finish succeeds, whatever happens to this process (R5). A refusal
    # above changed nothing, so it left any earlier state as it was.
    _pend(home, to)
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
    # 3. gone, for real: "absent" only (unreadable is not gone, C5)
    gone = ops.wait(lambda: not ops.loaded(old_l) and not ops.proc_alive(PROC[old])
                    and all(ops.listening(p) == "absent" for p in ports_of(h, old)), wait_s)
    if not gone:
        left = [w for w, on in ((f"{old_l} still loaded", ops.loaded(old_l)),
                                (f"a {PROC[old]} process still runs", ops.proc_alive(PROC[old])),
                                *((f"port {p}: {ops.listening(p)}", ops.listening(p) != "absent")
                                  for p in ports_of(h, old)))
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
    # 5. the radio server first: the new modem keys through its lane, and
    # Mercury exits at start when it can't open it (radio_io.c:323-336,
    # mercury_engine.c:292-299; panel M1 r2 C1). Then the modem.
    lane_radios = [r for r in target["radios"] if r.get("lane")]
    for r in lane_radios:
        out = []
        if ops.restart(r["label"], f"{render.AGENTS}/{r['label']}.plist", out):
            _say_out(out, log)
            # Its old modem is gone and the new one can't start without it:
            # try once more to bring the radio back, and say exactly how (R6).
            again = []
            back = not ops.loaded(r["label"]) and ops.up(r["label"], f"{render.AGENTS}/{r['label']}.plist", again) == 0
            _say_out(again, log)
            log(f"stopped: the radio server {r['label']} did not restart; {to} was NOT loaded. "
                + (f"A second load brought {r['label']} back. " if back else
                   f"It is still down: launchctl bootstrap gui/$(id -u) ~/{render.AGENTS}/{r['label']}.plist "
                   f"(or start it from the menu-bar widget). ")
                + f"Then run `ranch-radio vara-backend {to} --apply` again (a radio that is not loaded "
                  f"counts as idle for it)")
            return 1
        if not ops.wait(lambda r=r: ops.lane_refusal(target, r, to) is None, wait_s):
            log(f"stopped: the radio server {r['label']} is not listening on {to}'s lane "
                f"({ops.lane_refusal(target, r, to)}); {to} was NOT loaded")
            return 1
    new_l = label_of(target, to)
    out = []
    if ops.up(new_l, f"{render.AGENTS}/{new_l}.plist", out):
        _say_out(out, log)
        log(f"stopped: {new_l} did not load")
        return 1
    if not ops.wait(lambda: ops.modem_refusal(target, to) is None, wait_s):
        log(f"stopped: {new_l} is loaded but {to} is not listening as it must "
            f"({ops.modem_refusal(target, to)})")
        return 1
    if not ops.loaded(new_l) or ops.loaded(old_l):
        log(f"stopped: after the switch {new_l} should be loaded and {old_l} not "
            f"(loaded: {new_l} {ops.loaded(new_l)}, {old_l} {ops.loaded(old_l)})")
        return 1
    log(f"restarted the radio server; loaded {new_l} ({to} listening)")
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
