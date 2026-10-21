"""Where a host file lives.

An installed Ranch Radio keeps its own host file outside the checkout, in
~/.config/ranch-radio/hosts/<host>.json (what `ranch-radio init` writes), so a
`git pull` never touches it and the page's saves don't dirty the repo. The
repo's hosts/ is the fallback: a setup that keeps its host files in the checkout works as before.

Search order for <host>.json, first found wins:
    $RANCH_RADIO_HOSTS (a directory; set to "" to skip the user directory)
    ~/.config/ranch-radio/hosts
    <repo>/hosts
The host-file lock (hostlock) lives beside the file it guards.
"""

import os
from pathlib import Path

USER_HOSTS = "~/.config/ranch-radio/hosts"


def user_dir():
    env = os.environ.get("RANCH_RADIO_HOSTS")
    if env is not None:
        return Path(os.path.expanduser(env)) if env else None
    return Path(os.path.expanduser(USER_HOSTS))


def dirs(repo_hosts):
    out = []
    u = user_dir()
    if u is not None:
        out.append(u)
    out.append(Path(repo_hosts))
    return out


def find(repo_hosts, host):
    """The host file for host, or None."""
    for d in dirs(repo_hosts):
        p = d / f"{host}.json"
        if p.is_file():
            return p
    return None


def all_hosts(repo_hosts):
    """{name: path} for every host file; the user directory wins on a clash."""
    out = {}
    for d in reversed(dirs(repo_hosts)):
        if d.is_dir():
            for p in sorted(d.glob("[!._]*.json")):
                out[p.stem] = p
    return dict(sorted(out.items()))


def shown(path):
    """A host file's path as the operator knows it."""
    home = os.path.expanduser("~")
    s = str(path)
    return "~" + s[len(home):] if s.startswith(home + os.sep) else s
