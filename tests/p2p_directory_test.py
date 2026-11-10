"""p2p_directory: the read-only P2P directory behind the Peer picker.

Covers: a good file; wrong schema; oversize; truncated JSON; hostile strings
(control characters drop the row, HTML stays verbatim data); a row outside
the amateur bands is dropped and counted, the rest kept; a fetch failure
serves the cached copy with its age; no cache and no network gives an empty
list with a reason; a failed fetch is not retried at once; the host-file
switch (absent/false/object, bad values refused); the centre -> dial table
(Pat v1.0.0 app/freq.go:68-92); distance and "this station" in the view.
The directory server is a local fake; nothing touches the real URL.

    python3 tests/p2p_directory_test.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

import p2p_directory as P  # noqa: E402
import testkit  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


def entry(**kw):
    e = {"centre_khz": 7103.5, "mode": "VARA_HF", "bw_hz": 500, "control": "attended",
         "hours": "daily 1400-1600Z", "flags": []}
    e.update(kw)
    return e


def station(call="N9ABC", grid="EM12", **kw):
    s = {"call": call, "grid": grid, "note": "NVIS dipole", "confirmed": "2026-11-01",
         "source": "maintainer", "stale": False, "entries": [entry()]}
    s.update(kw)
    return s


def doc(stations, **kw):
    d = {"schema": 1, "generated": "2026-11-10T18:00:00Z", "licence": "CC0-1.0",
         "count": len(stations), "stations": stations}
    d.update(kw)
    return json.dumps(d).encode()


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


async def main():
    # --- the conversion table and the host-file switch (no network) ---
    for mode, centre, dial in [("VARA_HF", 7103.5, 7102.0), ("ARDOP", 7101.5, 7100.0), ("PACTOR", 14108, 14106.5),
                               ("VARA_FM", 145050, 145050), ("PACKET", 145090, 145090),
                               ("ROBUST_PACKET", 10147.3, None)]:
        check(f"dial {mode} {centre}", P.dial_khz(centre, mode), dial)
    check("config: absent -> on, default URL", P.config(None), (P.DEFAULT_URL, P.DEFAULT_REFRESH_H))
    check("config: false -> off", P.config(False), None)
    check("config: object", P.config({"url": "https://example.org/v1/directory.json", "refresh_hours": 12}),
          ("https://example.org/v1/directory.json", 12.0))
    for bad in ({"url": "http://example.org/x"}, {"url": "file:///etc/passwd"}, {"refresh_hours": 0},
                {"refresh_hours": True}, "yes", 3):
        try:
            P.config(bad)
            check(f"config refuses {bad!r}", "accepted", "refused")
        except P.DirectoryError:
            check(f"config refuses {bad!r}", "refused", "refused")
    check("config: http on this machine is fine (tests)",
          P.config({"url": "http://127.0.0.1:8080/v1/directory.json"})[0], "http://127.0.0.1:8080/v1/directory.json")

    # --- parse_doc: whole-file refusals and per-row drops ---
    rows, dropped, gen = P.parse_doc(doc([station()]))
    check("good file: one row, none dropped", (len(rows), dropped, gen), (1, 0, "2026-11-10T18:00:00Z"))
    check("good row carries the dial frequency", rows[0]["entries"][0]["dial_khz"], 7102.0)
    for name, body in [("wrong schema", doc([station()], schema=2)), ("truncated JSON", doc([station()])[:-7]),
                       ("oversize", b" " * (P.MAX_BYTES + 1)), ("not an object", b"[1,2]"),
                       ("no station list", json.dumps({"schema": 1}).encode())]:
        try:
            P.parse_doc(body)
            check(f"{name}: refused whole", "accepted", "refused")
        except P.DirectoryError:
            check(f"{name}: refused whole", "refused", "refused")
    hostile = [station("N0AAA", note="<script>alert(1)</script>"),            # kept: HTML is data
               station("N0BBB", note="bell\x07"),                              # control char: dropped
               station("N0CCC", entries=[entry(centre_khz=7350)]),             # out of band: dropped
               station("N0DDD", entries=[entry(mode="FT8")]),                  # unknown mode: dropped
               station("N0EEE", entries=[entry(bw_hz=600)]),                   # bad width: dropped
               station("not a call"),                                          # bad call: dropped
               station("N0FFF", grid="ZZ99"),                                  # bad grid: dropped
               station("N0GGG", entries=[entry(hours="h" * 41)]),              # hours too long: dropped
               station("N0HHH", entries=[entry(centre_khz=float("nan"))]),     # NaN: dropped
               station("N0III", entries=[entry()] * 13),                       # 13 entries: dropped
               "not an object",
               station("N0JJJ", entries=[entry(mode="PACKET", centre_khz=145090, baud=1200)])]
    hostile[-1]["entries"][0].pop("bw_hz")
    rows, dropped, _ = P.parse_doc(doc(hostile))
    check("hostile file: the good rows kept", [r["call"] for r in rows], ["N0AAA", "N0JJJ"])
    check("hostile file: the rest dropped and counted", dropped, 10)
    check("HTML in a note is kept verbatim (the page escapes it)", rows[0]["note"], "<script>alert(1)</script>")
    check("packet row keeps baud, dial = centre", (rows[1]["entries"][0]["baud"], rows[1]["entries"][0]["dial_khz"]),
          (1200, 145090))

    # --- fetching: a local fake directory server ---
    state = {"body": doc([station(), station("K1ABC", grid="FN31")]), "status": 200, "hits": 0}

    async def serve(request):
        state["hits"] += 1
        return web.Response(status=state["status"], body=state["body"], content_type="application/json")

    app = web.Application()
    app.router.add_get("/v1/directory.json", serve)
    runner = web.AppRunner(app)
    await runner.setup()
    port = testkit.free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    url = f"http://127.0.0.1:{port}/v1/directory.json"
    tmp = tempfile.mkdtemp(prefix="p2pdir-")
    cache = os.path.join(tmp, P.CACHE_FILE)
    clock = Clock()
    try:
        async with aiohttp.ClientSession() as http:
            d = P.Directory(url, 6, cache_path=cache, clock=clock)
            v = d.view()
            check("before any fetch: empty, with a reason", (v["rows"], bool(v["reason"])), ([], True))
            await d.refresh(http)
            v = d.view(here_grid="EM12", mycall="N9ABC-10")
            check("fetched: two rows", [r["call"] for r in v["rows"]], ["N9ABC", "K1ABC"])
            check("fetched: age 0, no error", (v["age_s"], v["error"], v["reason"]), (0, None, None))
            check("own call (any SSID) marked this station", [r["self"] for r in v["rows"]], [True, False])
            check("distance from the station's grid", (v["rows"][0]["distance_km"], v["rows"][1]["distance_km"] > 1500),
                  (0, True))
            check("a copy is kept on disk", os.path.exists(cache), True)

            await d.refresh(http)
            check("not refetched before refresh_hours", state["hits"], 1)
            clock.t += 6 * 3600
            state["status"] = 500
            await d.refresh(http)
            v = d.view()
            check("fetch fails: rows still served", len(v["rows"]), 2)
            check("fetch fails: age and the error shown", (v["age_s"], "500" in (v["error"] or "")), (6 * 3600, True))
            await d.refresh(http)
            check("a failed fetch is not retried at once", state["hits"], 2)
            clock.t += P.RETRY_S
            state["status"], state["body"] = 200, doc([station()], schema=7)
            await d.refresh(http)
            check("a bad file on refresh keeps the good copy", (len(d.view()["rows"]), "schema" in d.view()["error"]),
                  (2, True))

            # A restart: the next Directory starts from the copy on disk.
            d2 = P.Directory(url, 6, cache_path=cache, clock=clock)
            check("restart: the cached copy is served at once", len(d2.view()["rows"]), 2)
            check("restart: with its true age", d2.view()["age_s"], 6 * 3600 + P.RETRY_S)

            # No cache and no network.
            dead = P.Directory(f"http://127.0.0.1:{testkit.free_port()}/v1/directory.json", 6,
                               cache_path=os.path.join(tmp, "none.json"), clock=clock)
            await dead.refresh(http)
            v = dead.view()
            check("no cache, no network: empty list", v["rows"], [])
            check("no cache, no network: the reason says so", v["reason"].startswith("No copy of the directory yet"), True)

            # Oversize over the wire is refused, not read whole.
            state["body"] = b"{" + b" " * (P.MAX_BYTES + 10) + b"}"
            big = P.Directory(url, 6, cache_path=None, clock=clock)
            await big.refresh(http)
            check("oversize over the wire: refused", ("kB" in (big.view()["error"] or ""), big.view()["rows"]), (True, []))
    finally:
        await runner.cleanup()


asyncio.run(main())
print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
