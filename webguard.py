"""Cross-site guards shared by the station (station.py) and the packet hub
(tnc_hub.py). The LAN is trusted; a web page from the internet, open in a
browser on the LAN, is not (APRS plan, threat model).

Two checks:
  - host_allowed: the Host a request names must be one an internet page can't
    point at us. DNS rebinding gives an attacker's name (evil.example) this
    machine's address, so its page becomes same-origin with us; refusing
    names we don't know closes that. Allowed: IP literals, single-label names
    (linux-station), this machine's own names, every local-only domain (.local,
    .lan, .home, .home.arpa, .internal, .localdomain, ... : no one can
    register them on the internet, so no attacker can point one at us;
    2026-10-19: "allow all local host names"), Tailscale MagicDNS
    (.ts.net), and any names given with --allow-host. Not "any name that
    resolves to a LAN address": that is exactly what rebinding fakes.
  - same_origin: a browser's Origin, when present, must be exactly this
    server: scheme and authority (review 2026-10-19, Codex).
"""
import ipaddress
import re
import socket
from urllib.parse import urlsplit

LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain", ".intranet",
                  ".private", ".corp", ".localhost", ".ts.net")


def _own_names():
    names = {"localhost"}
    for f in (socket.gethostname, socket.getfqdn):
        try:
            names.add(f().lower().rstrip("."))
        except OSError:
            pass
    return names


OWN_NAMES = _own_names()


def host_name(host):
    """'linux-station:7300' -> 'linux-station'; '[::1]:7310' -> '::1'; lower case."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host[1:].partition("]")[0]
    if host.count(":") == 1:
        host = host.partition(":")[0]
    return host.rstrip(".")


def host_allowed(host, extra=()):
    name = host_name(host)
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if name in OWN_NAMES or "." not in name or name.endswith(LOCAL_SUFFIXES):
        return True
    return name in {n.lower().rstrip(".") for n in extra}


def same_origin(request):
    origin = request.headers.get("Origin")
    if origin is None:
        return True                     # not a browser (apps, scripts, the station's proxy)
    o = urlsplit(origin)
    return (o.scheme.lower() == request.scheme.lower()
            and o.netloc.lower() == (request.host or "").lower())


def cross_site_guard(allow_hosts=(), log=None):
    """aiohttp middleware: the LAN is trusted and any of its pages may
    transmit (by design); a web page from elsewhere, open in a browser on the
    LAN, may not. Every request must name a Host we know (DNS rebinding);
    browsers send Origin with every POST and every WebSocket handshake
    (WebSockets get no CORS check), so both must come from this server's own
    pages. Used by the station (APRS plan M0) and the radio server (modules
    plan M0, 2026-10-21: the Mac's radio page had no guard)."""
    from aiohttp import web

    @web.middleware
    async def guard(request, handler):
        if not host_allowed(request.host, allow_hosts):
            if log:
                log.warning("request for unknown host %r refused (%s %s)", request.host, request.method, request.path)
            return web.json_response({"error": "unknown host name (add it with --allow-host)"}, status=403)
        upgrade = request.headers.get("Upgrade", "").lower() == "websocket"
        if (upgrade or request.method not in ("GET", "HEAD", "OPTIONS")) and not same_origin(request):
            if log:
                log.warning("cross-site %s %s refused (Origin %s)", request.method, request.path,
                            request.headers.get("Origin"))
            return web.json_response({"error": "cross-site request refused"}, status=403)
        return await handler(request)
    return guard


def allowed_names(args):
    """--allow-host names plus the TLS names (not the %l placeholder)."""
    return [*getattr(args, "allow_host", []), *[n for n in getattr(args, "tls_name", []) if n != "%l"]]


# ---------------------------------------------------------------- proxying a local backend
# Shared by the station (its radios' pages, the packet hub) and the packet
# add-on on a radio's page.

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailers", "transfer-encoding", "upgrade", "host", "content-length"}

PLAIN_PATH = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=:@/-]*")


def canonical_tail(request):
    """The proxied path as the backend will receive it, or None if it isn't a
    plain path. aiohttp keeps `x/../api/lend` in the route's tail while the
    outgoing URL normalises it to `api/lend`, so a check on the tail could be
    walked around (review 2026-10-19, Codex; reproduced on linux-station). Refuse
    dot segments (also %2e-encoded, decoded by then), backslashes, a leftover
    percent sign (double encoding) and empty segments other than a trailing one."""
    tail = request.match_info["tail"]
    # The tail is already percent-decoded: a decoded "?", "#" or control
    # character would change what the outgoing URL means (review 2026-10-19,
    # Codex: /packet/api/lend%3fx reached lend). Only plain path characters.
    if not PLAIN_PATH.fullmatch(tail):
        return None
    parts = tail.split("/")
    for i, seg in enumerate(parts):
        if seg in (".", "..") or (seg == "" and i != len(parts) - 1):
            return None
    return tail


