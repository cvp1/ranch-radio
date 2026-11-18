"""mesh.py's automatic resend and long texts in parts, in this process.

The engine runs against a stand-in bridge process: what it writes to the
bridge is read back here, and the bridge's answers (sent, ack, send_failed)
are handed to it the way its reader would. Timers are shortened through the
module's constants. Checks:
  - split_text: one packet when it fits; else "[n/N] " parts of at most 200
    bytes, between words, a long word between characters (never inside one),
    nothing lost, more than 9 parts refused
  - a NAK the mesh may get over (MAX_RETRANSMIT) is sent again by itself,
    RESEND_S apart, at most len(RESEND_S) more times, then left failed
  - a resend that gets through ends delivered, the old reason gone
  - not sent again: another NAK reason, a write error, a reaction, with
    auto_resend off, after Cancel, after the node goes away
  - a long text goes one part at a time, in order, each after the one before
    is ACKed (or its ACK wait runs out); a part that finally fails drops the
    rest; one sent again holds the rest until it settles; Cancel on a part
    not sent yet drops it and the ones after it
  - a reply too long for one packet, and a text over 9 parts, are refused
  - after a restart, a part that was never sent shows failed

    python3 tests/mesh_resend_test.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import mesh  # noqa: E402

mesh.RESEND_S = (0.1, 0.2, 0.3)
mesh.PART_GAP_S = 0.05
mesh.ACK_WAIT_S = 30.0
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


# --- split_text (pure)
check("fits one packet: itself", mesh.split_text("hello ranch"), ["hello ranch"])
check("exactly 200 bytes: one packet", mesh.split_text("x" * 200) == ["x" * 200], True)
words = " ".join(f"word{i:03d}" for i in range(50))           # 399 bytes
parts = mesh.split_text(words)
check("399 bytes of words: 3 parts", len(parts), 3)
check("  numbered [1/3] [2/3] [3/3]", [p[:6] for p in parts], ["[1/3] ", "[2/3] ", "[3/3] "])
check("  each at most 200 bytes", all(len(p.encode()) <= 200 for p in parts), True)
check("  split between words, nothing lost", " ".join(p[6:] for p in parts) == words, True)
long_word = "y" * 500
parts = mesh.split_text(long_word)
check("one 500-byte word: split between characters", "".join(p[6:] for p in parts) == long_word, True)
check("  each at most 200 bytes", all(len(p.encode()) <= 200 for p in parts), True)
accents = "é" * 300                                            # 600 bytes, two a character
parts = mesh.split_text(accents)
check("two-byte characters are never cut in half", "".join(p[6:] for p in parts) == accents, True)
check("  each at most 200 bytes", all(len(p.encode()) <= 200 for p in parts), True)
emoji = "🛰️ " * 80
parts = mesh.split_text(emoji.strip())
check("emoji: each part at most 200 bytes", all(len(p.encode()) <= 200 for p in parts), True)
check("9 parts' worth is allowed", len(mesh.split_text("z" * (194 * 9))), 9)
check("10 parts' worth is refused", mesh.split_text("z" * (194 * 9 + 1)), None)


# --- the engine against a stand-in bridge
class Station:
    def __init__(self):
        self.notices = []
        self.out = []

    def notice(self, msg, level="info", ws=None):
        self.notices.append((level, msg))

    def _broadcast(self, payload):
        self.out.append(json.loads(payload))

    async def _send(self, ws, payload):
        self.out.append(json.loads(payload))


class Stdin:
    def __init__(self):
        self.cmds = []

    def write(self, b):
        self.cmds.append(json.loads(b))

    async def drain(self):
        pass


def engine(auto_resend=True, log=None):
    args = SimpleNamespace(mesh_python="py", mesh_bridge="b.py", mesh_port="x", mesh_key=[], mesh_log=log,
                           mesh_auto_resend=auto_resend)
    e = mesh.MeshEngine(Station(), args)
    e.proc = SimpleNamespace(stdin=Stdin(), returncode=None)
    e.connected = True
    e.info = {"me": {"id": "!b2a6fd78"}, "channels": [{"index": 0, "name": "LongFast"}]}
    return e


ids = iter(range(5000, 9000))


def sends(e):
    return [c for c in e.proc.stdin.cmds if c["cmd"] == "send"]


def answer(e, cmd, ok=True, reason="NONE"):
    """The bridge took CMD (sent, with a packet id), then the mesh's verdict."""
    pid = next(ids)
    e._on_bridge({"ev": "sent", "ref": cmd["ref"], "id": pid})
    if ok is not None:
        e._on_bridge({"ev": "ack", "id": pid, "ok": ok, "reason": reason, "by": "!0000beef", "relayed": False})
    return pid


