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
import os
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
# browsers that do not match a WebSocket against 'self', and blob: (a page's
# own exports). frame-src allows any http(s) page: the Logbook tab frames the
# operator's Wavelog on another host (a framed page runs in its own origin). No frame-ancestors:
# a home dashboard may embed the station. Referrer-Policy is same-origin,
# not no-referrer: no-referrer makes a browser send "Origin: null" on a form
# POST, and same_origin above would refuse it. (2026-11-21, review plan PR 3.)
CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob: https:",
    "connect-src 'self' ws: wss: blob:",
    "frame-src 'self' http: https:",
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



# ---------------------------------------------------------------- /healthz
# A readiness answer for scripts, monitors and `ranch-radio status`: is this
# process up, which role is it, and what about it is degraded. Not the
# restart check: service.idle() reads the WebSocket hello (TX, Winlink,
# rigctl clients) and stays the only thing that decides a restart is safe
# (review plan PR 1, panel 2026-11-21). The route is guarded like every
# other: the Host check above, and the server's token when it has one.

def healthz(role, facts, authorised=None):
    """An aiohttp handler for GET /healthz. facts() -> (dict, [degraded...])."""
    import time
    from aiohttp import web
    started = time.monotonic()

    async def handler(request):
        if authorised is not None and not authorised(request):
            return web.json_response({"error": "token required"}, status=401)
        try:
            extra, degraded = facts()
        except Exception as e:                       # noqa: BLE001 - a health answer never raises
            extra, degraded = {}, [f"facts failed: {type(e).__name__}"]
        body = {"ok": not degraded, "role": role, "uptime_s": int(time.monotonic() - started),
                "pid": os.getpid(), "degraded": list(degraded), **extra}
        return web.json_response(body, headers={"Cache-Control": "no-store"})
    return handler



# ---------------------------------------------------------------- operator / read-only
# Two modes, set in the host file's `auth` block (render passes them on):
#   open          (default) every LAN page operates, as before.
#   readonly-lan  a page without the operator's session can watch (state,
#                 spectrum, audio, logs, map) and press STOP TX, and nothing
#                 else: no keying, no QSY, no Winlink, no settings, no add-on
#                 or module changes. The operator unlocks with the token
#                 (POST /auth/login sets a signed cookie, HttpOnly,
#                 SameSite=Strict, 12 h).
# The token lives in a file only the user can read (checked at start; a
# group- or world-readable or missing file stops the server: fail closed),
# or $RANCH_RADIO_TOKEN; never in argv. The radio server enforces its own
# commands (it owns the transmitter): the station forwards the browser's
# cookie through its proxy, and its own lane and watch connections carry the
# token as a Bearer header. A cookie is HMAC(token, expiry): every process on
# the host checks it with the same file, and changing the token ends every
# session (review plan PR 4, panel 2026-11-21).
import hashlib
import hmac
import time as _time

AUTH_MODES = ("open", "readonly-lan")
COOKIE = "rr_op"
SESSION_S = 12 * 3600


class AuthError(SystemExit):
    pass


def read_token_file(path):
    """The token from a file only its owner can read; AuthError otherwise."""
    p = os.path.expanduser(path)
    try:
        st = os.stat(p)
    except OSError as e:
        raise AuthError(f"auth.token_file {path}: {e.strerror}; readonly-lan needs it (fail closed)") from e
    if st.st_mode & 0o077:
        raise AuthError(f"auth.token_file {path} is readable by others (mode {st.st_mode & 0o777:o}); "
                        f"chmod 600 it (fail closed)")
    token = open(p).read().strip()
    if len(token) < 12:
        raise AuthError(f"auth.token_file {path}: the token is shorter than 12 characters (fail closed)")
    return token


