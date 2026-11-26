### Fixed

- Imaging: weather fax off the air. Real start tones (NMC Pt. Reyes, 12786 kHz)
  carry far less of their signal in the 300 Hz line than a clean test tone, so
  no chart ever started; now they do, and band noise and chart content still
  start nothing. NMC's phasing pulse is dim grey, not white, so charts now
  line up on it and its lines are left off. A chart is no longer slanted by a
  clock error misread from phasing (it read -158 ppm on a square chart); set
  `clock_ppm` if your sound card needs it.