async def until(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


def mine(e):
    return [m for m in e.messages if m.get("mine")]


async def main():
    # A NAK the mesh may get over: sent again, at most 3 more times
    e = engine()
    await e.send_text(None, "gate is open", 0)
    answer(e, sends(e)[-1], ok=False, reason="MAX_RETRANSMIT")
    rec = mine(e)[-1]
    check("NAK: failed, and a time to send it again", (rec["ack"], "retry_at" in rec), ("failed", True))
    check("  the page heard about the retry time",
          any(o.get("update", {}).get("retry_at") for o in e.station.out), True)
    for n in (2, 3, 4):
        check(f"  sent again by itself (send {n})", await until(lambda: len(sends(e)) == n), True)
        check("    the same text, same channel", (sends(e)[-1]["text"], sends(e)[-1]["ch"]), ("gate is open", 0))
        answer(e, sends(e)[-1], ok=False, reason="MAX_RETRANSMIT")
    await asyncio.sleep(0.6)
    check("  then no more: 4 sends in all", len(sends(e)), 4)
    check("  left failed, nothing pending", (rec["ack"], "retry_at" in rec, rec.get("tries")), ("failed", False, 3))

    # The first try's ACK wait does not cut the resend's short
    # (resend at 0.5 s, each try's wait 1.0 s: the first's ends at 1.0, the
    # second's at 1.5; looked at 1.25 s in, a margin of 0.25 s either side)
    mesh.RESEND_S, mesh.ACK_WAIT_S = (0.5, 0.5, 0.5), 1.0
    e = engine()
    await e.send_text(None, "slow ack", 0)
    t0 = time.monotonic()
    answer(e, sends(e)[-1], ok=False, reason="MAX_RETRANSMIT")
    await until(lambda: len(sends(e)) == 2)
    second = answer(e, sends(e)[-1], ok=None)
    await asyncio.sleep(max(0.0, 1.25 - (time.monotonic() - t0)))   # past the first try's wait only
    check("the first try's ACK wait leaves the resend alone", mine(e)[-1]["ack"], "sent")
    e._on_bridge({"ev": "ack", "id": second, "ok": True, "reason": "NONE", "by": "!0000beef", "relayed": False})
    check("  and its ACK settles it", mine(e)[-1]["ack"], "delivered")
    mesh.RESEND_S, mesh.ACK_WAIT_S = (0.1, 0.2, 0.3), 30.0

    # A resend that gets through
    e = engine()
    await e.send_text(None, "second try", 0)
    answer(e, sends(e)[-1], ok=False, reason="MAX_RETRANSMIT")
    await until(lambda: len(sends(e)) == 2)
    answer(e, sends(e)[-1])
    rec = mine(e)[-1]
    check("a resend that gets through: delivered, no stale reason", (rec["ack"], rec.get("why")), ("delivered", None))

    # Not sent again
    for name, kw, run in (
            ("another NAK reason (NO_CHANNEL)", {}, lambda e: answer(e, sends(e)[-1], ok=False, reason="NO_CHANNEL")),
            ("a write error", {}, lambda e: e._on_bridge({"ev": "send_failed", "ref": sends(e)[-1]["ref"],
                                                          "error": "serial write failed"})),
            ("auto_resend off", {"auto_resend": False},
             lambda e: answer(e, sends(e)[-1], ok=False, reason="MAX_RETRANSMIT"))):
        e = engine(**kw)
        await e.send_text(None, "once", 0)
        run(e)
        await asyncio.sleep(0.3)
        check(f"not sent again: {name}", (len(sends(e)), mine(e)[-1]["ack"]), (1, "failed"))
    e = engine()
    e.messages.append({"k": "msg", "id": 1001, "t": 1.0, "from": "!0000beef", "to": "^all", "ch": 0, "dm": False,
                       "text": "gate is closed"})
    await e.send_text(None, "👍", 0, reply_to=1001, emoji=True)
    answer(e, sends(e)[-1], ok=False, reason="MAX_RETRANSMIT")
    await asyncio.sleep(0.3)
    check("not sent again: a reaction", len(sends(e)), 1)

    # Cancel
    e = engine()
    await e.send_text(None, "cancel me", 0)
    answer(e, sends(e)[-1], ok=False, reason="MAX_RETRANSMIT")
    rec = mine(e)[-1]
    await e.cancel(None, rec["t"])
    await asyncio.sleep(0.3)
    check("Cancel: not sent again", len(sends(e)), 1)
    check("  failed, saying it was cancelled", (rec["ack"], "cancelled" in rec.get("why", ""), "retry_at" in rec),
          ("failed", True, False))
    n = len(e.station.notices)
    await e.cancel(None, rec["t"])
    check("  Cancel again: nothing to cancel, a warning", e.station.notices[n:] and e.station.notices[-1][0], "warn")

    # The node goes away
    e = engine()
    await e.send_text(None, "going away", 0)
    answer(e, sends(e)[-1], ok=False, reason="MAX_RETRANSMIT")
    e.connected = False
    e._stop_resends("the mesh node went away")
    await asyncio.sleep(0.3)
    rec = mine(e)[-1]
    check("node gone: not sent again, nothing pending", (len(sends(e)), "retry_at" in rec), (1, False))

    # A long text, in parts
    e = engine()
    await e.send_text(None, words, 0)
    recs = mine(e)
    check("long text: 3 records, the first sending, the rest queued",
          [r["ack"] for r in recs], ["sending", "queued", "queued"])
    check("  only the first goes to the bridge", [c["text"][:6] for c in sends(e)], ["[1/3] "])
    check("  each knows its part", [r["part"] for r in recs], [[1, 3], [2, 3], [3, 3]])
    answer(e, sends(e)[-1])
    check("  part 2 after part 1 is ACKed", await until(lambda: len(sends(e)) == 2), True)
    check("    in order", sends(e)[-1]["text"][:6], "[2/3] ")
    answer(e, sends(e)[-1])
    await until(lambda: len(sends(e)) == 3)
    answer(e, sends(e)[-1])
    check("  all three delivered", [r["ack"] for r in recs], ["delivered"] * 3)
    check("  nothing left in flight", e._parts, {})

    # A part that finally fails drops the rest
    e = engine()
    await e.send_text(None, words, 0)
    answer(e, sends(e)[-1], ok=False, reason="NO_CHANNEL")
    await asyncio.sleep(0.2)
    recs = mine(e)
    check("a part fails for good: the rest are dropped", [r["ack"] for r in recs], ["failed"] * 3)
    check("  saying why", recs[1].get("why"), "an earlier part was not delivered")
    check("  nothing more to the bridge", len(sends(e)), 1)

    # A part sent again holds the rest
    e = engine()
    await e.send_text(None, words, 0)
    answer(e, sends(e)[-1], ok=False, reason="MAX_RETRANSMIT")
    await until(lambda: len(sends(e)) == 2)
    check("a part NAKed: it is sent again before the next part", sends(e)[-1]["text"][:6], "[1/3] ")
    answer(e, sends(e)[-1])
    check("  then part 2", await until(lambda: len(sends(e)) == 3 and sends(e)[-1]["text"][:6] == "[2/3] "), True)

    # No ACK at all: unknown, and the next part goes anyway
    mesh.ACK_WAIT_S = 0.2
    e = engine()
    await e.send_text(None, words, 0)
    answer(e, sends(e)[-1], ok=None)
    check("no ACK: unknown after the wait, and part 2 goes", await until(lambda: len(sends(e)) == 2), True)
    check("  part 1 unknown", mine(e)[0]["ack"], "unknown")
    mesh.ACK_WAIT_S = 30.0

    # Cancel a part not sent yet
    e = engine()
    await e.send_text(None, words, 0)
    recs = mine(e)
    await e.cancel(None, recs[2]["t"])
    answer(e, sends(e)[-1])
    await asyncio.sleep(0.2)
    check("Cancel on a queued part: the parts not sent yet are dropped",
          [r["ack"] for r in recs], ["delivered", "failed", "failed"])
    check("  only part 1 went out", len(sends(e)), 1)

    # Refused before the bridge
    e = engine()
    e.messages.append({"k": "msg", "id": 1001, "t": 1.0, "from": "!0000beef", "to": "^all", "ch": 0, "dm": False,
                       "text": "gate is closed"})
    for name, kw in (("a reply over 200 bytes", {"text": "x" * 201, "reply_to": 1001}),
                     ("over 9 parts", {"text": "z" * (194 * 9 + 1)})):
        n = len(e.station.notices)
        await e.send_text(None, kw.pop("text"), 0, **kw)
        check(f"refused: {name}", (len(sends(e)), e.station.notices[n:] and e.station.notices[-1][0]), (0, "warn"))

    # A restart: a part never sent shows failed
    tmp = tempfile.mkdtemp(prefix="meshresend-")
    log = os.path.join(tmp, "mesh.jsonl")
    e = engine(log=log)
    await e.send_text(None, words, 0)
    e2 = engine(log=log)
    e2._load()
    states = [(m["ack"], m.get("why")) for m in e2.messages]
    check("restart: the part in flight unknown, the queued ones failed",
          states, [("unknown", None), ("failed", "not sent: the server restarted"),
                   ("failed", "not sent: the server restarted")])


asyncio.run(main())

# The add-on's switch: a bool, on by default ("false" as text must not leave it on)
from addons.mesh import MeshAddon  # noqa: E402
check("add-on: auto_resend on by default", MeshAddon(None, {"port": "/dev/null"}).args.mesh_auto_resend, True)
check("add-on: auto_resend false turns it off",
      MeshAddon(None, {"port": "/dev/null", "auto_resend": False}).args.mesh_auto_resend, False)
try:
    MeshAddon(None, {"port": "/dev/null", "auto_resend": "false"})
    check('add-on refuses auto_resend "false" (text)', "accepted", "ValueError")
except ValueError:
    check('add-on refuses auto_resend "false" (text)', "ValueError", "ValueError")

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
