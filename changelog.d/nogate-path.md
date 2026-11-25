### Changed

- `packet.aprs.path` may end in `NOGATE` or `RFONLY` (still at most two hops before it), so
  messages stay off APRS-IS and a reply proves the RF path; both the host-file check and the
  hub accept it.
