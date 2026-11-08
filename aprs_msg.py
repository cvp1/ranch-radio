"""APRS messaging for the packet hub: compose, send with retries, match acks,
and (mode "full" only) ack what is addressed to us.

The October 2026 M3 design (plans/2026-10-19-aprs-build-plan.md) as amended
by the 2026-11-07 plan (APRS two-way messaging, sections 3.3-3.4):

Modes (host file packet.aprs.messaging, --aprs-messaging):
  off   nothing (the default).
  send  compose, send, retry, and match acks, rejs and reply-acks to OUR
        messages. The hub never acknowledges inbound traffic in any form: no
        standalone ack, and no reply-ack id on our messages ({MM} only, never
        {MM}AA). For a radio whose firmware may ack by itself (U1).
  full  also acks every message to an owned call heard live on RF after the
        start cooldown, and puts a free ack ({MM}AA) on a message to a peer
        whose last message advertised reply-acks with a 2-character id.
        Only with firmware_acks false: otherwise the hub runs as "send" and
        says why. Status shows the effective mode.

Owned identity: exact call-SSIDs (owned_calls); every frame goes out from one
of them, TOCALL APZRR0, the host file's path (WIDE1-1,WIDE2-1).

Retry slots: t0 (the first actual transmission) + 0, 30, 90, 210, 450, 930 s,
deadline t0 + 1,410 s. One transmission per slot at most; a slot blocked by a
pause or a refusal may still go later within that slot, else it is skipped
(logged, not a try). Passed slots are never caught up. The deadline never
moves. One unacked message per addressee; the rest wait queued.

Pauses, rechecked immediately before every transmit: an off-channel hold
(the UV-PRO add-on's Winlink QSY), transmit off or stopped by the operator,
the radio lent or not linked, any KISS client, an AGWPE client with monitor
or raw on, a packet link up, an inbound window open, and the start cooldown.

Cancel (Engine.cancel, the page's Cancel): a queued or pending message ends
"cancelled" at once and is never sent again; the next one queued to that
station starts.

Crash safety: before a message's frame is handed to the radio its row reads
"sending" with the slot already used (next_try past it); after, "pending" with
the try counted. A restart that finds "sending" counts that slot as tried, so
no slot is ever sent twice. Messages resume at their next slot after the
cooldown; one past its deadline is failed (restart).

Nothing that arrives over APRS-IS (on_is) is ever acked or matched as an ack;
it feeds only the firmware-ack evidence readout and the probe.
"""
import asyncio
import collections
import logging
import os
import time

import aprs
import ax25
from ax25 import Frame, FrameError
from ax25_link import TxRefused

log = logging.getLogger("aprs_msg")

MODES = ("off", "send", "full")
TOCALL = "APZRR0"
OFFSETS = (0, 30, 90, 210, 450, 930)    # retry slots, seconds after the first transmission
DEADLINE_S = 1410                       # after the last retry, a final wait for the ack
ACK_SLOT_S = 30                         # one ack per (owned, sender, msgno) this long
ACK_DUE_S = 10                          # an ack not sent this long after its message is dropped
PACE_S = 2.0                            # UI frames this far apart, acks first
COOLDOWN_S = 60
FW_ACK_WINDOW_S = 120                   # the firmware-ack evidence window (U1)
TICK_S = 0.5
MSGNO_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
ACTIVE = ("queued", "pending", "sending")


class MsgError(ValueError):
    pass


def effective_mode(mode, firmware_acks):
    """(effective mode, why it differs or None)."""
    if mode not in MODES:
        return "off", f"unknown messaging mode {mode!r}"
    if mode == "full" and firmware_acks is not False:
        return "send", ("full needs firmware_acks: false (the radio's firmware may ack by itself; "
                        f"firmware_acks is {firmware_acks!r}): running as send, no acks sent")
    return mode, None


def _norm(call):
    return aprs.norm_call(call)


