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


LOOPBACK = ("127.0.0.1", "::1")


def from_this_machine(request):
    """True only for a caller on this machine. A proxy on this machine (the
    station in front of a radio, `tailscale serve` in front of the station)
    makes every caller's connection look like loopback, so the forwarded
    chain counts too: every X-Forwarded-For hop must be loopback as well,
    and a request through Tailscale Serve (Tailscale-Headers-Info, sent on
    every one) never is. Tailscale sets X-Forwarded-For to the tailnet
    peer (seen live 2026-11-15); the station chains it (forwarded_for)."""
    if request.remote not in LOOPBACK:
        return False
    if "Tailscale-Headers-Info" in request.headers:
        return False
    hops = [h.strip() for v in request.headers.getall("X-Forwarded-For", []) for h in v.split(",")]
    return all(h in LOOPBACK for h in hops if h)


def forwarded_for(request):
    """The X-Forwarded-For a proxy here sends on: what it was given, then the
    caller it saw. A caller can add hops but never remove the real one."""
    prior = ", ".join(request.headers.getall("X-Forwarded-For", []))
    return f"{prior}, {request.remote}" if prior else str(request.remote)


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



# ---------------------------------------------------------------- response headers
# Every page renders text that arrived over the air or the mesh (APRS
# comments, node names, Winlink subjects) through innerHTML, so a missed
# esc() is script on the LAN page. The policy makes that miss inert: only
# our own files run (no inline script, no eval), and plugins and <base> are
# off. Inline style stays allowed (the pages set style= freely); images may
# come from any https host (map tiles from four basemap servers, and what
# their redirects reach). connect-src names ws:/wss: as well as 'self' for
# browsers that do not match a WebSocket against 'self'. No frame-ancestors:
# a home dashboard may embed the station. Referrer-Policy is same-origin,
# not no-referrer: no-referrer makes a browser send "Origin: null" on a form
# POST, and same_origin above would refuse it. (2026-11-21, review plan PR 3.)
CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob: https:",
    "connect-src 'self' ws: wss:",
    "media-src 'self' blob:",
    "worker-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
))
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


async def _security_headers(request, response):
    # setdefault: a response that brings its own (a proxied radio page, a
    # Winlink attachment with Pat's sandbox policy) keeps it.
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)


def add_security_headers(app):
    """Attach the headers to every response the app sends, WebSocket
    handshakes and static files included (on_response_prepare runs for
    all of them; a middleware misses an already-prepared response)."""
    app.on_response_prepare.append(_security_headers)
    return app


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


