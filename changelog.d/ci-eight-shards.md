### Changed

- CI runs the suite on eight shards instead of four, and the Python 3.12 and
  macOS lanes in two halves each: a pull request waits ~7 min instead of ~14.
  Shard timings refreshed from main.

### Fixed

- Test ports come from below the OS's ephemeral range, so another process's
  outbound socket can no longer take a port between the pick and the server's
  bind (a rigctl listener lost 50067 that way, 2026-11-26).
- The Python 3.12 and macOS lanes isolate each test as the shards do: scratch
  XDG dirs as well as HOME, loopback servers by default.
