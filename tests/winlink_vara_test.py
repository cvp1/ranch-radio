"""Winlink over VARA HF (xara), beside ARDOP: plan winlink-vara, PR A.

server.py --sim with tests/fake_pat.py as Pat and tests/fake_xara.py as
xara. xara keys the radio itself through its own lane
(--winlink-vara-rigctl-port); Pat (ptt_ctrl false) never keys for VARA.
Nothing here transmits: the radio is the simulator.

Unit: check_url (varahf in, varafm / vara out); PatEngine's default modems.
Server:
  - off (no --winlink-vara-*): modems ["ardop"], a varahf connect is refused,
    and nothing listens on a VARA lane port (a leftover xara can't key)
  - on: modems ["ardop", "varahf"]; Pat's config is checked by host and port
    at every arm (varahf.addr, varahf.rig's hamlib address, ptt_ctrl), the
    field to fix is named, and the fixture's password never shows
  - a session keyed by the modem: our QRT's CW ID rides the QRT frame's key
    (on air, source xara), their QRT's CW ID is a fresh key (on air), Pat
    returning before the CW's T 0 keeps the lane armed until it; the
    session record line; the VARA lane's keepalive outlasts ARDOP's
  - cross-modem: during a VARA session the ARDOP lane can't key, and the
    reverse; an unarmed VARA connect (Pat dialing on its own) is refused,
    xara exits non-zero, a new xara serves the next session
  - failure paths, each ending in RX, the lane disarmed, the mode and codec
    put back and an honest ID status: xara dies, Pat dies, operator abort,
    session cap, page closed; ARDOP -> VARA -> ARDOP back to back

    .venv/bin/python tests/winlink_vara_test.py
"""
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import testkit  # noqa: E402
import winlink  # noqa: E402
import winlink_test_lib as lib  # noqa: E402
from rigctl_server import WINLINK_KEEPALIVE_S, WINLINK_VARA_KEEPALIVE_S  # noqa: E402

fails = 0
PASSWORD = "s3cret-fake-pw"
VURL = "varahf:///N0CALL?bw=500&freq=3588.5"
lib.APPS, lib.LANE, lib.PATP, lib.WEB = (testkit.free_port() for _ in range(4))
VLANE, XPORT = testkit.free_port(), testkit.free_port()
while XPORT + 1 in (lib.APPS, lib.LANE, lib.PATP, lib.WEB, VLANE):
    XPORT = testkit.free_port()


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


# --- unit -----------------------------------------------------------------

band = lambda hz: "80m" if 3.5e6 <= hz <= 4e6 else None  # noqa: E731
check("check_url: varahf accepted", winlink.check_url("varahf:///N0CALL?bw=500&freq=3583", band), None)
check("check_url: varafm refused", (winlink.check_url("varafm:///N0CALL", band) or "")[:23],
      "Unsupported transport '")
check("check_url: bare vara refused", (winlink.check_url("vara:///N0CALL", band) or "")[:23], "Unsupported transport '")
check("check_url: varahf off-band freq refused", "outside the amateur bands" in
      (winlink.check_url("varahf:///N0CALL?freq=5000", band) or ""), True)
check("PatEngine default modems: ARDOP only", winlink.DEFAULT_HF_MODEMS, ("ardop",))


# --- harness ----------------------------------------------------------------

def pat_cfg(path, **varahf):
    v = {"addr": f"localhost:{XPORT}", "bandwidth": 500, "rig": "ic7300", "ptt_ctrl": False}
    v.update(varahf)
    rigs = varahf.pop("_rigs", None) or {"ic7300": {"address": f"localhost:{lib.LANE}", "network": "tcp"}}
    v.pop("_rigs", None)
    testkit.pat_config(path, varahf=v, hamlib_rigs=rigs, secure_login_password=PASSWORD)
    return path


