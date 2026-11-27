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
from pathlib import Path

from . import render

IDLE_SCRIPT = r"""
import asyncio, json, ssl, sys
import aiohttp
async def main(url):
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    try:
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(url, ssl=ctx, timeout=5) as ws:
                # The hello, whatever comes first (a scope frame, a state): read
                # until it, 5 s in all (2026-11-23, bug bash).
                end = asyncio.get_running_loop().time() + 5
                while True:
                    m = await ws.receive(timeout=max(0.1, end - asyncio.get_running_loop().time()))
                    if m.type != aiohttp.WSMsgType.TEXT:
                        if m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            raise ConnectionError("closed before hello")
                        continue
                    h = json.loads(m.data)
                    if isinstance(h, dict) and h.get("t") == "hello":
                        break
    except Exception as e:
        print(json.dumps({"up": False, "why": type(e).__name__})); return
    st, wl, cf = h.get("state") or {}, h.get("winlink") or {}, h.get("config") or {}
    # tuning: an ATU tune is on the air with tx still false (civ.py sets
    # tuner 2 alone); server.py's own gates count it as transmitting.
    print(json.dumps({"up": True, "tx": bool(st.get("tx")), "tuning": st.get("tuner") == 2,
                      "armed": bool(wl.get("armed")),
                      "session": bool(wl.get("session")), "rigctl": int(h.get("rigctl_clients") or 0),
                      "stopped": bool(cf.get("tx_stopped")), "stop_saved": bool(cf.get("stop_saved")),
                      "ptt": bool(h.get("ptt_held")),
                      # the station's Pat: its exchanges, and whether it is up now (live
                      # child, this generation's Status, live WebSocket: PatEngine.pat_ready)
                      "inbound": bool(wl.get("inbound")), "pat_busy": bool(wl.get("pat_busy")),
                      "admitting": bool(wl.get("admitting")),
                      "listen_open": bool((wl.get("listen") or {}).get("window") or (wl.get("listen") or {}).get("busy")),
                      "pat_ready": wl.get("pat_ready") is True and not wl.get("blocked")}))
asyncio.run(main(sys.argv[1]))
"""

