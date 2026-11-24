### Fixed

- A STOP TX that isn't on disk can no longer be cleared by `update` or `restart`. `stop_saved`, which both trust, said only that a STOP TX file was configured. So a STOP TX sent with the shutdown flag (by any page, read-only included), or one whose save failed, still read as saved, and a restart came back able to transmit. It now says whether the file holds the latch, and the existing guards hold the restart.
- `update --apply` no longer restarts a running radio that accepts a connection but never answers: it may be hung while keyed. It is left on the old code and held for the next update, as `ranch-radio restart` already does. A radio with nothing listening (crashed, or crash-looping on bad code) is still restarted, so an update can still fix it. A rollback keeps the lenient check.
- The idle probe waits for the radio's hello, skipping a scope frame or state message that comes first. It also reports an ATU tune, which is on the air with TX still off, so neither `update` nor `restart` interrupts one.
- STOP TX drops a key-down or tune start still queued for the CI-V port, so it can't reach the radio after the latch (it was unkeyed ~0.1 s later, a blip on the air).
