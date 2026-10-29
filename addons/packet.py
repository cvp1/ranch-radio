"""Packet add-on: the packet hub (tnc_hub.py) as a tab on a radio's page.

A Linux host shows the hub on its station page (station.py). A Mac has no
station: its one radio server (server.py) runs the host's add-ons, and this
one gives it the same Packet tab. The hub's own page (APRS map and heard
list, the transmit switch, the Winlink packet link) is proxied under
/packet/ and shown in a frame, as the station does.

Options (rendered from the host file's packet block):

    {"url": "http://127.0.0.1:7310", "name": "Direwolf", "title": "Packet (AIOC)"}

- The proxy passes only plain paths (webguard.canonical_tail), so nothing
  reaches a hub endpoint by a dot-segment or encoding trick. Lending the
  radio (api/lend, api/return) is for this machine only, checked against the
  real caller.
- POSTs (the transmit switch) carry X-Ranch-Radio, the hub's header for its
  own pages. The radio server's cross-site guard has already checked the
  browser's Origin.
- Off: the tab stays, and the proxy answers 503. The hub runs on regardless:
  this add-on shows it, it doesn't own it.
"""

import asyncio
import logging
import time

import aiohttp
from aiohttp import web

import webguard

from . import Addon

log = logging.getLogger("addon.packet")
POLL_S = 10


class Packet(Addon):
    name = "packet"
    label = "Packet"
    title = "APRS and the Winlink packet link (the packet hub)"

    def __init__(self, host, opts):
        super().__init__(host, opts)
        self.url = str(opts.get("url") or "http://127.0.0.1:7310").rstrip("/")
        self.state = {"up": False, "last_rx": None, "tx": False}
        self._session = None
        self._task = None
        self._on = False

    async def start(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10, sock_connect=3),
                                              auto_decompress=False)
        self._on = True
        self._task = asyncio.create_task(self._poll(), name="packet-hub-poll")

    async def stop(self):
        self._on = False
        if self._task:
            self._task.cancel()
            self._task = None
        if self._session:
            await self._session.close()
            self._session = None
        self.state = {"up": False, "last_rx": None, "tx": False}

    async def _poll(self):
        """The hub's link state, for the tab's dot; said again only on change."""
        while True:
            try:
                async with self._session.get(f"{self.url}/api/status") as r:
                    d = await r.json(content_type=None)
                radio, tx = d.get("radio") or {}, d.get("tx") or {}
                new = {"up": bool(radio.get("up")), "last_rx": radio.get("last_rx"), "tx": bool(tx.get("enabled"))}
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, AttributeError):
                new = {"up": False, "last_rx": None, "tx": False}
            changed = (new["up"], new["tx"]) != (self.state["up"], self.state["tx"])
            self.state = new
            if changed:
                self.emit({"state": self.snapshot()})
            await asyncio.sleep(POLL_S)

    def snapshot(self):
        age = round(time.time() - self.state["last_rx"]) if self.state.get("last_rx") else None
        return {"up": self.state["up"], "tx": self.state["tx"], "last_rx_age": age,
                "hub_name": self.opts.get("name") or "", "hub_title": self.opts.get("title") or ""}

    def http(self, app, authorised):
        async def slash(request):
            raise web.HTTPFound("/packet/" + (f"?{request.query_string}" if request.query_string else ""))

        async def page(request):
            if not self._on or self._session is None:
                return web.json_response({"error": "the Packet add-on is off"}, status=503)
            if request.headers.get("Upgrade", "").lower() == "websocket":
                return web.json_response({"error": "the packet hub has no websocket"}, status=400)
            tail = webguard.canonical_tail(request)
            if tail is None:
                return web.json_response({"error": "not a plain path"}, status=400)
            if tail in ("api/lend", "api/return") and request.remote not in ("127.0.0.1", "::1"):
                return web.json_response({"error": "lending the radio is only allowed from this machine"},
                                         status=403)
            url = f"{self.url}/{tail}" + (f"?{request.query_string}" if request.query_string else "")
            headers = {k: v for k, v in request.headers.items() if k.lower() not in webguard.HOP | {"origin"}}
            headers["X-Ranch-Radio"] = "1"
            try:
                async with self._session.request(request.method, url, headers=headers,
                                                 data=await request.read() if request.body_exists else None,
                                                 allow_redirects=False) as r:
                    body = await r.read()
                    out = {k: v for k, v in r.headers.items() if k.lower() not in webguard.HOP}
                    return web.Response(status=r.status, body=body, headers=out)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                log.info("packet hub %s: %s", url, e)
                return web.json_response({"error": "the packet hub is not answering"}, status=502)

        app.router.add_get("/packet", slash)
        app.router.add_route("*", "/packet/{tail:.*}", page)


ADDON = Packet
