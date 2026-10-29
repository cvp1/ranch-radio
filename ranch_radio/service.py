"""Starting and restarting what a host file renders, safely.

    start(h)                       first start after install (enable + start)
    restart(h, changed)            reload what an install changed, radios idle only
    idle(h, r)                     is radio r (or the station) safe to restart now?

`install` only writes files. These run the service manager, which install
never did; doing it by hand left the IC-7300 stopped on mac-shack (bootstrap
fails outside the GUI session) and the QMX unserved on linux-station (units never
enabled), both 2026-10-21. A radio that is transmitting or in a Winlink
session is never restarted: it's reported, to be done later. A unit in the
host file's manual_restart is never restarted either.

A restart keeps what was stopped stopped: a Mac agent that wasn't loaded
(switched off from the menu-bar widget) is written but not started, as
systemd's try-restart does on Linux. Only a newly installed agent is loaded.

RANCH_RADIO_LAUNCHCTL / RANCH_RADIO_SYSTEMCTL: stand-ins for tests.
RANCH_RADIO_PROBE: a stand-in for the probe (a command given the URL; prints
the probe's JSON line).
"""

import json
import os
import shlex
import subprocess
import time

from . import render

IDLE_SCRIPT = r"""
import asyncio, json, ssl, sys
import aiohttp
async def main(url):
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    try:
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(url, ssl=ctx, timeout=5) as ws:
                h = json.loads((await ws.receive(timeout=5)).data)
    except Exception as e:
        print(json.dumps({"up": False, "why": type(e).__name__})); return
    st, wl = h.get("state") or {}, h.get("winlink") or {}
    print(json.dumps({"up": True, "tx": bool(st.get("tx")), "armed": bool(wl.get("armed")),
                      "session": bool(wl.get("session"))}))
asyncio.run(main(sys.argv[1]))
"""


def _cmd(name):
    return os.environ.get(f"RANCH_RADIO_{name.upper()}", name)


def run(argv, out):
    """Run one command; record (line, rc, error) in out. Returns rc."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        rc, err = r.returncode, (r.stderr or r.stdout).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        rc, err = 127, f"{type(e).__name__}: {e}"
    out.append((" ".join(argv), rc, err if rc else ""))
    return rc


def _url(h, r=None):
    if r is None:                                   # the Linux station
        return f"{'wss' if (h.get('station') or {}).get('https') else 'ws'}://127.0.0.1:7300/ws"
    if h["os"] == "macos":
        return f"{'wss' if r.get('https') else 'ws'}://127.0.0.1:7300/ws"
    return f"ws://127.0.0.1:{r['web']}/ws"


def probe(h, r=None):
    """What a radio's server (or, r None, the Linux station) says about
    itself: {"up", "tx", "armed", "session"}, or {"up": False, "why"}."""
    stand_in = os.environ.get("RANCH_RADIO_PROBE")
    argv = (shlex.split(stand_in) if stand_in else
            [os.path.expanduser(h["python"]), "-c", IDLE_SCRIPT]) + [_url(h, r)]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        st = json.loads(p.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return {"up": False, "why": "not answering"}
    return st if isinstance(st, dict) else {"up": False, "why": "bad answer"}


def servers(h):
    """{key: (h, r)} for every server this host runs that answers a probe:
    a Mac's radio servers, a Linux host's station and radios with a web port."""
    if h["os"] == "macos":
        return {r["id"]: r for r in h["radios"]}
    out = {"station": None} if h.get("station") else {}
    out.update({r["id"]: r for r in h["radios"] if r.get("web")})
    return out


def idle(h, r=None):
    """(ok, why): ok when nothing would be cut off by a restart. A server
    that doesn't answer is idle (there's nothing to interrupt)."""
    st = probe(h, r)
    if st.get("why") == "not answering":
        return True, "not answering"
    if not st.get("up"):
        return True, "not running"
    name = r["name"] if r else "the station"
    if st.get("tx"):
        return False, f"{name} is transmitting"
    if st.get("armed") or st.get("session"):
        return False, f"{name} is in a Winlink session"
    return True, "idle"


def _agents(h, arts):
    return [(a.content["Label"], a.path) for a in arts if a.fmt == "plist"]


def _wait_s():
    return float(os.environ.get("RANCH_RADIO_LAUNCHD_WAIT", "10"))


def _loaded(label):
    return run([_cmd("launchctl"), "print", f"gui/{os.getuid()}/{label}"], []) == 0


def _until(pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.25)
    return pred()


def _mac_down(label, out):
    """bootout, then wait until launchd has really let go: it returns before
    the old instance is gone, and a load in that window does nothing while
    still exiting 0 (mac-shack, 2026-10-21: the IC-7300 stayed down)."""
    run([_cmd("launchctl"), "bootout", f"gui/{os.getuid()}/{label}"], out)
    if not _until(lambda: not _loaded(label), _wait_s()):
        out.append((f"{label}: still loaded {_wait_s():g} s after bootout", 1, ""))


def _mac_up(label, path, out):
    """bootstrap, falling back to load (bootstrap fails outside the Mac's GUI
    session: 5: Input/output error). Then make sure it is loaded: retry the
    load once, and say so loudly if it still isn't."""
    lc, uid = _cmd("launchctl"), os.getuid()
    full = os.path.expanduser(f"~/{path}")
    if run([lc, "bootstrap", f"gui/{uid}", full], out) != 0:
        run([lc, "load", "-w", full], out)
    if _until(lambda: _loaded(label), _wait_s()):
        return 0
    run([lc, "load", "-w", full], out)
    if _until(lambda: _loaded(label), _wait_s()):
        return 0
    out.append((f"{label}: NOT RUNNING after bootstrap and load; start it from the menu-bar widget "
                f"or: launchctl load -w ~/{path}", 1, ""))
    return 1


