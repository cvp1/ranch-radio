"""The APRS round-trip probe: can someone actually reach this station?

From the packet page (Probe button, TX ARMED), never scheduled. One message
goes through the messaging engine (aprs_msg.py) to the host file's probe
target, with its text plus a fresh 3-character token so a text reply can be
correlated. Then five legs are judged, each "yes" (with when and the
evidence), "not seen", or "unknown" (with why), over a window from the first
actual transmission to the engine's deadline + 60 s (t_first + 1,470 s):

  L1 sent              our tx of this msgno, at or after t_first.
  L2 digipeated copy   on RF, at or after t_first, exactly our source,
                       destination and info, with an H-bit set on a digi
                       call that isn't ours. Never an APRS-IS line.
  L3 reached APRS-IS   from the IS observer, at or after t_first, exactly
       via RF          our source, destination and info, with an RF-entry q
                       construct (qAR, qAO, qAr, qAo) from an iGate that
                       isn't our login. Any other IS copy is noted, never yes.
  L4 return over RF    on RF, at or after t_first, from the target (inner
                       source if wrapped) to one of our calls, with an
                       ack/rej or reply-ack of THIS msgno, or text holding
                       THIS token; the IS->RF iGate is recorded if wrapped.
                       Other target traffic is noted "not correlated".
  L5 acked             the engine's message state is acked.

Positive evidence is kept once seen. A coverage gap (the radio not linked,
lent or held off channel, the hub not running; for L3 the observer not
connected and verified) only turns absent evidence into "unknown". Wording
never diagnoses the antenna: L1 yes with L2 not seen reads "no digipeated
copy observed" (a direct iGate path needs no digi).

Refused unless the page is armed, the effective messaging mode is send or
full, nothing pauses or holds messaging, a target is set, and the previous
probe's deadline has passed. Results persist (table probes) for the IS->RF
gating decision (plan D5); the page shows the last 20.
"""
import asyncio
import logging
import random
import string
import time

import aprs
import aprs_msg

log = logging.getLogger("aprs_probe")

WINDOW_AFTER_DEADLINE_S = 60
WINDOW_S = aprs_msg.DEADLINE_S + WINDOW_AFTER_DEADLINE_S        # 1,470 s from the first transmission
RF_ENTRY_Q = ("qAR", "qAO", "qAr", "qAo")
TOKEN_CHARS = string.ascii_letters + string.digits
LEGS = ("L1", "L2", "L3", "L4", "L5")
TICK_S = 1.0


class ProbeError(ValueError):
    pass


def _gap_overlaps(gaps, a, b):
    """Any [start, end|None] gap overlapping [a, b]."""
    for s, e in gaps:
        if s < b and (e is None or e > a):
            return True
    return False


