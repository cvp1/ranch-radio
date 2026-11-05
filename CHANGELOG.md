# Changelog

All notable changes to Ranch Radio are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **MeshCore settings from the page.** The MeshCore tab's Settings form
  changes the node's name, location and location sharing, contact adding,
  extra ACKs, radio (frequency, bandwidth, spreading factor, coding rate,
  with presets: the host's `"presets"`, then USA/Canada recommended), TX
  power, and on a build with Wi-Fi, Wi-Fi (on/off, network, a write-only
  password) and the Bluetooth PIN, plus a Reboot button. Every value is
  checked against the node's schema before it is written; risky changes
  ask first. The Wi-Fi password never comes back from the node and is
  never logged or sent to a page.
- **MeshCore over Wi-Fi and Bluetooth.** The `meshcore` add-on's `"port"`
  takes `tcp:HOST:PORT` for a companion with Wi-Fi on (port 5000) and
  `ble:NAME` for one over Bluetooth, as well as a serial device; a comma
  list is tried in order (`"tcp:192.168.1.50:5000,ble:NODE"`: Wi-Fi, else
  Bluetooth). A link that goes quiet is noticed within about two minutes
  (the battery read failing twice) and the bridge reconnects.
  `tools/meshcore_wifi.py PORT` puts a companion on Wi-Fi from its USB port
  through the firmware's rescue CLI (hold the button at boot), asking for the
  password with no echo, then finds the node on the LAN. The companion's TCP
  port has no login: keep it on a network you trust.
- **A viscous fill-in digipeater in the packet hub** (`digi.py`, host file
  `"packet": {"digi": {"call": ...}}`). WIDE1-1 first hops and packets to its
  own call only, callsign substitution, a 30 s duplicate window, its own rate
  limit at the transmit gate. A WIDE1-1 packet waits `viscous_s` (5 s) and is
  dropped if a wide digipeater repeats it first: measured over three weeks of
  one station's log, 84% of local WIDE1-1 packets were already repeated by a
  wide digi, so a plain fill-in there would mostly add duplicates.
- **The hub's iGate beacons its own position to APRS-IS** (`"igate":
  {"beacon_min": 30}`), never RF, with the receive-only iGate symbol `R&`; at
  login and every `beacon_min` after, a reconnect not resetting the interval.
- **MeshCore tab (`meshcore` add-on).** A MeshCore companion node on USB
  (`companion_radio_usb` firmware) gets its own tab: the node's name, model,
  firmware, radio settings, battery and location; the nodes it has heard
  advertise, newest first, with hops and distance; channel and direct
  messages, with sending (140 bytes) and delivery ACKs for direct ones; and
  Advert / Flood advert buttons (one per 30 s). `meshcore_bridge.py` runs
  under its own venv with the `meshcore` package
  (`python3 -m venv ~/.local/share/venvs/meshcore && ~/.local/share/venvs/meshcore/bin/pip install meshcore`),
  sets the node's clock on connect and changes no radio settings. MeshCore
  and Meshtastic are separate meshes, so a host can run this next to the
  Mesh tab. Config: `"meshcore": {"port": "/dev/ttyUSB0"}` in the host's
  add-ons; off frees the port for the MeshCore apps or a flash.
- **Winlink packet sessions tune the UV-PRO for you, and put it back.** When
  a packet gateway is picked in the Winlink panel and Connect is confirmed,
  the `uvpro` add-on (control-port mode, `"control"`) puts the hub's radio on
  the gateway's frequency before Pat dials: a channel already on it (FM
  simplex, TX on; the one selected now first), else VFO A on a radio that has
  one, else the spare channel named by the new `"qsy_channel"` option,
  written for the session and written back after. Dual watch and scan go off
  for the session. Afterwards the radio goes back to the channel it was on,
  once no packet link is up on it (`"restore_wait_s"`, 30 s; 5 s while the
  server stops); a restore that fails is retried for 10 minutes and done
  first on the next connect. With nothing to tune with, or the add-on
  borrowing the radio (no `"control"`), the connect is refused, saying why,
  instead of dialing on the old channel. While the radio is tuned, page edits
  that would move it are refused and the add-on can't be switched off. With
  the add-on off nothing changes: the panel says which frequency the radio
  must be on, as before. Add-ons now stop in reverse order, so Winlink ends
  its session (and the radio is put back) before the UV-PRO add-on stops.

### Fixed

- **Winlink on a Mac's radio page reaches the packet hub.** The station page
  has always offered a packet session through the hub's radio; a Mac has no
  station page, and its radio server's Winlink panel knew only its own radio
  (ARDOP), so the UV-50PRO never appeared there and a packet URL would have
  armed the HF radio. The radio server now routes a session by its URL's
  transport as the station does: `ardop` arms this radio, `ax25*` goes
  through the Packet add-on while it reports the hub up (the hub's own
  transmit gate applies; refused, saying why, when the hub is down, the add-on
  is off or there is none), `telnet` keys nothing. The panel gets the
  station's Via selector: this radio, the hub's radio while it is up, telnet.

- **macOS: a Bluetooth packet hub opens the radio from a child process's
  main thread.** On macOS 27 `openRFCOMMChannelSync` succeeds only on a
  process's main thread; from the hub's link thread it failed with
  kIOReturnError on every dial (a Mac mini on macOS 27 and a UV-50PRO, 2026-11-04). Each
  `RfcommLink` now runs `bt_rfcomm.py --child`: the child owns the channel
  and the run loop, bytes cross its stdin/stdout, and it exits when the link
  ends. The hub's own interface is unchanged. The Serial Port channel is
  looked up by SDP at every dial, because the UV-50PRO moves it (1 after one
  power-cycle, 4 after another). Known radio behaviour, not fixed here: the
  KISS service refuses every open after a dropped link until the radio is
  power-cycled (seen on the Linux station too, 2026-11-04).

### Changed

- **The `uvpro` add-on and a Bluetooth packet hub run on macOS too.** The
  add-on reaches the radio through the packet hub's control port, which the
  hub carries over IOBluetooth on a Mac (as it has since 2026-10-14) and BlueZ
  on Linux, so `ranch-radio add uvpro` now offers itself on both. `doctor`
  checks for BlueZ only on Linux and for `IOBluetooth` (pyobjc) only on a Mac
  with a `bt:` TNC. The add-on is for the whole Benshi family: the BTECH
  UV-50PRO mobile is the UV-PRO's counterpart on the same protocol
  (HTCommander lists both), so the bridge's connect budget now allows for its
  180 channels (a UV-PRO has 32): `CONNECT_TIMEOUT_S` 40 → 240 s, the
  add-on's `READ_TIMEOUT_S` 60 → 270 s. Untested against a UV-50PRO yet.

### Added

- **A receive-only iGate in the packet hub** (`packet.igate`, `igate.py`),
  for any TNC: the UV-PRO over Bluetooth, a serial or network KISS TNC, or
  Direwolf. Heard packets go to APRS-IS with `qAR` and the iGate's login,
  once per packet (not per digipeater copy); TCPIP, TCPXX, NOGATE and
  RFONLY paths and queries are skipped, and third-party packets are
  unwrapped. Nothing from APRS-IS reaches the radio. The passcode is read
  from a file. The renderer refuses a host file with both this and
  `packet.direwolf.igate`. `api/status` gains `"igate"`.
  `tests/igate_test.py` and `tests/tnc_hub_test.py` (scenario_igate,
  against a fake APRS-IS server).

- **Next openings on the Propagation tab.** From a skywatch InfluxDB that has
  its propagation forecast (`skywatch_forecast_window`): each band and region
  forecast open in the next hours (`"forecast_h"`, default 12), soonest first,
  the one open now marked, with its best chance and the forecast's age. Only
  cells the newest forecast wrote count, so a withdrawn opening never shows.
  No forecast measurement, no section; unreachable reads unknown. The
  Overview card names the next opening. `tests/propagation_page_test.py`
  checks the tab in a browser.

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
