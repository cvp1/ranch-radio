"""Other stations' radios on the LAN (host file "station": {"peers": [...]}).

The ask (2026-11-24): "any ranch radio server can use any radio that is
connected to a ranch radio server on the LAN". A station lists the other
stations it may use; each one's radios then show in this station's radio
switcher as <radio>@<peer> (g90@linux-station), and /r/<radio>@<peer>/ is that
radio's own page, WebSocket and audio, proxied through the peer's station.

What stays where it was:
  - The radio server that owns a radio still keys it: its transmit time
    limit, STOP TX latch and lane are on that host, whoever's page asks.
    STOP TX on this station's page reaches a peer's radio as it reaches a
    local one (the page's latch speaks to every radio it lists).
  - A peer's radios are never this station's shared rigctl port, Winlink
    lane or JS8 radio: those need the radio's sound card on this machine.
    They live in their own list (Peers), so the local paths cannot pick one up.
  - Nothing chains: a station's /api/radios lists its own radios only, so a
    peer of a peer is not reachable through it (and two stations naming each
    other cannot loop).

Trust: a peer's station serves a self-signed certificate (server.py
tls_context), so its SHA-256 is pinned in the host file (pin_sha256);
`ranch-radio peer-pin URL` prints it. Plain http is allowed only for a peer
named with no pin (a test, or a LAN that wants it). A peer with auth on takes
a token_file: its bearer token goes up only for a request this station counts
as the operator's, never a read-only page's.
"""
import asyncio
import json
import logging
import os
import re
import time
from urllib.parse import urlsplit

import aiohttp

import webguard
from station_peers_config import PeerError, check_peers, normal_pin  # noqa: F401 (station.py uses them here)

log = logging.getLogger("station")

POLL_S = float(os.environ.get("RR_PEERS_POLL_S", "10"))   # how often a peer's radio list is read (tests: less)
TIMEOUT_S = 4.0                # one read; a peer slower than this counts as away
MAX_LIST_BYTES = 64 * 1024     # a peer's /api/radios answer, at most
RADIO_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
SEP = "@"                      # <radio>@<peer>: "@" never appears in a radio id (SLUG)


class Peer:
    """One other station: where it is, how it is trusted, what radios it has."""

    def __init__(self, cfg):
        self.name = cfg["name"]
        self.url = cfg["url"].rstrip("/")
        u = urlsplit(self.url)
        self.netloc = u.netloc                     # the Host its guard knows it by
        self.pin = cfg.get("pin_sha256")
        # aiohttp checks the certificate's SHA-256 instead of a CA (it is self-signed).
        self.ssl = aiohttp.Fingerprint(bytes.fromhex(self.pin)) if self.pin else None
        self.token_file = cfg.get("token_file")
        self.radios = []                           # [{"id", "name", "present"}] as it last said
        self.up = False
        self.error = None                          # why it is away (shown in the radio's title)
        self.seen = None                           # when it last answered

    def _token(self):
        if not self.token_file:
            return None
        try:
            return webguard.read_token_file(self.token_file)
        except (OSError, ValueError, webguard.AuthError) as e:     # it refuses a file others can read
            log.warning("peer %s: token_file: %s", self.name, e)
            return None

    def headers(self, operator):
        """What a request to this peer carries: the Host it knows itself by, and
        its token only for this station's operator."""
        h = {"Host": self.netloc}
        tok = self._token() if operator else None
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        return h

    def snapshot(self):
        return (self.up, tuple((r["id"], r["name"], r["present"]) for r in self.radios))

    async def poll(self, session):
        """Read its radio list. -> True when what it says changed."""
        before = self.snapshot()
        try:
            async with session.get(f"{self.url}/api/radios", ssl=self.ssl, headers=self.headers(False),
                                   timeout=aiohttp.ClientTimeout(total=TIMEOUT_S), allow_redirects=False) as r:
                if r.status != 200:
                    raise PeerError(f"HTTP {r.status}")
                raw = await r.content.read(MAX_LIST_BYTES + 1)
                if len(raw) > MAX_LIST_BYTES:
                    raise PeerError("radio list too long")
                self.radios = parse_radios(json.loads(raw))
            if not self.up:
                log.info("peer %s: up, %d radio(s): %s", self.name, len(self.radios),
                         ", ".join(r["id"] for r in self.radios) or "none")
            self.up, self.error, self.seen = True, None, time.time()
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as e:
            why = "its certificate is not the pinned one" if isinstance(e, aiohttp.ServerFingerprintMismatch) else (str(e) or type(e).__name__)
            if self.up or self.error != why:
                log.warning("peer %s (%s): away: %s", self.name, self.url, why)
            self.up, self.error = False, why
        return self.snapshot() != before


def parse_radios(d):
    """A peer's /api/radios answer -> [{"id", "name", "present"}], only what
    looks like a radio (an id this side can route, a short plain name)."""
    if not isinstance(d, dict) or not isinstance(d.get("radios"), list):
        raise ValueError("not a radio list")
    out = []
    for r in d["radios"][:32]:
        if not isinstance(r, dict) or not isinstance(r.get("id"), str) or not RADIO_ID.fullmatch(r["id"]):
            continue
        name = r.get("name") if isinstance(r.get("name"), str) else r["id"]
        out.append({"id": r["id"], "name": name[:40], "present": r.get("present") is True})
    return out


class Peers:
    """Every peer this station names, read every POLL_S; on_change() when any
    one's radios or reachability change."""

    def __init__(self, cfgs, on_change=lambda: None):
        self.peers = [Peer(c) for c in cfgs]
        self.on_change = on_change

    def __bool__(self):
        return bool(self.peers)

    def infos(self):
        """The peers' radios as the page's radio list has them (station.Radio.info's shape)."""
        out = []
        for p in self.peers:
            for r in p.radios:
                out.append({"id": f"{r['id']}{SEP}{p.name}", "name": f"{r['name']} ({p.name})",
                            "present": p.up and r["present"], "remote": p.name, "rigctl": None,
                            "shared": False, "winlink": False, "modems": [],
                            **({} if p.up else {"away": p.error or "not answering"})})
        return out

    def summary(self):
        """Each peer station, for the page: whether it answers, why not, and how
        many radios it last listed. A peer that has never answered lists no
        radios, so without this the page had nothing to say about it."""
        return [{"name": p.name, "up": p.up, "radios": len(p.radios),
                 **({} if p.up else {"error": p.error or "not answering yet"})} for p in self.peers]

    def find(self, rid):
        """'<radio>@<peer>' -> (Peer, the radio's id there), or None."""
        rid_, sep, name = str(rid).rpartition(SEP)
        if not sep:
            return None
        p = next((x for x in self.peers if x.name == name), None)
        if p is None or not any(r["id"] == rid_ for r in p.radios):
            return None
        return p, rid_

    async def loop(self):
        async with aiohttp.ClientSession() as session:
            while True:
                changed = False
                for ok in await asyncio.gather(*(p.poll(session) for p in self.peers), return_exceptions=True):
                    if isinstance(ok, BaseException):
                        log.warning("peer poll: %s", ok)
                    elif ok:
                        changed = True
                if changed:
                    self.on_change()
                await asyncio.sleep(POLL_S)