class Probe:
    def __init__(self, hub, engine, target=None, text="", clock=time.time):
        self.hub, self.engine, self.clock = hub, engine, clock
        self.target = aprs.norm_call(target) if target else None
        self.text = text or ""
        self.cur = None                 # the running probe (a dict, as stored)
        self._dirty = False
        self._task = None
        engine.listeners.append(self)

    # ------------------------------------------------------------ life

    async def start(self, run=True):
        """Resume a probe left running: the time the hub was down is a gap."""
        rows = await self.hub.aprs.call("probes", 1)
        now = self.clock()
        if rows and rows[0]["state"] == "running":
            p = rows[0]
            last = p.get("last_seen") or p["t"]
            p["coverage"] = p.get("coverage") or {"rf": [], "is": []}
            p["coverage"]["rf"].append([last, now, "the hub not running"])
            p["coverage"].setdefault("down", []).append([last, now])     # the observer wasn't watching either
            p["notes"] = (p.get("notes") or []) + [f"hub not running {round(now - last)} s"]
            if p.get("t_first") is not None:
                # Our frame, as sent: the evidence for L2 and L3 is matched against it.
                row = await self.hub.aprs.call("msg_get", p["msg_id"])
                if row:
                    info = aprs.message_info(row["dst"], row["text"], row["msgno"], reply_ack=row["reply_ack"])
                    p["frame"] = [row["src"], aprs_msg.TOCALL, info]
            self.cur = p
            await self._save()
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
            except Exception:     # noqa: BLE001
                log.exception("probe pass failed")
            await asyncio.sleep(TICK_S)

    # ------------------------------------------------------------ starting one

    def refuse_why(self, armed):
        if not armed:
            return "arm TX on the page first"
        if self.engine.effective not in ("send", "full"):
            return f"messaging is {self.engine.effective}"
        if not self.target:
            return "no probe target is set (host file packet.aprs.probe.target)"
        why = self.engine.pause_why()
        if why:
            return f"not now: {why}"
        p = self.cur
        if p is not None:
            end = (p["t_first"] + aprs_msg.DEADLINE_S) if p.get("t_first") else p["t"] + aprs_msg.DEADLINE_S
            if self.clock() < end:
                return f"the previous probe's deadline has not passed ({int(end - self.clock()) + 1} s left)"
        return None

    async def begin(self, armed=False):
        why = self.refuse_why(armed)
        if why:
            raise ProbeError(why)
        token = "".join(random.SystemRandom().choice(TOKEN_CHARS) for _ in range(3))
        text = f"{self.text} {token}".strip()
        try:
            row = await self.engine.compose(self.target, text)
        except aprs_msg.MsgError as e:
            raise ProbeError(str(e)) from None
        if self.cur is not None and self.cur.get("state") == "running":
            await self._finish()            # the previous window, closed now (its deadline has passed)
        now = self.clock()
        p = {"t": now, "target": self.target, "text": text, "token": token, "msg_id": row["id"],
             "msgno": row["msgno"], "t_first": None, "window_end": None, "state": "running",
             "legs": {k: None for k in LEGS}, "coverage": {"rf": [], "is": [], "down": []}, "notes": [],
             "last_seen": now}
        p["id"] = await self.hub.aprs.call("probe_insert", {k: v for k, v in p.items() if k != "id"})
        self.cur = p
        log.warning("probe %d: %s to %s (msgno %s)", p["id"], text, self.target, row["msgno"])
        return p

    # ------------------------------------------------------------ evidence

    def _ours(self):
        """(src, dest, info) of our frame, once sent."""
        return self.cur and self.cur.get("frame")

    def _yes(self, leg, t, evidence):
        p = self.cur
        if p["legs"].get(leg) and p["legs"][leg].get("v") == "yes":
            return                          # kept once seen
        p["legs"][leg] = {"v": "yes", "t": t, "evidence": evidence}
        self._dirty = True

    def _note(self, text):
        notes = self.cur["notes"]
        if text not in notes and len(notes) < 30:
            notes.append(text)
            self._dirty = True

    def _in_window(self, t):
        p = self.cur
        return p is not None and p.get("t_first") is not None and p["t_first"] <= t <= p["window_end"]

    def on_tx(self, row, frame):
        p = self.cur
        if p is None or row["id"] != p["msg_id"]:
            return
        t = self.clock()
        if p["t_first"] is None:
            p["t_first"] = row["first_sent"] or t
            p["window_end"] = p["t_first"] + WINDOW_S
            p["frame"] = [str(frame.src), str(frame.dest), frame.info.decode("latin-1")]
        self._yes("L1", p["t_first"], f"tx {frame.tnc2()}")

    def on_state(self, row):
        p = self.cur
        if p is None or row["id"] != p["msg_id"]:
            return
        if row["state"] == "acked" and p.get("t_first") is not None:
            self._yes("L5", row.get("acked_t") or self.clock(), "the engine matched an ack to this msgno")

    def on_rf(self, t, f, d):
        ours = self._ours()
        if not ours or not self._in_window(t):
            return
        own_bases = {aprs.base_call(c) for c in self.engine.owned} | set(self.hub.my_bases)
        if (str(f.src), str(f.dest), f.info.decode("latin-1")) == tuple(ours):
            used = [str(x) for x in f.digis if x.bit7 and x.call not in own_bases]
            if used:
                self._yes("L2", t, f"digipeated by {', '.join(used)}: {f.tnc2()}")
            return
        m = d.get("inner") if d.get("third_party") else d
        if not m or m.get("src") != self.cur["target"]:
            return
        to = aprs.norm_call(m.get("addressee") or "")
        if to not in self.engine.owned_set:
            return
        msgno = self.cur["msgno"]
        hit = ((m.get("type") in ("ack", "rej") and m.get("msgno") == msgno)
               or (m.get("type") == "message" and m.get("reply_ack") == msgno)
               or (m.get("type") == "message" and self.cur["token"] in (m.get("text") or "")))
        if hit:
            via = f" via the IS->RF iGate {d['src']}" if d.get("third_party") else " direct on RF"
            self._yes("L4", t, f"{m.get('type')} from {m['src']}{via}: {f.tnc2()}")
        else:
            self._note(f"target traffic seen, not correlated: {f.tnc2()[:120]}")

    def on_is(self, t, d):
        ours = self._ours()
        if not ours or not self._in_window(t):
            return
        if (d.get("src"), d.get("dest"), d.get("info")) != tuple(ours):
            return
        path = d.get("path") or []
        q = next((i for i, x in enumerate(path) if x.startswith("q")), None)
        login = (self.hub.igate.login if self.hub.igate else "").upper()
        if q is not None and q + 1 < len(path) and path[q] in RF_ENTRY_Q and path[q + 1].upper() != login:
            self._yes("L3", t, f"{path[q]} by the iGate {path[q + 1]}: {','.join(path)}")
        else:
            self._note(f"IS copy, not via RF ({path[q] if q is not None else 'no q construct'}"
                       f"{', our own login' if q is not None and q + 1 < len(path) and path[q + 1].upper() == login else ''})")

    # ------------------------------------------------------------ coverage and the end

    def rf_ok(self):
        h = self.hub
        if not h.radio.up:
            return False, "the radio not linked"
        if h.lent:
            return False, "the radio lent"
        if h.state.holds:
            return False, "off channel (held)"
        return True, None

    def observer_gaps(self):
        ig = self.hub.igate
        if ig is None or not getattr(ig, "observe", None):
            return None
        return [list(g) for g in ig.gaps]

    async def tick(self):
        p = self.cur
        if p is None or p["state"] != "running":
            return
        now = self.clock()
        ok, why = self.rf_ok()
        gaps = p["coverage"]["rf"]
        if not ok and (not gaps or gaps[-1][1] is not None):
            gaps.append([now, None, why])
            self._dirty = True
        elif ok and gaps and gaps[-1][1] is None:
            gaps[-1][1] = now
            self._dirty = True
        if p.get("t_first") is None and now - p["t"] > aprs_msg.DEADLINE_S:
            await self._finish()
            return
        if p.get("window_end") is not None and now >= p["window_end"]:
            await self._finish()
            return
        if self._dirty or now - (p.get("last_seen") or 0) > 30:
            p["last_seen"] = now
            await self._save()

    def verdicts(self, final=False):
        """Each leg: yes/not seen/unknown (or waiting while the window is open)."""
        p = self.cur
        out = {}
        t1 = p.get("t_first")
        end = p.get("window_end") or (p["t"] + aprs_msg.DEADLINE_S)
        rf_gap = t1 is not None and _gap_overlaps([g[:2] for g in p["coverage"]["rf"]], t1, end)
        obs = p["coverage"].get("is")
        is_gap = obs is None or (t1 is not None and _gap_overlaps(obs + p["coverage"].get("down", []), t1, end))
        for leg in LEGS:
            got = p["legs"].get(leg)
            if got and got.get("v") == "yes":
                out[leg] = got
                continue
            if not final:
                out[leg] = {"v": "waiting"}
                continue
            if leg == "L1":
                out[leg] = {"v": "not seen", "why": "never sent" + (f" ({p['notes'][-1]})" if p["notes"] else "")}
            elif t1 is None:
                out[leg] = {"v": "unknown", "why": "nothing was sent"}
            elif leg == "L3":
                out[leg] = ({"v": "unknown", "why": "no APRS-IS observer" if obs is None else "observer coverage incomplete"}
                            if is_gap else {"v": "not seen", "why": "no IS copy via RF"})
            elif rf_gap:
                g = next(g for g in p["coverage"]["rf"] if g[0] < end and (g[1] is None or g[1] > t1))
                out[leg] = {"v": "unknown", "why": f"RF coverage incomplete ({g[2] if len(g) > 2 else 'gap'})"}
            elif leg == "L2":
                out[leg] = {"v": "not seen", "why": "no digipeated copy observed"}
            elif leg == "L4":
                out[leg] = {"v": "not seen", "why": "no correlated reply heard on RF"}
            else:
                out[leg] = {"v": "not seen", "why": "no ack matched"}
        return out

    async def _finish(self):
        p = self.cur
        gaps = self.observer_gaps()
        p["coverage"]["is"] = gaps
        for g in p["coverage"]["rf"]:
            if g[1] is None:
                g[1] = self.clock()
        p["legs"] = self.verdicts(final=True)
        p["state"] = "done"
        p["last_seen"] = self.clock()
        await self._save()
        log.warning("probe %d done: %s", p["id"], ", ".join(f"{k} {v['v']}" for k, v in p["legs"].items()))

    async def _save(self):
        p = self.cur
        if p.get("state") == "running":
            p["coverage"]["is"] = self.observer_gaps()
        self._dirty = False
        await self.hub.aprs.call("probe_update", p["id"], {k: p.get(k) for k in (
            "t_first", "window_end", "state", "legs", "coverage", "notes", "last_seen")})

    def describe(self, p):
        live = self.cur is not None and p["id"] == self.cur["id"] and self.cur["state"] == "running"
        legs = self.verdicts() if live else p["legs"]
        return {**{k: p.get(k) for k in ("id", "t", "target", "text", "token", "msgno", "t_first", "window_end",
                                          "state", "notes")}, "legs": legs}

    def status(self):
        return {"target": self.target, "text": self.text, "running": self.describe(self.cur) if self.cur else None,
                "refuse": self.refuse_why(True)}
