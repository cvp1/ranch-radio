### Added

- Imaging add-on: SSTV pictures and HF weather-fax charts, decoded from a
  radio's receive audio. SSTV in Martin M1/M2, Scottie S1/S2/DX, Robot 36/72
  and PD 90/120/180 (the ISS's mode), started by the picture's VIS header;
  fax at 60/90/120/240 LPM, IOC 576 or 288, started by the station's start
  tone and lined up from its phasing lines, or started by hand when joined
  mid-chart. SSTV measures a sound card's clock error from its sync pulses,
  so pictures aren't slanted; fax takes it from `clock_ppm` (0 by default). The picture coming in shows as it arrives; finished ones are kept
  as PNGs (the newest 100 by default), each named for its time, mode and
  dial. Receive only: it listens to the radio's audio as a page does, never
  tunes or keys it, and decodes nothing while the radio transmits. Needs
  numpy in the station's Python (`ranch-radio doctor --fix`).
