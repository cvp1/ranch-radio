"""The P2P directory picker in the Winlink panel's Peer mode, in headless
Chromium, against a local fake directory and tests/fake_pat.py as Pat.

Covers: the picker lists the directory under the Peer row; rows this station
can call come first; a row it can't call is dimmed and says why (no VARA HF
here, no packet radio, a mode it doesn't run, this station's own call); a
flag is shown; HTML in a note stays text; a click fills call, DIAL frequency
and the URL; a click NEVER connects (Pat gets no connect), and the positive
control: Connect then its confirm does reach Pat with the picked URL; a
stale copy shows its age when the directory can't be reached; switched off
in the host file, the list is absent. The picker is gone in Gateway mode.

    .venv/bin/python tests/winlink_p2pdir_page_test.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp_browser  # noqa: E402
import testkit  # noqa: E402
import winlink_test_lib as L  # noqa: E402
from winlink_test_lib import fake_log, stop  # noqa: E402

WEB = L.WEB
CDP = testkit.free_port()
CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:60} got {got!r}" + ("" if ok else f" want {want!r}"))


def entry(centre, mode, width, control="attended", hours="daily", flags=()):
    key = "baud" if mode in ("PACKET", "ROBUST_PACKET") else "bw_hz"
    return {"centre_khz": centre, "mode": mode, key: width, "control": control, "hours": hours, "flags": list(flags)}


def station(call, entries, note="", grid="EM12"):
    return {"call": call, "grid": grid, "note": note, "confirmed": "2026-11-01", "source": "maintainer",
            "stale": False, "entries": entries}


DOC = {"schema": 1, "generated": "2026-11-10T18:00:00Z", "licence": "CC0-1.0", "stations": [
    station("N0VAR", [entry(7103.5, "VARA_HF", 500)]),                               # no VARA HF on this station
    station("N0ARD", [entry(7101.5, "ARDOP", 500)], note="<b>bold</b> & <img src=x onerror=alert(1)>"),
    station("N0PKT", [entry(145090, "PACKET", 1200, "auto", "24/7")]),              # no packet radio here
    station("N0VFM", [entry(145050, "VARA_FM", 12500)]),                            # a mode this station doesn't run
    station("N9ABC", [entry(7102.5, "ARDOP", 500)]),                                # this station's own call (fake Pat)
    station("K1AUT", [entry(7090, "ARDOP", 500, "auto", "24/7", ["hf-auto-outside-97.221b"])], grid="FN31"),
]}
DOC["count"] = len(DOC["stations"])


class Dir(BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):
        Dir.hits += 1
        body = json.dumps(DOC).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def wait_for(pred, timeout=10):
    """Poll pred until it holds (a ceiling, not a guess); its last value."""
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.1)
    return pred()


def recs(path, route):
    return [r for r in fake_log(path) if r.get("route") == route]


def start(*extra, cache=None):
    state_dir = tempfile.mkdtemp(prefix="wlp2pdir-")
    if cache is not None:
        with open(os.path.join(state_dir, "p2p-directory.json"), "w") as f:
            json.dump(cache, f)
    return L.start({"FAKE_PAT_TX_S": "2"}, "--addons-state", os.path.join(state_dir, "addons.json"), *extra)


TEXT = "(s) => (document.querySelector(s)?.textContent || '')"
VISIBLE = "(s) => { const e = document.querySelector(s); return !!e && !e.closest('[hidden]') && e.offsetParent !== null; }"
ROWS = ("[...document.querySelectorAll('#wl-p2pdir .wl-dir')].map((r) => "
        "[r.dataset.call, r.tagName, r.classList.contains('wl-dir-off'), r.textContent])")

if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")

srv = ThreadingHTTPServer(("127.0.0.1", 0), Dir)
threading.Thread(target=srv.serve_forever, daemon=True).start()
DIR_URL = f"http://127.0.0.1:{srv.server_address[1]}/v1/directory.json"

b = cdp_browser.Browser(CHROME, CDP)


def open_peer():
    b.goto(f"http://127.0.0.1:{WEB}/?v=tabs")
    b.wait("document.querySelector('#conn')?.textContent.includes('Connected')")
    b.js("document.querySelector('#st-nav a[href=\"#/winlink\"]').click()")
    b.wait(f"({VISIBLE})('#winlink')")
    b.wait(f"({TEXT})('#wl-mycall').includes('N9ABC')")
    b.js("document.querySelector('#wl-mode [data-v=peer]').click()")


try:
    server, flog = start("--p2pdir-url", DIR_URL)
    try:
        b.goto(f"http://127.0.0.1:{WEB}/?v=tabs")
        b.wait("document.querySelector('#conn')?.textContent.includes('Connected')")
        b.js("document.querySelector('#st-nav a[href=\"#/winlink\"]').click()")
        b.wait(f"({VISIBLE})('#winlink')")
        b.js("document.querySelector('#wl-mode [data-v=gateway]').click()")
        check("gateway mode: no directory list", b.js(f"({VISIBLE})('#wl-p2pdir')"), False)
        check("gateway mode: the directory is not fetched", Dir.hits, 0)
        open_peer()
        rows = b.wait(f"{ROWS}.length === 6 && {ROWS}", 15)
        check("peer mode: the directory lists every entry", isinstance(rows, list) and len(rows), 6)
        check("the directory was fetched through the station once", Dir.hits, 1)
        rows = rows if isinstance(rows, list) else []
        by = {r[0]: r for r in rows}
        callable_ = [r[0] for r in rows if not r[2]]
        check("rows this station can call", sorted(callable_), ["K1AUT", "N0ARD"])
        check("callable rows come first", [r[2] for r in rows], [False, False, True, True, True, True])
        check("callable rows are buttons, dimmed ones are not", (by["N0ARD"][1], by["N0VAR"][1]), ("BUTTON", "DIV"))
        check("ARDOP row shows the dial frequency (centre - 1.5 kHz)", "7100 kHz" in by["N0ARD"][3], True)
        check("VARA HF row: says why", "no VARA HF here" in by["N0VAR"][3], True)
        check("packet row: says why", "no packet radio here" in by["N0PKT"][3], True)
        check("VARA FM row: says why", "isn't a mode this station runs" in by["N0VFM"][3], True)
        check("own call: dimmed as this station", "this station" in by["N9ABC"][3], True)
        check("the flag is shown", "97.221(b)" in by["K1AUT"][3], True)
        check("a note's HTML stays text (no element made)",
              b.js("document.querySelectorAll('#wl-p2pdir img, #wl-p2pdir b').length"), 0)
        check("the note is in the row's title as text",
              "<b>bold</b>" in (b.js("document.querySelector('#wl-p2pdir [data-call=N0ARD]').title") or ""), True)
        check("a Get listed link points at the spec",
              b.js("document.querySelector('#wl-p2pdir .wl-dir-head a')?.getAttribute('href')"),
              DIR_URL.replace("v1/directory.json", ""))

        before = len(recs(flog, "connect"))
        b.js("document.querySelector('#wl-p2pdir [data-call=N0VAR]').click()")
        check("a dimmed row's click fills nothing", b.js("document.querySelector('#wl-peer-call').value"), "")
        b.js("document.querySelector('#wl-p2pdir [data-call=N0ARD]').click()")
        check("a click fills the call", b.js("document.querySelector('#wl-peer-call').value"), "N0ARD")
        check("a click fills the DIAL frequency", b.js("document.querySelector('#wl-peer-freq').value"), "7100")
        check("the URL is the peer URL", b.js("document.querySelector('#wl-url').value"), "ardop:///N0ARD?freq=7100")
        check("the picked row is marked", b.js("document.querySelector('#wl-p2pdir [data-call=N0ARD]').classList.contains('on')"),
              True)
        time.sleep(2)            # a ceiling for a connect that must not come
        check("a click does NOT connect (Pat got no connect)", len(recs(flog, "connect")), before)
        check("a click does not arm Connect's confirm", b.js(f"({TEXT})('#wl-connect')"), "Connect")

        # Positive control: Connect, then its confirm, does reach Pat with the picked URL.
        b.js("document.querySelector('#wl-connect').click()")
        check("Connect asks to confirm, naming the peer", bool(b.wait(f"({TEXT})('#wl-connect').includes('peer N0ARD')", 3)),
              True)
        b.js("document.querySelector('#wl-connect').click()")
        got = wait_for(lambda: recs(flog, "connect")[before:], 15)
        check("control: Connect + confirm reaches Pat with the picked URL", got and got[-1].get("url"),
              "ardop:///N0ARD?freq=7100")
        b.js("document.querySelector('#wl-stop').click()")
        b.wait(f"!({TEXT})('#wl-state').includes('Connecting')", 20)
    finally:
        stop(server)

    # --- the directory can't be reached: the cached copy shows, with its age ---
    dead = f"http://127.0.0.1:{testkit.free_port()}/v1/directory.json"
    cache = {"fetched": time.time() - 7 * 3600, "url": dead, "doc": DOC}
    server, flog = start("--p2pdir-url", dead, cache=cache)
    try:
        open_peer()
        rows = b.wait(f"{ROWS}.length === 6 && {ROWS}", 20)
        check("unreachable: the cached copy is listed", isinstance(rows, list) and len(rows), 6)
        head = b.js(f"({TEXT})('#wl-p2pdir .wl-dir-head')")
        check("unreachable: the copy's age is shown", "fetched 7 h ago" in head, True)
        check("unreachable: it says it couldn't refresh", "couldn't refresh" in head, True)
    finally:
        stop(server)

    # --- switched off in the host file: no list ---
    server, flog = start()          # winlink_test_lib: directory false unless a URL is given
    try:
        open_peer()
        b.wait(f"({VISIBLE})('#wl-peer-call')")
        time.sleep(1.5)
        check("off: the peer fields show", b.js(f"({VISIBLE})('#wl-peer-call')"), True)
        check("off: no directory list", b.js(f"({VISIBLE})('#wl-p2pdir')"), False)
    finally:
        stop(server)
finally:
    b.close()
    srv.shutdown()

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
