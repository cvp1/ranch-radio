"""The README's screenshots, from a simulated radio with made-up data.

Starts a --sim G90 with the Propagation add-on, fed by local stand-ins for
NOAA SWPC, a skywatch InfluxDB and PSKReporter (no network, no real
callsigns, no real locators), then captures the pages with headless
Chromium into docs/images/:

    radio-page.png     the radio page: band badges and the space-weather strip
    propagation.png    the Propagation tab
    phone.png          the radio page at phone size, the first screen
    station.png        a two-radio station's overview (from tests/station_page_test.py)

    python3 tools/readme_screenshots.py          # CHROME= to pick a browser
"""
import asyncio
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

from aiohttp import web

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from cdp_browser import Browser  # noqa: E402

OUT = os.path.join(ROOT, "docs", "images")
CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome") or next(
    (p for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",) if os.path.exists(p)), None)
if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------------------------------------------------------------- made-up data
NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)


def tag(dt, z=""):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + z


KP = [{"time_tag": tag(NOW), "kp_index": 2, "estimated_kp": 2.33, "kp": "2M"}]
XRAY = [{"time_tag": tag(NOW, "Z"), "flux": 4.1e-7, "energy": "0.1-0.8nm"}]
F107 = [{"time_tag": tag(NOW - timedelta(hours=3)), "flux": 148.0}]

# band: {source: (decodes, best SNR, farthest km)}, {region: decodes}
HEARD = {
    "40m": ({"jtdx": (64, 2, 2900), "skimmer": (31, 6, 3400)}, {"NA_EAST": 41, "NA_WEST": 38, "SA": 6}),
    "30m": ({"skimmer": (22, -4, 6100)}, {"NA_EAST": 12, "EU": 7}),
    "20m": ({"jtdx": (180, 9, 9800), "skimmer": (95, 14, 10400)}, {"EU": 110, "NA_EAST": 80, "JA": 42, "SA": 21}),
    "17m": ({"skimmer": (48, 3, 12600)}, {"JA": 19, "VK_ZL": 14, "OCEANIA_DEEP": 8}),
    "15m": ({"skimmer": (37, 1, 8800)}, {"SA": 18, "EU": 11, "NA_EAST": 5}),
    "10m": ({"skimmer": (9, -12, 5200)}, {"SA": 7}),
}


def influx_json(by_region):
    series = []
    for band, (src, regions) in HEARD.items():
        if by_region:
            series += [{"tags": {"band": band, "region": r}, "columns": ["time", "count"], "values": [[0, n]]}
                       for r, n in regions.items()]
        else:
            series += [{"name": "skywatch_decode", "tags": {"band": band, "source": s},
                        "columns": ["time", "n", "best", "far"], "values": [[0, n, snr, km]]}
                       for s, (n, snr, km) in src.items()]
    return {"results": [{"statement_id": 0, "series": series}]}


# Receivers with made-up callsigns; only counts and countries reach the page.
PSK = [("20m", 14_075_000, "Germany", "JO62", -8), ("20m", 14_075_400, "Spain", "IN80", -14),
       ("20m", 14_074_900, "Brazil", "GG66", -11), ("20m", 14_075_200, "Germany", "JN58", -6),
       ("15m", 21_075_300, "Argentina", "GF05", -15), ("15m", 21_074_800, "Chile", "FF46", -9),
       ("10m", 28_075_500, "Japan", "PM95", -17)]


def psk_xml():
    t = int(NOW.timestamp())
    rows = "\n".join(
        f'<receptionReport receiverCallsign="X{i}TEST" receiverLocator="{loc}" senderCallsign="N0CALL" '
        f'senderLocator="FN31" frequency="{hz}" flowStartSeconds="{t - 300 * i}" mode="FT8" '
        f'receiverDXCC="{dxcc}" sNR="{snr}" />'
        for i, (_, hz, dxcc, loc, snr) in enumerate(PSK))
    return f'<?xml version="1.0"?>\n<receptionReports>\n{rows}\n</receptionReports>'


def stand_ins(port):
    """NOAA, InfluxDB and PSKReporter stand-ins on 127.0.0.1:port, in a thread."""
    async def noaa(r):
        return web.json_response({"kp": KP, "xray": XRAY, "f107": F107}[r.match_info["name"]])

    async def influx(r):
        return web.json_response(influx_json("GROUP BY band, region" in r.query.get("q", "")))

    async def pskr(r):
        return web.Response(text=psk_xml(), content_type="text/xml")

    def run():
        loop = asyncio.new_event_loop()
        app = web.Application()
        app.router.add_get("/noaa/{name}", noaa)
        app.router.add_get("/query", influx)
        app.router.add_get("/psk", pskr)
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", port).start())
        loop.run_forever()
    threading.Thread(target=run, daemon=True).start()


