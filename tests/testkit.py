"""Shared test plumbing: OS-assigned ports, waiting on a condition instead of a
fixed sleep (CI's two-CPU runners are slower than any guess), and a clean Pat."""
import socket
import time


def free_port():
    """A TCP port the OS says is free (bind to port 0)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pair_port(taken=()):
    """A port whose next one up is free too, neither in `taken`: ardopcf and
    xara (and their stand-ins) listen on PORT and PORT+1. The OS hands ports
    out in a run, so a stand-in's second port once landed on a station's web
    port, which looked exactly like a hung station (2026-11-09)."""
    taken = set(taken)
    while True:
        p = free_port()
        if p in taken or p + 1 in taken:
            continue
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p + 1))
            except OSError:
                continue
        return p


def lan_address():
    """A non-loopback address on which a 0.0.0.0 listener answers, or None.
    The hostname may resolve to 127.0.0.1 and the default route may be a VPN
    tunnel, whose own address a host cannot reach (a five-second timeout per
    call, and a scenario that raises, 2026-11-10): try several and keep the
    first a control listener answers on."""
    cands = []
    try:
        cands += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    for target in ("192.168.1.1", "10.0.0.1", "192.0.2.1"):  # ranch-radio: allow (generic test addresses)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
                u.connect((target, 9))       # routing lookup only; nothing is sent
                cands.append(u.getsockname()[0])
        except OSError:
            pass
    for ip in dict.fromkeys(cands):
        if ip.startswith("127.") or ip == "0.0.0.0":
            continue
        with socket.socket() as ctl:
            ctl.bind(("0.0.0.0", 0))
            ctl.listen()
            try:
                socket.create_connection((ip, ctl.getsockname()[1]), timeout=1).close()
                return ip
            except OSError:
                continue
    return None


def wait_for(pred, timeout=4.0, step=0.05):
    """True as soon as pred() holds, False if it still doesn't after timeout."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def pat_config(path, **over):
    """Write a Pat config.json safe for the server's preflight (no listen,
    schedule or beacon) and return its path, so no test reads this machine's
    own Pat config."""
    import json
    cfg = {"mycall": "N9ABC", "secure_login_password": "", "locator": "FN31",
           "listen": [], "schedule": {}, "ax25": {"engine": "agwpe", "beacon": {"every": 0}},
           "agwpe": {"addr": "127.0.0.1:8000", "radio_port": 0},
           "connect_aliases": {}, "hamlib_rigs": {}, "version_reporting_disabled": True}
    for k, v in over.items():
        cfg[k] = v
    with open(path, "w") as f:
        json.dump(cfg, f)
    return path


def real_pat_env(home, *calls):
    """Environment for a real `pat` kept inside HOME, with its periodic
    account check pre-stamped for each call: otherwise Pat asks the CMS over
    the internet and waits two minutes on a web prompt before any connect."""
    import json
    import os
    from datetime import UTC, datetime
    state = os.path.join(home, "pat")
    os.makedirs(state, exist_ok=True)
    stamp = json.dumps(datetime.now(UTC).isoformat())
    for c in calls:
        for name in ("account-confirmed", "version_report"):
            with open(os.path.join(state, f".{name}_{c.upper()}.json"), "w") as f:
                f.write(stamp + "\n")
    return {**os.environ, "HOME": home, "XDG_CONFIG_HOME": home, "XDG_DATA_HOME": home,
            "XDG_STATE_HOME": home}


def fake_pat_cmd(**over):
    """The command for tests/fake_pat.py as Pat, with a fresh pat_config()."""
    import shlex
    import sys
    import tempfile
    cfg = pat_config(tempfile.NamedTemporaryFile(prefix="patcfg-", suffix=".json", delete=False).name, **over)
    return f"{shlex.quote(sys.executable)} tests/fake_pat.py --config {shlex.quote(cfg)}"
