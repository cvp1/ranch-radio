"""The station's own CW ID on Mercury HF's lane (plan winlink-mercury M0).

Mercury sends no station ID, so this radio server sends one (47 CFR 97.119(a):
at the end of each communication and at least every 10 minutes during one),
as the lane's PTT owner, only inside an armed Mercury session, through the
same lane gate and can_key() as Mercury's own keys: unarmed, STOP TX, --no-tx,
another owner or an ATU tune refuse it the same way (RPRT -11, no PTT).

Mercury plays its frame's audio even when its key is refused
(tcp_interfaces.c:2496-2508), so the ID is never keyed where a Mercury frame
may be playing:
  - a refused Mercury T 1 opens an "audio may be playing" window for THAT
    client, closed only by that client's own T 0 or after WINDOW_S; windows
    outlive a disarm and a re-arm (the frame does);
  - a release of Mercury's key that is not its own T 0 (the keepalive cut, a
    TUNE-like key, a disconnect) opens the full window too;
  - after any Mercury release or T 0, RELEASE_GUARD_S of quiet first.
With no Mercury owner, no open window and the guard past, the ID may key,
including when Mercury has been idle a long time (a gate falling due between
frames): that is safe because a Mercury T 1 during the ID releases the ID's
key BEFORE Mercury hears its refusal (collide(), awaited in the lane), so the
frame it then plays is never under our PTT. That collision cuts the ID: owed,
never retried, the fence latched until disarm, and the session ends.

  - End of the session (id_tail): fence -> Mercury quiet -> ID -> the caller
    disarms. The station's RemoteLane waits end_wait_s for it.
  - 10 minutes (hard gate): GATE_S after Mercury's first key since the last
    ID that went on air, fence; the ID as soon as Mercury is quiet.
  - The first trial's bound: BOUND_S after arming, fence, ask the station to
    abort Pat (its winlink.abort, through the lane socket), and the ID as soon
    as Mercury is quiet; Pat returning ends the session (winlink_end). If the
    station never ends it, the backstop disarms.
  - STOP TX, disarm, the arming page gone, audio failing or raising, a limit
    cutting the key: the ID stops at once and stays owed. Nothing here ever
    keys later because of it: every task belongs to one arm (token) and dies
    at disarm.

Only an ID played to the end and released with its own T 0 counts as on air
(ledger source "station", IdLedger.id_done).

Timing budget (worst_case_end_s, checked by mercury_id_test): from the moment
the gate or the bound falls due, the ID waits at most WAIT_MAX_S: Mercury's
key, which lasts at most 33 s from key-down whatever it sends meanwhile
(RigctlOwner.max_key_s, the hard cap in Station.safety_loop), then a full
33 s window, the guard, 1 s. Then lead + the longest CW call check_call
accepts (twelve zeros, 17.4 s at 18 WPM) + tail. 500 + 0.25 + 67.2 + 0.15 +
17.4 + 0.36 = 585.4 s <= 590 s for the gate; the bound (490 s from arming,
which is no later than Mercury's first key) ends by 575.4 s. If Mercury
never goes quiet for that long (refused frame after refused frame, each
renewing its window) the ID cannot start: the session is disarmed at
due + WAIT_MAX_S (<= 567.5 s) with the ID owed and a WARNING, the fence
latched; nothing keys for it later (panel pb34973ec68 F1).
"""
import asyncio
import json
import logging
import time

import cw_id
import rigctl_server

log = logging.getLogger("station_id")