class Engine:
    def __init__(self, hub, mode="off", owned=(), firmware_acks=None, path=("WIDE1-1", "WIDE2-1"),
                 cooldown_s=COOLDOWN_S, clock=time.time):
        self.hub = hub
        self.clock = clock
        self.mode = mode
        self.firmware_acks = firmware_acks
        self.effective, self.why = effective_mode(mode, firmware_acks)
        self.owned = [_norm(c) for c in owned]
        self.owned_set = set(self.owned)
        self.path = list(path)
        self.cooldown_s = cooldown_s
        self.started = clock()
        self.out = {}                       # id -> row, the active outbound messages
        self.acks_due = collections.deque() # (t_heard, owned, peer, msgno)
        self.acked = {}                     # (owned, peer, msgno) -> when our ack's transmit returned
        self.peer_ra = {}                   # peer -> (their 2-char msgno, t): a free ack to give (full)
        self.fw = collections.deque(maxlen=20)   # firmware-ack evidence (send mode, U1)
        self.our_acks = collections.deque(maxlen=200)
        self.counts = collections.Counter()
        self.listeners = []                 # objects with on_tx / on_state / on_rf / on_is (the probe)
        self.hook = None                    # tests: an async fn(name, item) at decision points
        self.last_pause = None
        self._lock = asyncio.Lock()
        self._task = None
        self._pending_tasks = set()

    async def settle(self):
        """Wait for the store writes that heard acks started (tests, shutdown)."""
        while self._pending_tasks:
            await asyncio.gather(*list(self._pending_tasks), return_exceptions=True)

    # ------------------------------------------------------------ life

    async def start(self, run=True):
        """Load the active messages; a 'sending' row's slot counts as tried."""
        store = self.hub.aprs
        await store.call("set_owned", self.owned)
        now = self.clock()
        for row in await store.call("msg_active"):
            if row["state"] == "sending":
                row["state"] = "pending"
                row["tries"] = (row["tries"] or 0) + 1
                log.warning("message %s to %s: the hub stopped while sending slot %d; counted as tried",
                            row["msgno"], row["dst"], row["tries"])
                await self._save(row, state="pending", tries=row["tries"])
            if row["first_sent"] is not None and now >= (row["deadline"] or 0):
                await self._finish(row, "failed", why="restart: past its deadline")
                continue
            self.out[row["id"]] = row
        if run:
            self._task = asyncio.create_task(self._loop())
        return self

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:     # noqa: BLE001 - the loop must survive one bad pass
                self.counts["errors"] += 1
                log.exception("messaging pass failed")
            await asyncio.sleep(TICK_S)

    # ------------------------------------------------------------ pauses

    def pause_why(self):
        """Why no APRS message or ack may go now, or None."""
        h = self.hub
        if self.effective == "off":
            return f"messaging is off{f' ({self.why})' if self.why else ''}"
        if h.aprs is None:
            return "no APRS store (--no-aprs)"
        if h.state.holds:
            x = h.state.holds[0]
            return (f"off-channel hold by {x['by']}" + (f": {x['why']}" if x.get("why") else "")
                    + (f" (back: {x['back']})" if x.get("back") else ""))
        if not h.gate.enabled:
            return "transmit is " + ("stopped by the operator" if h.state.inhibit else "off")
        lent = h.lent_why()
        if lent:
            return lent
        if not h.radio.up:
            return "the radio is not connected"
        if h.kiss_clients:
            return f"a KISS client is connected ({next(iter(h.kiss_clients)).name}); it may be another messaging owner"
        mon = [c for c in h.agw_clients if c.mon or c.raw]
        if mon:
            c = mon[0]
            return f"AGWPE client {c.name} is monitoring ({'raw' if c.raw else 'monitor'} on); it may be another messaging owner"
        links = [l for l, _ in h.links.values() if l.state != "disconnected"]
        if links:
            return f"a packet link is up ({links[0].key[1]})"
        if h.inbound and self.clock() < h.inbound["until"]:
            return f"an inbound window is open for {', '.join(h.inbound['calls'])}"
        until = self.started + self.cooldown_s
        if self.clock() < until:
            return f"start cooldown ({int(until - self.clock()) + 1} s left)"
        return None

    # ------------------------------------------------------------ compose

    async def compose(self, to, text):
        """A message from a page. Returns the row; raises MsgError with why not."""
        if self.effective == "off":
            raise MsgError(f"messaging is off{f' ({self.why})' if self.why else ''}")
        if not isinstance(to, str) or not isinstance(text, str):
            raise MsgError("to and text are strings")
        try:
            peer = ax25.call_str(*ax25.parse_call(to))
        except FrameError as e:
            raise MsgError(f"to: {e}") from None
        if peer in self.owned_set:
            raise MsgError("that is one of this station's own calls")
        try:
            aprs.message_info(peer, text, "AA")
        except ValueError as e:
            raise MsgError(f"text: {e}") from None
        if not text.strip():
            raise MsgError("text: empty")
        why = self.pause_why()
        if why:
            raise MsgError(f"not now: {why}")
        async with self._lock:
            msgno = await self._msgno()
            busy = any(r["dst"] == peer for r in self.out.values())
            row = {"t": self.clock(), "port": "rf", "src": self.owned[0], "dst": peer, "text": text,
                   "msgno": msgno, "reply_ack": None, "peer_replyack": 0,
                   "state": "queued" if busy else "pending", "first_sent": None, "tries": 0,
                   "next_try": None, "deadline": None, "acked_t": None, "why": None}
            row["id"] = await self.hub.aprs.call("msg_insert", {k: v for k, v in row.items()})
            self.out[row["id"]] = row
        log.info("message %s to %s composed (%s): %s", msgno, peer, row["state"], text)
        self._emit("on_state", row)
        return row

    async def cancel(self, mid, why="cancelled from the page"):
        """Stop sending one of our messages: no further tries. Under the pass's
        lock, so it never lands between a frame's "sending" record and its
        transmit. A late ack for it matches nothing; its msgno stays reserved
        to its deadline. The next message queued to the same station starts."""
        async with self._lock:
            row = self.out.get(mid)
            if row is None:
                stored = await self.hub.aprs.call("msg_get", mid)
                if stored is None or stored["dir"] != "out":
                    raise MsgError("no such outbound message")
                raise MsgError(f"it is already {stored['state']}: nothing to cancel")
            if row["state"] not in ACTIVE:    # an ack matched a moment ago; its finish is on the way
                raise MsgError(f"it is already {row['state']}: nothing to cancel")
            await self._finish(row, "cancelled", why=why)
        log.warning("message %s to %s cancelled", row["msgno"], row["dst"])
        return row

    async def _msgno(self):
        busy = await self.hub.aprs.call("msgnos_busy", self.clock())
        busy |= {r["msgno"] for r in self.out.values()}
        start = int(await self.hub.aprs.call("meta", "next_msgno", 0) or 0)
        n = len(MSGNO_CHARS) ** 2
        for i in range(n):
            k = (start + i) % n
            m = MSGNO_CHARS[k // len(MSGNO_CHARS)] + MSGNO_CHARS[k % len(MSGNO_CHARS)]
            if m not in busy:
                await self.hub.aprs.call("set_meta", "next_msgno", (k + 1) % n)
                return m
        raise MsgError("every message number is in use")

    # ------------------------------------------------------------ the pass

    async def tick(self):
        """One decision pass: deadlines, then at most one frame (an ack first)."""
        async with self._lock:
            await self._tick()

    async def _tick(self):
        now = self.clock()
        while self.acks_due and now - self.acks_due[0][0] > ACK_DUE_S:
            self.acks_due.popleft()
            self.counts["acks_dropped"] += 1
        for row in list(self.out.values()):
            if row["state"] in ("pending", "sending") and row["first_sent"] is not None and now >= row["deadline"]:
                await self._finish(row, "failed", why=f"no ack after {row['tries']} tr{'y' if row['tries'] == 1 else 'ies'}")
            elif row["first_sent"] is None and row["state"] == "pending" and now >= row["t"] + DEADLINE_S:
                await self._finish(row, "failed", why=f"never sent: {self.last_pause or 'paused'}")
        why = self.pause_why()
        self.last_pause = why
        due = self._due(now)
        if why:
            # An ack heard while another owner may be present (or off channel,
            # or stopped) is dropped, never sent once the pause ends.
            self.counts["acks_dropped"] += len(self.acks_due)
            self.acks_due.clear()
            for row, n in due:
                await self._skip_passed(row, n, now)
            return
        last = self.hub.last_ui_tx
        if last is not None and now - last < PACE_S:
            return
        if self.acks_due and self.effective == "full":
            await self._send_ack(*self.acks_due.popleft()[1:])
            return
        for row, n in due:
            await self._skip_passed(row, n, now)
            await self._send_msg(row, n)
            return

    def _slot(self, row, now):
        """The slot index now falls in (0..5) for a sent message."""
        t0 = row["first_sent"]
        return max(i for i, off in enumerate(OFFSETS) if t0 + off <= now)

    def _due(self, now):
        """[(row, slot)] that may transmit now: the first active message per
        addressee, unsent, or in a slot not yet used."""
        out, seen = [], set()
        for row in sorted(self.out.values(), key=lambda r: r["id"]):
            if row["dst"] in seen:
                continue
            seen.add(row["dst"])
            if row["state"] != "pending":
                continue
            if row["first_sent"] is None:
                out.append((row, 0))
                continue
            if now >= row["deadline"] or row["next_try"] > now:
                continue
            out.append((row, self._slot(row, now)))
        return out

    async def _skip_passed(self, row, n, now):
        """Slots that passed unsent are skipped (logged, never caught up)."""
        if row["first_sent"] is None:
            return
        start = row["first_sent"] + OFFSETS[n]
        if row["next_try"] < start:
            missed = [i for i, off in enumerate(OFFSETS) if row["next_try"] <= row["first_sent"] + off < start]
            if missed:
                self.counts["slots_missed"] += len(missed)
                log.info("message %s to %s: slot%s %s missed (%s), not caught up", row["msgno"], row["dst"],
                         "s" if len(missed) > 1 else "", ", ".join(str(i + 1) for i in missed),
                         self.last_pause or "refused")
            await self._save(row, next_try=start)

    def _frame(self, row):
        ra = None
        if self.effective == "full":
            got = self.peer_ra.get(row["dst"])
            if got and self.clock() - got[1] < DEADLINE_S:
                ra = got[0]
        info = aprs.message_info(row["dst"], row["text"], row["msgno"], reply_ack=ra, capable=True)
        return Frame.ui(row["src"], TOCALL, info.encode("latin-1"), digis=self.path), ra

    async def _send_msg(self, row, n):
        t0 = row["first_sent"] if row["first_sent"] is not None else self.clock()
        nxt = t0 + OFFSETS[n + 1] if n + 1 < len(OFFSETS) else t0 + DEADLINE_S
        before = {k: row[k] for k in ("state", "first_sent", "next_try", "deadline")}
        await self._save(row, state="sending", first_sent=t0, next_try=nxt, deadline=t0 + DEADLINE_S)
        _test_point("aprs-after-sending-record")
        if self.hook:
            await self.hook("before_transmit", row)
        if row["state"] != "sending":
            return                            # acked (or rejected) meanwhile: nothing to send
        why = self.pause_why()
        try:
            if why:
                raise TxRefused(why)
            frame, ra = self._frame(row)
            self.hub.transmit(frame, "aprs:msg")
        except (TxRefused, ValueError, FrameError) as e:
            self.counts["msg_refused"] += 1
            await self._save(row, **before, why=f"slot {n + 1} not sent: {e}")
            log.info("message %s to %s: slot %d not sent: %s", row["msgno"], row["dst"], n + 1, e)
            return
        _test_point("aprs-after-write")
        if ra:
            self.peer_ra.pop(row["dst"], None)
        await self._save(row, state="pending", tries=(row["tries"] or 0) + 1, reply_ack=ra, why=None)
        self.counts["msg_sent"] += 1
        self._emit("on_tx", row, frame)

    async def _send_ack(self, owned, peer, msgno):
        key = (owned, peer, msgno)
        if self.clock() - self.acked.get(key, -1e18) < ACK_SLOT_S:
            return
        frame = Frame.ui(owned, TOCALL, aprs.ack_info(peer, msgno).encode("latin-1"), digis=self.path)
        if self.hook:
            await self.hook("before_transmit", key)
        why = self.pause_why()
        try:
            if why:
                raise TxRefused(why)
            self.hub.transmit(frame, "aprs:ack")
        except TxRefused as e:
            self.counts["acks_refused"] += 1
            log.info("ack %s to %s not sent: %s (the next copy is acked)", msgno, peer, e)
            return
        self.acked[key] = self.clock()           # the slot starts once transmit() returned
        self.our_acks.append((owned, peer, msgno, self.clock()))
        self.counts["acks_sent"] += 1
        if len(self.acked) > 500:
            cut = self.clock() - ACK_SLOT_S
            self.acked = {k: v for k, v in self.acked.items() if v >= cut}

    async def _save(self, row, **changes):
        row.update(changes)
        await self.hub.aprs.call("msg_update", row["id"], changes)

    async def _finish(self, row, state, why=None, t=None):
        await self._save(row, state=state, why=why, acked_t=t)
        self.out.pop(row["id"], None)
        log.info("message %s to %s: %s%s", row["msgno"], row["dst"], state, f" ({why})" if why else "")
        nxt = next((r for r in sorted(self.out.values(), key=lambda r: r["id"])
                    if r["dst"] == row["dst"] and r["state"] == "queued"), None)
        if nxt:
            await self._save(nxt, state="pending")
        self._emit("on_state", row)

    # ------------------------------------------------------------ heard

    def on_rf(self, t, f):
        """A UI frame heard live on RF (never backfill or reload). Sync, from
        the hub's receive path; the store writes it causes run as tasks."""
        if self.effective == "off" or f.kind != "U" or f.utype != ax25.U_UI or f.pid != ax25.PID_NO_L3:
            return
        d = aprs.decode(str(f.src), str(f.dest), [str(x) for x in f.digis], f.info)
        for l in self.listeners:
            if hasattr(l, "on_rf"):
                l.on_rf(t, f, d)
        m = d.get("inner") if d.get("third_party") else d
        if not m or m.get("error") and m.get("type") == "unknown":
            return
        src, typ = m["src"], m["type"]
        to = _norm(m.get("addressee") or "")
        if src in self.owned_set:
            if typ == "ack":
                self._fw_ack_seen(t, src, to, m["msgno"], "RF")
            return
        if to not in self.owned_set:
            return
        if typ == "message" and m.get("msgno"):
            if m.get("reply_ack"):
                self._match(t, src, to, m["reply_ack"], "acked", "free ack")
            if m.get("replyack_capable") and len(m["msgno"]) == 2:
                self.peer_ra[src] = (m["msgno"], t)
            if self.effective == "full":
                if t >= self.started + self.cooldown_s:
                    self.acks_due.append((t, to, src, m["msgno"]))
            self.fw.append({"t": t, "from": src, "to": to, "msgno": m["msgno"], "ack": None})
        elif typ in ("ack", "rej"):
            self._match(t, src, to, m["msgno"], "acked" if typ == "ack" else "rejected", typ)

    def on_is(self, t, d):
        """A packet from the APRS-IS observer: evidence only, never acked or matched."""
        for l in self.listeners:
            if hasattr(l, "on_is"):
                l.on_is(t, d)
        m = d.get("inner") if d.get("third_party") else d
        if m and m.get("type") == "ack" and m["src"] in self.owned_set:
            self._fw_ack_seen(t, m["src"], _norm(m.get("addressee") or ""), m["msgno"], "APRS-IS")

    def _match(self, t, peer, owned, msgno, state, how):
        """An ack, rej or free ack completes our message only from the exact
        peer, to the owned call that sent it, with its exact id, once sent."""
        row = next((r for r in self.out.values() if r["dst"] == peer and r["src"] == owned
                    and r["msgno"] == msgno and r["state"] in ("pending", "sending")
                    and ((r["tries"] or 0) >= 1 or r["state"] == "sending")), None)
        if row is None:
            self.counts["acks_unmatched"] += 1
            return
        log.info("message %s to %s %s (%s)", msgno, peer, state, how)
        row["state"] = state                  # at once: nothing more is sent for it
        self.counts[state] += 1
        task = asyncio.get_running_loop().create_task(self._finish_matched(row, state, t))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _finish_matched(self, row, state, t):
        async with self._lock:
            await self._finish(row, state, t=t)

    def _fw_ack_seen(self, t, owned, peer, msgno, where):
        for e in self.fw:
            if e["ack"] is None and e["to"] == owned and e["from"] == peer and e["msgno"] == msgno \
                    and 0 <= t - e["t"] <= FW_ACK_WINDOW_S:
                ours = any(a[:3] == (owned, peer, msgno) and abs(a[3] - t) < FW_ACK_WINDOW_S for a in self.our_acks)
                e["ack"] = {"t": t, "via": where, "ours": ours}

    # ------------------------------------------------------------ status

    def fw_readout(self):
        now = self.clock()
        out = []
        for e in list(self.fw)[::-1]:
            if e["ack"]:
                verdict = "ack seen, ours" if e["ack"]["ours"] else "ack seen, not ours"
            elif now - e["t"] >= FW_ACK_WINDOW_S:
                verdict = "none seen"
            else:
                verdict = "waiting"
            out.append({**e, "verdict": verdict})
        return out

    def describe(self, row):
        r = {k: row.get(k) for k in ("id", "t", "src", "dst", "text", "msgno", "state", "tries", "first_sent",
                                     "deadline", "acked_t", "why", "reply_ack")}
        if row.get("state") == "pending" and row.get("first_sent") is not None and row.get("next_try"):
            used = sum(1 for off in OFFSETS if row["first_sent"] + off < row["next_try"])
            r["slot"] = f"{used}/{len(OFFSETS)}"
        return r

    def status(self):
        return {"mode": self.mode, "effective": self.effective, "why": self.why, "owned": self.owned,
                "firmware_acks": self.firmware_acks, "path": self.path, "tocall": TOCALL,
                "pause": self.pause_why(), "cooldown_until": self.started + self.cooldown_s,
                "active": [self.describe(r) for r in sorted(self.out.values(), key=lambda r: r["id"])],
                "firmware_ack_evidence": self.fw_readout(), **dict(self.counts)}

    def _emit(self, what, *args):
        for l in self.listeners:
            fn = getattr(l, what, None)
            if fn:
                try:
                    fn(*args)
                except Exception:      # noqa: BLE001
                    log.exception("messaging listener failed")


def _test_point(name):
    """tnc_hub.test_point's crash points, for the messaging tests."""
    if os.environ.get("RANCH_RADIO_TEST_CRASH_AT") == name:
        log.warning("test crash point %s", name)
        logging.shutdown()
        os._exit(75)
