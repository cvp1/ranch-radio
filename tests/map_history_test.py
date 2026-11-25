"""The Map tab's History view (addons/map.py "history", static/addons/map.js).

  route      /map/history serves the configured page whole, with its OWN policy:
             a CSP sandbox without allow-same-origin (an opaque origin: no
             station cookies or API), inline script allowed only inside it,
             no fetches, no forms, framed only by the station
  refusals   nothing set or the file missing -> 404 with why; over the cap ->
             413, never a truncated page
  snapshot   says whether there is a page and when it was built, with or
             without the logbook sync
  host file  addons.map.history takes a path; shell characters are refused

    python3 tests/map_history_test.py
"""
import os as _os
_os.environ["RANCH_RADIO_OFFLINE"] = "1"

import asyncio  # noqa: E402
import copy  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import webguard  # noqa: E402
from addons import map as M  # noqa: E402
from ranch_radio.render import HostError, validate  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


class Host:
    def __init__(self):
        self.logsync, self.notes, self.sent = None, [], []

    def notice(self, msg, level="info", ws=None):
        self.notes.append((level, msg))

    def _broadcast(self, payload):
        self.sent.append(payload)

    async def _send(self, ws, payload):
        self.sent.append(payload)


tmp = Path(tempfile.mkdtemp())
page = tmp / "two-way.html"
page.write_text("<!doctype html><script>document.title='hist'</script><p>N7XYZ: <b>heard</b></p>")


async def get(addon, path="/map/history"):
    app = webguard.add_security_headers(web.Application())   # the station's own headers, as station.py adds them
    addon.http(app, None)
    async with TestClient(TestServer(app)) as c:
        r = await c.get(path)
        return r.status, r.headers, await r.read()


# ---- route
st, hd, body = asyncio.run(get(M.Map(Host(), {"history": str(page)})))
csp = hd.get("Content-Security-Policy", "")
check("route: serves the configured page whole", (st, body), (200, page.read_bytes()))
check("  as html", hd.get("Content-Type", "").split(";")[0], "text/html")
check("  sandboxed", csp.startswith("sandbox allow-scripts"), True)
check("  WITHOUT allow-same-origin (an opaque origin: nothing of the station's)", "allow-same-origin" in csp, False)
check("  no fetches, no forms, framed only by the station",
      all(x in csp for x in ("connect-src 'none'", "form-action 'none'", "frame-ancestors 'self'")), True)
check("  inline script only inside that sandbox; no script from anywhere", "script-src 'unsafe-inline'" in csp
      and "https:" not in csp.split("script-src", 1)[1].split(";")[0], True)
check("  not cached (rebuilt daily)", hd.get("Cache-Control"), "no-store")
check("  the station's strict policy does not replace it (setdefault keeps the page's own)", csp == M.HISTORY_CSP, True)
check("  the station's other headers still apply", hd.get("X-Content-Type-Options"), "nosniff")

# ---- refusals
st, _, body = asyncio.run(get(M.Map(Host(), {})))
check("refused: nothing set -> 404 saying where to set it", (st, b"addons.map.history" in body), (404, True))
st, _, body = asyncio.run(get(M.Map(Host(), {"history": str(tmp / "missing.html")})))
check("refused: file missing -> 404 'isn't there yet'", (st, b"isn't there yet" in body), (404, True))
big = tmp / "big.html"
with open(big, "wb") as f:
    f.truncate(M.HISTORY_MAX_BYTES + 1)
st, _, body = asyncio.run(get(M.Map(Host(), {"history": str(big)})))
check("refused: over the cap -> 413, not a truncated page", st, 413)

# ---- snapshot
check("snapshot: no page set", M.Map(Host(), {}).snapshot()["history"], None)
h = M.Map(Host(), {"history": str(page)}).snapshot()["history"]
check("snapshot: a page, with when it was built (even without the logbook sync)",
      (h["ok"], h["built"]), (True, int(page.stat().st_mtime)))
check("snapshot: a missing page says why", M.Map(Host(), {"history": str(tmp / "nope.html")}).snapshot()["history"]["ok"], False)
check("snapshot: ~ expands", M.Map(Host(), {"history": "~/x.html"}).history, _os.path.expanduser("~/x.html"))

# ---- host file
station_h = json.loads((ROOT / "examples/hosts/linux-station.json").read_text())


def with_history(v):
    h2 = copy.deepcopy(station_h)
    h2.setdefault("addons", {})["map"] = {"enabled": True, "history": v}
    return h2


try:
    validate(with_history("~/ai-os/tools/radio-reach/out/index.html"))
    check("host file: a history path is accepted", True, True)
except HostError as e:
    check("host file: a history path is accepted", str(e), True)
for bad in ("~/x.html; rm -rf ~", "$(id)", "-x", "a b.html"):
    try:
        validate(with_history(bad))
        check(f"host file: refused {bad!r}", "accepted", "refused")
    except HostError:
        check(f"host file: refused {bad!r}", "refused", "refused")

print(f"\n{'all ok' if not fails else f'{fails} FAILED'}", flush=True)
_os._exit(1 if fails else 0)
