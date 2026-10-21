"""Reading the MD-UV390's screen (opengd77_screen.py): text drawn with the
firmware's own fonts, and a real screen picture from the radio.

    python3 tests/opengd77_screen_test.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import opengd77_screen as s  # noqa: E402
from fake_opengd77 import channel_screen, render_screen  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


LAYOUT = {"slots": [1, 2, 3], "names": {1: "SV DMR", 2: "SV GMRS", 3: "MTLM DMR"},
          "zones": [{"name": "AZ SV DMR", "slots": [1, 3]}, {"name": "GMRS", "slots": [2]}]}

px = s.pixels(channel_screen("MTLM DMR", "AZ SV DMR Ch:2"))
check("lines: every font, top to bottom", [l["text"] for l in s.lines(px)],
      ["DMR TS1 3W C1", "Local", "MTLM DMR", "AZ SV DMR Ch:2"])
st = s.state(px, LAYOUT)
check("channel mode: zone, number, the radio's channel in that zone",
      (st["mode"], st["zone"], st["number"], st["slot"], st["channel"], st["confirmed"]),
      ("channel", "AZ SV DMR", 2, 3, "MTLM DMR", True))
check("power and mode from the top bar", (st["power"], st["bar"][0]), ("3W", "DMR"))
st = s.state(s.pixels(channel_screen("SV GMRS", "All Channels Ch:2", bar="FM 750mW")), LAYOUT)
check("All Channels: Ch:n is the slot", (st["zone"], st["slot"], st["channel"], st["power"]),
      ("All Channels", 2, "SV GMRS", "750mW"))
st = s.state(s.pixels(channel_screen("SV DMR", "AZ SV DMR Ch:2")), LAYOUT)
check("a name the zone doesn't give is not confirmed", (st["channel"], st["confirmed"]), ("MTLM DMR", False))
st = s.state(s.pixels(render_screen([(2, 2, "font_6x8", "FM 3W"), (8, 40, "font_8x16", "146.52000"),
                                     (8, 80, "font_8x16", "446.00000")])), LAYOUT)
check("VFO mode (no zone line): unknown, with the text", (st["mode"], st["lines"][1:]),
      ("unknown", ["146.52000", "446.00000"]))
check("a blank screen: nothing read", s.state(s.pixels(render_screen([])))["lines"], [])
check("white text on a dark theme reads the same",
      [l["text"] for l in s.lines(s.pixels(render_screen([(2, 114, "font_6x8", "GMRS Ch:1")], fg=0xFFFF, bg=0x0000)))],
      ["GMRS Ch:1"])

# The real radio, mac-shack, 2026-10-17 12:57: channel mode, SV DMR, All Channels.
with open(os.path.join(ROOT, "tests/fixtures/opengd77-screen-all-channels.bin"), "rb") as f:
    real = s.pixels(f.read())
st = s.state(real, {"slots": list(range(1, 32)), "names": {1: "SV DMR"}, "zones": [{"name": "AZ SV DMR", "slots": [1]}]})
check("real screen: every line", st["lines"], ["DMR", "cS1", "10W", "C1", "100%", "Local", "SV DMR", "All Channels Ch:1"])
check("real screen: All Channels, Ch 1, SV DMR, 10W",
      (st["mode"], st["zone"], st["number"], st["channel"], st["confirmed"], st["power"]),
      ("channel", "All Channels", 1, "SV DMR", True, "10W"))

print(f"\n{'PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
