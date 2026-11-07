"""The round-trip probe at the wire and on the packet page (plan 2026-11-07,
section 4): the real hub on tests/fake_kiss_radio.py with messaging "send", a
probe target, and the APRS-IS observer on an in-process fake server; the
page in headless Chromium.

  - POST api/aprs/probe without the page's armed flag: refused
  - the Probe button is disabled until TX ARMED; a click sends one message
    to the target (on the fake radio); a digi copy heard on RF, the IS copy
    with qAR,K7RDG-1 from the fake server, and a }-wrapped ack via AA7JB
    make L1-L5 "yes" in api/aprs/probes and on the page
  - yes, not seen and unknown render differently; unknown is never green
  - a second probe before the first one's deadline: refused, saying so

    CHROME=... python3 tests/packet_probe_page_test.py [screenshot.png]
"""
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
os.environ.pop("CC_OBS_TOKENS_FILE", None)
from ax25 import Frame  # noqa: E402
from cdp_browser import Browser  # noqa: E402
import tnc_hub_test as T  # noqa: E402
from aprs_msg_wire_test import FakeAprsIs  # noqa: E402

CDP = 19000 + random.randrange(1000)
CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
SHOT = sys.argv[1] if len(sys.argv) > 1 else None
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")

logdir = tempfile.mkdtemp(prefix="probepage-")
pfile = os.path.join(logdir, "passcode")
with open(pfile, "w") as f:
    f.write("12345\n")
srv = FakeAprsIs()
radio = T.FakeKissRadio().start()
hub = T.HubProc(radio, tx=True, extra=("--log-dir", logdir, "--aprs-messaging", "send", "--aprs-cooldown", "0",
                                       "--aprs-probe-target", "WXBOT", "--aprs-probe-text", "wx 85602",
                                       "--igate-login", "N9ABC-10", "--igate-passcode-file", pfile,
                                       "--igate-server", f"127.0.0.1:{srv.port}", "--igate-observe"))
b = Browser(CHROME, CDP)
LEGS = "[...document.querySelectorAll('#probe-table tr.probe-row')].slice(0, 1).flatMap((r) => [...r.querySelectorAll('.leg')].map((l) => l.textContent))"
try:
    T.wait_for(lambda: (hub.status()["igate"].get("observe") or {}).get("verified"), 15)
    code, body = hub.post("/api/aprs/probe", {})
    check("api/aprs/probe without the page's armed flag: refused", (code, body.get("error")), (409, "arm TX on the page first"))
    b.goto(f"http://127.0.0.1:{hub.http}/#messages")
    check("the Probe button names the target", b.wait("document.querySelector('#probe-target').textContent"), "to WXBOT")
    check("  and is disabled until TX ARMED", b.js("document.querySelector('#probe-btn').disabled"), True)
    b.js("window.confirm = () => true")
    b.js("document.querySelector('#msg-arm').click()")
    check("armed: enabled", b.wait("!document.querySelector('#probe-btn').disabled && 'on'"), "on")
    b.js("document.querySelector('#probe-btn').click()")
    sent = T.wait_for(lambda: radio.transmitted(), 10) or []
    f = Frame.decode(sent[0][1]) if sent else None
    info = f.info.decode() if f else ""
    check("a click sends one probe message to the target", (str(f.src), str(f.dest), info.split(":")[1].strip()) if f else None,
          ("N9ABC", "APZRR0", "WXBOT"))
    msgno = info.rsplit("{", 1)[-1].rstrip("}")
    time.sleep(0.5)
    copy = Frame.ui("N9ABC", "APZRR0", info.encode(), digis=["K7RDG-1", "WIDE2-1"])
    copy.digis[0].bit7 = True                          # K7RDG-1 repeated it
    radio.inject(copy.encode(), lossy=False)
    srv.send(f"N9ABC>APZRR0,K7RDG-1*,WIDE2-1,qAR,K7RDG-1:{info}\r\n".encode())
    radio.inject(T.aprs("AA7JB", f"}}WXBOT>APRS,TCPIP,AA7JB*::N9ABC    :ack{msgno}", dest="APDW18"), lossy=False)
    got = T.wait_for(lambda: (lambda p: p and all(v["v"] == "yes" for v in p[0]["legs"].values()) and p)(
        T._get(hub, "/api/aprs/probes")["probes"]), 15)
    check("api/aprs/probes: L1-L5 yes", {k: v["v"] for k, v in (got or [{"legs": {}}])[0]["legs"].items()},
          {k: "yes" for k in ("L1", "L2", "L3", "L4", "L5")})
    check("  the page shows five yes", b.wait(f"(l => l.length === 5 && l.every((x) => x === 'yes') && l)({LEGS})", 15),
          ["yes"] * 5)
    code, body = hub.post("/api/aprs/probe", {"armed": True})
    check("a second probe before the first one's deadline: refused", (code, body.get("error", "").startswith("the previous probe")),
          (409, True))

    # Rendering: a finished probe with each verdict (written as the hub's own store rows are).
    db = sqlite3.connect(os.path.join(logdir, "aprs.db"))
    legs = {"L1": {"v": "yes", "t": 1, "evidence": "tx"}, "L2": {"v": "not seen", "why": "no digipeated copy observed"},
            "L3": {"v": "unknown", "why": "observer coverage incomplete"}, "L4": {"v": "unknown", "why": "RF coverage incomplete"},
            "L5": {"v": "not seen", "why": "no ack matched"}}
    db.execute("INSERT INTO probes (t, target, text, token, msg_id, msgno, t_first, window_end, state, legs, coverage, notes,"
               " last_seen) VALUES (?, 'WXBOT', 'wx 85602 abc', 'abc', 0, 'ZZ', ?, ?, 'done', ?, '{}', '[]', ?)",
               (time.time() + 5, time.time(), time.time() + 1470, json.dumps(legs), time.time()))
    db.commit()
    db.close()
    b.js("aprsPage.msgStatus()")
    check("a done probe's verdicts on the page", b.wait(f"(l => l[1] === 'not seen' && l)({LEGS})", 10),
          ["yes", "not seen", "unknown ?", "unknown ?", "not seen"])
    colors = b.js("(() => { const r = document.querySelector('#probe-table tr.probe-row'); const c = (k) => getComputedStyle("
                  "r.querySelector(`.leg[data-leg=${k}]`)).color; return [c('L1'), c('L2'), c('L3')]; })()")
    check("yes, not seen and unknown look different", len(set(colors)), 3)
    check("  unknown is not the 'yes' green", colors[2] != colors[0], True)
    if SHOT:
        b.screenshot(SHOT)
finally:
    b.close()
    hub.stop()
    radio.stop()
    srv.stop()
    shutil.rmtree(logdir, ignore_errors=True)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
