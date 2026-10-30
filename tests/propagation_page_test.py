"""The Propagation tab in a real browser (headless Chromium), against a
simulated radio and stand-ins for NOAA, InfluxDB and PSKReporter (the ones
tools/readme_screenshots.py uses):

  - Next openings: one line per band and region window, soonest first, the
    one open now marked "now to ...", each with its best chance; the
    forecast's age
  - a skywatch with no forecast measurement shows no Next openings section
    (not set up is not "no openings"); Conditions and Bands still show

    python3 tests/propagation_page_test.py
"""
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from cdp_browser import Browser  # noqa: E402
import readme_screenshots as RS  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} got {got!r}" + ("" if ok else f" want {want!r}"))


tmp = tempfile.mkdtemp(prefix="prop-page-")
sp = RS.free_port()
RS.stand_ins(sp)


def run():
    p, port = RS.serve(f"http://127.0.0.1:{sp}", tmp)
    b = Browser(RS.CHROME, RS.free_port(), size="1400,1000")
    try:
        b.goto(f"http://127.0.0.1:{port}/#/propagation")
        b.wait("!!document.querySelector('.pg-board')")
        return b.js("""(() => {
          const h = [...document.querySelectorAll('.pg-h')].map((e) => e.firstChild.textContent.trim());
          const rows = [...document.querySelectorAll('.pg-open')].map((e) => [
            e.querySelector('.pg-open-band').textContent, e.querySelector('.pg-open-region').textContent.trim(),
            e.querySelector('.pg-open-when').textContent, e.querySelector('.pg-open-p').textContent,
            e.classList.contains('live')]);
          const note = [...document.querySelectorAll('.pg-section')].find((s) => s.textContent.includes('Next openings'))
            ?.querySelector('.pg-note')?.textContent.replace(/\\s+/g, ' ').trim() || null;
          return {h, rows, note};
        })()""")
    finally:
        b.close()
        p.terminate()
        p.wait(10)


try:
    print("# a forecast with five openings")
    got = run()
    check("sections: Conditions, Next openings, Bands", got["h"], ["Conditions", "Next openings", "Bands"])
    check("one line per window, soonest first", [r[:2] for r in got["rows"]],
          [["17m", "NA east"], ["15m", "S America"], ["20m", "Japan"], ["10m", "S America"], ["40m", "Europe"]])
    check("the window open now says so", (got["rows"][0][2].startswith("now to "), got["rows"][0][4]), (True, True))
    check("  a later one shows its hours (UTC)", got["rows"][1][2].count("Z"), 2)
    check("  and isn't marked open now", got["rows"][1][4], False)
    check("each shows its best chance", [r[3] for r in got["rows"]], ["71%", "64%", "58%", "52%", "83%"])
    check("the note says when it was issued (2-3 h before)",
          bool(re.search(r"issued [23] h ago by skywatch", got["note"] or "")), True)

    print("\n# skywatch without a forecast measurement")
    RS.FORECAST["on"] = False
    got = run()
    check("no Next openings section", got["h"], ["Conditions", "Bands"])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
