"""The Mesh tab's conversations: what the operator keeps, hides and reads.

Nothing here touches the mesh. A message sent is sent; deleting it removes it
from this station's history only, and every other node keeps its copy. That is
said on the page wherever Delete is offered.

A conversation is "c<channel index>" or "d<node id>" (conv_of). Per
conversation the station keeps (ConvStore, a JSON file, mode 600):
    archived  hidden from the rail, under Archived; a new incoming message
              brings it back unless it is muted
    muted     no toast, its unread count drawn quietly; stays archived
    pinned    first in its section
    read_t    messages heard after this are unread: the count survives a
              reload and is the same in every browser
A conversation the file has never seen is read up to the file's "since", so
the first start does not mark the whole history unread.

Deleting (one message, or a whole conversation) moves the messages to a trash
file (Trash, JSON lines, mode 600, the newest TRASH_KEEP batches) and hands
back the batch's ref, which Undo restores. Reactions to a deleted message go
with it. A message of ours still being sent (sending, sent, queued, or waiting
to be sent again) is never deleted: its ACK and resend still point at it.
"""

import json
import logging
import os
import re
import time

log = logging.getLogger("mesh")

CONV = re.compile(r"^(c[0-7]|d![0-9a-f]{8})$")
TRASH_KEEP = 20                      # delete batches Undo can still bring back
FLAGS = {"archive": ("archived", True), "unarchive": ("archived", False),
         "mute": ("muted", True), "unmute": ("muted", False),
         "pin": ("pinned", True), "unpin": ("pinned", False)}
IN_FLIGHT = ("sending", "sent", "queued")


def conv_of(m):
    """The conversation a message (or reaction) belongs to, or None."""
    if m.get("dm"):
        peer = m.get("to") if m.get("mine") else m.get("from")
        return f"d{peer}" if peer else None
    ch = m.get("ch")
    return f"c{ch}" if isinstance(ch, int) and not isinstance(ch, bool) else None


def in_flight(m, resends=()):
    return bool(m.get("mine")) and (m.get("ack") in IN_FLIGHT or m.get("t") in resends
                                    or m.get("retry_at") is not None)


def pick(messages, conv=None, ts=None, resends=()):
    """The messages to delete: all of conv, or those whose t is in ts, plus the
    reactions to any of them. -> (to delete, kept because still being sent)."""
    want = set(ts or ())
    chosen = [m for m in messages if (conv is not None and conv_of(m) == conv) or m.get("t") in want]
    ids = {m.get("id") for m in chosen if m.get("id") is not None and not (m.get("emoji") and m.get("reply_id"))}
    chosen += [m for m in messages if m not in chosen and m.get("emoji") and m.get("reply_id") in ids]
    keep = [m for m in chosen if in_flight(m, resends)]
    return [m for m in chosen if m not in keep], keep


def _write_atomic(path, text):
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


class ConvStore:
    """{"since": t, "convs": {conv: {"archived", "muted", "pinned", "read_t"}}}"""

    def __init__(self, path, now=None):
        self.path = path
        self.since = time.time() if now is None else now
        self.convs = {}
        if path:
            self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
        except FileNotFoundError:
            self.save()                       # "since" is now: the history so far counts as read
            return
        except (OSError, ValueError) as e:
            log.warning("mesh conversations %s not read: %s", self.path, e)
            return
        if isinstance(d.get("since"), (int, float)):
            self.since = d["since"]
        self.convs = {k: v for k, v in (d.get("convs") or {}).items() if CONV.match(str(k)) and isinstance(v, dict)}

    def save(self):
        if not self.path:
            return
        try:
            _write_atomic(self.path, json.dumps({"since": self.since, "convs": self.convs}))
        except OSError as e:
            log.warning("mesh conversations %s not written: %s", self.path, e)

    def view(self):
        """What the page gets: every conversation's state, and the default read_t."""
        return {"since": self.since, "convs": self.convs}

    def read_t(self, conv):
        return (self.convs.get(conv) or {}).get("read_t", self.since)

    def act(self, conv, action, t=None):
        """archive / unarchive / mute / unmute / pin / unpin; read (up to t);
        unread (t: the newest incoming message, which becomes unread again).
        -> True if anything changed. Raises ValueError on a bad one."""
        if not CONV.match(str(conv)):
            raise ValueError("not a conversation")
        rec = dict(self.convs.get(conv) or {})
        if action in FLAGS:
            key, val = FLAGS[action]
            if val:
                rec[key] = True
            else:
                rec.pop(key, None)
        elif action in ("read", "unread"):
            if isinstance(t, bool) or not isinstance(t, (int, float)):
                raise ValueError("read and unread need the message time t")
            rec["read_t"] = max(self.read_t(conv), float(t)) if action == "read" else float(t) - 0.001
        else:
            raise ValueError(f"unknown conversation action {action!r}")
        if rec == (self.convs.get(conv) or {}):
            return False
        if rec:
            self.convs[conv] = rec
        else:
            self.convs.pop(conv, None)
        self.save()
        return True

    def heard(self, m):
        """An incoming message: it brings its conversation back from the
        archive unless muted. -> True if that changed anything."""
        conv = conv_of(m)
        rec = self.convs.get(conv)
        if m.get("mine") or not rec or not rec.get("archived") or rec.get("muted"):
            return False
        if m.get("emoji") and m.get("reply_id"):
            return False                       # a reaction is not a reason to come back
        rec.pop("archived")
        if not rec:
            self.convs.pop(conv)
        self.save()
        return True

    def forget(self, conv):
        """A DM deleted whole: its state goes too (a channel keeps its own)."""
        if conv.startswith("d") and self.convs.pop(conv, None) is not None:
            self.save()


class Trash:
    """Deleted messages, newest TRASH_KEEP batches: {"ref", "t", "what", "msgs"}."""

    def __init__(self, path):
        self.path = path
        self.batches = []
        if path:
            try:
                with open(path) as f:
                    for line in f:
                        try:
                            b = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(b, dict) and isinstance(b.get("msgs"), list) and b.get("ref"):
                            self.batches.append(b)
            except FileNotFoundError:
                pass
            except OSError as e:
                log.warning("mesh trash %s not read: %s", path, e)
            self.batches = self.batches[-TRASH_KEEP:]

    def _save(self):
        if not self.path:
            return
        try:
            _write_atomic(self.path, "".join(json.dumps(b) + "\n" for b in self.batches))
        except OSError as e:
            log.warning("mesh trash %s not written: %s", self.path, e)

    def put(self, msgs, what, now=None):
        now = time.time() if now is None else now
        ref = f"{now:.6f}"
        self.batches.append({"ref": ref, "t": now, "what": what, "msgs": msgs})
        del self.batches[:-TRASH_KEEP]
        self._save()
        return ref

    def take(self, ref):
        """The batch's messages, and the batch out of the trash; None if gone."""
        for i, b in enumerate(self.batches):
            if b["ref"] == ref:
                del self.batches[i]
                self._save()
                return b["msgs"]
        return None
