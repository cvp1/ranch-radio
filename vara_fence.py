"""The VARA backend switch's fence, as the radio server and the station
read it (plan winlink-mercury 2.6; ranch_radio/vara_backend.py writes it).
Stdlib only.

  CUTOVER_FENCE  {"until": epoch s}: a switch is running; nothing arms,
                 keys or tunes. Expires: a switch that died must not wedge
                 the radio.
  + PENDING      {"to": backend}: the modem is switched but Pat is not yet
                 restarted on its settings; VARA HF sessions are refused
                 until `--apply --finish`. Never expires (panel M1 r2 C7).
"""
import json
import logging
import time

log = logging.getLogger("server")

CUTOVER_FENCE = "~/.local/state/radio-remote/vara-cutover.json"
PENDING_SUFFIX = ".pending"
_EXPIRED_SEEN = set()


def _read_json(path):
    """(dict or None, unreadable?)"""
    try:
        with open(path) as f:
            d = json.load(f)
    except FileNotFoundError:
        return None, False
    except (OSError, ValueError):
        return None, True
    return (d if isinstance(d, dict) else None), not isinstance(d, dict)


def _unreadable(path):
    return (f"a VARA backend switch is under way: its fence file {path} can't be read. If no "
            f"`ranch-radio vara-backend` is running, move it aside (mv {path} ~/.Trash/) and run "
            f"`ranch-radio vara-backend <xara|mercury> --apply` again")


def cutover_fenced(path, scheme):
    """Why a session of this scheme (or, scheme None, a key or a tune) may
    not start now because of a backend switch (None: it may). An unreadable
    file refuses, naming it; an expired switch fence is ignored and logged
    once; the pending-Pat state (VARA HF only) does not expire."""
    d, bad = _read_json(path)
    if bad:
        return _unreadable(path)
    if d is not None:
        until = d.get("until")
        if isinstance(until, (int, float)) and time.time() < until:
            return "a VARA backend switch is under way (ranch-radio vara-backend)"
        if (path, until) not in _EXPIRED_SEEN:     # once per fence, not per key
            _EXPIRED_SEEN.add((path, until))
            log.warning("winlink: ignoring an expired VARA backend switch fence %s", path)
    if scheme != "varahf":
        return None
    d, bad = _read_json(path + PENDING_SUFFIX)
    if bad:
        return _unreadable(path + PENDING_SUFFIX)
    if d is not None:
        return "the VARA backend switch waits for Pat's settings (ranch-radio vara-backend --apply --finish)"
    return None
