# Station tools (mac-shack)

- `codecvol/codecvol.c`: read or set the output level of a CoreAudio device by
  name; prints the level in dB. Reading is harmless:
  `codecvol "USB Audio CODEC"`. Build:
  `cc -O2 -o codecvol codecvol.c -framework CoreAudio -framework CoreFoundation`

- `ardop-tools/tonetest.py`: **TRANSMITS.** Keys the IC-7300 through Radio
  Remote's rigctl for ardopcf's TWOTONETEST (hard cap 8 s) and logs PO / ALC /
  SWR, for setting the codec level. Run only with the operator at the radio and his go
  for that test. Used 2026-10-14 to find -38 dB on 80 m (ALC 0.03, SWR 1.0).
