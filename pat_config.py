"""Pat's config file, read for what keeps Pat from being launched.

Before every start of Pat (winlink.PatEngine), and in `ranch-radio doctor`,
the config Pat will read is checked: a non-empty "listen" (Pat listens on
every launch when no --listen is given, app/app.go), a non-empty
"schedule" (`pat http` runs scheduled connects and listens on its own,
cli/schedule.go), or ax25.beacon.every > 0 means Pat would answer, beacon
or dial with nobody asking. Then Pat is not launched, and the page and
doctor say what to clear. An unreadable file is refused too.

The file holds the Winlink password: only those keys (and agwpe.addr) are
read, nothing else is returned or logged. Stdlib only (doctor imports it).
"""

import json
import os
import sys


def default_pat_config(env=None, platform=None):
    """Where Pat v1.0 reads its config when not given --config: its XDG
    config dir (github.com/adrg/xdg: $XDG_CONFIG_HOME, else ~/Library/
    Application Support on macOS, ~/.config elsewhere) + pat/config.json."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    base = env.get("XDG_CONFIG_HOME") or os.path.expanduser(
        "~/Library/Application Support" if platform == "darwin" else "~/.config")
    return os.path.join(base, "pat", "config.json")


def _flag(argv, *names):
    """The value of a --flag VALUE / --flag=VALUE in argv, or None."""
    for i, a in enumerate(argv):
        for n in names:
            if a == n and i + 1 < len(argv):
                return argv[i + 1]
            if a.startswith(n + "="):
                return a[len(n) + 1:]
    return None


def pat_config_path(args, pat_cmd):
    """The config file the Pat this engine launches will read: the host's
    --winlink-pat-config, else a --config in the Pat command, else Pat's
    own default."""
    explicit = getattr(args, "winlink_pat_config", None)
    if explicit:
        return os.path.expanduser(explicit)
    given = _flag(pat_cmd, "--config")
    return os.path.expanduser(given) if given else default_pat_config()


def _key(d, name):
    """d[name] the way Go's encoding/json matches it (case-insensitive)."""
    if not isinstance(d, dict):
        return None
    if name in d:
        return d[name]
    for k, v in d.items():
        if isinstance(k, str) and k.lower() == name:
            return v
    return None


def read_pat_config(path):
    """(fields, why): the four keys preflight needs from Pat's config, or
    (None, why it can't be read). Never returns or logs anything else from
    the file: it holds the Winlink password."""
    try:
        with open(path, "rb") as f:
            cfg = json.loads(f.read())
    except FileNotFoundError:
        return None, f"Pat's config file {path} does not exist"
    except (OSError, ValueError) as e:
        return None, f"Pat's config file {path} can't be read ({type(e).__name__})"
    if not isinstance(cfg, dict):
        return None, f"Pat's config file {path} is not a JSON object"
    ax25 = _key(cfg, "ax25")
    return {"listen": _key(cfg, "listen"), "schedule": _key(cfg, "schedule"),
            "beacon_every": _key(_key(ax25, "beacon"), "every"),
            "agwpe_addr": _key(_key(cfg, "agwpe"), "addr")}, None


def preflight(path, pat_cmd=(), env=None):
    """Why a Pat started with this config and command would listen, beacon
    or connect on its own (so it must not be launched), or None. An
    unreadable config is refused too: degrade toward safety."""
    env = os.environ if env is None else env
    if _flag(list(pat_cmd), "--listen", "-l"):
        return "Pat's command line has --listen; take it out (listening is started by Radio Remote only)"
    for var in ("PAT_LISTEN", "PAT_SCHEDULE", "PAT_AX25_BEACON_EVERY"):
        if env.get(var) not in (None, "", "0", "[]"):
            return f"the environment sets {var}, which Pat applies over its config; unset it"
    fields, why = read_pat_config(path)
    if why:
        return why
    clear = []
    if fields["listen"] not in (None, [], ""):
        clear.append("\"listen\" (make it [])")
    if fields["schedule"] not in (None, {}, ""):
        clear.append("\"schedule\" (make it {})")
    every = fields["beacon_every"]
    if isinstance(every, (int, float)) and not isinstance(every, bool) and every > 0:
        clear.append("ax25.beacon.every (make it 0)")
    elif every not in (None, 0) and not isinstance(every, (int, float)):
        clear.append("ax25.beacon.every (not a number; make it 0)")
    if clear:
        return f"Pat's config would listen, beacon or connect on its own; clear {', '.join(clear)} in {path}"
    return None
