### Fixed

- Switching an add-on off and back on quickly, say from two pages, no longer leaves it showing On with no engine. The registry runs one on/off at a time per add-on, through the state write and broadcast. Mesh's stop awaited its bridge's exit and then cleared the engine an overlapping start had just made.
- A rollback after an update that died and was retried now puts the original file back. The journal kept the first attempt's backup path even when that attempt died before moving the file there, and the rollback skipped it and left the new file in place.