WINDOW_S = rigctl_server.WINLINK_MERCURY_KEEPALIVE_S   # a refused (or cut) frame may play this long
RELEASE_GUARD_S = 0.2        # quiet this long after Mercury's last release or T 0 before the ID keys
WAIT_MAX_S = rigctl_server.WINLINK_MERCURY_KEEPALIVE_S + WINDOW_S + RELEASE_GUARD_S + 1.0
PTT_LEAD_S = 0.15            # keyed before the tone: the radio's TX path settles
CHUNK = 640                  # bytes: 20 ms of audio.py's 16 kHz s16le
CHUNK_S = 0.02
AHEAD = 3                    # chunks written ahead of real time (CoreAudio keeps <= 300 ms)
TAIL_S = 0.3                 # after the last chunk: the card's buffer drains before T 0
ID_KEEPALIVE_S = 1.0         # the keyer refreshes every chunk; a stalled keyer is cut by safety_loop
BOUND_BACKSTOP_S = 20.0      # after the bound: the station's abort ends the session within this
TICK_S = 0.25
END_MARGIN_S = 5.0
ID_DEADLINE_S = 590.0        # every ID ends by here (10:00 less margin), from the first unidentified key
GATE_S = 500.0               # 8:20 (derivation above)
BOUND_S = 490.0              # 8:10: the first trial's cap on a Mercury session (plan 2.2 b)
FULL_WINDOW_CAUSES = ("keepalive", "cap", "disconnect", "forced")   # a release that was not Mercury's own T 0
WINDOWS_MAX = 64             # Mercury clients tracked at once; past it, every new one is "open" (safe)
COLLIDE_FLUSH_S = 1.0        # a collision's unkey must reach the radio within this before Mercury hears no


def id_s_max(wpm=cw_id.DEFAULT_WPM):
    """Lead, the longest CW call, and the tail: the longest an ID keeps the key."""
    return PTT_LEAD_S + cw_id.longest_s(wpm) + TAIL_S + AHEAD * CHUNK_S


def worst_case_end_s(due_s, wpm=cw_id.DEFAULT_WPM):
    """When an ID that falls due at due_s ends at the latest."""
    return due_s + TICK_S + WAIT_MAX_S + id_s_max(wpm)


# The station's RemoteLane waits this long for the end-of-session ID before it
# closes the lane (which disarms): Mercury's longest key and window, then the
# longest ID, and a margin. Sent in the armed message (end_wait_s).
END_WAIT_S = WAIT_MAX_S + id_s_max() + END_MARGIN_S


