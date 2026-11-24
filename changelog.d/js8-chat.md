### Changed

- The JS8 tab is laid out for chatting, on the Mesh tabs' chat view: a rail
  (All stations, Talking with you with unread counts, Heard with dB, grid
  and offset), the conversation as bubbles with your own lines on the right
  (queued, on the air, sent, not sent, by the radio's own keying), frames
  still arriving shown as "receiving", and the composer pinned under it with
  HB, CQ, the queries for the open station, offset and speed. Band activity
  sits beside it.

### Fixed

- The JS8 waterfall is readable: a JS8 signal was painted dark blue on black
  (16 dB over the noise mapped to a quarter of the colours). Floor and Range
  sliders set black and white and redraw the history; each decode is boxed
  where it was on the air (offset, width for its speed, on-air time) and
  labelled with the sender; a strip names who has been on each offset in the
  last 5 minutes; hover reads offset and station; your TX offset is a bracket
  as wide as your signal; dashed lines mark the JS8 cycles. Clicking a box or
  name opens that station.
