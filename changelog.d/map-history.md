### Added

- The Map tab has a History view: when the host file's `addons.map.history` names a page (one built
  elsewhere that covers longer than the map's hour, every medium), a History button swaps the live map
  for it, served at `/map/history`. The page is served with its own CSP sandbox and framed sandboxed,
  without `allow-same-origin`: its script runs in an opaque origin that reaches no station cookie or API,
  makes no fetches and posts no forms, so text heard over the air inside it can't act on the station.
