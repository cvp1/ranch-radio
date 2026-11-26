"""Mercury HF as this host runs it (plan winlink-mercury M1): its listeners,
checked before a Mercury session is armed and by doctor, and the INI its
launchd agent is pinned to. Stdlib only: ranch_radio/render.py and doctor.py
import it, and so does server.py.

Mercury v1.9.25 binds its TNC control (-p), data (-p + 1) and broadcast (-b)
listeners to every interface and the control port can key the transmitter
with no login (tcp_interfaces.c:373,2362; its README). The build this repo
carries (mercury/0001-bind-addr-env.patch) binds them to MERCURY_BIND_ADDR,
which the agent sets to 127.0.0.1. A binary without the patch, a hand-started
Mercury or a -G/-U websocket would listen on the LAN: so the radio server
reads the live listeners (lsof) before every Mercury arm and refuses unless
every Mercury listener is 127.0.0.1 on exactly the expected ports, and every
expected port is Mercury's.
"""

LABEL = "ranch-radio.mercury"       # its launchd agent
VERSION = "1.9.25"                  # the pinned tag (v1.9.25, 33efac4)
PATCH_MARK = b"MERCURY_BIND_ADDR"   # the bind patch's env var, present in a patched binary
BIND_ENV = {"MERCURY_BIND_ADDR": "127.0.0.1"}
PORT, BROADCAST_PORT, LANE_PORT = 8340, 8342, 4535
MAX_KEYDOWN_S = 16
# lsof: every TCP socket of this user's processes (listeners, and the lane
# connections that bind a Mercury lane client to its process), machine-
# readable (p pid, c command, n address, T state). Read-only; it connects to
# nothing. Called by absolute path: a launchd agent's PATH
# (/opt/homebrew/bin:/usr/bin:/bin) does not have /usr/sbin, where macOS
# keeps it (panel M1 #3).
LSOF_ARGS = ("-nP", "-iTCP", "-F", "pcnT")
LSOF_CANDIDATES = ("/usr/sbin/lsof", "/usr/bin/lsof")
LSOF_TIMEOUT_S = 5.0


def lsof_bin(candidates=LSOF_CANDIDATES):
    """The system lsof, by absolute path whatever PATH says; None if absent."""
    import os
    return next((c for c in candidates if os.path.isfile(c) and os.access(c, os.X_OK)), None)


def lsof_output(rc, stdout, stderr):
    """lsof's text, or raise: the probe must succeed completely (panel M1 #1).
    lsof exits 1 on any error as well as on no match, so exit 1 is accepted
    only with nothing printed (nothing open: later refused as not
    listening); rows and exit 1 mean a partial inventory, and refuse."""
    if rc == 0 or (rc == 1 and not stdout.strip()):
        return stdout
    err = " ".join(stderr.split())[:200]
    raise OSError(f"lsof exited {rc}" + (f": {err}" if err else "") + " (an incomplete listing is not trusted)")


def ports(port=PORT, broadcast_port=BROADCAST_PORT):
    """Mercury's three listeners: control, data (+1), broadcast."""
    return {port: "control", port + 1: "data", broadcast_port: "broadcast"}


def _addr(s):
    addr, _, port = s.rpartition(":")
    try:
        return addr.strip("[]"), int(port)
    except ValueError:
        return addr.strip("[]"), None


def parse_lsof(text):
    """[Row] from `lsof -F pcnT` output. Row = (pid, command, address,
    port, state, peer): address and port are the local end; peer is
    (address, port) for a connection ("a:b->c:d"), else None. A file with no
    TCP state counts as a listener unless it has a peer (then "UNKNOWN")."""
    rows, pid, cmd, cur = [], None, "", None
    for line in (text or "").splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            pid, cmd, cur = (int(val) if val.isdigit() else val), "", None
        elif tag == "c":
            cmd = val
        elif tag == "f":
            cur = None
        elif tag == "n":
            local, arrow, remote = val.partition("->")
            a, p = _addr(local)
            cur = [pid, cmd, a, p, None, _addr(remote) if arrow else None]
            rows.append(cur)
        elif tag == "T" and val.startswith("ST=") and cur is not None:
            cur[4] = val[3:]
    out = []
    for r in rows:
        if r[4] is None:
            # No TCP state: a plain listener row (-F pcn) is a listener; a
            # connection row's state is unknown, and never matches a lane client.
            r[4] = "UNKNOWN" if r[5] else "LISTEN"
        out.append(tuple(r))
    return out


def is_mercury(cmd):
    return cmd.lower().startswith("mercury")


