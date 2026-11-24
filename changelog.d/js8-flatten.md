### Fixed

- The JS8 waterfall is no longer a white slab. Each 10 Hz bin is measured
  against its own recent noise (Flatten, as in JS8Call and WSJT-X), so the
  radio's filter shape drops out and signals stand above the band's noise;
  frequencies outside the radio's filter show as flat grey. Floor and Range
  start at 6 and 20 dB.
