"""APRS plan M2: the packet page's Stations, Map, Messages and Raw tabs, in
headless Chromium, against the real hub on tests/fake_kiss_radio.py.

The hub backfills the heard sample (tests/fixtures/aprs/rx-sample.jsonl),
then a moving station and an object are injected on the fake radio. Checks:
  - the station list fills; the filter and a chip narrow it; a row opens the
    station's detail
  - the map draws the stations with a position, and the selected station's
    track with the right points; our own position is on it
  - the object is on the map, and its owner's kill (a delta) takes it off
  - Messages lists what was heard; Raw still shows the heard list and traffic
  - nothing is transmitted

    python3 tests/packet_page_test.py [screenshot.png]
"""
import json
import os
import random
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
from cdp_browser import Browser  # noqa: E402
from fake_kiss_radio import FakeKissRadio  # noqa: E402
import tnc_hub_test as T  # noqa: E402

CDP = 19000 + random.randrange(1000)
CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
SHOT = sys.argv[1] if len(sys.argv) > 1 else None
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} got {got!r}" + ("" if ok else f" want {want!r}"))


if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")

COUNT = "(s) => document.querySelectorAll(s).length"
sys.path.insert(0, os.path.join(ROOT, "tests"))
import corpus  # noqa: E402
SAMPLE = corpus.sample()
if SAMPLE is None:
    print("skip  packet page: it is built on a receive sample (tests/corpus.py); none here")
    sys.exit(0)
logdir = tempfile.mkdtemp(prefix="packetpage-")
shutil.copy(SAMPLE, os.path.join(logdir, "rx.jsonl"))
radio = FakeKissRadio().start()
hub = T.HubProc(radio, tx=False, extra=("--log-dir", logdir, "--lat", "32.2", "--lon", "-105.9"))
b = Browser(CHROME, CDP)
try:
    TRACK = [(40.81, -105.17), (40.82, -105.16), (40.83, -105.15)]
    for lat, lon in TRACK:
        d, m = divmod(lat * 60, 60)
        ld, lm = divmod(-lon * 60, 60)
        radio.inject(T.aprs("KF7TRK-9", f"!{int(d):02d}{m:05.2f}N/{int(ld):03d}{lm:05.2f}W>moving"), lossy=False)
        time.sleep(0.05)
    radio.inject(T.aprs("K7OWN", ";TESTOBJ  *111111z4049.00N/10510.00Wr146.52"), lossy=False)
    T.wait_for(lambda: T._get(hub, "/api/aprs/objects?rev=0")["rows"], 5)

    b.goto(f"http://127.0.0.1:{hub.http}/#stations")
    n = b.wait(f"({COUNT})('#st-table tr.st-row')")
    total = T._get(hub, "/api/aprs/stations?rev=0")
    check("the station list fills (every station in the store)", n, len(total["rows"]))
    b.js("(() => { const i = document.querySelector('#st-filter'); i.value = 'kf7trk'; i.dispatchEvent(new Event('input')); })()")
    check("the filter narrows it", b.js("[...document.querySelectorAll('#st-table tr.st-row')].map((r) => r.dataset.call)"),
          ["KF7TRK-9"])
    b.js("(() => { const i = document.querySelector('#st-filter'); i.value = ''; i.dispatchEvent(new Event('input')); })()")
    b.js("document.querySelector('#st-chips [data-chip=wx]').click()")
    wx = b.js(f"({COUNT})('#st-table tr.st-row')")
    check("the Weather chip narrows it", 0 < wx < n, True)
    b.js("document.querySelector('#st-chips [data-chip=all]').click()")

    b.js("document.querySelector('#st-table tr[data-call=\"KF7TRK-9\"]').click()")
    check("a row opens the station's detail",
          b.wait("!document.querySelector('#aprs-detail').hidden && document.querySelector('#aprs-detail h3').textContent.split(' ')[0]"),
          "KF7TRK-9")
    check("  with its track", b.wait("document.querySelector('#aprs-detail').textContent.includes('3 points')"), True)

    b.js("aprsPage.showTab('map')")
    nodes = b.wait(f"({COUNT})('#aprs-mapbox .mm-node')")
    placed = len([r for r in total["rows"] if r["lat"] is not None])
    objs = len([o for o in T._get(hub, "/api/aprs/objects?rev=0")["rows"] if o["live"] and o["lat"] is not None])
    check("the map draws every placed station and object, and us", nodes, placed + objs + 1)
    check("  we're on it", b.js(f"({COUNT})('#aprs-mapbox .mm-node.me')"), 1)
    check("  the selected station is marked", b.js("document.querySelector('#aprs-mapbox .mm-node.sel text:not(.mm-glyph)')?.textContent"),
          "KF7TRK-9")
    check("  its track: one point per fix", b.js(f"({COUNT})('#aprs-mapbox .mm-trk-pt')"), 3)
    pts = b.js("document.querySelector('#aprs-mapbox .mm-trk').getAttribute('points').split(' ').length")
    check("  drawn as one line", pts, 3)
    HAS_OBJ = "[...document.querySelectorAll('#aprs-mapbox .mm-node.k-obj title')].some((t) => t.textContent.includes('obj:TESTOBJ|K7OWN'))"
    check("  the object is on the map", b.js(HAS_OBJ), True)
    if SHOT:
        b.screenshot(SHOT)

    radio.inject(T.aprs("K7OWN", ";TESTOBJ  _111111z4049.00N/10510.00Wr"), lossy=False)
    check("the owner's kill takes it off the map (a delta)",
          b.wait(f"!({HAS_OBJ}) && 'gone'", 12), "gone")

    b.js("document.querySelector('#aprs-detail .close').click()")
    check("closing the detail clears the track", b.js(f"({COUNT})('#aprs-mapbox .mm-trk-pt')"), 0)

    b.js("aprsPage.showTab('messages')")
    msgs = len(T._get(hub, "/api/aprs/messages")["messages"])
    check("Messages lists what was heard", b.wait(f"({COUNT})('#msg-table tr') - 1"), msgs)

    b.js("aprsPage.showTab('raw')")
    check("Raw: the heard list", b.wait(f"({COUNT})('#heard tr') > 1"), True)
    check("Raw: the traffic shows the injected frames",
          b.wait("[...document.querySelectorAll('#recent td.mono')].some((t) => t.textContent.includes('KF7TRK-9'))"), True)
    check("the tab is in the address", b.js("location.hash"), "#raw")
    check("nothing transmitted", radio.transmitted(), [])
finally:
    b.close()
    hub.stop()
    radio.stop()
    shutil.rmtree(logdir, ignore_errors=True)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
