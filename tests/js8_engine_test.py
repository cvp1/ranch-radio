"""The JS8 engine's launcher (js8_engine.py; the js8 module's render is js8_module_test).

  - version gate: upstream 0.7.1 and +rr.1 refused, +rr.2 and later taken;
    a missing or foreign binary is a refusal, never a start
  - a refused start touches nothing: no settings.json, no exec
  - settings put back over a hostile settings.json (TX armed, auto-reply,
    heartbeat and ACKs on, the default 127.0.0.1:4532, WebSocket on 0.0.0.0,
    the web console on, PSK Reporter on, the passband widening, a schedule);
    the operator's own choices kept; written mode 600
  - an unreadable settings.json moved aside (never deleted), defaults written
  - the exact jf8call argv; QT offscreen; PULSE_SOURCE/SINK only with --pulse
  - --rigctl takes only 127.0.0.1:PORT

Imports js8_engine alone, so a launcher change selects this test only.

    python3 tests/js8_engine_test.py
"""
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import js8_engine  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


tmp = Path(tempfile.mkdtemp(prefix="rr-js8-"))


def fake_bin(name, out):
    p = tmp / name
    p.write_text(f"#!/bin/sh\necho '{out}'\n")
    p.chmod(0o755)
    return str(p)


# ---------------------------------------------------------------- version gate
check("upstream 0.7.1 refused", js8_engine.check_version("0.7.1") is not None, True)
check("+rr.1 refused", js8_engine.check_version("0.7.1+rr.1") is not None, True)
check("+rr.2 taken", js8_engine.check_version("0.7.1+rr.2"), None)
check("a later +rr taken", js8_engine.check_version("0.8.0+rr.10"), None)
check("version read from the binary", js8_engine.version(fake_bin("good", "JF8Call 0.7.1+rr.2")), "0.7.1+rr.2")
for label, path in (("missing", str(tmp / "nothing")), ("not jf8call", fake_bin("other", "hello"))):
    try:
        js8_engine.version(path)
        check(f"a {label} binary refused", "started", "refused")
    except js8_engine.EngineError:
        check(f"a {label} binary refused", "refused", "refused")

GOOD, UPSTREAM = fake_bin("jf8call", "JF8Call 0.7.1+rr.2"), fake_bin("jf8call-up", "JF8Call 0.7.1")


def run(cfg, bin_path=GOOD, extra=()):
    calls, logs = [], []
    rc = js8_engine.main(["run", "--bin", bin_path, "--config-dir", str(cfg), "--rigctl", "127.0.0.1:4533",
                          "--ws-port", "4537", "--call", "N0CALL", "--grid", "FN31", "--audio-in", "USB Audio CODEC",
                          "--audio-out", "USB Audio CODEC", *extra],
                         execve=lambda *a: calls.append(a), log=logs.append)
    return rc, calls, logs


# ---------------------------------------------------------------- refused start touches nothing
cfg = tmp / "refused"
rc, calls, logs = run(cfg, UPSTREAM)
check("upstream: exit 2", rc, 2)
check("upstream: never exec'd", calls, [])
check("upstream: no settings.json written", (cfg / "settings.json").exists(), False)
check("upstream: says why", any("not the Ranch Radio build" in m for m in logs), True)

# ---------------------------------------------------------------- settings put back
cfg = tmp / "hostile"
cfg.mkdir()
hostile = {"txEnabled": True, "autoReply": True, "heartbeatEnabled": True, "heartbeatAckEnabled": True,
           "rigctld": "127.0.0.1:4532", "rigPort": "127.0.0.1:4532", "rigModel": 1, "wsHost": "0.0.0.0",
           "webHost": "0.0.0.0", "webPort": 8088, "pskReporterEnabled": True, "autoWidenPassband": True,
           "autoAtu": True, "freqSchedule": [{"freqKhz": 7078}], "callsign": "N9ABC",
           "txFreqHz": 1234, "submode": 2, "stationInfo": "QTH ranch", "groups": ["@ARES"]}
