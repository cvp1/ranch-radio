# Changelog

All notable changes to Ranch Radio are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Network KISS TNC packet hub.** `"packet": {"tnc": "tcp:host:port"}` in a
  host file renders a `packet-hub.service` that talks to a KISS-over-TCP TNC
  such as Direwolf, with no Bluetooth, BlueZ or UV-PRO dependency. The hub's
  configured name shows on the station page and in the Winlink via list. It
  reconnects by itself if the TNC goes away, retrying after 2, 4, 8, 16, then
  every 30 seconds.
- **Radio-less stations.** `station.rigctl_port: 0` turns off the shared rigctl
  port. The station then has no primary radio, and the page hides the
  shared-port text. A host with no radios can run the packet hub on its own.
- **WirePlumber rules** rendered from each radio's `audio_in` and `audio_out`
  node names, for stable sound card naming and session priority.
- **Docs and examples** for the network KISS TNC: a README section,
  `examples/direwolf/direwolf.conf` (every transmitting line commented out) and
  `examples/hosts/linux-direwolf.json`.
- `tests/packet_hub_station_test.py`, covering the above with the example
  host files.

### Changed

- **Python 3.12 or newer is now required** (README and installer gate). CI only
  tests 3.14, and nothing in the source needed the previously stated floor.
- `aiohttp` must be 3.14.4 or newer.
- **Packet hub name on the station page.** The page shows the host file's
  `packet.name` (for example `Packet (Direwolf)`). A network TNC with no name
  shows `KISS TNC`. A host with no name and a Bluetooth TNC keeps the `UV-PRO`
  label it always had, so existing setups look the same.
- `station.rigctl_port` is validated against the radios' own ports, and the
  station fails with a clear message if the shared port can't bind.
- Safe `ruff` auto-fixes across the code (import order, `TimeoutError`,
  `re.IGNORECASE`, `removeprefix` and `removesuffix`). No behavior change.

### Security

- `aprs_store` refuses to update a station column that is not in
  `STATION_COLS`. Every current caller already complied, so nothing changes in
  practice. The two SQL strings built with f-strings now say why they are safe
  (identifiers come from module constants, values stay bound).
