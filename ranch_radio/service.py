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

RANCH_RADIO_LAUNCHCTL / RANCH_RADIO_SYSTEMCTL: stand-ins for tests.
"""

import json
import os
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


def idle(h, r=None):
    """(ok, why): ok when nothing would be cut off by a restart. A server
    that doesn't answer is idle (there's nothing to interrupt)."""
    py = os.path.expanduser(h["python"])
    try:
        p = subprocess.run([py, "-c", IDLE_SCRIPT, _url(h, r)], capture_output=True, text=True, timeout=20)
        st = json.loads(p.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
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
    others = [u for u in units if u not in radio_units]
    if others:
        run([sc, "--user", "enable", "--now"] + others, out)
    for r in h["radios"]:
        if os.path.exists(r["device"]):
            run([sc, "--user", "start", f"{r['id']}-remote.socket", f"{r['id']}-remote.service"], out)
    return out


def restart(h, changed, check_idle=idle):
    """Reload what changed. Returns (done, deferred): deferred lists what
    wasn't restarted and why (a radio in use, a manual_restart unit)."""
    out, deferred = [], []
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
                    continue
            _mac_down(label, out)
            _mac_up(label, path, out)
        return out, deferred
    sc = _cmd("systemctl")
    run([sc, "--user", "daemon-reload"], out)
    units = {}
    for a in changed:
        if a.path.endswith("station.json"):
            units["station.service"] = None
        elif a.fmt == "systemd":
            name = a.path.rsplit("/", 1)[-1]
            unit = a.path.rsplit("/", 2)[-2][:-2] if name.endswith(".conf") else name
            units[unit] = _radio_of(h, a)
    if render.WIREPLUMBER_CONF in {a.path for a in changed}:
        deferred.append("wireplumber: restart it yourself when no radio is in use "
                        "(systemctl --user restart wireplumber); every radio's audio drops briefly")
    for unit, r in sorted(units.items()):
        if unit in manual:
            deferred.append(f"{unit}: {manual[unit]}")
            continue
        if unit.endswith(".socket"):
            continue                                    # sockets hold the port across restarts
        ok, why = check_idle(h, r)
        if not ok:
            deferred.append(f"{unit}: {why}; restart it later")
            continue
        run([sc, "--user", "try-restart", unit], out)
    return out, deferred