# Sent just before a manual stop or restart: STOP TX latches the radio, so
# nothing keys between the idle check and the service manager's stop. Marked
# shutdown, so the radio doesn't save it: a restarted radio starts as it was
# (stopped only if the operator had pressed STOP TX).
TXSTOP_SCRIPT = r"""
import asyncio, json, ssl, sys
import aiohttp
async def main(url, why):
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    try:
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(url, ssl=ctx, timeout=5) as ws:
                await ws.receive(timeout=5)
                await ws.send_str(json.dumps({"t": "tx_stop", "why": why, "shutdown": True}))
                while True:
                    m = json.loads((await ws.receive(timeout=5)).data)
                    if m.get("t") == "tx_stop" and m.get("stopped"):
                        print(json.dumps({"stopped": True})); return
    except Exception as e:
        print(json.dumps({"stopped": False, "why": type(e).__name__}))
asyncio.run(main(sys.argv[1], sys.argv[2]))
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
    if r is None:                                   # the station (both OSes)
        return f"{'wss' if (h.get('station') or {}).get('https') else 'ws'}://127.0.0.1:7300/ws"
    return f"ws://127.0.0.1:{r['web']}/ws"          # a radio's own page, on loopback behind it


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
    """{key: r} for every server this host runs that answers a probe: the
    station (key "station", r None) and each radio with a web port."""
    out = {"station": None} if h.get("station") else {}
    out.update({r["id"]: r for r in h["radios"] if r.get("web")})
    return out


# What the probe says when nothing accepts the connection (IDLE_SCRIPT prints
# the exception's type name): ClientConnectorError is aiohttp's refused connect.
NOT_LISTENING = {"ClientConnectorError", "ConnectionRefusedError"}


def idle(h, r=None, drop_rigctl=False, strict=False):
    """(ok, why): ok when nothing would be cut off by a restart. A server
    that doesn't answer is idle (there's nothing to interrupt).

    A connected rigctl client (JTDX, WSJT-X) counts too: a restart drops
    its rig control, and JTDX waits on an error dialog instead of
    reconnecting. drop_rigctl (update --drop-rigctl)
    restarts anyway; transmitting and Winlink are never overridden."""
    st = probe(h, r)
    name = r["name"] if r else "the station"
    if not st.get("up"):
        # strict (the update's own restart, 2026-11-23, bug bash): a server the
        # service manager runs that doesn't answer may be keyed and hung, so it
        # is left for later, as `ranch-radio restart` already does. A rollback
        # keeps the lenient reading: it must be able to restart a server the
        # new code left hung.
        # Refused means nothing is listening (crashed, or between restarts):
        # nothing transmits through it, and an update must be able to put
        # fixed code on a server that crash-loops. Anything else (a handshake
        # or hello that times out) may be a hung server still keyed.
        refused = st.get("why") in NOT_LISTENING
        if strict and not refused and active(h, r["id"] if r else "station"):
            return False, (f"{name} is running but not answering ({st.get('why', '?')}): it can't be shown "
                           "idle, so it is left on the old code")
        return True, "not answering" if st.get("why") == "not answering" else "not running"
    if st.get("tx") or st.get("tuning"):
        return False, f"{name} is {'tuning (ATU)' if st.get('tuning') and not st.get('tx') else 'transmitting'}"
    if st.get("armed") or st.get("session"):
        return False, f"{name} is in a Winlink session"
    if st.get("stopped") and not st.get("stop_saved"):     # in memory only: a restart would clear it
        return False, f"{name} has STOP TX latched; a restart would clear it"
    n = int(st.get("rigctl") or 0)
    if n and not drop_rigctl:
        return False, (f"{name} has {n} rigctl client{'s' if n != 1 else ''} connected (JTDX, WSJT-X...): "
                       "a restart drops its rig control (update --apply --drop-rigctl to restart anyway)")
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
        st = h.get("station")
        if st is not None and "station" in keys:
            label = st.get("label", render.STATION_LABEL)
            _mac_down(label, out)
            _mac_up(label, f"{render.AGENTS}/{label}.plist", out)
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
        st_label = (h.get("station") or {}).get("label", render.STATION_LABEL)
        labels = {}
        for a in changed:
            if a.fmt == "plist":
                labels[a.content["Label"]] = (a.path, _radio_of(h, a))
            elif a.path == render.STATION_JSON:         # the station reads it at start
                labels.setdefault(st_label, (f"{render.AGENTS}/{st_label}.plist", None))
        for label, (path, r) in labels.items():
            if r is not None or label == st_label:      # a radio, or the station (r None): in use?
                ok, why = check_idle(h, r)
                if not ok:
                    deferred.append(f"{label}: {why}; restart it later")
                    held.append(r["id"] if r is not None else "station")
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


def touched(h, changed, new=(), skip=()):
    """What restart(h, changed, new=new, skip=skip) may restart or start, as
    JSON for the update's journal, with what a rollback needs to put each
    back: Mac {"label", "path", "radio", "station", "loaded" (before), "new"};
    Linux {"unit", "radio", "module", "new"}. new: paths installed for the first time."""
    changed = [a for a in changed if a.module not in skip]
    if h["os"] == "macos":
        st_label = (h.get("station") or {}).get("label", render.STATION_LABEL)
        out = {}
        for a in changed:
            if a.fmt == "plist":
                r = _radio_of(h, a)
                out.setdefault(a.content["Label"], (a.path, r["id"] if r else None, a.path in new))
            elif a.path == render.STATION_JSON:
                out.setdefault(st_label, (f"{render.AGENTS}/{st_label}.plist", None, False))
        return [{"label": label, "path": path, "radio": rid, "station": label == st_label,
                 "loaded": _loaded(label), "new": is_new} for label, (path, rid, is_new) in out.items()]
    out = {}
    for a in changed:
        if a.path.endswith("station.json"):
            out.setdefault("station.service", (None, "station", False))
        elif a.fmt == "systemd":
            name = a.path.rsplit("/", 1)[-1]
            dropin = name.endswith(".conf")
            unit = a.path.rsplit("/", 2)[-2][:-2] if dropin else name
            r = _radio_of(h, a)
            # A new drop-in for a unit that was there already doesn't make the unit new.
            out.setdefault(unit, (r["id"] if r else None, a.module, a.path in new and not dropin))
    return [{"unit": u, "radio": rid, "module": mod, "new": is_new} for u, (rid, mod, is_new) in out.items()]


def roll_back(h, agents, home=None, check_idle=idle, held=None, restored=()):
    """After a rollback has put the files back (updater.restore_files): bring
    what the update restarted back up on them, and stop what it added. Works
    from the journal, never from a render: the old code's reading of an
    edited host file is not what ran before. Returns (done, deferred) like
    restart(); held gets the id of each server left alone because it is in
    use (transmitting, in a Winlink session)."""
    out, deferred = [], []
    held = [] if held is None else held
    home = Path(home or os.path.expanduser("~"))
    radios = {r["id"]: r for r in h["radios"]}
    manual = h.get("manual_restart") or {}
    if h["os"] == "macos":
        for e in agents:
            label, path = e["label"], e["path"]
            if not (home / path).exists():
                # Added by the update: nothing of it is left to run.
                if _loaded(label):
                    _mac_down(label, out)
                    out.append((f"{label}: added by the update; booted out", 0, ""))
                continue
            if not e.get("loaded"):
                if _loaded(label):                      # stopped before: stopped again
                    _mac_down(label, out)
                continue
            r = radios.get(e.get("radio"))
            if r is not None or e.get("station"):
                ok, why = check_idle(h, r)
                if not ok:
                    deferred.append(f"{label}: {why}; restart it later")
                    held.append(r["id"] if r is not None else "station")
                    continue
            if _loaded(label):
                _mac_down(label, out)
            _mac_up(label, path, out)
        return out, deferred
    sc = _cmd("systemctl")
    run([sc, "--user", "daemon-reload"], out)
    if render.WIREPLUMBER_CONF in set(restored):
        deferred.append("wireplumber: its file is back as before the update; restart it yourself when no "
                        "radio is in use (systemctl --user restart wireplumber)")
    for e in agents:
        unit = e["unit"]
        if e.get("new") and not (home / render.UNIT_DIR / unit).exists():
            run([sc, "--user", "disable", "--now", unit], out)   # added by the update
            continue
        if unit in manual:
            deferred.append(f"{unit}: {manual[unit]}")
            continue
        if unit.endswith(".socket") or unit == render.UPDATE_SERVICE:
            continue
        if unit == render.UPDATE_TIMER:
            run([sc, "--user", "restart", unit], out)
            continue
        r = radios.get(e.get("radio"))
        ok, why = check_idle(h, r)
        if not ok:
            deferred.append(f"{unit}: {why}; restart it later")
            held.append(e.get("module") or "station")
            continue
        run([sc, "--user", "try-restart", unit], out)
    return out, deferred


# ---------------------------------------------------------------- status, stop, restart
# By hand, from `ranch-radio status|stop|restart`. Stricter than the update's
# idle(): a server the service manager says is running but that does not
# answer is NOT taken as idle (it may be keyed and hung), and restart refuses
# a radio whose STOP TX is latched only in memory (a server without
# --stop-state: a restart would quietly clear it). A saved latch restarts
# stopped, so it doesn't hold a restart back (2026-11-21). No --force: transmitting and Winlink are never
# overridden here; the service manager is the operator's own escape hatch
# (review plan PR 2, panel 2026-11-21).

def _unit_names(h, key):
    if key == "station":
        return ["station.service"]
    return [f"{key}-remote.socket", f"{key}-remote.service"]     # the socket would start it again


def _label(h, key):
    if key == "station":
        return (h.get("station") or {}).get("label", render.STATION_LABEL)
    return next(r["label"] for r in h["radios"] if r["id"] == key)


def active(h, key):
    """Does the service manager have it running? (Mac: loaded; Linux: active)."""
    if h["os"] == "macos":
        return _loaded(_label(h, key))
    unit = _unit_names(h, key)[-1]
    return run([_cmd("systemctl"), "--user", "is-active", "--quiet", unit], []) == 0


def status(h):
    """One row per server: {key, name, active, up, tx, armed, session, rigctl, stopped, why}."""
    rows = []
    for key, r in servers(h).items():
        on = active(h, key)
        st = probe(h, r) if on else {"up": False, "why": "not running"}
        rows.append({"key": key, "name": r["name"] if r else "the station", "active": on, **st})
    return rows


def stoppable(h, key, r, restarting=False, drop_rigctl=False):
    """(ok, why) for a manual stop/restart of server key."""
    name = r["name"] if r else "the station"
    if not active(h, key):
        return True, "not running"
    st = probe(h, r)
    if not st.get("up"):
        return False, (f"{name} is running but not answering ({st.get('why', '?')}): it can't be shown idle, "
                       "so it is left alone (the service manager can still stop it)")
    if st.get("tx") or st.get("tuning"):
        return False, f"{name} is {'tuning (ATU)' if st.get('tuning') and not st.get('tx') else 'transmitting'}"
    if st.get("armed") or st.get("session"):
        return False, f"{name} is in a Winlink session"
    if restarting and st.get("stopped") and not st.get("stop_saved"):
        return False, f"{name} has STOP TX latched; a restart would clear it (resume TX first, or stop it instead)"
    n = int(st.get("rigctl") or 0)
    if n and not drop_rigctl:
        return False, (f"{name} has {n} rigctl client{'s' if n != 1 else ''} connected (JTDX, WSJT-X...) "
                       "(--drop-rigctl to go ahead)")
    return True, "idle"


def _admit_stop(h, r, why):
    """STOP TX on a radio just before it goes down. True when latched."""
    if r is None:
        return True                                     # the station keys nothing itself
    stand_in = os.environ.get("RANCH_RADIO_TXSTOP")
    argv = (shlex.split(stand_in) if stand_in else
            [os.path.expanduser(h["python"]), "-c", TXSTOP_SCRIPT]) + [_url(h, r), why]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        return bool(json.loads(p.stdout.strip().splitlines()[-1]).get("stopped"))
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return False


def _down(h, key, out):
    if h["os"] == "macos":
        _mac_down(_label(h, key), out)
    else:
        run([_cmd("systemctl"), "--user", "stop"] + _unit_names(h, key), out)


def _up(h, key, out):
    if h["os"] == "macos":
        label = _label(h, key)
        _mac_up(label, f"{render.AGENTS}/{label}.plist", out)
    else:
        sc = _cmd("systemctl")
        units = _unit_names(h, key)
        run([sc, "--user", "reset-failed"] + units, out)
        run([sc, "--user", "start"] + units, out)


def lifecycle(h, keys, action, drop_rigctl=False):
    """stop or restart each server in keys. Returns (done, refused)."""
    out, refused = [], []
    known = servers(h)
    for key in keys:
        if key not in known:
            refused.append(f"{key}: not a server on {h['host']} (one of: {', '.join(known)})")
            continue
        r = known[key]
        ok, why = stoppable(h, key, r, restarting=action == "restart", drop_rigctl=drop_rigctl)
        if not ok:
            refused.append(f"{key}: {why}")
            continue
        if why != "not running" and not _admit_stop(h, r, f"ranch-radio {action}"):
            refused.append(f"{key}: STOP TX before the {action} was not confirmed; left alone")
            continue
        if action == "stop":
            if why == "not running":
                out.append((f"{key}: already stopped", 0, ""))
                continue
            _down(h, key, out)
        else:
            if why != "not running":
                _down(h, key, out)
            _up(h, key, out)
    return out, refused
