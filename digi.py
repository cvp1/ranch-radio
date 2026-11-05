"""Fill-in digipeater for the packet hub ("packet": {"digi": {...}}).

    d = Digi("N9ABC-1", viscous_s=5)
    d.heard(frame, t)          # every frame the radio hears
    for f in d.due(t): ...     # frames to transmit now (hub.transmit_digi)

A fill-in, by the APRS New-N paradigm (WIDEn-N, 2005 on) as Direwolf and aprx
apply it, for a site already inside wide digipeater coverage:

- Only UI frames, and only two kinds of request: the first hop is WIDE1-1
  (the fill-in alias: "WIDE1-1,WIDE2-1" from a mobile or HT), or the next
  unused hop is this digipeater's own call. Never WIDE2-n and up (the wide
  digis' job), never the obsolete RELAY / WIDE / TRACE aliases, never a
  WIDE1-1 that some other digi already repeated.
- Viscous (aprx's term): a WIDE1-1 request is held viscous_s seconds. If a
  digipeated copy of the same packet is heard in that time, a wide digi has
  it covered and the hold is dropped; only the packets nothing else repeated
  go out. A request to this call by name goes at once.
- Callsign substitution: WIDE1-1 becomes this call, marked used
  ("WIDE1-1,WIDE2-1" -> "N9ABC-1*,WIDE2-1"). The callsign in the path
  identifies every transmission (47 CFR 97.119(a)).
- The same packet (source, destination, info) goes at most once per DUPE_S,
  however many copies are heard; nothing from this call, and nothing
  carrying TCPIP / TCPXX (internet traffic doesn't belong on RF).

The hub's TxGate still decides: transmit on, the radio not lent, and the
digipeater's own rate limit.
"""

from ax25 import Address, Frame, U_UI

DUPE_S = 30.0
FILL_IN = ("WIDE1", 1)
NO_RF = {"TCPIP", "TCPXX"}


def dupe_key(f):
    return (str(f.src), str(f.dest), bytes(f.info))


def plan(f, me):
    """(path, immediate) for a frame this digipeater should repeat, or
    (None, why) when it shouldn't. path: the new digi list."""
    if f.kind != "U" or f.utype != U_UI:
        return None, "not a UI frame"
    if f.src.same_station(me):
        return None, "our own"
    if any(d.call in NO_RF for d in f.digis):
        return None, "internet traffic"
    nxt = next((i for i, d in enumerate(f.digis) if not d.bit7), None)
    if nxt is None:
        return None, "no unused hop"
    hop = f.digis[nxt]
    if hop.same_station(me):
        path = [Address(d.call, d.ssid, d.bit7) for d in f.digis]
        path[nxt].bit7 = True
        return path, True
    if nxt == 0 and (hop.call, hop.ssid) == FILL_IN:
        path = [Address(me.call, me.ssid, True)] + [Address(d.call, d.ssid, d.bit7) for d in f.digis[1:]]
        return path, False
    return None, "not a fill-in request"


class Digi:
    def __init__(self, call, viscous_s=5.0):
        self.me = Address.from_text(call)
        self.viscous_s = float(viscous_s)
        self.pending = {}          # dupe key -> (due time, frame to send)
        self.sent = {}             # dupe key -> time sent
        self.counts = {"requests": 0, "held": 0, "covered": 0, "dupes": 0, "queued": 0}

    def status(self):
        return {"enabled": True, "call": str(self.me), "viscous_s": self.viscous_s,
                "pending": len(self.pending), **self.counts}

    def heard(self, f, t):
        key = dupe_key(f)
        if key in self.pending and any(d.bit7 for d in f.digis):
            del self.pending[key]          # a digipeater repeated it: covered
            self.counts["covered"] += 1
            return
        path, how = plan(f, self.me)
        if path is None:
            return
        self.counts["requests"] += 1
        if len(self.sent) > 500:
            self.sent = {k: v for k, v in self.sent.items() if t - v < DUPE_S}
        if t - self.sent.get(key, -DUPE_S) < DUPE_S or key in self.pending:
            self.counts["dupes"] += 1
            return
        out = Frame(f.dest, f.src, path, "U", utype=U_UI, pid=f.pid, info=f.info, command=True)
        if how is True:
            self.pending[key] = (t, out)
        else:
            self.pending[key] = (t + self.viscous_s, out)
            self.counts["held"] += 1

    def due(self, t):
        """Frames whose hold is over, oldest first; they count as sent."""
        ready = sorted((v[0], k) for k, v in self.pending.items() if v[0] <= t)
        out = []
        for _, k in ready:
            out.append(self.pending.pop(k)[1])
            self.sent[k] = t
            self.counts["queued"] += 1
        return out

    def next_due(self):
        return min((v[0] for v in self.pending.values()), default=None)
