"""Read the text off an OpenGD77 radio's screen picture.

The radio draws text with fixed-width bitmap fonts (opengd77_font.py, taken
from the firmware): each glyph is a w x h cell, foreground pixels only. So
the text in a screen picture can be read exactly, not guessed: every cell
of the picture is compared with every glyph of every font, and a line of
text is a run of matching cells side by side on one baseline.

What the page shows as "on the radio now" comes from this, because the
picture is the only live view of the radio's state over USB: the zone,
channel and master power it saves to flash are written only at power-off
or when a programming tool asks (MD-UV380 firmware R20260131: settings.c
settingsSaveIfNeeded is RD5R-only; batteryAndPowerManagement.c, usb_com.c).
"""

import re
from collections import Counter

import opengd77_font

W, H = 160, 128


def pixels(raw, w=W, h=H):
    """The framebuffer (big-endian 16-bit pixels) as a list of ints."""
    return [(raw[2 * i] << 8) | raw[2 * i + 1] for i in range(w * h)]


def _glyph_columns(data, w, h):
    cols = []
    for x in range(w):
        v = 0
        for page in range((h + 7) // 8):
            v |= data[x + page * w] << (8 * page)
        cols.append(v & ((1 << h) - 1))
    return tuple(cols)


_TABLES = None


def _tables():
    global _TABLES
    if _TABLES is None:
        _TABLES = []
        for name, (w, h, glyphs) in opengd77_font.FONTS.items():
            table = {}
            for code, data in glyphs.items():
                cols = _glyph_columns(data, w, h)
                if any(cols) and 32 < code < 127:          # printable ASCII; no blank glyphs
                    table.setdefault(cols, chr(code))
            _TABLES.append((name, w, h, table))
    return _TABLES


def lines(px, w=W, h=H):
    """Text lines on the screen: [{"text", "x", "y", "font"}], top to bottom.
    A pixel is foreground when it isn't the background colour (the commonest)."""
    bg = Counter(px).most_common(1)[0][0]
    col = [0] * w                                       # column x as a bit per row
    for y in range(h):
        row = px[y * w:(y + 1) * w]
        for x in range(w):
            if row[x] != bg:
                col[x] |= 1 << y
    found = []
    for name, fw, fh, table in _tables():
        mask = (1 << fh) - 1
        for y in range(h - fh + 1):
            win = [(c >> y) & mask for c in col]
            hits = {}
            for x in range(w - fw + 1):
                ch = table.get(tuple(win[x:x + fw]))
                if ch:
                    hits[x] = ch
            for x0 in sorted(hits):
                if x0 not in hits or (x0 - fw in hits):
                    continue                            # not the start of a run
                text, x = "", x0
                while True:
                    if x in hits:
                        text += hits.pop(x)
                    elif x + fw in hits and not any(win[x:x + fw]):
                        text += " "                     # one blank cell inside a line
                    else:
                        break
                    x += fw
                if len(text.replace(" ", "")) >= 2:
                    found.append({"text": text, "x": x0, "y": y, "font": name, "w": fw, "h": fh})
    # A small font can match inside a big glyph by chance: keep a line only if
    # no line with more lit cells covers it.
    def box(t):
        return t["x"], t["y"], t["x"] + len(t["text"]) * t["w"], t["y"] + t["h"]

    def inside(a, b):
        ax0, ay0, ax1, ay1 = box(a)
        bx0, by0, bx1, by1 = box(b)
        return ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1
    found.sort(key=lambda t: -(len(t["text"].replace(" ", "")) * t["w"] * t["h"]))
    kept = []
    for t in found:
        if not any(inside(t, k) for k in kept):
            kept.append(t)
    kept.sort(key=lambda t: (t["y"], t["x"]))
    return [{k: t[k] for k in ("text", "x", "y", "font")} for t in kept]


_ZONE_LINE = re.compile(r"^(?P<zone>.+?) Ch:(?P<n>\d+)$")
_POWER = re.compile(r"^(\d+(?:\.\d+)?(?:mW|W)|\+W-)$")


def state(px, layout=None):
    """What the screen says the radio is on. Channel mode is recognised by
    its bottom line, "<zone> Ch:<n>"; then the channel is looked up in the
    radio's own zone table (layout from opengd77.OpenGD77.layout) and must
    also be one of the lines on the screen. Returns a dict with "mode"
    ("channel", or "unknown" for VFO mode, menus, a call screen...), and
    for channel mode zone, number, channel name and slot, plus power and
    the top bar's words, and "lines" (all text read)."""
    ls = lines(px)
    out = {"mode": "unknown", "lines": [l["text"] for l in ls]}
    top = [l for l in ls if l["y"] < 12]
    words = " ".join(l["text"] for l in top).split()
    out["bar"] = words
    out["power"] = next((t for t in words if _POWER.match(t)), None)
    for l in reversed(ls):
        m = _ZONE_LINE.match(l["text"].strip())
        if not m:
            continue
        zone, n = m.group("zone").strip(), int(m.group("n"))
        out.update(mode="channel", zone=zone, number=n)
        if layout:
            names = layout.get("names") or {}
            zones = {z["name"]: z["slots"] for z in layout.get("zones") or []}
            slot = None
            if zone in zones and 0 < n <= len(zones[zone]):
                slot = zones[zone][n - 1]
            elif zone not in zones and n in names:          # "All Channels": Ch:n is the slot
                slot = n
            name = names.get(slot)
            shown = any(name and l["text"].strip() == name for l in ls)
            out.update(slot=slot, channel=name, confirmed=bool(shown))
        break
    return out