class AuthPolicy:
    def __init__(self, mode="open", token=None):
        if mode not in AUTH_MODES:
            raise AuthError(f"auth.mode {mode!r}: one of {', '.join(AUTH_MODES)}")
        if mode != "open" and not token:
            raise AuthError(f"auth.mode {mode}: no token (auth.token_file or $RANCH_RADIO_TOKEN); fail closed")
        self.mode, self.token = mode, token

    @classmethod
    def from_config(cls, mode=None, token_file=None):
        mode = mode or "open"
        token = os.environ.get("RANCH_RADIO_TOKEN") or (read_token_file(token_file) if token_file else None)
        return cls(mode, token)

    def _sign(self, exp):
        return hmac.new(self.token.encode(), f"operator|{exp}".encode(), hashlib.sha256).hexdigest()

    def cookie_value(self, now=None):
        exp = int((now or _time.time()) + SESSION_S)
        return f"{exp}.{self._sign(exp)}"

    def cookie_ok(self, value, now=None):
        try:
            exp_s, sig = str(value).split(".", 1)
            exp = int(exp_s)
        except ValueError:
            return False
        return exp > (now or _time.time()) and hmac.compare_digest(sig, self._sign(exp))

    def bearer_ok(self, request):
        h = request.headers.get("Authorization", "")
        return h.startswith("Bearer ") and hmac.compare_digest(h[7:].strip(), self.token)

    def role(self, request):
        """'operator' or 'readonly'."""
        if self.mode == "open":
            return "operator"
        if self.bearer_ok(request) or self.cookie_ok(request.cookies.get(COOKIE, "")):
            return "operator"
        return "readonly"

    def session_expiry(self, request):
        """When this request's operator session ends (None: no expiry, open or Bearer)."""
        if self.mode == "open" or self.bearer_ok(request):
            return None
        v = request.cookies.get(COOKIE, "")
        try:
            return int(v.split(".", 1)[0]) if self.cookie_ok(v) else 0
        except ValueError:
            return 0

    def bearer_headers(self):
        """For this host's own connections to its radios (RemoteLane, RadioWatch)."""
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}


def tx_off_only(body):
    """A transmit switch request that turns transmit off (the hub's Off): safe for anyone."""
    return isinstance(body, dict) and body.get("enabled") is False


def auth_guard(policy, readonly_ok=()):
    """aiohttp middleware: in readonly-lan, a read-only caller gets only
    GET/HEAD/OPTIONS, the /auth routes, and the POST paths in readonly_ok
    (path -> predicate(body) or None for any body)."""
    from aiohttp import web

    @web.middleware
    async def guard(request, handler):
        request["rr_role"] = policy.role(request)
        if (policy.mode == "open" or request["rr_role"] == "operator"
                or request.method in ("GET", "HEAD", "OPTIONS") or request.path.startswith("/auth/")):
            return await handler(request)
        allow = dict(readonly_ok).get(request.path, False)
        if allow is not False:
            if allow is None:
                return await handler(request)
            try:
                body = await request.json()
            except ValueError:
                body = None
            if allow(body):
                return await handler(request)
        return web.json_response({"error": "read-only: unlock to operate", "role": "readonly"}, status=403)
    return guard


def add_auth_routes(app, policy):
    """POST /auth/login {token}, POST /auth/logout, GET /auth/whoami."""
    from aiohttp import web

    async def login(request):
        try:
            token = str((await request.json()).get("token", ""))
        except (ValueError, AttributeError):
            token = ""
        if policy.mode == "open":
            return web.json_response({"role": "operator", "mode": "open"})
        if not token or not hmac.compare_digest(token, policy.token):
            return web.json_response({"error": "wrong token"}, status=401)
        r = web.json_response({"role": "operator", "mode": policy.mode, "expires_s": SESSION_S})
        r.set_cookie(COOKIE, policy.cookie_value(), max_age=SESSION_S, path="/", httponly=True,
                     samesite="Strict", secure=request.secure)
        return r

    async def logout(request):
        r = web.json_response({"role": "readonly" if policy.mode != "open" else "operator", "mode": policy.mode})
        r.del_cookie(COOKIE, path="/")
        return r

    async def whoami(request):
        return web.json_response({"role": policy.role(request), "mode": policy.mode},
                                 headers={"Cache-Control": "no-store"})

    app.router.add_post("/auth/login", login)
    app.router.add_post("/auth/logout", logout)
    app.router.add_get("/auth/whoami", whoami)


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


