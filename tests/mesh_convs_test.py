"""mesh_convs: the Mesh tab's conversation state and its trash, without a server.

Checks:
  - conv_of: a channel message, our DM (its "to"), a DM to us (its "from")
  - pick: a whole conversation, or messages by t, with the reactions to them;
    ours still being sent (sending, sent, queued, waiting to be sent again)
    is kept, not deleted
  - ConvStore: a new file is read up to its "since"; archive / mute / pin and
    their undo; read only moves forward; unread makes the newest one unread
    again; the same action twice changes nothing; a bad conversation or
    action is refused; an incoming message brings an archived conversation
    back unless muted, a reaction or our own does not; state survives a
    reload, mode 600; a DM forgotten whole drops its state, a channel keeps it
  - Trash: put hands back a ref, take returns the messages once; the newest
    TRASH_KEEP batches are kept; it survives a reload, mode 600

    python3 tests/mesh_convs_test.py
"""
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mesh_convs as mc  # noqa: E402

FAILS = 0


def check(name, got, want):
    global FAILS
    ok = got == want
    FAILS += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


ME, RDG, TRK = "!b2a6fd78", "!0000cafe", "!0000dead"
chan = {"k": "msg", "t": 10.0, "id": 1, "from": RDG, "ch": 0, "dm": False, "text": "hi all"}
react = {"k": "msg", "t": 11.0, "id": 2, "from": TRK, "ch": 0, "dm": False, "text": "👍", "emoji": True, "reply_id": 1}
dm_in = {"k": "msg", "t": 12.0, "id": 3, "from": RDG, "to": ME, "ch": 0, "dm": True, "text": "psst"}
dm_out = {"k": "msg", "t": 13.0, "id": 4, "from": ME, "to": RDG, "ch": 0, "dm": True, "text": "yes", "mine": True, "ack": "delivered"}
sending = {"k": "msg", "t": 14.0, "id": None, "from": ME, "to": RDG, "ch": 0, "dm": True, "text": "wait", "mine": True, "ack": "sending"}
retry = {"k": "msg", "t": 15.0, "id": 5, "from": ME, "ch": 1, "dm": False, "text": "again", "mine": True, "ack": "failed", "retry_at": 99.0}
msgs = [chan, react, dm_in, dm_out, sending, retry]

check("conv_of: a channel message", mc.conv_of(chan), "c0")
check("conv_of: a DM to us", mc.conv_of(dm_in), f"d{RDG}")
check("conv_of: our DM", mc.conv_of(dm_out), f"d{RDG}")

gone, kept = mc.pick(msgs, conv=f"d{RDG}")
check("pick a DM: the settled ones go", [m["t"] for m in gone], [12.0, 13.0])
check("  the one still sending stays", [m["t"] for m in kept], [14.0])
gone, kept = mc.pick(msgs, ts=[10.0])
check("pick a message: its reactions go with it", sorted(m["t"] for m in gone), [10.0, 11.0])
gone, kept = mc.pick(msgs, conv="c1")
check("pick: waiting to be sent again is kept", ([m["t"] for m in gone], [m["t"] for m in kept]), ([], [15.0]))
gone, kept = mc.pick(msgs, conv="c1", resends={15.0})
check("pick: a resend timer keeps it too", [m["t"] for m in kept], [15.0])

with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "mesh-convs.json")
    s = mc.ConvStore(path, now=100.0)
    check("a new file: read up to since", s.read_t("c0"), 100.0)
    check("  written at once, mode 600", oct(stat.S_IMODE(os.stat(path).st_mode)), "0o600")
    check("archive changes it", s.act("c0", "archive"), True)
    check("  archive again changes nothing", s.act("c0", "archive"), False)
    s.act(f"d{RDG}", "pin")
    s.act(f"d{RDG}", "mute")
    check("pin and mute kept", s.convs[f"d{RDG}"], {"pinned": True, "muted": True})
    s.act(f"d{RDG}", "unmute")
    check("unmute drops the flag", s.convs[f"d{RDG}"], {"pinned": True})
    check("read moves forward", (s.act("c0", "read", 150.0), s.read_t("c0")), (True, 150.0))
    check("  never back", (s.act("c0", "read", 120.0), s.read_t("c0")), (False, 150.0))
    s.act("c0", "unread", 140.0)
    check("unread: the newest is unread again", s.read_t("c0") < 140.0, True)
    for bad, why in ((("x1", "archive"), "not a conversation"), (("c0", "explode"), "unknown"), (("c0", "read"), "need the message time")):
        try:
            s.act(*bad)
            check(f"refused: {bad}", "accepted", why)
        except ValueError as e:
            check(f"refused: {bad}", why in str(e), True)
    check("an incoming message brings c0 back", (s.heard(chan), s.convs["c0"].get("archived")), (True, None))
    s.act("c0", "archive")
    check("  a reaction does not", (s.heard(react), s.convs["c0"].get("archived")), (False, True))
    check("  our own does not", s.heard(dict(chan, mine=True)), False)
    s.act("c0", "mute")
    check("  muted: it stays archived", (s.heard(chan), s.convs["c0"].get("archived")), (False, True))
    r = mc.ConvStore(path, now=999.0)
    check("reload: state and since kept", (r.convs["c0"].get("muted"), r.since, r.read_t("c3")), (True, 100.0, 100.0))
    r.forget(f"d{RDG}")
    r.forget("c0")
    check("forget: a DM's state goes, a channel's stays", (f"d{RDG}" in r.convs, "c0" in r.convs), (False, True))

    tpath = os.path.join(d, "mesh-trash.jsonl")
    t = mc.Trash(tpath)
    ref = t.put([chan], "message", now=1.0)
    check("trash: mode 600", oct(stat.S_IMODE(os.stat(tpath).st_mode)), "0o600")
    check("  survives a reload", [m["t"] for m in mc.Trash(tpath).take(ref)], [10.0])
    t = mc.Trash(tpath)
    check("  take after it was taken: gone", t.take(ref), None)
    refs = [t.put([dict(chan, t=float(i))], "message", now=10.0 + i) for i in range(mc.TRASH_KEEP + 3)]
    check("  only the newest TRASH_KEEP kept", (len(t.batches), t.take(refs[0]), t.take(refs[-1]) is not None),
          (mc.TRASH_KEEP, None, True))

sys.exit(1 if FAILS else 0)
