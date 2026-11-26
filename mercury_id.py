"""The station's own CW ID on Mercury HF's lane (plan winlink-mercury M0).

Mercury sends no station ID, so this radio server sends one (47 CFR 97.119(a):
at the end of each communication and at least every 10 minutes during one),
as the lane's PTT owner, only inside an armed Mercury session, through the
same lane gate and can_key() as Mercury's own keys: unarmed, STOP TX, --no-tx,
another owner or an ATU tune refuse it the same way (RPRT -11, no PTT).

Mercury plays its frame's audio even when its key is refused
(tcp_interfaces.c:2496-2508). So the ID is never keyed in a refused slot,
where that audio would go on air mixed into it. It is keyed only when
Mercury's own key has just ended (its T 0, or the 33 s keepalive cut) and no
refused frame may still be playing: a refused Mercury T 1 opens an "audio may
be playing" window that closes on that client's T 0 or after
WINLINK_MERCURY_KEEPALIVE_S. A Mercury T 1 during the ID cuts the ID at once:
the ID is owed, never retried, the fence stays latched and the session ends.

  - End of the session (id_tail): fence (Mercury's T 1 refused) -> wait for
    its key and any audio window (bounded) -> ID -> the caller disarms.
  - 10 minutes (hard gate): GATE_S after Mercury's first key since the last
    ID that went on air, fence; the ID goes as soon as Mercury is quiet.
    Fence at 9:00 + Mercury's longest key 33 s + the ID ~5 s: inside 10:00.
  - The first trial's bound: BOUND_S after arming, fence and ask the station
    to abort Pat (its winlink.abort, through the lane socket); Pat returning
    ends the session the usual way (the end-of-session ID). If the station
    never ends it, the backstop does: ID -> disarm.
  - STOP TX, disarm, the arming page gone, audio failing, a limit cutting the
    key: the ID stops at once and stays owed. Nothing here ever keys later
    because of it: every task belongs to one arm (token) and dies at disarm.

Only an ID played to the end and released with its own T 0 counts as on air
(ledger source "station", IdLedger.id_done).
"""
import asyncio
import json
import logging
import time

import cw_id
import rigctl_server

log = logging.getLogger("station_id")

GATE_S = 540.0               # 9:00: the ID goes before 10:00 even behind Mercury's longest key
BOUND_S = 510.0              # 8:30: the first trial's cap on a Mercury session (plan 2.2 b)
WINDOW_S = rigctl_server.WINLINK_MERCURY_KEEPALIVE_S   # a refused frame may play this long
WAIT_MAX_S = 2 * WINDOW_S + 1.0   # Mercury's key, then at most two refused frames; then give up
RELEASE_GUARD_S = 0.2        # Mercury unkeyed this long before the ID keys: its tail audio is out
PTT_LEAD_S = 0.15            # keyed before the tone: the radio's TX path settles
CHUNK = 640                  # bytes: 20 ms of audio.py's 16 kHz s16le
CHUNK_S = 0.02
AHEAD = 3                    # chunks written ahead of real time (CoreAudio keeps <= 300 ms)
TAIL_S = 0.3                 # after the last chunk: the card's buffer drains before T 0
ID_KEEPALIVE_S = 1.0         # the keyer refreshes every chunk; a stalled keyer is cut by safety_loop
BOUND_BACKSTOP_S = 20.0      # after the bound: the station's abort ends the session within this
TICK_S = 0.25


