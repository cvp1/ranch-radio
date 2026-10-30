"""static/addons.js in a real page (headless Chromium): messages for an
add-on whose script hasn't registered yet are kept and replayed after its
hello snapshot, in order, and a fresh snapshot drops them.

Before, they were dropped: a UV-PRO that connected while its script loaded
left the panel on the hello snapshot (no battery, no channels) until a
reload (uvpro_page_test, 2 in 8 flake-hunt runs, 2026-10-30).

    python3 tests/addons_page_test.py
"""
import os
import random
import shutil
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from cdp_browser import Browser  # noqa: E402

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


web = 22000 + random.randrange(1000)
server = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(web),
                           "--host", "127.0.0.1", "--rigctl-port", "0"],
                          cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
for _ in range(300):
    try:
        socket.create_connection(("127.0.0.1", web), timeout=0.2).close()
        break
    except OSError:
        time.sleep(0.1)
else:
    server.kill()
    raise SystemExit("server did not start")

b = Browser(CHROME, web + 1000)
try:
    b.goto(f"http://127.0.0.1:{web}/")
    check("the page has the add-on loader", b.wait("typeof addons === 'object' && 'yes'"), "yes")

    # A made-up add-on whose script never arrives on its own (no such file):
    # its messages come before it registers, as on a slow link.
    SEEN = "window.__seen"
    b.js("""(() => {
      window.__seen = [];
      addons.hello([{name: "zz", label: "ZZ", title: "", css: false, on: true, error: null}],
                   {zz: {on: true, n: 0}});
      addons.event({t: "addon", addon: "zz", n: 1});
      addons.event({t: "addon", addon: "zz", n: 2});
      addons.register("zz", {hello: (s) => __seen.push("hello " + s.n), event: (m) => __seen.push("event " + m.n)});
    })()""")
    check("early messages replayed after the snapshot, in order", b.js(SEEN),
          ["hello 0", "event 1", "event 2"])
    b.js("addons.event({t: 'addon', addon: 'zz', n: 3})")
    check("  then messages go straight to it", b.js(SEEN)[-1], "event 3")

    # A fresh snapshot before the script registers supersedes what came earlier.
    b.js("""(() => {
      window.__seen = [];
      addons.hello([{name: "yy", label: "YY", title: "", css: false, on: true, error: null}], {yy: {on: true, n: 0}});
      addons.event({t: "addon", addon: "yy", n: 1});
      addons.hello([{name: "yy", label: "YY", title: "", css: false, on: true, error: null}], {yy: {on: true, n: 5}});
      addons.event({t: "addon", addon: "yy", n: 6});
      addons.register("yy", {hello: (s) => __seen.push("hello " + s.n), event: (m) => __seen.push("event " + m.n)});
    })()""")
    check("a fresh snapshot drops the messages before it", b.js(SEEN), ["hello 5", "event 6"])

    # Switched off: nothing is replayed into it.
    b.js("""(() => {
      window.__seen = [];
      addons.hello([{name: "xx", label: "XX", title: "", css: false, on: false, error: null}], {});
      addons.event({t: "addon", addon: "xx", n: 1});
      addons.register("xx", {hello: (s) => __seen.push("hello"), event: (m) => __seen.push("event " + m.n)});
    })()""")
    check("switched off: nothing replayed", b.js(SEEN), [])
finally:
    b.close()
    server.terminate()
    server.wait(10)

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
