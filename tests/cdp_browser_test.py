"""The page tests' Chromium helper (tests/cdp_browser.py) when Chrome fails to
start. Before 2026-11-20 it waited 30 s whether or not Chrome was still
running, threw away Chrome's stderr, and said only "chromium did not start"
(the flaky CI failures of addons_page_test and others at 31-33 s said nothing
more). Stand-in Chromes, one real start; nothing transmits, no page is served.

  a Chrome that exits: fails at once with its exit code and its stderr tail,
    and leaves no profile directory behind
  a Chrome that hangs with the port closed: fails at the bound, says it is
    still running, kills it, shows its stderr
  a DevTools port that answers with no page: says so
  the stderr tail is bounded
  a real Chrome still starts, evaluates and closes cleanly

    python3 tests/cdp_browser_test.py
"""
import glob
import os
import shutil
import stat
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import cdp_browser  # noqa: E402
from cdp_browser import Browser  # noqa: E402
import testkit  # noqa: E402

CHROME = (os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
          or next((p for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                               "/Applications/Chromium.app/Contents/MacOS/Chromium") if os.path.exists(p)), None))
TMP = tempfile.mkdtemp(prefix="cdp-browser-test-")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:66} got {got!r}" + ("" if ok else f" want {want!r}"))


def fake(name, body):
    """A stand-in Chrome: a Python script that takes Chrome's argv."""
    path = os.path.join(TMP, name)
    with open(path, "w") as f:
        f.write(f"#!{sys.executable}\nimport sys, time, json, re\n{body}\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


def start(chrome, bound=None):
    """(message, seconds, profile dirs left) for a start expected to fail."""
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "page-chrome-*")))
    saved = getattr(cdp_browser, "START_S", None)
    if bound is not None:
        cdp_browser.START_S = bound
    t0 = time.monotonic()
    try:
        Browser(chrome, testkit.free_port())
        msg = "it started"
    except SystemExit as e:
        msg = str(e)
    finally:
        cdp_browser.START_S = saved
    left = set(glob.glob(os.path.join(tempfile.gettempdir(), "page-chrome-*"))) - before
    return msg, time.monotonic() - t0, left


try:
    print("# a Chrome that exits", flush=True)
    msg, took, left = start(fake("exits", 'sys.stderr.write("early line\\nboom: no usable sandbox\\n"); sys.exit(3)'))
    check("it fails at once, not after the 30 s bound", took < 10, True)
    check("  it names the exit code", "exited with code 3" in msg, True)
    check("  it shows Chrome's stderr", "boom: no usable sandbox" in msg, True)
    check("  it keeps the old words, for anyone grepping CI logs", msg.startswith("chromium did not start"), True)
    check("  no profile directory is left", left, set())

    print("\n# a Chrome that hangs with the DevTools port closed", flush=True)
    pidfile = os.path.join(TMP, "hang.pid")
    msg, took, left = start(fake("hangs", f'import os; open({pidfile!r}, "w").write(str(os.getpid())); '
                                          'sys.stderr.write("stuck on something\\n"); sys.stderr.flush(); time.sleep(600)'),
                            bound=1.5)
    check("it fails at the bound", 1.4 < took < 10, True)
    check("  it says Chrome is still running", "still running after 1.5 s" in msg, True)
    check("  and that the port never answered", "the DevTools port never answered" in msg, True)
    check("  it shows Chrome's stderr", "stuck on something" in msg, True)
    pid = int(open(pidfile).read())
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    check("  the hung Chrome is killed", alive, False)
    check("  no profile directory is left", left, set())

    print("\n# a DevTools port that answers with no page", flush=True)
    serve = fake("nopage", """
from http.server import BaseHTTPRequestHandler, HTTPServer
port = int(next(a for a in sys.argv if a.startswith("--remote-debugging-port=")).split("=")[1])
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        b = json.dumps([{"type": "service_worker"}]).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a):
        pass
HTTPServer(("127.0.0.1", port), H).serve_forever()
""")
    msg, took, left = start(serve, bound=1.5)
    check("it says the port answered with no page", "answered with no page (1 target(s))" in msg, True)
    check("  an empty stderr says so", "(empty)" in msg, True)

    print("\n# the stderr tail is bounded", flush=True)
    big = os.path.join(TMP, "big.log")
    with open(big, "w") as f:
        f.write("".join(f"line {i} " + "x" * 300 + "\n" for i in range(1000)))
    tail = getattr(cdp_browser, "tail", lambda *a: "")
    t = tail(big) or "\n"
    check("at most 20 lines", len(t.splitlines()) <= cdp_browser.TAIL_LINES, True)
    check("  at most 4000 bytes", len(t.encode()) <= cdp_browser.TAIL_BYTES, True)
    check("  ending at the last line", (t.splitlines() or [""])[-1].startswith("line 999 "), True)
    check("a missing file is empty", tail(os.path.join(TMP, "none")), "")

    print("\n# a real Chrome", flush=True)
    if not CHROME:
        check("chromium found (set CHROME=)", False, True)
    else:
        b = Browser(CHROME, testkit.free_port())
        check("it starts and evaluates", b.js("1 + 1"), 2)
        d = b.dir
        b.close()
        left = sorted(os.path.relpath(os.path.join(r, f), d) for r, _, fs in os.walk(d) for f in fs)
        check("  close removes its profile (what is left)", (os.path.exists(d), left[:10]), (False, []))
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'FAIL' if fails else 'PASS'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