# The server, with the add-on's NOAA and PSKReporter URLs pointed at the stand-ins.
LAUNCH = """
import runpy, sys
sys.path.insert(0, {root!r})
from addons import propagation as P
b = {base!r}
P.URL_KP, P.URL_XRAY, P.URL_F107, P.URL_PSK = b + "/noaa/kp", b + "/noaa/xray", b + "/noaa/f107", b + "/psk"
sys.argv = ["server.py"] + sys.argv[1:]
runpy.run_path({server!r}, run_name="__main__")
"""


def serve(base, tmp):
    addons = os.path.join(tmp, "addons.json")
    with open(addons, "w") as f:
        json.dump({"propagation": {"enabled": True, "skywatch": {"url": base, "db": "skywatch"},
                                   "pskreporter": {"call": "N0CALL"}, "window_min": 60}}, f)
    web_port, rigctl = free_port(), free_port()
    code = LAUNCH.format(root=ROOT, base=base, server=os.path.join(ROOT, "server.py"))
    p = subprocess.Popen([sys.executable, "-c", code, "--sim", "--rig", "g90", "--no-audio",
                          "--listen", str(web_port), "--host", "127.0.0.1",
                          "--rigctl-port", str(rigctl), "--rigctl-host", "127.0.0.1",
                          "--addons", addons, "--addons-state", os.path.join(tmp, "state.json")],
                         cwd=ROOT, env=dict(os.environ, HOME=tmp, RANCH_RADIO_HOSTS=""),
                         stdout=open(os.path.join(tmp, "server.log"), "w"), stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", web_port), timeout=0.2).close()
            return p, web_port
        except OSError:
            time.sleep(0.1)
    raise SystemExit("server did not start: see " + os.path.join(tmp, "server.log"))


def clip(b, path, width, height, scale=1):
    """The top width x height of the page (CSS pixels), at scale."""
    import base64
    r = b.call("Page.captureScreenshot", format="png",
               clip={"x": 0, "y": 0, "width": width, "height": height, "scale": scale})
    with open(path, "wb") as f:
        f.write(base64.b64decode(r["result"]["data"]))


def content_height(b, sel):
    return b.js(f"Math.ceil(Math.max(...[...document.querySelectorAll({sel!r})]"
                ".map((e) => e.getBoundingClientRect().bottom)) + 24)")


def main():
    os.makedirs(OUT, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="readme-shots-")
    sp = free_port()
    stand_ins(sp)
    p, port = serve(f"http://127.0.0.1:{sp}", tmp)
    url = f"http://127.0.0.1:{port}/"
    b = Browser(CHROME, free_port(), size="1500,1000")
    try:
        b.goto(url)
        b.wait("!!document.querySelector('.prop-strip') && document.querySelectorAll('.prop-badge').length > 2")
        time.sleep(1.5)                                   # let the spectrum fill in
        clip(b, os.path.join(OUT, "radio-page.png"), 1500, content_height(b, ".card, .panel, section"))

        b.goto(url + "#/propagation")
        b.wait("!!document.querySelector('.pg-you')")
        time.sleep(0.5)
        b.screenshot(os.path.join(OUT, "propagation.png"))

        b.call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=2, mobile=True)
        b.goto(url)
        b.wait("!!document.querySelector('.prop-strip')")
        time.sleep(1.5)
        clip(b, os.path.join(OUT, "phone.png"), 390, 844)            # at the 2x device scale

        # The station overview: the station page test's own screenshot, cropped.
        shots = os.path.join(tmp, "station")
        os.makedirs(shots)
        subprocess.run([sys.executable, os.path.join(ROOT, "tests", "station_page_test.py"), shots],
                       cwd=ROOT, env=dict(os.environ, CHROME=CHROME), check=True, stdout=subprocess.DEVNULL)
        b.call("Emulation.clearDeviceMetricsOverride")
        b.goto("file://" + os.path.join(shots, "station-overview.png"))
        b.js("document.body.style.margin = '0'")
        clip(b, os.path.join(OUT, "station.png"), 1500, 290)
    finally:
        b.close()
        p.terminate()
        p.wait(10)
    for n in ("radio-page.png", "propagation.png", "phone.png", "station.png"):
        print(os.path.join("docs", "images", n), os.path.getsize(os.path.join(OUT, n)), "bytes")


if __name__ == "__main__":
    main()