def snapshot(rows, expected):
    """(why, info): why Mercury's listeners are not safe to arm against
    (None: they are), and the verified instance {"pid", "binds"}. The
    instance is the ONE process holding the expected ports; every listener of
    that process (whatever it is called) must be 127.0.0.1 on an expected
    port; every expected port must be its; any other process called mercury
    listening anywhere is refused too. expected: {port: what}."""
    listen = [r for r in rows if r[4] == "LISTEN"]
    for pid, c, a, p, _, _ in listen:
        if p in expected and not is_mercury(c):
            return f"port {p} (Mercury's {expected[p]} port) is held by {c or 'another process'}, not Mercury", None
    owners = sorted({r[0] for r in listen if r[3] in expected}, key=str)
    if len(owners) > 1:
        return f"Mercury's ports are held by {len(owners)} processes (pids {', '.join(map(str, owners))})", None
    pid = owners[0] if owners else None
    for rpid, c, a, p, _, _ in listen:
        if rpid != pid and not is_mercury(c):
            continue
        if rpid != pid:
            return f"a second Mercury (pid {rpid}) listens on {a}:{p}", None
        if p not in expected:
            return (f"Mercury listens on {a}:{p}, which is not one of its ports here "
                    f"({', '.join(map(str, sorted(expected)))}): a -U/-G websocket or another Mercury"), None
        if a != "127.0.0.1":
            where = "every interface" if a in ("*", "0.0.0.0", "::", "") else a
            return (f"Mercury's {expected[p]} port {p} listens on {where}, not 127.0.0.1 only: anyone on the "
                    f"network could key the radio (is the bind patch in, and MERCURY_BIND_ADDR set?)"), None
    binds = frozenset((a, p) for rpid, _, a, p, _, _ in listen if pid is not None and rpid == pid)
    gone = [f"{expected[p]} {p}" for p in sorted(expected) if ("127.0.0.1", p) not in binds]
    if gone:
        return (f"Mercury is not listening on its {', '.join(gone)} port{'s' if len(gone) > 1 else ''} "
                f"(not running?)"), None
    return None, {"pid": pid, "binds": binds}


def listener_refusal(rows, expected):
    """snapshot()'s why alone (doctor)."""
    return snapshot(rows, expected)[0]


def lane_client_pid(rows, peer_port, lane_port):
    """The pid of the process whose TCP connection from 127.0.0.1:peer_port
    reaches the lane on lane_port, or None."""
    for pid, _, a, p, state, peer in rows:
        if (peer == ("127.0.0.1", lane_port) and p == peer_port and a == "127.0.0.1"
                and state == "ESTABLISHED"):
            return pid
    return None


def run_lsof(timeout=LSOF_TIMEOUT_S):
    """lsof's text, synchronously (doctor); raises OSError when it can't be read."""
    import subprocess
    exe = lsof_bin()
    if exe is None:
        raise OSError(f"lsof not found (looked in {', '.join(LSOF_CANDIDATES)})")
    try:
        r = subprocess.run([exe, *LSOF_ARGS], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise OSError(f"lsof did not answer in {timeout:g} s") from e
    return lsof_output(r.returncode, r.stdout, r.stderr)


def ini_text(call, port, broadcast_port, lane_port):
    """The INI the agent runs Mercury with (-C): no UI, CoreAudio, Hamlib
    NET rigctl PTT on this host's Mercury lane, keydowns held to 16 s.
    Mercury has no INI key for the ARQ bandwidth: BW2300 is its default
    (datalink_arq/carousel.c) and Pat's URL bw= sets each session's
    (docs/TNC.md, BW); a BW500 peer still forces 500 (plan 2.3, not refused).
    The agent's flags repeat the ports and PTT: the CLI wins over the INI."""
    return "\n".join([
        "; Written by ranch-radio from the host file (winlink.mercury). Edit the host file, not this.",
        "[main]",
        "ui_enabled = false",
        "sound_system = coreaudio",
        f"arq_tcp_base_port = {port}",
        f"broadcast_tcp_port = {broadcast_port}",
        f"callsign = {call}",
        "verbose = false",
        "log_max_mb = 20",
        "",
        "[ptt]",
        "method = hamlib",
        "hamlib_model = 2",
        f"device = 127.0.0.1:{lane_port}",
        "",
        "[arq]",
        f"max_keydown_s = {MAX_KEYDOWN_S}",
        "",
    ])


def agent_argv(bin_path, ini, audio_in, audio_out, port, broadcast_port, lane_port, log):
    """The launchd agent's ProgramArguments: no -G, no -U (no websocket), no
    -Q (it keys the radio for a second), PTT through the Mercury lane only."""
    return [bin_path, "-C", ini, "-x", "coreaudio", "-i", audio_in, "-o", audio_out,
            "-p", str(port), "-b", str(broadcast_port),
            "-P", "hamlib", "-R", "2", "-A", f"127.0.0.1:{lane_port}", "-L", log]
