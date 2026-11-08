"""Logbook add-on: your web logbook (Wavelog, Cloudlog or any logbook with a
web page) in a tab beside Propagation.

The page shows the logbook in a frame, with a link to open it in its own tab.
Nothing here talks to the logbook: the browser loads it directly, with its
own login.

Options (the host file's addons block), one of:

    "logbook": {"enabled": true, "url": "https://192.168.1.20:8087/"}
    "logbook": {"enabled": true, "port": 8087}

`port` (with optional "scheme", default https, and "path", default "/")
means the logbook is on the machine the page was opened from, under the
same name the browser used for the page. Use it for a logbook on the radio's
own machine: the browser then counts the frame as the same site as the page,
so the logbook's login cookie (SameSite=Lax, Wavelog's default) is sent and
the login holds inside the frame. A `url` on another name works only if the
logbook's cookies allow a cross-site frame.

The frame needs the logbook to allow framing (Wavelog does by default) and,
on a secure (https) page, a secure logbook: browsers refuse a plain-http
frame inside an https page, so the tab then offers only the link.
"""
from urllib.parse import urlsplit

from . import Addon


def resolve(opts):
    """The add-on's options -> {"url"} or {"port", "scheme", "path"}.
    Raises ValueError, naming the field, for anything else."""
    url, port = opts.get("url"), opts.get("port")
    if url and port is not None:
        raise ValueError("give the logbook's url or its port, not both")
    if url:
        if not isinstance(url, str):
            raise ValueError("url must be a string")
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"url must be http(s)://host[:port]/path, got {url!r}")
        return {"url": url}
    if port is None:
        raise ValueError("needs the logbook's url, or its port on this machine")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"port must be 1-65535, got {port!r}")
    scheme = opts.get("scheme", "https")
    if scheme not in ("http", "https"):
        raise ValueError(f"scheme must be http or https, got {scheme!r}")
    path = opts.get("path", "/")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError(f"path must start with /, got {path!r}")
    return {"port": port, "scheme": scheme, "path": path}


class Logbook(Addon):
    name = "logbook"
    label = "Logbook"
    title = "Your web logbook (Wavelog), in a tab"
    group = "tools"
    css = True

    def __init__(self, host, opts):
        super().__init__(host, opts)
        self.target = resolve(opts)       # a bad config fails here: shown as not loaded, with the reason

    def snapshot(self):
        return dict(self.target)


ADDON = Logbook
