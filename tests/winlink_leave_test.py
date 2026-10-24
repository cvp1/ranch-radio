"""Winlink disarm puts the radio's mode (and SSB audio source) back even when
the last poll reply still says "transmitting" for a moment after our unkey,
and never changes the mode under a real transmission.

The bug (2026-10-23, found de-flaking qmx_test): _winlink_mode_leave cleared
what it had saved, saw tx still set by a reply computed before the unkey
reached the radio, and returned: the restore was lost for good. In-process
and deterministic: a stub radio whose tx clears 0.3 s late, or never.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:60} got {got!r}" + ("" if ok else f" want {want!r}"))


class Radio:
    caps = {"features": [], "data_modes": True}

    def __init__(self):
        self.state = {"tx": True}
        self.calls = []

    async def set(self, k, v):
        self.calls.append((k, v))

    async def ssb_from_usb(self, on):
        self.calls.append(("ssb_from_usb", on))


class Stub:
    def __init__(self):
        self.radio = Radio()
        self._winlink_mode_was = ("USB", True)
        self._winlink_ssb = True
        self.notices = []

    def notice(self, msg, level="info", ws=None):
        self.notices.append(msg)


async def stale_then_rx():
    st = Stub()

    async def unkey_lands():
        await asyncio.sleep(0.3)               # the next poll says RX
        st.radio.state["tx"] = False
    t = asyncio.ensure_future(unkey_lands())
    await server.Station._winlink_mode_leave(st)
    await t
    return st


async def still_keyed():
    st = Stub()
    await server.Station._winlink_mode_leave(st)
    return st


st = asyncio.run(stale_then_rx())
check("stale TX reply: SSB source and mode put back", st.radio.calls,
      [("ssb_from_usb", False), ("mode", "USB"), ("data", True)])
check("stale TX reply: no warning", st.notices, [])
st = asyncio.run(still_keyed())
check("really transmitting: nothing changed under it", st.radio.calls, [])
check("really transmitting: the operator is told", len(st.notices), 1)
print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
