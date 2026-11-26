"""A callsign as Morse, rendered as keyed-tone PCM: the station's own CW ID
for Winlink over Mercury HF (plan winlink-mercury M0). Pure: no I/O.

Mercury sends no station ID (its CWID ON answers OK and sends nothing), so the
radio server sends one itself, through the radio's TX audio at the Winlink
session's codec level: a keyed tone in USB-D (J2A), the method xara and
ardopcf use on these stations. 47 CFR 97.119(b)(1) caps an automatic CW ID at
20 words per minute: render() refuses anything faster.

Timing is PARIS (a dit is 1.2 / WPM seconds; dah 3, gap inside a character 1,
between characters 3, between words 7). Each element starts and ends with a
raised-cosine edge (5 ms) inside its own length, so the keying clicks less and
the total length is exactly the Morse length. Samples are placed from the
running time, so a long call does not drift by rounding.

The tone's peak is digital full scale: the drive is set by the codec level
the Winlink session already holds (codec_level, --winlink-codec-db), as it is
for the modems' own audio. Not yet measured against ALC on a real radio
(plan section 5 dummy-load trial).
"""
import array
import math

DEFAULT_WPM = 18
MAX_WPM = 20                 # 47 CFR 97.119(b)(1)
TONE_HZ = 750
RATE = 16_000                # audio.py's RATE: s16le mono
EDGE_MS = 5.0
PEAK = 32767

MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.",
    "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.",
    "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-",
    "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-", "5": ".....",
    "6": "-....", "7": "--...", "8": "---..", "9": "----.", "/": "-..-.",
}


MAX_CALL_LEN = 12


def check_call(call):
    """The callsign as sent (upper case), or ValueError naming what is wrong.
    Letters, digits and '/', 3 to 12 characters, at least one digit."""
    c = str(call or "").strip().upper()
    if not 3 <= len(c) <= MAX_CALL_LEN:
        raise ValueError(f"callsign {call!r}: 3 to {MAX_CALL_LEN} characters")
    bad = sorted({ch for ch in c if ch not in MORSE})
    if bad:
        raise ValueError(f"callsign {call!r}: no Morse for {''.join(bad)!r}")
    if not any(ch.isdigit() for ch in c):
        raise ValueError(f"callsign {call!r}: needs a digit")
    return c


def _check_wpm(wpm):
    if not 0 < wpm <= MAX_WPM:
        raise ValueError(f"CW ID speed {wpm} WPM: must be above 0 and at most {MAX_WPM} "
                         "(47 CFR 97.119(b)(1))")


def elements(call):
    """[(on, units)] for the call: key-down and key-up stretches in dit units,
    no trailing gap."""
    out = []
    for i, ch in enumerate(check_call(call)):
        if i:
            out.append((False, 3))
        for j, e in enumerate(MORSE[ch]):
            if j:
                out.append((False, 1))
            out.append((True, 1 if e == "." else 3))
    return out


def units(call):
    return sum(n for _, n in elements(call))


def duration_s(call, wpm=DEFAULT_WPM):
    _check_wpm(wpm)
    return units(call) * 1.2 / wpm


def _char_units(ch):
    code = MORSE[ch]
    return sum(1 if e == "." else 3 for e in code) + len(code) - 1


def longest_call():
    """The longest-sounding call check_call accepts: its longest character
    (by dit units; "0", a digit, so the call is valid) MAX_CALL_LEN times.
    Timing budgets take this, not a guess."""
    ch = max(MORSE, key=_char_units)
    return check_call(ch * MAX_CALL_LEN)


def longest_s(wpm=DEFAULT_WPM):
    return duration_s(longest_call(), wpm)


def render(call, wpm=DEFAULT_WPM, tone_hz=TONE_HZ, rate=RATE, edge_ms=EDGE_MS, peak=PEAK):
    """s16le mono PCM of the call keyed at wpm (ValueError above MAX_WPM)."""
    _check_wpm(wpm)
    if not 0 < tone_hz < rate / 2:
        raise ValueError(f"tone {tone_hz} Hz: must be below {rate / 2:g} Hz")
    dit = 1.2 / wpm
    edge = max(1, round(edge_ms / 1000 * rate))
    els = elements(call)
    total = round(sum(n for _, n in els) * dit * rate)
    out = array.array("h", bytes(2 * total))
    w = 2 * math.pi * tone_hz / rate
    t = 0.0
    for on, n in els:
        a, t = round(t * rate), t + n * dit
        b = round(t * rate)
        if not on:
            continue
        ln = b - a
        e = min(edge, ln // 2)
        for k in range(ln):
            if k < e:
                g = 0.5 - 0.5 * math.cos(math.pi * k / e)
            elif k >= ln - e:
                g = 0.5 - 0.5 * math.cos(math.pi * (ln - 1 - k) / e)
            else:
                g = 1.0
            out[a + k] = int(round(peak * g * math.sin(w * (a + k))))
    return out.tobytes()
