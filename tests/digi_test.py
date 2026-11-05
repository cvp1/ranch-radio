"""The packet hub's fill-in digipeater (digi.py, "packet": {"digi": {...}}).

  - what it repeats: a first-hop WIDE1-1, or its own call as the next hop;
    never WIDE2-n, RELAY/WIDE/TRACE, an already-repeated WIDE1-1, its own
    packets, internet traffic or connected-mode frames
  - how the path reads after: callsign substitution, marked used
  - viscous: a held WIDE1-1 is dropped when a digipeated copy is heard, sent
    when none is; a request by name goes at once
  - duplicates: one send per packet per DUPE_S
  - the host file renders "digi" onto the hub's command line; bad blocks refused

The hub at the wire, against a fake radio, is tests/tnc_hub_test.py's
scenario_digi.

    python3 tests/digi_test.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import digi  # noqa: E402
from ax25 import Address, Frame, U_SABM  # noqa: E402
from ranch_radio import render  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


def heard(src, info=b"!4049.00N/10510.00W>test", digis=(), used=0, dest="APRS"):
    """A frame as the radio hands it over: the first `used` digis have repeated it."""
    f = Frame.decode(Frame.ui(src, dest, info, digis=digis).encode())
    for d in f.digis[:used]:
        d.bit7 = True
    return f


def path(f):
    return ",".join(str(d) + ("*" if d.bit7 else "") for d in f.digis)


def run(frames, me="N9ABC-1", viscous=5.0, until=60.0):
    """Feed (t, frame) pairs; return [(t, tnc2)] the digipeater sends."""
    d = digi.Digi(me, viscous)
    out, events = [], sorted(frames, key=lambda x: x[0])
    t, i = 0.0, 0
    while t <= until:
        while i < len(events) and events[i][0] <= t:
            d.heard(events[i][1], events[i][0])
            i += 1
        for f in d.due(t):
            out.append((round(t, 1), f"{f.src}>{f.dest},{path(f)}"))
        t = round(t + 0.1, 1)
    return out, d


me = Address.from_text("N9ABC-1")

# ---------------------------------------------------------------- the rules
check("WIDE1-1 first hop: substituted by our call, used; WIDE2-1 left for the wide digis",
      path(Frame(None, None, digi.plan(heard("KF7XYZ-9", digis=["WIDE1-1", "WIDE2-1"]), me)[0])),
      "N9ABC-1*,WIDE2-1")
check("WIDE1-1 waits (viscous)", digi.plan(heard("KF7XYZ-9", digis=["WIDE1-1", "WIDE2-1"]), me)[1], False)
check("our call as the next hop: marked used, at once",
      [path(Frame(None, None, digi.plan(heard("KF7XYZ-9", digis=["N9ABC-1", "WIDE2-1"]), me)[0])),
       digi.plan(heard("KF7XYZ-9", digis=["N9ABC-1", "WIDE2-1"]), me)[1]],
      ["N9ABC-1*,WIDE2-1", True])
check("our call after a used hop", path(Frame(None, None, digi.plan(
    heard("KF7XYZ-9", digis=["K7RDG-1", "N9ABC-1"], used=1), me)[0])), "K7RDG-1*,N9ABC-1*")
for p, used, why in ((["WIDE2-1"], 0, "WIDE2-1 (a wide digi's job)"),
                     (["WIDE2-2"], 0, "WIDE2-2"),
                     (["WIDE1-1", "WIDE2-1"], 1, "a WIDE1-1 another digi already repeated"),
                     (["K7RDG-1", "WIDE1-1"], 1, "WIDE1-1 not first"),
                     (["RELAY", "WIDE2-1"], 0, "RELAY (obsolete)"),
                     (["WIDE", "WIDE2-1"], 0, "WIDE (obsolete)"),
                     (["TRACE", "WIDE2-1"], 0, "TRACE (obsolete)"),
                     (["WIDE1-2"], 0, "WIDE1-2"),
                     (["N9ABC-2"], 0, "another SSID of our call"),
                     ([], 0, "no path")):
    check(f"not repeated: {why}", digi.plan(heard("KF7XYZ-9", digis=p, used=used), me)[0], None)
check("not repeated: our own packet", digi.plan(heard("N9ABC-1", digis=["WIDE1-1"]), me)[0], None)
check("not repeated: TCPIP in the path", digi.plan(heard("KF7XYZ-9", digis=["WIDE1-1", "TCPIP"]), me)[0], None)
sabm = Frame.decode(Frame(Address.from_text("N9ABC"), Address.from_text("KF7XYZ"),
                          [Address.from_text("WIDE1-1")], "U", utype=U_SABM).encode())
check("not repeated: connected-mode", digi.plan(sabm, me)[0], None)

# ---------------------------------------------------------------- viscous
direct = heard("KF7XYZ-9", digis=["WIDE1-1", "WIDE2-1"])
copy = heard("KF7XYZ-9", digis=["AA7JB", "WIDE2-1"], used=1)    # a wide digi's copy
out, d = run([(1.0, direct)])
check("nothing else repeats it: sent after the hold", out, [(6.0, "KF7XYZ-9>APRS,N9ABC-1*,WIDE2-1")])
out, d = run([(1.0, direct), (3.0, copy)])
check("a wide digi repeats it inside the hold: not sent", out, [])
check("  ...counted as covered", d.counts["covered"], 1)
out, _ = run([(1.0, direct), (7.0, copy)])
check("a copy after the hold is too late to cancel", out, [(6.0, "KF7XYZ-9>APRS,N9ABC-1*,WIDE2-1")])
out, _ = run([(1.0, heard("KF7XYZ-9", digis=["N9ABC-1", "WIDE2-1"]))])
check("by name: at once, no hold", out, [(1.0, "KF7XYZ-9>APRS,N9ABC-1*,WIDE2-1")])
out, _ = run([(1.0, direct)], viscous=0)
check("viscous 0: a plain fill-in", out, [(1.0, "KF7XYZ-9>APRS,N9ABC-1*,WIDE2-1")])

# ---------------------------------------------------------------- duplicates
out, d = run([(1.0, direct), (2.0, direct), (20.0, direct), (40.0, direct)])
check("the same packet again: once per 30 s", [t for t, _ in out], [6.0, 45.0])
check("  ...the repeats inside the window counted", d.counts["dupes"], 2)
other = heard("KF7XYZ-9", info=b">different", digis=["WIDE1-1", "WIDE2-1"])
out, _ = run([(1.0, direct), (2.0, other)])
check("a different packet from the same station is its own", len(out), 2)

# ---------------------------------------------------------------- host file
station = json.loads((ROOT / "examples/hosts/linux-station.json").read_text())
station["packet"].update(tx=True, digi={"call": "N9ABC-1", "viscous_s": 5, "max_per_min": 4})
argv = render.hub_argv(station, "/usr/bin/python3", "/r", lambda p: p)
check("rendered: --digi, viscous and limit on the hub's line",
      [argv[argv.index("--digi") + 1], argv[argv.index("--digi-viscous") + 1],
       argv[argv.index("--max-digi-per-min") + 1]], ["N9ABC-1", "5", "4"])


def refused(h, change):
    h = json.loads(json.dumps(h))
    change(h)
    try:
        render.validate(h)
        return False
    except render.HostError:
        return True


check("accepted: the digi block", refused(station, lambda h: None), False)
check("refused: no call", refused(station, lambda h: h["packet"]["digi"].pop("call")), True)
check("refused: a bad call", refused(station, lambda h: h["packet"]["digi"].update(call="NOT A CALL")), True)
check("refused: viscous 60 s", refused(station, lambda h: h["packet"]["digi"].update(viscous_s=60)), True)
check("refused: 100 a minute", refused(station, lambda h: h["packet"]["digi"].update(max_per_min=100)), True)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
