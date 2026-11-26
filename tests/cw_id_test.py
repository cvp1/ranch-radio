"""cw_id.py: the station's CW ID as keyed-tone PCM (plan winlink-mercury M0).
Pure; nothing here plays or transmits.

  - PARIS timing at 18 WPM: a dit is 1.2/18 s; N0CALL is 73 units, its PCM
    exactly that long, each key-down and key-up stretch where Morse puts it
  - 20 WPM renders; 21 WPM, 0 and a negative speed are refused (97.119(b)(1))
  - 750 Hz: the zero crossings of a dah's steady part
  - 5 ms raised-cosine edges: an element starts and ends at zero, reaches full
    scale only after 5 ms, rises monotonically, never clips
  - callsigns: case, '/', and refusals (no digit, unknown character, length)

    python3 tests/cw_id_test.py
"""
import array
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cw_id  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:66} got {got!r}" + ("" if ok else f" want {want!r}"))


def refused(f, *a, **k):
    try:
        f(*a, **k)
    except ValueError as e:
        return str(e)
    return None


RATE = cw_id.RATE
dit = 1.2 / 18
# N -. (5) 0 ----- (19) C -.-. (11) A .- (5) L .-.. (9) L (9) + 5 gaps of 3 = 73
check("N0CALL: 73 dit units", cw_id.units("N0CALL"), 73)
check("18 WPM default; 20 WPM ceiling", (cw_id.DEFAULT_WPM, cw_id.MAX_WPM), (18, 20))
check("N0CALL at 18 WPM: 73 x 1.2/18 s", round(cw_id.duration_s("N0CALL"), 6), round(73 * dit, 6))
pcm = array.array("h", cw_id.render("N0CALL"))
check("N0CALL PCM length is the Morse length to the sample", len(pcm), round(73 * dit * RATE))

# key-down stretches from the PCM: every sample inside an element is nonzero
# somewhere; the gaps are exactly zero. Compare against the element table.
t, want_on = 0.0, []
for on, n in cw_id.elements("N0CALL"):
    a, t = round(t * RATE), t + n * dit
    if on:
        want_on.append((a, round(t * RATE)))
runs, i = [], 0
while i < len(pcm):
    if pcm[i] != 0 or (i + 1 < len(pcm) and pcm[i + 1] != 0):
        j = i
        while j < len(pcm) and any(pcm[k] != 0 for k in range(j, min(j + 3, len(pcm)))):
            j += 1
        runs.append((i, j))
        i = j
    else:
        i += 1
check("N0CALL: 21 key-down elements (2+5+4+2+4+4)", (len(runs), len(want_on)), (21, 21))
check("each element starts within 1 sample of where Morse puts it",
      all(abs(r[0] - w[0]) <= 2 for r, w in zip(runs, want_on)), True)
check("each element ends within 1 sample of where Morse puts it",
      all(abs(r[1] - w[1]) <= 3 for r, w in zip(runs, want_on)), True)
gaps_zero = all(pcm[k] == 0 for (_, b), (c, _) in zip(want_on, want_on[1:]) for k in range(b, c))
check("the gaps between elements are silent", gaps_zero, True)

check("20 WPM renders", len(cw_id.render("N0CALL", 20)) > 0, True)
check("21 WPM refused (97.119(b)(1))", bool(refused(cw_id.render, "N0CALL", 21)), True)
check("21 WPM refused for the duration too", bool(refused(cw_id.duration_s, "N0CALL", 21)), True)
check("0 WPM refused", bool(refused(cw_id.render, "N0CALL", 0)), True)
check("negative WPM refused", bool(refused(cw_id.render, "N0CALL", -5)), True)

# 750 Hz: the second dah of "0" (element 3), its steady middle
a, b = want_on[2]
e = round(cw_id.EDGE_MS / 1000 * RATE)
mid = pcm[a + e:b - e]
cross = sum(1 for x, y in zip(mid, mid[1:]) if (x < 0) != (y < 0))
hz = cross / 2 / (len(mid) / RATE)
check("tone is 750 Hz (+-10)", abs(hz - 750) <= 10, True)

# edges: element 1 (N's dah)
a, b = want_on[0]
el = pcm[a:b]
env = [max(abs(v) for v in el[k:k + 11]) for k in range(0, len(el) - 11, 11)]
check("element starts at zero", el[0], 0)
check("element ends near zero (< 2% FS)", abs(el[-1]) < 0.02 * 32767, True)
check("under 50% FS in the first 2 ms (rising edge)", max(abs(v) for v in el[:round(0.002 * RATE)]) < 0.5 * 32767, True)
check("full scale only after the 5 ms edge", max(abs(v) for v in el[e + 20:e + 80]) > 0.95 * 32767, True)
rise = env[:e // 11]
check("the rising edge is monotonic", all(x <= y + 50 for x, y in zip(rise, rise[1:])), True)
check("never clips", max(abs(v) for v in pcm) <= 32767, True)

check("lower case is sent upper case", cw_id.check_call("n0call"), "N0CALL")
check("a portable '/' call renders", len(cw_id.render("N0CALL/7")) > len(cw_id.render("N0CALL")), True)
check("no digit refused", bool(refused(cw_id.check_call, "NOCALL")), True)
check("unknown character refused", bool(refused(cw_id.check_call, "N0-CALL")), True)
check("too short refused", bool(refused(cw_id.check_call, "N0")), True)
check("too long refused", bool(refused(cw_id.check_call, "N0CALLN0CALL1")), True)
check("empty refused", bool(refused(cw_id.check_call, "")), True)
print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
