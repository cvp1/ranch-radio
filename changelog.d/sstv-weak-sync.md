### Fixed

- Imaging: SSTV off the air. A weak picture whose sync pulses don't show
  line by line is no longer dropped after a few lines: a picture now runs its
  mode's full length (Stop ends it sooner) and is timed at the end from all
  its lines folded together, which also corrects a sending station's clock up
  to ±3000 ppm. First seen on 14.230: a Martin M1 from a station running
  +1460 ppm was dropped after 3 lines; it now decodes whole and straight.