class MercuryIdent:
    def __init__(self, station, call, wpm=cw_id.DEFAULT_WPM, gate_s=GATE_S, bound_s=BOUND_S):
        self.st = station
        self.call = cw_id.check_call(call)
        self.pcm = cw_id.render(self.call, wpm)
        self.id_s = cw_id.duration_s(self.call, wpm)
        self.gate_s, self.bound_s = float(gate_s), float(bound_s)
        self.sess = rigctl_server.RigctlSession(station, "station-id", lane="winlink", modem="mercury")
        self.owner = self.sess.owner
        self.owner.keepalive_s = ID_KEEPALIVE_S
        self.owner.lost_msg = "the station's CW ID stalled"
        self.owner.station_id = True
        self.token = 0              # one per arm: a task of an earlier arm never keys
        self.armed_at = None        # monotonic
        self.unid_since = None      # monotonic: Mercury's first key since the last on-air ID
        self.fence = None           # why Mercury may not key now (None: it may)
        self.latched = False        # an ID was cut or failed: Mercury stays fenced until disarm
        self.window_until = 0.0     # monotonic: a refused frame's audio may play until then
        self.playing = False
        self.collided = False
        self._allow = False         # the keyer's own T 1 is due now
        self._task = None
        self._watch = None
        self._backstop = None
        self._bound_hit = False
        self.results = []           # (why, on_air, reason): every ID this server tried, newest last

    # --- the server's hooks ---

    def armed(self):
        """A Mercury session was armed (Station._winlink_arm_prepare)."""
        self._stop_tasks()
        self.token += 1
        self.armed_at = time.monotonic()
        self.unid_since = None
        self.fence, self.latched, self._bound_hit = None, False, False
        self.window_until = 0.0
        self._watch = asyncio.ensure_future(self._watch_loop(self.token))

    def disarmed(self):
        """The session is being disarmed (Station.winlink_disarm, any reason)."""
        self.token += 1
        self._stop_tasks()
        self.armed_at = None
        self.fence, self.latched = None, False

    def _stop_tasks(self):
        for t in (self._watch, self._backstop, self._task):
            if t is not None and not t.done() and t is not asyncio.current_task():
                t.cancel()
        self._watch = self._backstop = None

    def gate(self, owner):
        """Why `owner` may not key on Mercury's lane now (None: it may). The
        lane gate has already checked STOP TX, armed and the backend."""
        if owner is self.owner:
            return None if self._allow else "the station ID is not being sent now"
        if self.fence is None and self._gate_due():
            self._trip_gate()
        if self.fence:
            return f"station ID first ({self.fence})"
        return None

    def lane_event(self, owner, ev):
        """Mercury's lane: "keyed" (an accepted T 1), "refused", "idle" (a T 0
        from a client not keying)."""
        if owner is self.owner:
            return
        now = time.monotonic()
        if ev == "keyed":
            if self.unid_since is None:
                self.unid_since = now
        elif ev == "refused":
            self.window_until = now + WINDOW_S
            if self.playing:
                self.collided = True
                log.warning("station ID: Mercury asked to key during the ID: its audio may be "
                            "playing into it; cutting the ID")
        elif ev == "idle":
            self.window_until = 0.0     # its refused frame is over

    # --- views ---

    def mercury_keyed(self):
        o = self.st.ptt_owner
        return o is not None and o is not self.owner

    def busy(self):
        return self._task is not None and not self._task.done()

    def _gate_due(self):
        return self.unid_since is not None and time.monotonic() - self.unid_since >= self.gate_s

    # --- triggers ---

    def _trip_gate(self):
        self.fence = "the 10-minute station ID"
        log.warning("station ID: %g s since Mercury's first unidentified key; Mercury may not key "
                    "until the station ID is out", self.gate_s)
        self._start("10-minute ID", gate=True)

    def _start(self, why, gate=False):
        if not self.busy():
            self._task = asyncio.ensure_future(self._run(why, gate, self.token))
        return self._task

    async def _watch_loop(self, tok):
        while tok == self.token:
            if self.fence is None and self._gate_due():
                self._trip_gate()
            if not self._bound_hit and self.armed_at is not None and \
                    time.monotonic() - self.armed_at >= self.bound_s:
                self._bound(tok)
            await asyncio.sleep(TICK_S)

    def _bound(self, tok):
        self._bound_hit = True
        b = round(self.bound_s)
        why = f"the {b // 60}:{b % 60:02d} Mercury session bound"
        self.fence = why
        log.warning("station ID: %s: ending the session (Pat abort, then the station ID)", why)
        ws = self.st.winlink_owner
        if ws is not None:
            # The station's RemoteLane: its winlink.abort ends Pat; Pat
            # returning sends winlink_end, which runs the end-of-session ID.
            asyncio.ensure_future(self.st._send(ws, json.dumps({"t": "winlink", "end_request": why})))
        self._backstop = asyncio.ensure_future(self._bound_backstop(tok, why))

    async def _bound_backstop(self, tok, why):
        await asyncio.sleep(BOUND_BACKSTOP_S)
        if tok != self.token or not self.st.winlink_armed:
            return
        log.warning("station ID: the session did not end %g s after %s; ending it here",
                    BOUND_BACKSTOP_S, why)
        await self.end_of_session()
        if tok == self.token:
            await self.st.winlink_disarm(why)

    async def end_of_session(self, closing=lambda: False, notify=None):
        """id_tail for Mercury: fence, then the end-of-session ID if Mercury
        transmitted since the last one; returns when it is out, cut or given
        up (bounded). The caller disarms."""
        tok = self.token
        for _ in range(2):           # a gate ID running now, then (if Mercury keyed after it) ours
            if tok != self.token or not self.st.winlink_armed or closing():
                return
            self.fence = self.fence or "the end of the session"
            if not self.busy() and (self.unid_since is None or self.latched):
                return
            if notify:
                notify({"id": dict(self.st.id_ledger.snapshot(), waiting=True)})
            task = self._start("end of session")
            await asyncio.wait({task})

    # --- the ID ---

    async def _run(self, why, gate, tok):
        ok = await self._send(why, tok)
        if tok != self.token:
            return
        if ok:
            if gate and self.fence == "the 10-minute station ID":
                self.fence = None
            return
        # Any ID that did not go out whole latches the fence: no retry, in
        # this path or the end-of-session one (2026-11-25, mercury_id_test:
        # an end-of-session ID cut by a collision was sent again).
        self.latched = True
        if gate:
            # Cut or failed mid-session: the ID is owed, Mercury stays fenced,
            # and the session ends: it must not keep transmitting unidentified.
            self.latched = True
            await self.st.winlink_disarm("the station ID could not be sent; an ID is owed")

    def _why_stop(self, tok):
        st = self.st
        if st.tx_stopped:
            return "STOP TX"
        if tok != self.token or not st.winlink_armed:
            return "the session was disarmed"
        if self.collided:
            return "Mercury asked to key during it"
        if st.ptt_owner is not self.owner:
            return "its key was released (a limit, the radio or a disarm)"
        return None

    async def _send(self, why, tok):
        """One ID attempt. True only if played whole and released with its own T 0."""
        st = self.st
        end = time.monotonic() + WAIT_MAX_S
        quiet = None                 # monotonic since Mercury holds no key and no refused frame may play
        while True:
            now = time.monotonic()
            if tok != self.token or not st.winlink_armed:
                return False
            if self.mercury_keyed() or now < self.window_until:
                quiet = None
            elif quiet is None:
                quiet = now
            if quiet is not None and now - quiet >= RELEASE_GUARD_S:
                break
            if now > end:
                log.warning("station ID (%s) not sent: Mercury did not go quiet in %g s; an ID is owed",
                            why, WAIT_MAX_S)
                self.results.append((why, False, "Mercury never went quiet"))
                return False
            await asyncio.sleep(0.02)
        if tok != self.token or not st.winlink_armed:
            return False
        self.collided = False
        self._allow = True
        try:
            reply, _ = await self.sess.handle("T 1")
        finally:
            self._allow = False
        if not reply.startswith(f"RPRT {rigctl_server.RIG_OK}"):
            log.warning("station ID (%s) not sent: its key was refused (%s); an ID is owed",
                        why, reply.strip())
            self.results.append((why, False, "key refused"))
            return False
        led = st.id_ledger
        led.id_started(f"CW ID ({why})", time.time(), "station")
        log.info("station ID: keyed for %s (%s, %.1f s of CW)", self.call, why, self.id_s)
        self.playing = True
        stop = None
        try:
            stop = await self._play(tok)
        except asyncio.CancelledError:
            stop = "the session was disarmed"
            raise
        finally:
            self.playing = False
            if stop is None and st.ptt_owner is self.owner:
                await self.sess.handle("T 0")           # its own release: "normal"
                led.id_done(time.time(), True)
                self.unid_since = None
                self.results.append((why, True, None))
                log.info("station ID on air (%s)", why)
            else:
                stop = stop or self._why_stop(tok) or "its key was released"
                if st.ptt_owner is self.owner:
                    await st.unkey(f"station ID stopped: {stop}")
                led.id_done(time.time(), False, f"cut it ({stop})")
                self.results.append((why, False, stop))
        return stop is None

    async def _play(self, tok):
        """None when the whole ID was played; else why it stopped."""
        st = self.st
        loop = asyncio.get_running_loop()
        t = loop.time() + PTT_LEAD_S
        while loop.time() < t:
            why = self._why_stop(tok)
            if why:
                return why
            st.ptt_seen = time.monotonic()
            await asyncio.sleep(CHUNK_S)
        t0 = loop.time()
        audio = st.audio
        for i, off in enumerate(range(0, len(self.pcm), CHUNK)):
            why = self._why_stop(tok)
            if why:
                return why
            st.ptt_seen = time.monotonic()
            if audio is not None and not await audio.play(self.pcm[off:off + CHUNK]):
                return "audio playback failed"
            delay = t0 + (i + 1 - AHEAD) * CHUNK_S - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
        t = loop.time() + TAIL_S + AHEAD * CHUNK_S
        while loop.time() < t:
            why = self._why_stop(tok)
            if why:
                return why
            st.ptt_seen = time.monotonic()
            await asyncio.sleep(CHUNK_S)
        return self._why_stop(tok)
