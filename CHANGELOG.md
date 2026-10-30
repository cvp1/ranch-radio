# Changelog

All notable changes to Ranch Radio are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] - 2026-10-30

The first release. Updates still follow `main`; this tag marks a state the
full test suite passed on, to point people at and to come back to.

### Added

- **README screenshots**: the radio page, the Propagation tab, a phone and a
  station page, in `docs/images/`, retaken by `tools/readme_screenshots.py`
  from a simulated radio with made-up data. The README now covers updates
  holding off while JTDX or WSJT-X is connected (`--drop-rigctl`).
- **Updates leave a radio alone while JTDX or WSJT-X is connected.** A
  connected rigctl client now counts as busy, like transmitting or a Winlink
  session: a restart dropped its rig control, and JTDX waits on an error
  dialog instead of reconnecting. The update names the reason and holds the
  restart for the next update. `update --apply --drop-rigctl` restarts anyway
  (never while transmitting or in a Winlink session). The radio server's
  hello message reports `rigctl_clients`.
- **Propagation add-on** (`propagation`). Under the radio page's band buttons:
  the K index, GOES X-ray flare class and 10.7 cm solar flux from NOAA's Space
  Weather Prediction Center, and a heat badge on each band heard in the last
  hour. A Propagation tab shows the same per band: decodes, best SNR, farthest,
  regions and which receivers heard it (from a skywatch InfluxDB, optional),
  and who heard your callsign (PSKReporter, optional, every 10 minutes). No
  decodes reads "no data", never closed. `tests/propagation_addon_test.py`.
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
- **A daily update check** (`updates` module, on by default): a launchd agent
  or systemd timer runs `ranch-radio update --scheduled`, which notifies once
  per new commit, or applies it with `"updates": {"apply": true}`.
- **`ranch-radio update --check`**: what's new upstream, and which radios still
  run old code. Exits 2 when there is something to do.
- **Update health check and rollback.** After an update restarts the servers,
  each one that was serving must serve again. If one doesn't, the checkout
  returns to the previous commit, the old code is restarted, and the failed
  commit is skipped until a newer one lands. New code that crashes before it
  can check itself is rolled back by the code that pulled it. State, including
  the commit the servers were last restarted on, is kept in
  `~/.local/state/ranch-radio/update.json`. One update runs at a time.
- `tests/update_test.py`: the update end to end against a real git origin.
- **Direwolf as the packet hub's modem** (`"packet": {"tnc": "direwolf"}`), on
  macOS and Linux, for a radio on a USB sound card cable such as an AIOC.
  `direwolf_tnc.py` waits for the cable by USB ID and runs Direwolf with no
  network ports, talking to the hub over a pseudo-terminal. It restarts
  Direwolf when the cable drops off USB. PTT stays off unless the host file
  says `"ptt": "serial"`.
- **A receive-only iGate** (`packet.direwolf.igate`) sends received packets
  to APRS-IS and never transmits from it. The passcode is read from a file.
- **A Packet tab on a Mac's radio page** (`addons/packet.py`). A Mac has no
  station page, so its radio server shows the hub through the same
  path-checked proxy the station uses (now shared in `webguard.py`). It is on
  whenever the host file has a packet block and a radio.
- **The hub page's heading** comes from the host file's `packet.title`
  (`tnc_hub.py --title`) instead of always "UV-PRO Packet".
- **The packet hub renders on macOS** as a launchd agent, alongside Direwolf.
  `examples/hosts/mac-aioc.json` and `tests/direwolf_tnc_test.py`.

### Fixed

- **An add-on panel could stay out of date until a reload.** Messages for
  an add-on that arrived before its page script loaded were dropped, so the
  panel showed only the first snapshot: a UV-PRO that connected while the
  page opened showed no battery and no channels. They are now kept (up to
  500) and replayed after the snapshot; a fresh snapshot supersedes them.
  `tests/addons_page_test.py`.
- **A refused Winlink arm is logged** (`winlink: arm refused: <why>`), not
  only shown on the page that asked.
- **Flaky test: arming Winlink right after an app unkeys.**
  `civ_safety_test` armed as soon as the server first said "not
  transmitting", and a poll reply from before the unkey could land after it
  (about 1 run in 15 on one CPU). It now waits for a reply from after the
  unkey and fails loudly if there isn't one.
- **`ranch-radio doctor` printed an unpinned benlink fix.** Its install
  pinned the benlink commit the UV-PRO bridge is tested against, but the fix
  it printed for doing it by hand ran plain `pip install benlink`, which pulls
  a release the bridge can't parse. Both use the pin now, and
  `tests/modules_test.py` checks that CI and the flake-hunt image agree.
- **Direwolf's key-up delay is a host-file setting** (`packet.direwolf.txdelay`, ms).
  At Direwolf's 300 ms default, a BTECH UV-50X2 keyed too late for its
  packets to decode. At 600 ms HELIO repeated them.

- **Linux units with a `~` Python path** (what `ranch-radio init` writes)
  start. systemd doesn't expand `~`, so every rendered unit was refused. The
  path now renders as `%h`.
- **Installing a new Mac agent** no longer prints a failed `launchctl bootout`
  first.

### Changed

- **`ranch-radio update` without `--apply` pulls nothing.** It used to pull the
  code under the running servers, which import parts of it lazily.
- **`update` installs `requirements.txt`** into the checkout's `.venv`. With
  any other Python it stops before restarting anything.
- **Radios skipped as busy are restarted at the next update**, instead of
  running old code until someone remembers.
- **On a Mac, a restart leaves a stopped radio stopped.** A radio switched off
  from the widget is no longer started by an update, which matches systemd's
  `try-restart` on Linux.

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