def start(vara=True, env=None, extra=(), cfg_over=None):
    cfg = pat_cfg(tempfile.NamedTemporaryFile(prefix="patcfg-", suffix=".json", delete=False).name,
                  **(cfg_over or {}))
    logf = tempfile.NamedTemporaryFile(prefix="fakepat-", suffix=".jsonl", delete=False).name
    xlog = tempfile.NamedTemporaryFile(prefix="xara-", suffix=".log", delete=False).name
    state = os.path.join(tempfile.mkdtemp(prefix="wlvara-"), "addons.json")
    e = dict(os.environ, FAKE_PAT_RIGCTL=str(lib.LANE), FAKE_PAT_LOG=logf, **(env or {}))
    e.pop("CC_OBS_TOKENS_FILE", None)
    pat = f"{shlex.quote(sys.executable)} tests/fake_pat.py --config {shlex.quote(cfg)}"
    v = ["--winlink-vara-log", xlog, "--winlink-vara-rigctl-port", str(VLANE),
         "--winlink-vara-port", str(XPORT)] if vara else []
    p = subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(lib.WEB),
         "--rigctl-port", str(lib.APPS), "--winlink-rigctl-port", str(lib.LANE),
         "--winlink-pat", pat, "--winlink-pat-config", cfg, "--winlink-pat-port", str(lib.PATP),
         "--addons-state", state, *v, *extra],
        cwd=ROOT, env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    ports = (lib.WEB, lib.LANE, lib.PATP) + ((VLANE,) if vara else ())
    for _ in range(120):
        try:
            for port in ports:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            time.sleep(0.5)
            return p, {"cfg": cfg, "pat": logf, "xlog": xlog}
        except OSError:
            time.sleep(0.1)
    p.kill()
    print(p.stdout.read())
    raise SystemExit("server did not start")


