"""The host file's station.peers, checked (stdlib only: ranch_radio/render.py
validates it, and station_peers.py runs it). See station_peers.py for what a
peer is and why its certificate is pinned."""
import re
from urllib.parse import urlsplit

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
PIN = re.compile(r"[0-9a-f]{64}")


class PeerError(ValueError):
    pass


def normal_pin(v):
    """A SHA-256 fingerprint as openssl or a browser shows it (AB:CD:..., any
    case) -> 64 lowercase hex digits; None when it is not one."""
    s = str(v or "").replace(":", "").replace(" ", "").lower()
    return s if PIN.fullmatch(s) else None


def check_peers(peers, own_name=None):
    """The host file's station.peers, checked -> the list station.json carries.
    Raises PeerError naming the field."""
    if peers is None:
        return []
    if not isinstance(peers, list):
        raise PeerError("station.peers: a list of {name, url, pin_sha256}")
    out, names = [], set()
    for i, p in enumerate(peers):
        where = f"station.peers[{i}]"
        if not isinstance(p, dict) or set(p) - {"name", "url", "pin_sha256", "token_file"}:
            raise PeerError(f"{where}: takes name, url, pin_sha256 and token_file")
        name = p.get("name")
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise PeerError(f"{where}.name {name!r}: a short lowercase name (letters, digits, -)")
        if name in names:
            raise PeerError(f"{where}.name {name!r}: named twice")
        if own_name and name == own_name:
            raise PeerError(f"{where}.name {name!r}: that is this host")
        names.add(name)
        url = p.get("url")
        u = urlsplit(url) if isinstance(url, str) else None
        if (u is None or u.scheme not in ("http", "https") or not u.hostname or u.path not in ("", "/")
                or u.query or u.fragment or u.username or u.password or any(c.isspace() for c in url)):
            raise PeerError(f"{where}.url {url!r}: the peer station's address, like https://linux-station.local:7300")
        e = {"name": name, "url": url.rstrip("/")}
        if "pin_sha256" in p:
            pin = normal_pin(p["pin_sha256"])
            if pin is None:
                raise PeerError(f"{where}.pin_sha256: 64 hex digits (ranch-radio peer-pin {url} prints it)")
            e["pin_sha256"] = pin
        if u.scheme == "https" and "pin_sha256" not in e:
            # Its certificate is self-signed: without a pin, anything on the LAN could stand in for it.
            raise PeerError(f"{where}: an https peer needs pin_sha256 (ranch-radio peer-pin {url} prints it)")
        if u.scheme == "http" and "pin_sha256" in e:
            raise PeerError(f"{where}: pin_sha256 is for an https url")
        if "token_file" in p:
            if not isinstance(p["token_file"], str) or not p["token_file"].strip():
                raise PeerError(f"{where}.token_file: the path of a file holding the peer's token")
            e["token_file"] = p["token_file"]
        out.append(e)
    return out
