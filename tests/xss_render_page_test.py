"""An HTML payload heard over the air renders as text, in headless Chromium,
against the real packet hub on tests/fake_kiss_radio.py (no transmit).

A station's position comment carries <img src=x onerror=...> plus quotes.
The test asserts what escaping does, not what the policy does: the payload's
literal text is on the page and no <img src="x"> element exists. (With esc()
broken, script-src 'self' would still block the onerror, so "nothing ran"
alone would stay green; review plan PR 3, panel 2026-11-21.) It also checks
the policy is on the page and that loading the page broke none of it
(securitypolicyviolation events, recorded from the first script on).

    python3 tests/xss_render_page_test.py
"""
import os
import shutil
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
from cdp_browser import Browser  # noqa: E402
import tnc_hub_test as T  # noqa: E402
from testkit import free_port  # noqa: E402
import webguard  # noqa: E402

CDP = free_port()
CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
fails = 0
PAYLOAD = "<img src=x onerror=window.__pwn=1>'q\""
COUNT = "(s) => document.querySelectorAll(s).length"


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")

logdir = tempfile.mkdtemp(prefix="xsspage-")
radio = T.FakeKissRadio().start()
hub = T.HubProc(radio, tx=False, extra=("--log-dir", logdir, "--lat", "32.2", "--lon", "-105.9"))
b = Browser(CHROME, CDP)
try:
    radio.inject(T.aprs("KF7XSS-9", "!4049.00N/10510.00W>" + PAYLOAD), lossy=False)
    T.wait_for(lambda: any(r["call"] == "KF7XSS-9" for r in T._get(hub, "/api/aprs/stations?rev=0")["rows"]), 10)
    row = next(r for r in T._get(hub, "/api/aprs/stations?rev=0")["rows"] if r["call"] == "KF7XSS-9")
    check("the payload reached the store intact", any(PAYLOAD in str(v) for v in row.values()), True)

    with urllib.request.urlopen(f"http://127.0.0.1:{hub.http}/", timeout=5) as r:
        check("the page carries the policy", r.headers.get("Content-Security-Policy"), webguard.CSP)

    b.call("Page.enable")
    b.call("Page.addScriptToEvaluateOnNewDocument", source=(
        "window.__csp = []; document.addEventListener('securitypolicyviolation',"
        " (e) => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI), true);"))
    b.goto(f"http://127.0.0.1:{hub.http}/#stations")
    b.wait(f"({COUNT})('#st-table tr[data-call=\"KF7XSS-9\"]')")
    b.js("document.querySelector('#st-table tr[data-call=\"KF7XSS-9\"]').click()")
    b.wait("!document.querySelector('#aprs-detail').hidden")
    b.js("aprsPage.showTab('raw')")
    b.wait("[...document.querySelectorAll('#recent td')].some((t) => t.textContent.includes('KF7XSS-9'))")

    check("the payload shows as text in the station detail",
          b.wait("document.querySelector('#aprs-detail').textContent.includes('<img src=x')", 10), True)
    check("no <img src=x> element anywhere on the page", b.js(f"({COUNT})('img[src=\"x\"]')"), 0)
    check("no element carries an onerror attribute", b.js(f"({COUNT})('[onerror]')"), 0)
    check("nothing ran", b.js("window.__pwn === undefined"), True)
    check("the page loaded with no policy violations", b.js("window.__csp"), [])
    check("nothing transmitted", radio.transmitted(), [])
finally:
    b.close()
    hub.stop()
    radio.stop()
    shutil.rmtree(logdir, ignore_errors=True)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
