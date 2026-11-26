### Added

- Imaging: SSTV pictures whose VIS header never came through (too weak, or
  tuned in after it) are still found. Every 20 s the decoder folds the last
  picture's length of audio at each mode's line period, on a worker thread,
  and keeps what passes as `…-noheader.png`. On 14.230 it found two such
  pictures in 30 minutes; in 95 minutes of band noise and weather fax it
  found nothing. Off with `"sstv_scan": false`.