def xara(f, env=None):
    """fake_xara as launchd would run it (its argv shape: render.py's xara agent)."""
    ev = tempfile.NamedTemporaryFile(prefix="xaraev-", suffix=".jsonl", delete=False).name
    e = dict(os.environ, FAKE_XARA_EVENTS=ev, **(env or {}))
    p = subprocess.Popen([sys.executable, "tests/fake_xara.py", "--call", "N0CALL", "--hf", "500",
                          "--audio", "coreaudio", "--capture", "USB Audio CODEC", "--playback", "USB Audio CODEC",
                          "--radio", "--ptt", f"rigctld:127.0.0.1:{VLANE}", "--addr", "127.0.0.1",
                          "--port", str(XPORT), "--kiss-port", "off", "--drive-db", "-5",
                          "--log", f["xlog"], "--quiet"], cwd=ROOT, env=e,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.events = ev
    if not testkit.wait_for(lambda: _listening(XPORT) and _listening(XPORT + 1), 5):
        p.kill()
        raise SystemExit("fake xara did not start")
    return p


def _listening(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


def events(p, route=None):
    try:
        with open(p.events) as f:
            ev = [json.loads(line) for line in f if line.strip()]
    except OSError:
        return []
    return [e for e in ev if route is None or e["route"] == route]


def kill(p):
    if p.poll() is None:
        p.kill()
        p.wait(5)


def lane_cmd(port, line):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        f = s.makefile("r")
        s.sendall((line + "\n").encode())
        v = f.readline().strip()
        if v == "RPRT 0" and line == "T 1":
            s.sendall(b"T 0\n")
            f.readline()
        return v


def mode():
    with socket.create_connection(("127.0.0.1", lib.APPS), timeout=3) as s:
        f = s.makefile("r")
        s.sendall(b"m\n")
        return f.readline().strip()


def last_id(page):
    e = [x["id"] for x in page.events if x.get("t") == "winlink" and x.get("id")]
    return e[-1] if e else None


def ended(page, timeout=40):
    return page.wait(lib.ended, timeout)


def after(page, e0):
    """Once a session ended: RX, both lanes disarmed (refused at T 1)."""
    time.sleep(0.5)
    return (lib.ptt(), lane_cmd(lib.LANE, "T 1"), lane_cmd(VLANE, "T 1"))


def records(out):
    return [json.loads(ln.split("winlink session: ", 1)[1]) for ln in out.splitlines() if "winlink session: " in ln]


# --- off: no VARA flags ------------------------------------------------------

server, f = start(vara=False)
try:
    page = lib.WebPage(lib.WEB)
    snap = page.hello["winlink"]
    check("VARA off: modems ARDOP only", snap.get("modems"), ["ardop"])
    check("VARA off: nothing listens on a VARA lane port (a leftover xara can't key)", _listening(VLANE), False)
    page.send({"t": "winlink_connect", "url": VURL})
    n = page.wait(lambda e: e.get("t") == "notice" and "not set up" in e.get("msg", ""), 5)
    check("VARA off: a varahf connect is refused", n and n["msg"], "varahf is not set up on this station.")
    page.send({"t": "winlink_arm", "v": True, "scheme": "varahf"})
    n = page.wait(lambda e: e.get("t") == "notice" and "not armed" in e.get("msg", ""), 5)
    check("VARA off: arming for varahf refused", bool(n), True)
    page.close()
finally:
    out = lib.stop(server)

# --- on: Pat's config is checked by host and port, at every arm ---------------

server, f = start()
xp = xara(f)
try:
    page = lib.WebPage(lib.WEB)
    check("VARA on: modems ARDOP and VARA HF", page.hello["winlink"].get("modems"), ["ardop", "varahf"])
    check("VARA on: xara's startup T 0 accepted, nothing keyed",
          ([e["reply"] for e in events(xp, "key")], lib.ptt()), (["RPRT 0"], "0"))
    bad = [
        ("varahf.addr on another host", {"addr": f"192.168.1.50:{XPORT}"}, "varahf.addr"),
        ("varahf.addr on another port", {"addr": "localhost:8399"}, "varahf.addr"),
        ("varahf.rig empty", {"rig": ""}, "varahf.rig"),
        ("varahf.rig's address on another host",
         {"_rigs": {"ic7300": {"address": f"192.168.1.50:{lib.LANE}"}}}, "hamlib_rigs.ic7300.address"),
        ("varahf.ptt_ctrl true", {"ptt_ctrl": True}, "varahf.ptt_ctrl"),
    ]
    for what, over, field in bad:
        pat_cfg(f["cfg"], **over)
        page.events.clear()
        page.send({"t": "winlink_connect", "url": VURL})
        n = page.wait(lambda e: e.get("t") == "notice" and "VARA HF not ready" in e.get("msg", ""), 5)
        check(f"A5 {what}: refused, names {field}", bool(n) and field in n["msg"], True)
        check(f"A5 {what}: never armed", any(e.get("armed") for e in page.events if e.get("t") == "winlink"), False)
    pat_cfg(f["cfg"])
    page.close()
finally:
    out = lib.stop(server)
    kill(xp)
check("A5: the fixture's password is never in the server log", PASSWORD in out, False)

# --- sessions keyed by the modem ------------------------------------------------

server, f = start(extra=("--winlink-mode", "LSB", "--winlink-codec-db", "-38"))
xp = xara(f)
try:
    page = lib.WebPage(lib.WEB)
    m0, codec0 = mode(), page.hello["winlink"].get("codec_db")
    page.send({"t": "winlink_connect", "url": VURL})
    check("ours: armed for VARA HF",
          bool(page.wait(lambda e: e.get("t") == "winlink" and e.get("armed") and e.get("scheme") == "varahf", 8)),
          True)
    check("ours: radio in the session's mode while armed", mode(), "LSB")
    check("ours: ARDOP lane can't key during a VARA session", lane_cmd(lib.LANE, "T 1"), "RPRT -11")
    e = ended(page)
    check("ours: session ended ok", e and e.get("ok"), True)
    i = (e or {}).get("id") or {}
    check("ours: the CW ID rode the QRT frame's key: on air, source xara, nothing owed",
          ((i.get("last") or {}).get("on_air"), (i.get("last") or {}).get("source"), i.get("owed")),
          (True, "xara", False))
    check("ours: xara's every key accepted", {e["reply"] for e in events(xp, "key")}, {"RPRT 0"})
    check("ours: Pat never keyed the lane for VARA",
          [r for r in lib.fake_log(f["pat"]) if r["route"] == "ptt_on"], [])
    check("ours: Pat's QSY went through the ARDOP lane, applied while armed",
          [(r["hz"], r["reply"]) for r in lib.fake_log(f["pat"]) if r["route"] == "qsy"], [(3588500, "RPRT 0")])
    check("ours: RX, both lanes disarmed after", after(page, e), ("0", "RPRT -11", "RPRT -11"))
    check("ours: mode and codec put back", (mode(), page.snapshot().get("codec_db")), (m0, codec0))

    # their QRT: a fresh key right after "tx cw"
    kill(xp)
    xp = xara(f, {"FAKE_XARA_SCRIPT": "theirs"})
    page.events.clear()
    t0 = time.time()
    page.send({"t": "winlink_connect", "url": VURL})
    e = ended(page)
    i = (e or {}).get("id") or {}
    check("theirs: session ended ok (a new xara: Pat's handle re-inits)", e and e.get("ok"), True)
    check("theirs: this session's CW ID on air, source xara",
          ((i.get("last") or {}).get("on_air"), (i.get("last") or {}).get("source"),
           (i.get("last") or {}).get("start", 0) > t0), (True, "xara", True))

    # A3 (i) at the server: Pat returns before the CW's T 0; the lane stays armed until it
    kill(xp)
    xp = xara(f, {"FAKE_XARA_EARLY_DISC": "1", "FAKE_XARA_CW_S": "3"})
    page.events.clear()
    t0 = time.time()
    page.send({"t": "winlink_connect", "url": VURL})
    e = ended(page)
    time.sleep(1.0)
    i = (e or {}).get("id") or {}
    check("early return: session ended ok", e and e.get("ok"), True)
    check("early return: it is this session's ID", (i.get("last") or {}).get("start", 0) > t0, True)
    keys = events(xp, "key")
    check("early return: the CW's T 0 was answered (still armed)",
          keys and (keys[-1]["on"], keys[-1]["reply"]), (False, "RPRT 0"))
    check("early return: on air, source xara", ((i.get("last") or {}).get("on_air"), (i.get("last") or {}).get("source")),
          (True, "xara"))
    page.close()
finally:
    out = lib.stop(server)
    kill(xp)
lines = out.splitlines()
rel = [n for n, ln in enumerate(lines) if "varahf lane PTT released (normal)" in ln]
dis = [n for n, ln in enumerate(lines) if "winlink: disarmed" in ln]
check("early return: the last VARA release comes before the disarm", bool(rel and dis) and rel[-1] < dis[-1], True)
check("ours: Pat's QSY applied on the ARDOP lane while armed",
      any("3588500 Hz applied (armed)" in ln for ln in lines), True)
recs = [r for r in records(out) if r["scheme"] == "varahf"]
check("session record: one line per session, the trial's fields",
      [(r["scheme"], r["bw"], r["gateway"], r["freq_khz"], r["outcome"], r["max_level"]) for r in recs][:1],
      [("varahf", "500", "N0CALL", 3588.5, "ok", 3)])
check("session record: three sessions, each with seconds and message counts",
      [(type(r["seconds"]).__name__, r["sent"], r["received"]) for r in recs],
      [("float", 0, 1)] * 3)

# --- the VARA lane: keepalive, cross-modem gate ---------------------------------

server, f = start()
try:
    page = lib.WebPage(lib.WEB)
    check("VARA keepalive outlasts ARDOP's", WINLINK_VARA_KEEPALIVE_S > WINLINK_KEEPALIVE_S, True)
    page.send({"t": "winlink_arm", "v": True})                       # ARDOP (no scheme, as before)
    time.sleep(0.3)
    check("ARDOP armed: VARA lane can't key", lane_cmd(VLANE, "T 1"), "RPRT -11")
    check("ARDOP armed: ARDOP lane keys", lane_cmd(lib.LANE, "T 1"), "RPRT 0")
    page.send({"t": "winlink_arm", "v": False})
    page.send({"t": "winlink_arm", "v": True, "scheme": "varahf"})
    time.sleep(0.3)
    check("VARA armed: ARDOP lane can't key", lane_cmd(lib.LANE, "T 1"), "RPRT -11")
    c = socket.create_connection(("127.0.0.1", VLANE), timeout=3)
    cf = c.makefile("r")
    c.sendall(b"T 1\n")
    check("VARA armed: VARA lane keys", cf.readline().strip(), "RPRT 0")
    time.sleep(WINLINK_KEEPALIVE_S + 1.5)
    check(f"VARA lane silent {WINLINK_KEEPALIVE_S + 1.5:g}s: still keyed", lib.ptt(), "1")
    time.sleep(WINLINK_VARA_KEEPALIVE_S - WINLINK_KEEPALIVE_S)
    check(f"VARA lane silent past {WINLINK_VARA_KEEPALIVE_S:g}s: unkeyed", lib.ptt(), "0")
    c.close()
    page.send({"t": "winlink_arm", "v": False})
    page.close()
finally:
    out = lib.stop(server)
check("cross-modem refusal logged with the reason", any("the armed Winlink session is" in ln for ln in out.splitlines()),
      True)

# --- unarmed: Pat dials on its own; xara is refused, exits; a new xara serves the next ---

server, f = start()
xp = xara(f)
try:
    page = lib.WebPage(lib.WEB)
    try:      # straight to Pat, past the page and the lane's arming (Pat dialing on its own)
        urllib.request.urlopen(f"http://127.0.0.1:{lib.PATP}/api/connect?url=" + urllib.parse.quote(VURL),
                               timeout=15).read()
    except Exception:  # noqa: BLE001 - 500: the session failed, as it should
        pass
    testkit.wait_for(lambda: xp.poll() is not None, 5)
    check("unarmed: xara's T 1 refused (RPRT -11)", [e["reply"] for e in events(xp, "key") if e["on"]][:1],
          ["RPRT -11"])
    check("unarmed: xara exits non-zero, as xara does", xp.poll(), 3)
    check("unarmed: nothing transmitted", lib.ptt(), "0")
    xp = xara(f)                                                   # launchd's KeepAlive
    page.send({"t": "winlink_connect", "url": VURL})
    e = ended(page)
    check("restart: the next session succeeds (Pat's VARA handle re-inits)", e and e.get("ok"), True)
    check("restart: Pat opened the modem again",
          len([r for r in lib.fake_log(f["pat"]) if r["route"] == "vara_open" and r.get("ok")]) >= 2, True)
    page.close()
finally:
    out = lib.stop(server)
    kill(xp)
warn = [ln for ln in out.splitlines() if "WARNING" in ln and "PTT refused" in ln]
check("unarmed: one refusal WARNING, naming xara's CW ID", len(warn) == 1 and "xara" in warn[0], True)

# --- failure paths: RX, disarmed, mode and codec back, an honest ID status ---------


def failure(name, env=None, extra=(), act=None, want_on_air=None, want_owed=None):
    server, f = start(extra=("--winlink-mode", "LSB", "--winlink-codec-db", "-38") + tuple(extra))
    xp = xara(f, env)
    try:
        page = lib.WebPage(lib.WEB)
        m0, codec0 = mode(), page.hello["winlink"].get("codec_db")
        page.send({"t": "winlink_connect", "url": VURL})
        page.wait(lambda e: e.get("t") == "winlink" and e.get("armed"), 8)
        if act:
            act(page, f, xp)
        if name == "page closed":                    # nobody to tell: ask a new page
            page = lib.WebPage(lib.WEB)
            e = {"ok": False} if testkit.wait_for(lambda: page.snapshot().get("session") is None, 45) else None
        else:
            e = ended(page, 45)
        check(f"{name}: session ended, not ok", bool(e) and e.get("ok") is False, True)
        time.sleep(1.5)
        snap = page.snapshot()
        i = snap.get("id") or {}
        check(f"{name}: RX and both lanes disarmed", after(page, e), ("0", "RPRT -11", "RPRT -11"))
        check(f"{name}: mode and codec put back", (mode(), snap.get("codec_db")), (m0, codec0))
        if want_on_air is not None:
            check(f"{name}: xara's ID {'on air' if want_on_air else 'not on air'}",
                  ((i.get("last") or {}).get("source"), (i.get("last") or {}).get("on_air")), ("xara", want_on_air))
        check(f"{name}: ID owed is {want_owed}", i.get("owed"), want_owed)
        page.close()
    finally:
        out = lib.stop(server)
        kill(xp)
    return out


def pat_pid(f):
    return next(r["pid"] for r in reversed(lib.fake_log(f["pat"])) if r["route"] == "start")


failure("xara dies", env={"FAKE_XARA_DIE_S": "1.5", "FAKE_XARA_BURST_S": "3"}, want_owed=True)
failure("Pat dies", env={"FAKE_XARA_BURST_S": "1.5", "FAKE_XARA_BURSTS": "3"},
        act=lambda page, f, xp: (time.sleep(1.5), os.kill(pat_pid(f), signal.SIGKILL)),
        want_on_air=True, want_owed=False)
failure("operator abort", env={"FAKE_XARA_BURST_S": "1.5", "FAKE_XARA_BURSTS": "4"},
        act=lambda page, f, xp: (time.sleep(1.5), page.send({"t": "winlink_stop"}),
                                 time.sleep(0.3), page.send({"t": "winlink_stop"})),
        want_on_air=True, want_owed=False)
failure("session cap", env={"FAKE_XARA_BURST_S": "1.5", "FAKE_XARA_BURSTS": "6"},
        extra=("--winlink-session-max", "3"), want_on_air=True, want_owed=False)
out = failure("page closed", env={"FAKE_XARA_BURST_S": "2", "FAKE_XARA_BURSTS": "4"},
              act=lambda page, f, xp: (time.sleep(1.5), page.close()), want_owed=True)
check("page closed: xara's key after the disarm was refused (operator gone)",
      any("PTT refused" in ln for ln in out.splitlines()), True)

# ARDOP -> VARA -> ARDOP, back to back
idlog = tempfile.mkdtemp(prefix="ardoplog-")
server, f = start(env={"FAKE_PAT_TX_S": "1", "FAKE_PAT_ARDOP_LOG": idlog, "FAKE_PAT_ID": "end"},
                  extra=("--winlink-ardop-log", idlog))
xp = xara(f)
try:
    page = lib.WebPage(lib.WEB)
    got = []
    for url in (lib.URL, VURL, lib.URL):
        page.events.clear()
        page.send({"t": "winlink_connect", "url": url})
        e = ended(page)
        i = (e or {}).get("id") or {}
        got.append((url.split(":")[0], bool(e and e.get("ok")), (i.get("last") or {}).get("source"),
                    (i.get("last") or {}).get("on_air"), i.get("owed")))
        time.sleep(0.5)
    check("ARDOP -> VARA -> ARDOP: each ok, its own modem's ID on air", got,
          [("ardop", True, "ardopcf", True, False), ("varahf", True, "xara", True, False),
           ("ardop", True, "ardopcf", True, False)])
    check("back to back: RX, both lanes disarmed", after(page, None), ("0", "RPRT -11", "RPRT -11"))
    page.close()
finally:
    lib.stop(server)
    kill(xp)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