class MercuryIdent:
    def __init__(self, station, call, wpm=cw_id.DEFAULT_WPM, gate_s=GATE_S, bound_s=BOUND_S,
                 clock=time.monotonic):
        self.st = station
        self.clock = clock          # the audio windows' clock (a test's fake one)
        self.call = cw_id.check_call(call)
        self.pcm = cw_id.render(self.call, wpm)
        self.id_s = cw_id.duration_s(self.call, wpm)
        self.gate_s, self.bound_s = float(gate_s), float(bound_s)
        self.end_wait_s = round(END_WAIT_S, 1)
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
        # Mercury clients whose frame may be playing: owner -> monotonic until.
        # Kept across disarm and arm: a refused frame plays on regardless.
        self.windows = {}
        self.overflow_until = 0.0   # past WINDOWS_MAX: a window nobody can close until then
        self.last_quiet = None      # (monotonic, cause): Mercury's last release or idle T 0
        self.playing = False
        self.collided = False
        self._release = None        # a collision's TX OFF on its way to the wire (a task)
        self._allow = False         # the keyer's own T 1 is due now
        self._task = None
        self._watch = None
        self._backstop = None
        self._bound_hit = False
        self.results = []           # (why, on_air, reason): every ID this server tried, newest last

    # --- the server's hooks ---

    def armed(self):
        """A Mercury session was armed (Station._winlink_arm_prepare; never
        over a live Mercury session: winlink_arm refuses that)."""
        self._stop_tasks()
        self.token += 1
        self.armed_at = time.monotonic()
        self.unid_since = None
        self.fence, self.latched, self._bound_hit = None, False, False
        self._watch = asyncio.ensure_future(self._watch_loop(self.token))

    def disarmed(self):
        """The session is being disarmed (Station.winlink_disarm, any reason)."""
        self.token += 1
        self._stop_tasks()
        self.armed_at = None
        self.fence, self.latched = None, False
        self._prune()

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
        from a client not keying). True for a refusal during the ID: the lane
        must await collide() before it answers."""
        if owner is self.owner:
            return False
        now = time.monotonic()
        if ev == "keyed":
            if self.unid_since is None:
                self.unid_since = now
        elif ev == "refused":
            self._open_window(owner, WINDOW_S)
            if self.playing:
                self.collided = True
                return True
        elif ev == "idle":
            if self.windows.pop(owner, None) is not None:   # only its own frame is over
                self.last_quiet = (now, "idle")
        return False

    def _prune(self, now=None):
        now = self.clock() if now is None else now
        for o in [o for o, t in self.windows.items() if t <= now]:
            del self.windows[o]

    def _open_window(self, owner, length):
        """owner's frame may play for `length` from now (panel pb34973ec68 F5:
        pruned on every add and at disarm; at most WINDOWS_MAX owners)."""
        now = self.clock()
        until = now + length
        self._prune(now)
        if owner not in self.windows and len(self.windows) >= WINDOWS_MAX:
            # Full and nothing expired: no owner is dropped, and this window
            # counts as open regardless (the safe side).
            self.overflow_until = max(self.overflow_until, until)
            log.warning("station ID: more than %d Mercury clients with a frame in flight; "
                        "treating the channel as busy until the newest is over", WINDOWS_MAX)
            return
        self.windows[owner] = max(self.windows.get(owner, 0.0), until)

    def released(self, owner, cause):
        """Station.unkey: a Mercury client's key ended. cause "normal" (its own
        T 0), "keepalive", "disconnect", "forced"."""
        if owner is self.owner:
            return
        now = time.monotonic()
        self.last_quiet = (now, cause)
        if cause in FULL_WINDOW_CAUSES:
            # Not its own T 0: whatever it was playing (a TUNE-like key, a
            # frame past the cap) may still be playing; a T 0 from it later
            # says it is over.
            self._open_window(owner, WINDOW_S)

    async def collide(self):
        """Mercury asked to key during the ID: release the ID's key now, and
        answer only once that release is on the wire."""
        log.warning("station ID: Mercury asked to key during the ID: its audio may be playing "
                    "into it; cutting the ID before it is told no")
        # One release wait per collision, started before the unkey is queued
        # (it waits for the wire, so the order does not matter to it), so a
        # refusal arriving meanwhile on any connection waits on it too
        # (panel p84c693e4fa #1).
        if self._release is None or self._release.done():
            self._release = asyncio.ensure_future(self._release_on_wire(self.token))
        if self.st.ptt_owner is self.owner:
            await self.st.unkey("station ID stopped: Mercury asked to key during it")
        await asyncio.shield(self._release)

    async def refusal_hold(self):
        """Any refusal on Mercury's lane: while a collision's release is not
        yet on the wire, the reply waits for it (bounded). Survives the ID's
        own disarm: the release is the radio's, not the session's."""
        r = self._release
        if r is not None and not r.done():
            await asyncio.shield(r)

    async def _release_on_wire(self, tok):
        # The unkey is queued; Mercury hears no only once it is on the wire
        # (CI-V's state is optimistic: panel pb34973ec68 F3). Bounded: past
        # it Mercury is answered anyway, and nothing else keys.
        wait = getattr(self.st.radio, "wait_tx_off_written", None)
        if wait is None or await wait(COLLIDE_FLUSH_S):
            return
        log.warning("station ID: the unkey did not reach the radio within %g s of the collision; "
                    "Mercury is refused now", COLLIDE_FLUSH_S)
        if tok == self.token:
            # Only the arm it came from (panel p84c693e4fa #2): a session
            # armed since is not touched.
            self.latched = True
            self.fence = self.fence or "a station ID collided"

    # --- views ---

    def mercury_keyed(self):
        o = self.st.ptt_owner
        return o is not None and o is not self.owner

    def windows_open(self):
        now = self.clock()
        self._prune(now)
        return bool(self.windows) or now < self.overflow_until

    def quiet(self, now=None):
        """The ID may key: no Mercury owner, no frame that may be playing,
        and the guard past Mercury's last release or T 0."""
        # Not a strict own-release slot: panel pb34973ec68 F2 was reviewed and
        # rejected (2026-11-26): a collision unkeys before Mercury hears no,
        # and any forced release waits a full window.
        now = time.monotonic() if now is None else now
        if self.mercury_keyed() or self.windows_open():
            return False
        return self.last_quiet is None or now - self.last_quiet[0] >= RELEASE_GUARD_S

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
            # returning sends winlink_end, which joins the ID started here.
            asyncio.ensure_future(self.st._send(ws, json.dumps({"t": "winlink", "end_request": why})))
        if self.unid_since is not None:
            self._start("session bound")
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
        for _ in range(2):           # an ID running now, then (if Mercury keyed after it) ours
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
        try:
            ok = await self._send(why, tok)
        except asyncio.CancelledError:
            raise
        except Exception:                    # noqa: BLE001 - any fault is a failed ID, never a pass
            log.exception("station ID (%s): failed", why)
            ok = False
        if tok != self.token:
            return
        if ok:
            if gate and self.fence == "the 10-minute station ID":
                self.fence = None
            return
        # Any ID that did not go out whole latches the fence: no retry, in
        # this path or the end-of-session one (2026-11-25, mercury_id_test:
        # an end-of-session ID cut by a collision was sent again).
        # The session ends too, whichever path owed it (gate, bound, end): it
        # must not stay armed unidentified (panel pb34973ec68 F1).
        self.latched = True
        self.fence = self.fence or "a station ID did not go out"
        log.warning("station ID (%s) NOT sent: %s; disarming with the ID owed. Nothing will key "
                    "for it later.", why, self.results[-1][2] if self.results else "it failed")
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
        while not self.quiet():
            if tok != self.token or not st.winlink_armed:
                return False
            if time.monotonic() > end:
                log.warning("station ID (%s) not sent: Mercury did not go quiet in %g s; an ID is owed",
                            why, WAIT_MAX_S)
                self.results.append((why, False, "Mercury never went quiet"))
                return False
            await asyncio.sleep(0.02)
        if tok != self.token or not st.winlink_armed:
            return False
        # From here a Mercury T 1 is a collision, even while our own T 1 is
        # still on its way to the radio.
        self.collided = False
        self.playing = True
        self._allow = True
        try:
            reply, _ = await self.sess.handle("T 1")
        except BaseException:
            self.playing = False
            raise
        finally:
            self._allow = False
        if not reply.startswith(f"RPRT {rigctl_server.RIG_OK}"):
            self.playing = False
            log.warning("station ID (%s) not sent: its key was refused (%s); an ID is owed",
                        why, reply.strip())
            self.results.append((why, False, "key refused"))
            return False
        led = st.id_ledger
        led.id_started(f"CW ID ({why})", time.time(), "station")
        log.info("station ID: keyed for %s (%s, %.1f s of CW)", self.call, why, self.id_s)
        stop = "it did not finish"
        try:
            stop = await self._play(tok)
        except asyncio.CancelledError:
            stop = "the session was disarmed"
            raise
        except Exception as e:               # noqa: BLE001 - a playback fault fails the ID
            log.exception("station ID: playback raised")
            stop = f"audio playback raised {type(e).__name__}"
        finally:
            self.playing = False
            held = st.ptt_owner is self.owner
            if held:
                # Always our own T 0, so the lane's burst ends "normal"; the
                # ledger is told separately whether the ID was whole.
                r0, _ = await self.sess.handle("T 0")
                if not r0.startswith(f"RPRT {rigctl_server.RIG_OK}") and stop is None:
                    stop = f"its release was refused ({r0.strip()})"
                if st.ptt_owner is self.owner:           # never left keyed
                    await st.unkey("station ID: release")
                    stop = stop or "its release did not take"
            elif stop is None:
                stop = self._why_stop(tok) or "its key was released"
            if stop is None:
                led.id_done(time.time(), True)
                self.unid_since = None
                self.results.append((why, True, None))
                log.info("station ID on air (%s)", why)
            else:
                led.id_done(time.time(), False, f"cut it ({stop})")
                self.results.append((why, False, stop))
        return stop is None

    async def _play(self, tok):
        """None when the whole ID was played; else why it stopped."""
        st = self.st
        loop = asyncio.get_running_loop()
        why = self._why_stop(tok)
        if why:
            return why
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
