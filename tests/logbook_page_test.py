"""The Logbook tab in headless Chromium, against a simulated radio and a
stand-in logbook (a plain web server that records what it was asked for):

  - the logbook isn't loaded with the page, only when its tab is opened
  - then the frame shows the configured url, and the stand-in is asked for it
  - the "Open in its own tab" link points at the same url
  - a port-only block frames the logbook on the page's own host name

    python3 tests/logbook_page_test.py
"""
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from cdp_browser import Browser  # noqa: E402
import testkit  # noqa: E402

MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME = (os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
          or (MAC_CHROME if os.path.exists(MAC_CHROME) else None))
if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} got {got!r}" + ("" if ok else f" want {want!r}"))


asked = []


class Logbook(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        asked.append(self.path)
        body = b"<!doctype html><title>Logbook stand-in</title><h1>QSOs</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


lb_port = testkit.free_port()
lb = http.server.ThreadingHTTPServer(("127.0.0.1", lb_port), Logbook)
threading.Thread(target=lb.serve_forever, daemon=True).start()
tmp = tempfile.mkdtemp(prefix="logbook-page-")


def serve(block):
    path = os.path.join(tmp, "addons.json")
    with open(path, "w") as f:
        json.dump({"logbook": block}, f)
    web = testkit.free_port()
    p = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(web),
                          "--host", "127.0.0.1", "--rigctl-port", "0",
                          "--addons", path, "--addons-state", os.path.join(tmp, "state.json")],
                         cwd=ROOT, env=dict(os.environ, HOME=tmp, RANCH_RADIO_HOSTS=""),
                         stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(300):
        try:
            socket.create_connection(("127.0.0.1", web), timeout=0.2).close()
            return p, web
        except OSError:
            time.sleep(0.1)
    p.kill()
    raise SystemExit("server did not start")


FRAME = "document.querySelector('.lb-frame')?.getAttribute('src') || ''"
LINK = "document.querySelector('.lb-open')?.getAttribute('href') || ''"

try:
    print("# a url")
    url = f"http://127.0.0.1:{lb_port}/index.php/dashboard"
    p, web = serve({"enabled": True, "url": url})
    b = Browser(CHROME, testkit.free_port())
    try:
        b.goto(f"http://127.0.0.1:{web}/")
        b.wait("typeof addons === 'object' && !!document.querySelector('.addon-logbook') && 'yes'")
        time.sleep(1)
        check("not loaded with the page", (b.js(FRAME), list(asked)), ("", []))
        b.goto(f"http://127.0.0.1:{web}/#/logbook")
        check("its tab shows the frame on the url", b.wait(f"({FRAME}) || false"), url)
        for _ in range(50):
            if asked:
                break
            time.sleep(0.1)
        check("  and the logbook was asked for it", asked[:1], ["/index.php/dashboard"])
        check("the link opens the same url", b.js(LINK), url)
    finally:
        b.close()
        p.terminate()
        p.wait(10)

    print("\n# a port on the page's machine")
    p, web = serve({"enabled": True, "port": lb_port, "scheme": "http"})
    b = Browser(CHROME, testkit.free_port())
    try:
        b.goto(f"http://127.0.0.1:{web}/#/logbook")
        check("framed on the page's own host name", b.wait(f"({FRAME}) || false"), f"http://127.0.0.1:{lb_port}/")
    finally:
        b.close()
        p.terminate()
        p.wait(10)
finally:
    lb.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'all ok' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
