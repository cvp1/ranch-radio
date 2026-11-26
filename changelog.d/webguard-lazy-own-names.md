### Fixed

- The station, hub and server no longer wait on a reverse DNS lookup when they
  start: webguard looks up the machine's own name only when a request names a
  host that needs it. A resolver that never answers held every start ~35 s.

### Changed

- CI's macOS lane is one job again (~2 min of tests, was ~8 split in two).
