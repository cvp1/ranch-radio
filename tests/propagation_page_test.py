"""The Propagation tab in headless Chromium, against a simulated radio and the
NOAA, InfluxDB and PSKReporter stand-ins of tools/readme_screenshots.py:

  - Next openings: one line per band and region window, soonest first, the
    one open now marked "now to ...", each with its best chance; the
    forecast's age
  - a skywatch with no forecast measurement shows no Next openings section
    (not set up is not "no openings"); Conditions and Bands still show
  - before skywatch's and PSKReporter's first answer (heard_checked and
    psk_checked null, bands and psk empty) the Bands section and the radio
    page's strip say waiting, never "no decodes", "nobody" or "none"
    (2026-11-23, bug bash)

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


def run(unanswered=False):
    p, port = RS.serve(f"http://127.0.0.1:{sp}", tmp)
    b = Browser(RS.CHROME, RS.free_port(), size="1400,1000")
    try:
        b.goto(f"http://127.0.0.1:{port}/#/propagation")
        b.wait("!!document.querySelector('.pg-board')")
        if unanswered:
            # The snapshot as the add-on sends it before either source has
            # answered: empty bands and psk, the checked times null.
            b.js("""(() => { const s = {...addons.data('propagation'), heard_checked: null, psk_checked: null,
                     heard_error: null, psk_error: null, bands: {}, psk: {}};
                     addons.event({t: 'addon', addon: 'propagation', state: s}); })()""")
            b.wait("/Waiting for skywatch/.test(document.querySelector('.pg-board')?.textContent || '') || null")
            return b.js("""(() => {
              const bands = [...document.querySelectorAll('.pg-section')].find((s) => s.querySelector('.pg-h')?.textContent.includes('Bands'));
              const t = (bands?.textContent || '').replace(/\\s+/g, ' ');
              const strip = (document.querySelector('.prop-strip')?.textContent || '').replace(/\\s+/g, ' ');
              return {waiting: /Waiting for skywatch/.test(t), nodecodes: /No decodes:|Nothing decoded|no decodes/.test(t),
                      you: [...document.querySelectorAll('.pg-you')].map((e) => e.textContent.replace('Heard you', '').trim()),
                      strip_none: /heard you: none/.test(strip), strip_wait: /waiting/.test(strip), has_strip: !!document.querySelector('.prop-strip')};
            })()""")
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

    print("\n# before the first answer: waiting, not none")
    RS.FORECAST["on"] = True
    got = run(unanswered=True)
    check("Bands says it is waiting for skywatch", got["waiting"], True)
    check("  no 'no decodes' or 'Nothing decoded'", got["nodecodes"], False)
    if got["has_strip"]:
        check("  the strip: waiting, not 'heard you: none'", [got["strip_none"], got["strip_wait"]], [False, True])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
