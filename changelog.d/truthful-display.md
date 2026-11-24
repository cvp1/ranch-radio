### Fixed

- The Shack no longer says "Nothing on the air" for a radio whose state it doesn't know. Before its server's first message, or after the connection drops, it reads "Not known for <radio>" and redraws at once on a disconnect, rather than keeping the last TX state.
- A module that failed to load keeps its card on the Shack, with the reason; it was folded into the Offline chip (#193) and the reason was lost. The Offline chip links each item to its own tab; a radio with no tab is plain text.
- The radio page and the Propagation tab say "waiting" until skywatch and PSKReporter first answer. They showed "no decodes", "heard you: none" and "nobody" before any answer came.

### Changed

- "new DXCC or grid" is now "needed for DXCC or grids". The count includes a worked entity or grid that is new on this band or in this mode, which "new" overstated.