(cfg / "settings.json").write_text(json.dumps(hostile))
rc, calls, logs = run(cfg)
s = json.loads((cfg / "settings.json").read_text())
check("run: exit 0 and one exec", (rc, len(calls)), (0, 1))
check("put back: TX disarmed, no auto-reply, no heartbeat, no ACKs",
      (s["txEnabled"], s["autoReply"], s["heartbeatEnabled"], s["heartbeatAckEnabled"]), (False, False, False, False))
check("put back: the radio's own rigctl port, network rigctl, CAT PTT",
      (s["rigctld"], s["rigPort"], s["rigModel"], s["pttType"]), ("127.0.0.1:4533", "127.0.0.1:4533", 2, 1))
check("put back: WebSocket on loopback, web console off",
      (s["wsHost"], s["wsPort"], s["webHost"], s["webPort"]), ("127.0.0.1", 4537, "127.0.0.1", 0))
check("put back: nothing retunes or refilters by itself",
      (s["autoWidenPassband"], s["autoAtu"], s["freqSchedule"]), (False, False, []))
check("put back: PSK Reporter off unless asked", s["pskReporterEnabled"], False)
check("put back: the host file's call and grid", (s["callsign"], s["grid"]), ("N0CALL", "FN31"))
check("kept: the operator's offset, submode, info, groups",
      (s["txFreqHz"], s["submode"], s["stationInfo"], s["groups"]), (1234, 2, "QTH ranch", ["@ARES"]))
check("written mode 600", stat.S_IMODE(os.stat(cfg / "settings.json").st_mode), 0o600)
check("says what it put back", any("txEnabled" in m and "put back" in m for m in logs), True)

_, argv_, env = calls[0]
check("the exact jf8call argv", argv_, [GOOD, "--headless", "--config-dir", str(cfg), "--rigctld", "127.0.0.1:4533",
                                         "--ws-host", "127.0.0.1", "--ws-port", "4537", "--web-port", "0"])
check("QT offscreen", env.get("QT_QPA_PLATFORM"), "offscreen")
check("no PULSE_* without --pulse", "PULSE_SINK" in env and env["PULSE_SINK"] == "USB Audio CODEC", False)
_, calls, _ = run(tmp / "pulse", extra=["--pulse"])
env = calls[0][2]
check("--pulse names the card in PULSE_SOURCE and PULSE_SINK",
      (env.get("PULSE_SOURCE"), env.get("PULSE_SINK")), ("USB Audio CODEC", "USB Audio CODEC"))
_, _, _ = run(tmp / "pskr", extra=["--pskreporter"])
check("--pskreporter turns it on", json.loads((tmp / "pskr/settings.json").read_text())["pskReporterEnabled"], True)

# ---------------------------------------------------------------- unreadable settings
cfg = tmp / "bad"
cfg.mkdir()
(cfg / "settings.json").write_text("{not json")
rc, calls, logs = run(cfg)
aside = [p.name for p in cfg.iterdir() if p.name.startswith("settings.json.bad-")]
check("unreadable: moved aside, not deleted", len(aside), 1)
check("unreadable: fresh settings, TX disarmed", json.loads((cfg / "settings.json").read_text())["txEnabled"], False)

# ---------------------------------------------------------------- --rigctl is loopback only
for bad in ("0.0.0.0:4532", "192.168.1.5:4532", "127.0.0.1"):
    try:
        js8_engine.parse(["run", "--bin", GOOD, "--config-dir", "x", "--rigctl", bad, "--ws-port", "4537",
                          "--call", "N0CALL", "--grid", "FN31"])
        check(f"--rigctl {bad} refused", "taken", "refused")
    except SystemExit:
        check(f"--rigctl {bad} refused", "refused", "refused")

print(f"\n{fails} failure(s)" if fails else "\nall ok")
sys.exit(1 if fails else 0)
