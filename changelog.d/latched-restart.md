### Changed

- `ranch-radio restart` and `update` now restart a radio with STOP TX on when the radio saves it (`--stop-state`, which every rendered radio has): it comes back stopped. A latch held only in memory (a hand-run server, or a radio still on older code) still holds the restart back.

### Fixed

- A routine `ranch-radio stop` or `restart` no longer leaves the radio stopped when it starts again. The STOP TX sent just before the units go down was being saved like the operator's; it is now held in memory only, and a STOP TX the operator saved is left as it was.
