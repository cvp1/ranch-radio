"""iGate watch: do enough other stations still carry APRS-IS traffic onto the air here?

Messages from the internet reach this station only if a transmitting iGate in
range sends them onto RF; the hub's own iGate is receive-only. Every iGate that
does so is heard as a third-party packet (}...TCPIP...), and the store tallies
each one per local day, every copy counted (aprs_store igate_days).

Once per finished local day the watch judges that day:

    ok       at least --igate-watch-min iGates each delivered --igate-watch-packets
             or more packets
    low      fewer did
    unknown  fewer than --igate-watch-rf frames were heard at all that day (the
             radio wasn't hearing, so a quiet day can't be told from a dead path),
             or the tally started partway through the day

It is edge-triggered: a notification only when the state changes between ok
and low (a drop, then a recovery). unknown never changes the state and never
notifies. The state and the last day judged are kept in the store's meta, so a
restart neither repeats an alert nor forgets one.

    watch = Watch(store_call, notify, min_igates=2)
    await watch.check()          # judges yesterday if it hasn't been yet
"""

import asyncio
import logging
import time

log = logging.getLogger("igate_watch")

MIN_IGATES = 2
MIN_PACKETS = 3
MIN_RF = 200
EVERY_S = 600


def day_of(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def day_start(day):
    """The local midnight that starts day (YYYY-MM-DD), as a timestamp."""
    return time.mktime(time.strptime(day, "%Y-%m-%d"))


def verdict(tally, min_igates=MIN_IGATES, min_packets=MIN_PACKETS, min_rf=MIN_RF):
    """(state, delivering iGates, why) for one day's tally (aprs_store igate_day)."""
    delivering = sorted(c for c, n in tally["igates"].items() if n >= min_packets)
    start = day_start(tally["day"])
    if tally.get("since", 0) > start:
        return "unknown", delivering, "the tally started partway through the day"
    if tally["rf_frames"] < min_rf:
        return "unknown", delivering, (f"only {tally['rf_frames']} RF frames heard "
                                       f"(under {min_rf}): the radio wasn't hearing all day")
    if len(delivering) < min_igates:
        return "low", delivering, (f"{len(delivering)} iGate(s) sent APRS-IS traffic onto the air "
                                   f"(under {min_igates})")
    return "ok", delivering, f"{len(delivering)} iGates sent APRS-IS traffic onto the air"


class Watch:
    def __init__(self, store_call, notify, min_igates=MIN_IGATES, min_packets=MIN_PACKETS,
                 min_rf=MIN_RF, clock=time.time):
        self.call = store_call            # async (name, *args) -> any Db method, off the loop
        self.notify = notify              # async (title, text); must not raise
        self.min_igates, self.min_packets, self.min_rf = min_igates, min_packets, min_rf
        self.clock = clock
        self.last = None                  # the last day judged, as shown on the status

    def status(self):
        return {"enabled": True, "min_igates": self.min_igates, "min_packets": self.min_packets,
                "min_rf": self.min_rf, "last": self.last}

    async def check(self):
        """Judge yesterday (local) once. Returns the judgement, or None if done before."""
        yesterday = day_of(day_start(day_of(self.clock())) - 1)
        state = await self.call("meta", "igate_watch_state", "ok")
        if self.last is None:
            judged = await self.call("meta", "igate_watch_day", None)
            self.last = {"day": judged, "state": state} if judged else None
        if await self.call("meta", "igate_watch_day", None) == yesterday:
            return None
        tally = await self.call("igate_day", yesterday)
        now_state, delivering, why = verdict(tally, self.min_igates, self.min_packets, self.min_rf)
        new = state if now_state == "unknown" else now_state
        await self.call("set_meta", "igate_watch_day", yesterday)
        await self.call("set_meta", "igate_watch_state", new)
        self.last = {"day": yesterday, "verdict": now_state, "state": new, "delivering": delivering,
                     "rf_frames": tally["rf_frames"], "why": why}
        if new != state:
            if new == "low":
                log.warning("iGate watch: %s: %s (%s)", yesterday, why, ", ".join(delivering) or "none")
                await self.notify("APRS: fewer iGates carrying messages here",
                                  f"{yesterday}: {why}: {', '.join(delivering) or 'none'}. Messages from the "
                                  "internet may not reach this station over the air; run a probe.")
            else:
                log.info("iGate watch: %s: back to normal: %s", yesterday, why)
                await self.notify("APRS: iGates carrying messages again",
                                  f"{yesterday}: {why}: {', '.join(delivering)}.")
        else:
            log.info("iGate watch: %s: %s (%s)", yesterday, now_state, why)
        return self.last

    async def run(self, every=EVERY_S):
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:     # noqa: BLE001
                log.exception("iGate watch: check failed")
            await asyncio.sleep(every)
