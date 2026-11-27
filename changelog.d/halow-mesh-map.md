### Added

- The HaLow tab has a map: the bridge and every station it has had joined, each at its place from the host
  file (`position`, `places`; one with no place is drawn beside the bridge and says so). Beside each name, the
  dBm the bridge hears it at; each line to the bridge says which way the link is proven, in the Mesh map's
  colours (green it hears the bridge, blue the bridge hears it, purple both). A station that has left stays,
  fading, from a bounded list kept across restarts (`halow-seen.json`).
- The Mesh map's own node says how it is being received: "heard by N · best +X dB", from the nodes whose
  NeighborInfo or traceroute shows they hear it direct; its card lists each.

### Changed

- HaLow sits under Mesh in the tab bar, beside Meshtastic and MeshCore, instead of under Tools.
