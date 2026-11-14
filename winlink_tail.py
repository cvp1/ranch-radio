"""The end of a Winlink session's transmit: stay armed until the modem's
station ID is out (bounded). Stdlib only.

Split from winlink.py (2026-11-13) because the radio server runs this, and
importing winlink.py there loaded the whole Winlink stack (aiohttp, the P2P
directory, the listen plans) into the transmitting process; tests/affected.py
then tied every transmit-safety test to each Winlink feature change.
winlink.py re-exports all of this.
"""
import asyncio
import logging
import time

log = logging.getLogger("winlink")

ID_START_S = 8.0             # tracked: no ID started by then -> none is coming
ID_GRACE_S = 25.0            # never armed longer than this after Pat returns
TAIL_QUIET_S = 3.0           # not tracked: armed until the lane is this quiet


async def id_tail(station, started, closing=lambda: False, notify=None, scheme="ardop"):
    """Stay armed until the modem's end-of-session ID is out (bounded):
    ardopcf's (ARDOP) or xara's CW ID (VARA HF), by the session's scheme.

    Only for a session that transmitted, and only while still armed: the
    page closing disarms at once, an operator's Disarm too. Runs in the
    process that owns the radio (its lane, ledger and PTT), whoever runs Pat.
    Untracked (no log for this modem): armed until the lane is quiet for
    TAIL_QUIET_S. xara's blanks before its call are inside the keyed burst
    (crates/xara-phy/src/cwid.rs synth), so no quiet gap opens mid-ID; and a
    VARA lane exists only with xara's log, so VARA is always tracked."""
    import station_id
    st, led = station, station.id_ledger
    source = station_id.SOURCE_OF.get(scheme, "ardopcf")
    if not led.last_tx or led.last_tx < started:
        return                          # never keyed: nothing to identify
    t0 = time.monotonic()
    waited = False
    while st.winlink_armed and not closing():
        el = time.monotonic() - t0
        if el >= ID_GRACE_S:
            log.warning("winlink: no end of station ID after %gs; disarming", ID_GRACE_S)
            break
        if not st._winlink_keying():
            if led.tracked_for(scheme):
                if not led.in_progress(source):
                    if not led.owed():
                        break
                    if el >= ID_START_S:
                        log.warning("winlink: session transmitted but %s sent no "
                                    "station ID at its end; an ID is owed", source)
                        break
            elif time.time() - (led.last_ptt() or 0) >= TAIL_QUIET_S:
                break
        if not waited:
            waited = True
            log.info("winlink: holding the session armed for %s's station ID", source)
            if notify:
                notify({"id": dict(led.snapshot(), waiting=True)})
        await asyncio.sleep(0.1)
