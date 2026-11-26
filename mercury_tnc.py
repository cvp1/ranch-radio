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
# lsof: every TCP listener of this user's processes, machine-readable
# (p pid, c command, n address). Read-only; it never connects to anything.
LSOF_ARGV = ("lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "pcn")
LSOF_TIMEOUT_S = 5.0


def ports(port=PORT, broadcast_port=BROADCAST_PORT):
    """Mercury's three listeners: control, data (+1), broadcast."""
    return {port: "control", port + 1: "data", broadcast_port: "broadcast"}


def parse_lsof(text):
    """[(command, address, port)] from `lsof -F pcn` output. A row whose
    address does not parse keeps port None (and is refused if it is Mercury's)."""
    rows, cmd = [], ""
    for line in (text or "").splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            cmd = ""
        elif tag == "c":
            cmd = val
        elif tag == "n":
            addr, _, port = val.rpartition(":")
            try:
                p = int(port)
            except ValueError:
                p = None
            rows.append((cmd, addr.strip("[]"), p))
    return rows


def is_mercury(cmd):
    return cmd.lower().startswith("mercury")


def listener_refusal(rows, expected):
    """Why Mercury's listeners are not safe to arm against (None: they are).
    expected: {port: what}. Every Mercury listener must be 127.0.0.1 on an
    expected port; every expected port must be listened on, by Mercury only."""
    mine = [(c, a, p) for c, a, p in rows if is_mercury(c)]
    for c, a, p in mine:
        if p not in expected:
            return (f"Mercury listens on {a}:{p}, which is not one of its ports here "
                    f"({', '.join(map(str, sorted(expected)))}): a -U/-G websocket or another Mercury")
        if a != "127.0.0.1":
            where = "every interface" if a in ("*", "0.0.0.0", "::", "") else a
            return (f"Mercury's {expected[p]} port {p} listens on {where}, not 127.0.0.1 only: "
                    f"anyone on the network could key the radio (is the bind patch in, and MERCURY_BIND_ADDR set?)")
    for c, a, p in rows:
        if p in expected and not is_mercury(c):
            return f"port {p} (Mercury's {expected[p]} port) is held by {c or 'another process'}, not Mercury"
    heard = {p for _, _, p in mine}
    gone = [f"{expected[p]} {p}" for p in sorted(expected) if p not in heard]
    if gone:
        return f"Mercury is not listening on its {', '.join(gone)} port{'s' if len(gone) > 1 else ''} (not running?)"
    return None


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
