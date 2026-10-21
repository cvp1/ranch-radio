"""Save a radio's codec levels from its page into hosts/<host>.json.

    save(repo, host, radio_id, use, table) -> [written paths]

The host file stays the one source of the levels: the page's Save edits the
radio's "codec_db" (Winlink) or "voice_codec_db" (voice) string in place,
leaving the rest of the hand-formatted file as it was, checks the result
(render.validate, including codec_min_db), and installs that radio's unit so
`ranch-radio drift` stays clean. The running server already holds the new
level, so nothing is restarted. The file is left uncommitted in the repo.

On Linux the unit write is followed by `systemctl --user daemon-reload`
(never a restart): without it systemd starts the old unit next time, and a
Digirig replug restarted the G90 at the replaced, hotter level (review
2026-10-18, Grok). A Mac's launchd keeps the plist it bootstrapped until the
agent is stopped and started again. One lock covers the whole read-modify-
install, so two Saves at once can't lose either table (Sol).
"""

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from . import hostlock, paths, render

KEYS = {"winlink": "codec_db", "voice": "voice_codec_db"}


def table_text(table):
    """{"default": -32.0, "40 m": -32.0} -> "default=-32,40m=-32" (default first)."""
    def db(v):
        return f"{v:g}"
    parts = [f"default={db(table['default'])}"]
    parts += [f"{band.replace(' ', '')}={db(v)}" for band, v in table.items() if band != "default"]
    return ",".join(parts)


def _radio_span(text, rid):
    """(start, end) of radio rid's object in the host file's text."""
    m = re.search(r'\{\s*"id"\s*:\s*"' + re.escape(rid) + r'"', text)
    if not m:
        raise ValueError(f"radio {rid!r} not found in the host file")
    depth, i, n = 0, m.start(), len(text)
    while i < n:
        c = text[i]
        if c == '"':                        # skip a string, escapes included
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return m.start(), i
        i += 1
    raise ValueError(f"radio {rid!r}'s object never closes in the host file")


def set_key(text, rid, key, value):
    """The host file's text with radio rid's key set to value (a string)."""
    start, end = _radio_span(text, rid)
    body = text[start:end]
    pair = re.compile(r'("' + re.escape(key) + r'"\s*:\s*)"[^"]*"')
    if pair.search(body):
        body = pair.sub(lambda m: m.group(1) + json.dumps(value), body, count=1)
    else:
        # A first voice table goes right after the Winlink one, its sibling.
        anchor = re.search(r'"codec_db"\s*:\s*"[^"]*"', body)
        if not anchor:
            raise ValueError(f"{rid} has no codec_db in the host file; add one before saving levels")
        body = body[:anchor.end()] + f', "{key}": {json.dumps(value)}' + body[anchor.end():]
    return text[:start] + body + text[end:]


def save(repo, host, rid, use, table, home=None):
    path = paths.find(Path(repo) / "hosts", host)
    if path is None:
        raise ValueError(f"no host file for {host!r} (~/.config/ranch-radio/hosts or the repo's hosts/)")
    # The one host-file lock: the page's module apply and the CLI's add/remove
    # write this file too (review 2026-10-21, G4).
    with hostlock.locked(path.parent):
        return _save(path, rid, use, table, home)


def _save(path, rid, use, table, home):
    key = KEYS[use]
    text = path.read_text()
    new = set_key(text, rid, key, table_text(table))
    h = json.loads(new)
    render.validate(h)                       # codec_min_db, ports, ...
    r = next(x for x in h["radios"] if x["id"] == rid)
    if r.get(key) != table_text(table):
        raise ValueError(f"{key} did not land in {rid} (host file layout?)")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        f.write(new)
    os.replace(tmp, path)
    written = install_radio(h, rid, home)
    if h["os"] == "linux" and written:
        reload_systemd()
    return [str(path)] + written


def reload_systemd():
    """Load the written unit without restarting anything (RANCH_RADIO_SYSTEMCTL:
    a stand-in for tests). Raises ValueError if systemd refuses."""
    cmd = [os.environ.get("RANCH_RADIO_SYSTEMCTL", "systemctl"), "--user", "daemon-reload"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise ValueError(f"saved, but systemctl daemon-reload failed: {(r.stderr or r.stdout).strip()}")


def install_radio(h, rid, home=None):
    """Write this radio's rendered unit(s) if they differ; old ones go to the
    backup tree, as `ranch-radio install` does. No restart."""
    home = Path(home or os.path.expanduser("~"))
    backups = home / ".local/state/ranch-radio/backups" / time.strftime("%Y-%m-%d_%H%M%S")
    written = []
    for art in render.artifacts(h, home=str(home)):
        if art.module != rid or art.fmt not in ("systemd", "plist"):
            continue
        p = home / art.path
        if p.exists() and render.same(art, p.read_bytes()):
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_symlink() or p.exists():
            b = backups / art.path
            b.parent.mkdir(parents=True, exist_ok=True)
            p.rename(b)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.")
        with os.fdopen(fd, "w") as f:
            f.write(art.text())
        os.replace(tmp, p)
        written.append(str(p))
    return written
