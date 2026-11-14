"""The logbook sync's host-file block, checked (PLAN v3 §3). Standard
library only, and nothing from the other logsync*.py files: render.py (which
the radio server reaches when it saves a codec level) and the `ranch-radio`
CLI import this, so the radio process never loads the sync's network,
SQLite or UDP code.

    check_block(block)      raises ValueError naming the field
    Config(block)           the checked block, as logsync.py reads it
"""

import ipaddress
import os
import re
from urllib.parse import urlsplit

DESTS = ("wavelog", "qrz", "lotw")
DEFAULT_LEDGER = "~/.local/state/radio-remote/logsync.db"
QRZ_URL = "https://logbook.qrz.com/api"
LOTW_URL = "https://lotw.arrl.org/lotwuser/lotwreport.adi"
TIMING = {"pair_wait_s": 3.0, "tqsl_timeout_s": 60.0, "backoff_s": [30.0, 900.0], "http_timeout_s": 30.0,
          "lotw_every_s": 3600.0, "qrz_every_s": 3600.0, "wavelog_every_s": 600.0, "pull_at_start": True}
_CALL = re.compile(r"[A-Z0-9/]{3,12}")


def _addr_ok(text):
    host, sep, port = str(text).rpartition(":")
    return bool(sep and host and port.isdigit() and 1 <= int(port) <= 65535)


def _loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


class Config:
    """The host file's logsync block, checked. Raises ValueError naming the
    field (render.py calls this before anything renders)."""

    def __init__(self, b):
        if not isinstance(b, dict):
            raise ValueError("logsync must be an object")
        u = b.get("udp")
        if not isinstance(u, dict):
            raise ValueError("logsync.udp is required: {\"group\", \"port\"} or {\"bind\", \"port\", \"relay\"}")
        port = u.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"logsync.udp.port = {port!r}: a port number")
        self.port = port
        if ("group" in u) == ("bind" in u):
            raise ValueError("logsync.udp: give group (multicast) or bind (a unicast relay), one of them")
        if "group" in u:
            self.shape = "multicast"
            try:
                g = ipaddress.IPv4Address(u["group"])
            except (ValueError, TypeError):
                raise ValueError(f"logsync.udp.group = {u['group']!r}: an IPv4 multicast address") from None
            if not g.is_multicast:
                raise ValueError(f"logsync.udp.group = {u['group']!r}: not a multicast address (224.0.0.0/4)")
            self.group, self.iface = str(g), str(u.get("iface", "0.0.0.0"))
            if "relay" in u:
                raise ValueError("logsync.udp.relay is for the unicast relay (bind), not multicast")
        else:
            self.shape = "unicast"
            self.bind = str(u["bind"])
            self.relay = u.get("relay")
            if self.relay is not None and not _addr_ok(self.relay):
                raise ValueError(f"logsync.udp.relay = {self.relay!r}: host:port")
        fwd = b.get("forward", [])
        if not isinstance(fwd, list) or not all(isinstance(x, str) and _addr_ok(x) for x in fwd):
            raise ValueError("logsync.forward: a list of host:port")
        self.forward = fwd
        call = b.get("call")
        if call is not None and not (isinstance(call, str) and _CALL.fullmatch(call.upper())):
            raise ValueError(f"logsync.call = {call!r}: a callsign")
        self.call = call.upper() if call else None
        self.ledger = os.path.expanduser(str(b.get("ledger", DEFAULT_LEDGER)))
        self.wavelog = self._dest(b, "wavelog", ("url", "station_profile_id", "key_file"))
        if self.wavelog:
            url = self.wavelog["url"]
            if not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
                raise ValueError(f"logsync.wavelog.url = {url!r}: http(s)://host[:port]")
            sid = self.wavelog["station_profile_id"]
            if isinstance(sid, bool) or not isinstance(sid, int) or sid < 1:
                raise ValueError(f"logsync.wavelog.station_profile_id = {sid!r}: a positive number")
        self.qrz = self._dest(b, "qrz", ("key_file",))
        if self.qrz:
            self.qrz["url"] = self._https(self.qrz.get("url", QRZ_URL), "qrz")
            if not self.call:
                raise ValueError("logsync.call is required with qrz (QRZ asks for an identifiable User-Agent)")
        self.lotw = self._dest(b, "lotw", ("tqsl", "location", "login", "password_file"))
        if self.lotw:
            self.lotw["url"] = self._https(self.lotw.get("url", LOTW_URL), "lotw")
            loc = self.lotw["location"]
            if not isinstance(loc, str) or not loc.strip() or loc.startswith("-"):
                raise ValueError(f"logsync.lotw.location = {loc!r}: TQSL's station location name")
        t = dict(TIMING)
        for k, v in (b.get("timing") or {}).items():
            if k not in TIMING:
                raise ValueError(f"logsync.timing.{k}: unknown")
            t[k] = v
        lo, hi = t["backoff_s"]
        self.timing = t
        self.backoff = (float(lo), float(hi))

    @staticmethod
    def _dest(b, name, need):
        d = b.get(name)
        if d is None:
            return None
        if not isinstance(d, dict):
            raise ValueError(f"logsync.{name} must be an object")
        for k in need:
            if k not in d:
                raise ValueError(f"logsync.{name}.{k} is required")
        return dict(d)

    @staticmethod
    def _https(url, name):
        u = urlsplit(str(url))
        if u.scheme == "https" or (u.scheme == "http" and _loopback(u.hostname or "")):
            return str(url)
        raise ValueError(f"logsync.{name}.url must be https (it carries a credential)")

    @property
    def dests(self):
        return tuple(d for d in DESTS if getattr(self, d))


def check_block(block):
    """For render.py: raises ValueError naming the field."""
    Config(block)
