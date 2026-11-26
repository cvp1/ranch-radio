"""The JS8 engine: jf8call (JS8Call-compatible, headless) behind a radio.

    js8_engine.py run   --bin PATH --config-dir DIR --rigctl 127.0.0.1:PORT
                        --ws-port N --call CALL --grid GRID
                        [--audio-in NAME --audio-out NAME] [--pulse] [--pskreporter]
    js8_engine.py check --bin PATH        # the version gate, alone

render.py writes one of these per radio with a js8_port (a launchd agent on
a Mac, <id>-js8.service on Linux). `run` re-imposes the safe settings, then
execs jf8call. Stdlib only: render's checks import nothing from here, and
the radio server never does.

Why a launcher and not jf8call's own flags (review 2026-11-21, on a Mac host):
jf8call keeps everything in settings.json and writes back whatever a client
sets, so a value set once survives every restart. Each start therefore puts
back:

- TX disarmed (txEnabled false). Upstream defaults it on.
- No transmission of its own: autoReply, the heartbeat and its ACKs off. The
  Ranch Radio build also needs autoReply for the MSG auto-ACK and the
  overheard-relay "YES MSG ID" notice; upstream 0.7.1 sends both with only
  txEnabled, so a heard "@ALLCALL MSG" keys the radio.
- The rig only through the radio's own rigctl port, never jf8call's default
  127.0.0.1:4532. That port is a live radio on a Mac host, and our port puts
  every key through Station.key: STOP TX, --no-tx, --tx-timeout, and an
  unkey when the client goes quiet or dies (kill -9 mid-frame unkeyed in the
  same millisecond on the simulator, 2026-11-21).
- Nothing retunes or refilters a shared rig by itself: autoWidenPassband
  off (upstream widens any USB filter under 2800 Hz on every poll), autoAtu
  off, no frequency schedule.
- The WebSocket on loopback, its web console off. jf8call checks no Origin
  and no password, so only this host's station may talk to it.
- PSK Reporter only when the host file asks (upstream: on).

jf8call plays a frame even when the PTT it asked for was refused, and says
"tx.finished" all the same: what went out on air is the radio server's
state, never this engine's events.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

MIN_RR = 4                # the Ranch Radio build: VERSION 0.7.1+rr.4 and up (rr.4: whole multi-frame messages)
VERSION_RE = re.compile(r"JF8Call\s+(\S+)")
RR_RE = re.compile(r"\+rr\.(\d+)$")
SETTINGS = "settings.json"


class EngineError(Exception):
    pass


def version(bin_path, timeout=10):
    """jf8call's version string, e.g. "0.7.1+rr.2"."""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    try:
        out = subprocess.run([bin_path, "--version"], capture_output=True, text=True,
                             timeout=timeout, env=env)
    except FileNotFoundError:
        raise EngineError(f"{bin_path}: not found") from None
    except PermissionError:
        raise EngineError(f"{bin_path}: not executable") from None
    except subprocess.TimeoutExpired:
        raise EngineError(f"{bin_path} --version: no answer in {timeout} s") from None
    m = VERSION_RE.search(out.stdout + out.stderr)
    if not m:
        raise EngineError(f"{bin_path} --version: not jf8call ({(out.stdout or out.stderr).strip()[:80]!r})")
    return m.group(1)


def check_version(v):
    """None if v is the Ranch Radio build, else why not."""
    m = RR_RE.search(v)
    if not m:
        return (f"jf8call {v} is not the Ranch Radio build (+rr.{MIN_RR} or later): upstream "
                f"answers a heard @ALLCALL MSG on air by itself")
    if int(m.group(1)) < MIN_RR:
        return f"jf8call {v} is older than the Ranch Radio build +rr.{MIN_RR}"
    return None


def forced(a):
    """The keys every start puts back, whatever settings.json says."""
    return {
        "callsign": a.call, "grid": a.grid,
        "txEnabled": False, "autoReply": False,
        "heartbeatEnabled": False, "heartbeatAckEnabled": False,
        "autoAtu": False, "autoWidenPassband": False, "freqSchedule": [],
        "pskReporterEnabled": bool(a.pskreporter),
        "rigModel": 2, "rigctld": a.rigctl, "rigPort": a.rigctl, "pttType": 1,
        "wsEnabled": True, "wsHost": "127.0.0.1", "wsPort": a.ws_port,
        "webHost": "127.0.0.1", "webPort": 0,
        "audioInputName": a.audio_in or "", "audioOutputName": a.audio_out or "",
    }