def _radio_of(h, art):
    return next((r for r in h["radios"] if r["id"] == art.module), None)


def start(h, home=None):
    """First start: everything the host file renders, enabled and running."""
    out = []
    arts = render.artifacts(h, home=home or os.path.expanduser("~"))
    if h["os"] == "macos":
        for label, path in _agents(h, arts):
            if _loaded(label):
                out.append((f"{label}: already loaded", 0, ""))
                continue
            _mac_up(label, path, out)
        return out
    sc = _cmd("systemctl")
    run([sc, "--user", "daemon-reload"], out)
    units = [a.path.rsplit("/", 1)[-1] for a in arts if a.fmt == "systemd" and not a.path.endswith(".conf")]
    radio_units = [u for u in units if u.endswith(("-remote.service", "-remote.socket"))]
    if radio_units:      # WantedBy their device: a radio plugged in later gets its server
        run([sc, "--user", "enable"] + radio_units, out)
    # The update service is a oneshot with no [Install]: its timer starts it.
    others = [u for u in units if u not in radio_units and u != render.UPDATE_SERVICE]
    if others:
        run([sc, "--user", "enable", "--now"] + others, out)
    for r in h["radios"]:
        if os.path.exists(r["device"]):
            run([sc, "--user", "start", f"{r['id']}-remote.socket", f"{r['id']}-remote.service"], out)
    return out


def force_start(h, keys):
    """Start (or bounce) the servers in keys (servers(h) keys), whatever state
    they are in: after a rollback, a server that died on the bad code may be
    failed or stopped, which a try-restart leaves alone."""
    out = []
    if h["os"] == "macos":
        for r in h["radios"]:
            if r["id"] in keys:
                path = f"{render.AGENTS}/{r['label']}.plist"
                _mac_down(r["label"], out)
                _mac_up(r["label"], path, out)
        return out
    sc = _cmd("systemctl")
    units = (["station.service"] if "station" in keys else []) + \
        [f"{r['id']}-remote.service" for r in h["radios"] if r["id"] in keys]
    if units:
        run([sc, "--user", "reset-failed"] + units, out)
        run([sc, "--user", "restart"] + units, out)
    return out


def restart(h, changed, check_idle=idle, held=None, new=(), skip=()):
    """Reload what changed. Returns (done, deferred): deferred lists what
    wasn't restarted and why (a radio in use, a manual_restart unit).
    held, a list, gets the module id of each server left alone because it
    was in use (to restart later); new holds the paths just installed for
    the first time; skip holds module ids not to touch (the update job,
    when it is the one running)."""
    out, deferred = [], []
    held = [] if held is None else held
    changed = [a for a in changed if a.module not in skip]
    manual = h.get("manual_restart") or {}
    if h["os"] == "macos":
        labels = {}
        for a in changed:
            if a.fmt == "plist":
                labels[a.content["Label"]] = (a.path, _radio_of(h, a))
            elif a.path == render.MAC_ADDONS:           # the radio server reads it at start
                for r in h["radios"]:
                    labels.setdefault(r["label"], (f"{render.AGENTS}/{r['label']}.plist", r))
        for label, (path, r) in labels.items():
            if r is not None:
                ok, why = check_idle(h, r)
                if not ok:
                    deferred.append(f"{label}: {why}; restart it later")
                    held.append(r["id"])
                    continue
            if path not in new and not _loaded(label):
                # Stopped on purpose (the widget's Off): written, left stopped.
                out.append((f"{label}: not running before; left stopped (it starts on the new code)", 0, ""))
                continue
            if _loaded(label):                          # a new agent has nothing to boot out
                _mac_down(label, out)
            _mac_up(label, path, out)
        return out, deferred
    sc = _cmd("systemctl")
    run([sc, "--user", "daemon-reload"], out)
    units, module = {}, {}
    for a in changed:
        if a.path.endswith("station.json"):
            units["station.service"], module["station.service"] = None, "station"
        elif a.fmt == "systemd":
            name = a.path.rsplit("/", 1)[-1]
            unit = a.path.rsplit("/", 2)[-2][:-2] if name.endswith(".conf") else name
            units[unit], module[unit] = _radio_of(h, a), a.module
    if render.WIREPLUMBER_CONF in {a.path for a in changed}:
        deferred.append("wireplumber: restart it yourself when no radio is in use "
                        "(systemctl --user restart wireplumber); every radio's audio drops briefly")
    for unit, r in sorted(units.items()):
        if unit in manual:
            deferred.append(f"{unit}: {manual[unit]}")
            continue
        if unit.endswith(".socket"):
            continue                                    # sockets hold the port across restarts
        if unit == render.UPDATE_SERVICE:
            continue                                    # a oneshot: its next run reads the new file
        if unit == render.UPDATE_TIMER:
            run([sc, "--user", "enable", unit], out)    # a new timer starts; a changed one re-reads its time
            run([sc, "--user", "restart", unit], out)
            continue
        ok, why = check_idle(h, r)
        if not ok:
            deferred.append(f"{unit}: {why}; restart it later")
            held.append(module[unit])
            continue
        run([sc, "--user", "try-restart", unit], out)
    return out, deferred
