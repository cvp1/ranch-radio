"""Attended HF listening on the Winlink panel, in headless Chromium, with
server.py --sim behind a station and tests/fake_pat.py as Pat. Nothing goes
on air.

Covers: the HF row is shown (and the packet row hidden) when only
winlink.listen_hf is set up; channels are labelled with band, centre, modem
and the 97.221(b) segment; one click only asks to confirm and starts nothing;
the confirm says attended and to keep the page open; the second click opens
the window; the state line says "Listening (attended)" with the centre; the
HF row's Stop ends it; "I'm here" is hidden while nothing is asked; with no
listen_hf in the host file the row is absent.

    .venv/bin/python tests/winlink_listen_hf_page_test.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp_browser  # noqa: E402
import testkit  # noqa: E402
import winlink_test_lib as L  # noqa: E402

WEB = L.WEB
CDP = testkit.free_port()
CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
PLAN = {"channels": [{"khz": 7102.5, "modem": "ardop"}, {"khz": 7080, "modem": "ardop"}],
        "max_minutes": 60, "presence_minutes": 15}
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:60} got {got!r}" + ("" if ok else f" want {want!r}"))


def start(*extra):
    state = os.path.join(tempfile.mkdtemp(prefix="wlhfpage-"), "addons.json")
    return L.start({"FAKE_PAT_TX_S": "2"}, "--addons-state", state, *extra)


TEXT = "(s) => (document.querySelector(s)?.textContent || '')"
VISIBLE = "(s) => { const e = document.querySelector(s); return !!e && !e.closest('[hidden]') && e.offsetParent !== null; }"

if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")

b = cdp_browser.Browser(CHROME, CDP)


def open_panel():
    b.goto(f"http://127.0.0.1:{WEB}/?v=tabs")
    b.wait("document.querySelector('#conn')?.textContent.includes('Connected')")
    b.js("document.querySelector('#st-nav a[href=\"#/winlink\"]').click()")
    b.wait(f"({VISIBLE})('#winlink')")
    b.wait(f"({TEXT})('#wl-mycall').includes('N9ABC')")


try:
    server, flog = start("--winlink-listen-hf", json.dumps(PLAN))
    try:
        open_panel()
        check("the HF row is shown", bool(b.wait(f"({VISIBLE})('#wl-listen-hf')", 10)), True)
        check("no packet plan: the packet Listen row is hidden", b.js(f"({VISIBLE})('#wl-listen')"), False)
        opts = b.js("[...document.querySelectorAll('#wl-listen-hf-ch option')].map((o) => o.textContent)")
        check("channels: band, centre, modem, segment flag", opts,
              ["40 m · 7102.5 kHz ARDOP · 97.221(b) segment", "40 m · 7080 kHz ARDOP"])
        check("I'm here is hidden while nothing is asked", b.js(f"({VISIBLE})('#wl-listen-here')"), False)
        n0 = len([r for r in L.fake_log(flog) if r.get("route") == "start"])
        b.js("document.querySelector('#wl-listen-hf-btn').click()")
        conf = b.js(f"({TEXT})('#wl-listen-hf-btn')")
        check("one click only asks to confirm", conf.startswith("Confirm: answer calls on 7102.5 kHz ARDOP"), True)
        check("the confirm says to keep the page open", "keep this page open" in conf, True)
        time.sleep(1.5)
        check("one click starts nothing (Pat not relaunched)",
              len([r for r in L.fake_log(flog) if r.get("route") == "start"]) - n0, 0)
        b.js("document.querySelector('#wl-listen-hf-btn').click()")
        got = b.wait(f"({TEXT})('#wl-state').startsWith('Listening (attended) on 7.1025 MHz (ARDOP)')", 25)
        check("the second click opens it: Listening (attended) on the centre", bool(got), True)
        check("the HF row's Stop shows", b.js(f"({VISIBLE})('#wl-listen-hf-stop')"), True)
        check("Connect is disabled while listening", b.js("document.querySelector('#wl-connect').disabled"), True)
        b.js("document.querySelector('#wl-listen-hf-stop').click()")
        check("Stop ends it: Idle", bool(b.wait(f"({TEXT})('#wl-state') === 'Idle'", 30)), True)
        check("Listen shows again", bool(b.wait(f"({VISIBLE})('#wl-listen-hf-btn')", 10)), True)
    finally:
        L.stop(server)

    server, flog = start()
    try:
        open_panel()
        time.sleep(1.0)
        check("no listen_hf in the host file: no HF row", b.js(f"({VISIBLE})('#wl-listen-hf')"), False)
    finally:
        L.stop(server)
finally:
    b.close()

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