def prepare(a, log=print):
    """Write settings.json: the operator's own choices kept (TX offset,
    submode, station info, groups...), the forced keys put back. A file that
    isn't a JSON object is moved aside, never trusted and never deleted."""
    os.makedirs(a.config_dir, mode=0o700, exist_ok=True)
    path = os.path.join(a.config_dir, SETTINGS)
    old = {}
    try:
        with open(path) as f:
            old = json.load(f)
        if not isinstance(old, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        old = {}
    except (ValueError, OSError) as e:
        aside = f"{path}.bad-{time.strftime('%Y%m%d-%H%M%S')}"
        os.replace(path, aside)
        log(f"js8: {path} unreadable ({e}); moved to {aside}, starting from defaults")
        old = {}
    new = dict(old)
    new.update(forced(a))
    changed = sorted(k for k in forced(a) if old.get(k) != new[k] and k in old)
    if changed:
        log(f"js8: put back {', '.join(changed)}")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(new, f, indent=4, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
    return new


def argv(a):
    return [a.bin, "--headless", "--config-dir", a.config_dir, "--rigctld", a.rigctl,
            "--ws-host", "127.0.0.1", "--ws-port", str(a.ws_port), "--web-port", "0"]


def environ(a, base=None):
    env = dict(os.environ if base is None else base)
    env["QT_QPA_PLATFORM"] = "offscreen"
    if a.pulse:
        # jf8call opens the named node and falls back to PULSE_SOURCE/SINK:
        # name the radio's card in both, so a miss never lands on the
        # laptop's own microphone or speakers.
        if a.audio_in:
            env["PULSE_SOURCE"] = a.audio_in
        if a.audio_out:
            env["PULSE_SINK"] = a.audio_out
    return env


def parse(args):
    p = argparse.ArgumentParser(prog="js8_engine.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="is BIN the Ranch Radio build of jf8call?")
    c.add_argument("--bin", required=True)
    r = sub.add_parser("run", help="put the safe settings back, then exec jf8call")
    r.add_argument("--bin", required=True)
    r.add_argument("--config-dir", required=True)
    r.add_argument("--rigctl", required=True, help="the radio's rigctl port, 127.0.0.1:PORT")
    r.add_argument("--ws-port", type=int, required=True)
    r.add_argument("--call", required=True)
    r.add_argument("--grid", required=True)
    r.add_argument("--audio-in", default="")
    r.add_argument("--audio-out", default="")
    r.add_argument("--pulse", action="store_true", help="Linux: name the card in PULSE_SOURCE/PULSE_SINK too")
    r.add_argument("--pskreporter", action="store_true")
    a = p.parse_args(args)
    if a.cmd == "run":
        if not re.fullmatch(r"127\.0\.0\.1:\d{1,5}", a.rigctl):
            p.error(f"--rigctl {a.rigctl!r}: the radio's own port on this host, 127.0.0.1:PORT")
        if not 1 <= a.ws_port < 65536:
            p.error(f"--ws-port {a.ws_port}: a port")
    return a


def main(args=None, execve=os.execve, log=None):
    log = log or (lambda m: print(m, file=sys.stderr, flush=True))
    a = parse(sys.argv[1:] if args is None else args)
    try:
        v = version(a.bin)
    except EngineError as e:
        log(f"js8: {e}")
        return 2
    why = check_version(v)
    if why:
        log(f"js8: refusing to start: {why}")
        return 2
    if a.cmd == "check":
        print(f"jf8call {v}: the Ranch Radio build")
        return 0
    prepare(a, log)
    log(f"js8: jf8call {v} on {a.rigctl}, WebSocket 127.0.0.1:{a.ws_port}, TX disarmed")
    execve(a.bin, argv(a), environ(a))
    return 0                     # only a test's execve returns


if __name__ == "__main__":
    sys.exit(main())
