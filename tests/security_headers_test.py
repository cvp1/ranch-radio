"""webguard.add_security_headers: the CSP, nosniff and Referrer-Policy on
every response an app sends (a page, a static file, JSON, a WebSocket
handshake, a 404), and a response that brings its own policy keeps it
(a proxied radio page, a Winlink attachment). Also: the radio server, the
station and the packet hub each attach them, and the policy never allows
inline script or eval.

    python3 tests/security_headers_test.py
"""
import asyncio
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from aiohttp import ClientSession, web  # noqa: E402
import webguard  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


async def main():
    static = tempfile.mkdtemp(prefix="sechdr-")
    pathlib.Path(static, "a.js").write_text("1;\n")

    async def page(_):
        return web.Response(text="<p>hi</p>", content_type="text/html")

    async def api(_):
        return web.json_response({"ok": True})

    async def own(_):
        return web.Response(text="x", headers={"Content-Security-Policy": "sandbox"})

    async def ws(request):
        w = web.WebSocketResponse()
        await w.prepare(request)
        await w.close()
        return w

    app = web.Application()
    webguard.add_security_headers(app)
    app.router.add_get("/", page)
    app.router.add_get("/api", api)
    app.router.add_get("/own", own)
    app.router.add_get("/ws", ws)
    app.router.add_static("/static", static)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    try:
        async with ClientSession() as s:
            for path in ("/", "/api", "/static/a.js", "/nope"):
                async with s.get(base + path) as r:
                    check(f"{path}: the policy", r.headers.get("Content-Security-Policy"), webguard.CSP)
                    check(f"{path}: nosniff", r.headers.get("X-Content-Type-Options"), "nosniff")
                    check(f"{path}: referrer same-origin", r.headers.get("Referrer-Policy"), "same-origin")
            async with s.get(base + "/own") as r:
                check("a response's own policy is kept", r.headers.get("Content-Security-Policy"), "sandbox")
                check("  and still gets nosniff", r.headers.get("X-Content-Type-Options"), "nosniff")
            async with s.ws_connect(base + "/ws") as w:
                check("the WebSocket handshake carries it", w._response.headers.get("Content-Security-Policy"),
                      webguard.CSP)
    finally:
        await runner.cleanup()


asyncio.run(main())

parts = dict((p.split(" ", 1) + [""])[:2] for p in webguard.CSP.split("; "))
check("script-src is our own files only", parts.get("script-src"), "'self'")
check("no unsafe-inline or eval for script", any(x in webguard.CSP.split("style-src")[0] for x in ("unsafe-inline", "unsafe-eval")),
      False)
check("plugins off", parts.get("object-src"), "'none'")
check("no <base> hijack", parts.get("base-uri"), "'self'")
for f in ("server.py", "station.py", "tnc_hub.py"):
    check(f"{f} attaches the headers", "webguard.add_security_headers(app)" in (ROOT / f).read_text(), True)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
