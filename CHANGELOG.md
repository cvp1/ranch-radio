# Changelog

All notable changes to Ranch Radio are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- **One station on every host.** A Mac now runs the station (`station.py`)
  in front of its radio, as Linux always has: the page at 7300 with
  Winlink, Mesh, the packet tab, the add-ons and the Modules tab is the
  station's, and the radio's own server is behind it on loopback (7301).
  Pat, Mesh and the add-ons are no longer flags on the radio's agent.
  **Host file:** add `"station": {"https": true, "tls_names": [...]}`, give
  the radio `"web": 7301`, and move `https` and `tls_names` off the radio;
  `ranch-radio install` says exactly this until it is done. A station of
  one radio opens on that radio. The shared rigctl port stays off on a Mac
  unless the host file sets `station.rigctl_port`: JTDX keeps the radio's
  own port. ardopcf stays a launchd agent on a Mac; the station only checks
  it is there. VARA HF sessions arm the radio's lane through the station,
  which checks Pat's varahf settings as the radio's server did. One radio
  per Mac is no longer a limit.
- **The radio's server is radio-only.** `server.py` no longer takes
  `--winlink-pat*`, `--winlink-listen*`, `--winlink-session-max`, `--mesh-*`
  or `--host-file`: Pat, listen windows, Mesh and the Modules tab are the
  station's on every host, and the station arms a radio's lane for a session
  over the radio's own WebSocket (the radio settles on the session's
  frequency and finishes any ATU tune before it reports armed, as before).
  Between sessions the station's Winlink panel shows the radio's own lane
  state: codec level and station-ID ledger, through a standing read-only
  watch on each radio. The station page offers one via per HF modem a
  radio has (ARDOP, VARA HF), and the radio's own page no longer shows a
  Winlink panel or Modules tab. `--addons` stays: panel add-ons may run on
  a radio's own page.

### Added

- **Developer tools, for working on Ranch Radio itself.** `CLAUDE.md` is the
  map of the tree and the rules that bite (the two servers, no transmitting
  in tests, nothing personal in the repo), for anyone or any agent starting
  cold. `python3 tests/affected.py` names the test runs a change touches,
  from the import graph and the files each test spawns, and `--run` runs
  just those (`RR_ONLY` in `tests/run_all.sh`). `ranch-radio new-addon NAME`
  writes a new add-on's module, page script and test, and prints the catalog
  and manifest entries to add; the test fails until they are.
- **A Logbook tab beside Propagation.** The `logbook` add-on frames your web
  logbook (Wavelog, Cloudlog), loaded only when the tab is opened, with a link
  to open it in its own tab. Configure a `url`, or a `port` for a logbook on
  the radio's own machine, which is framed under the page's own name so the
  logbook's login holds. A secure page with a plain-http logbook gets the
  link only, with the reason.
- **MeshCore prefers Wi-Fi over Bluetooth, every time.** Every Wi-Fi link is
  tried before any Bluetooth one, and the address the companion last reported
  is tried before the configured one, so a DHCP move no longer strands the
  bridge on Bluetooth. On Bluetooth the bridge asks the node for its Wi-Fi
  address at start and every minute, and moves to Wi-Fi as soon as it answers
  there (Wi-Fi first, then Bluetooth let go).
- **APRS message management on the packet page's Messages tab.**
  - **Cancel** stops one of your messages that is still queued or retrying. It
    is never sent again, and the next message queued to that station starts.
  - **Resend** puts a failed, rejected or cancelled message back in compose.
    Send, with TX armed, sends it under a new message number.
  - **Delete** removes one message or a whole conversation, after a
    confirmation. The hub refuses either while one of yours in it is still
    being sent.
  - **Read marks and archive live in the hub's store** (schema 4,
    `thread_state`), so every browser shows the same unread badge. The badge
    now counts unread messages, not threads. Opening a conversation marks it
    read. **Mark unread** and **Mark all read** sit beside it.
  - **Archive** hides a conversation until a new message arrives, and
    **Archived (n)** lists the hidden ones.
  - **Export** downloads a conversation as text.
  - **Search:** the conversation list filters by call, and the heard list
    searches message text.
  - **Retention:** heard messages between other stations now go after 30
    days. Messages from or to any SSID of an owned call stay until you
    delete them. The table previously grew without limit.
  - On upgrade, every message already stored counts as read. The old
    per-browser marks are not carried over.
  - Cancelling a round-trip probe's message before it is sent ends the probe
    at once.
- **STOP TX: an always-visible kill switch on the radio page.** One press,
  in every view, ends this radio's transmission and any ATU tune, aborts a
  running Winlink session and listen window, and disarms both Winlink lanes
  (ARDOP and VARA HF). It then latches on the server: rigctl `T 1` and
  `G TUNE`, the page's PTT and tune, auto-tune, Winlink connects and listen
  windows are all refused until a page presses Resume. A reload or a second
  page sees the latch. Resume re-arms nothing. The latch does not survive a
  server restart. It covers this radio's server only; the packet hub keeps
  its own transmit gate.
- **iGate watch** (`igate_watch.py`, on by default; `--igate-watch-min 0`
  turns it off). The packet hub notices when fewer than two other stations'
  iGates carried APRS-IS traffic onto the air here in a day, the path
  messages from the internet need to reach a receive-only station. The store
  tallies every RF copy per iGate per local day (schema 3, `igate_days`,
  30 days kept), so two iGates delivering the same packet both count. A day
  with under 200 frames heard, or one the tally only saw part of, is judged
  unknown, never low. A desktop notification goes out on a drop and on the
  recovery only; `api/status` shows the last day judged.
- **Winlink over VARA HF (xara), beside ARDOP, on a Mac** (module `vara`,
  host file `winlink.xara`; plan winlink-vara, PR A). xara, unregistered,
  keys the radio through its own lane on the radio's server
  (`--winlink-vara-rigctl-port`, default 4534), only while a VARA session
  is armed; each Winlink lane keys only for its own modem's session. The
  page offers one via per modem (`HF · IC-7300 (VARA HF)`), its gateway list
  (`rmslist?mode=varahf`) and VARA peer URLs; the confirm names the modem.
  xara's CW ID is read from its log (`--winlink-vara-log`) and counted as on
  air only when the VARA lane's burst ended with xara's own T 0. A VARA
  session is refused until Pat's `varahf.addr`, `varahf.rig` (its hamlib
  address: this radio's lane) and `varahf.ptt_ctrl: false` are right,
  checked by host and port at every arm and in `doctor`. Every Winlink
  session now leaves one `winlink session: {...}` JSON line in the server
  log (scheme, bandwidth, gateway, frequency, seconds, outcome, messages,
  bytes, xara's highest speed level), the trial's keep-or-drop record.
  `doctor` warns about a xara agent or listener left behind with VARA off.
  Linux (the station) is not covered yet.
- **APRS messaging in the packet hub** (`aprs_msg.py`; host file
  `packet.aprs.messaging`, off by default). `send`: compose from the
  packet page's Messages tab (behind TX ARMED in that browser), send with
  retries at t0 + 0, 30, 90, 210, 450 and 930 s (one per slot, passed slots
  skipped, deadline t0 + 1,410 s), and match acks, rejs and reply-acks to
  our messages; the hub acknowledges nothing it hears and puts no reply-ack
  id on its messages. `full` also acks messages to `owned_calls` heard live
  on RF, and needs `firmware_acks: false` (otherwise it runs as `send` and
  says why). Messages pause while a KISS client, a monitoring AGWPE client,
  a link, an inbound window, a lend, a hold or the 60 s start cooldown is in
  the way; the reason is on the page. `POST api/aprs/message`,
  `GET api/aprs/thread`.
- **The operator's stop survives a restart.** Off writes `tx_inhibit` to
  `<log-dir>/tx-state.json` (atomic, fsynced) before transmit clears and
  before the page is told; a failed write still stops and says "not
  persisted". A hub started with `--tx` stays off while the inhibit is set;
  an unreadable or corrupt file starts it off with a notice.
- **Off-channel holds.** The UV-PRO add-on asks the hub to hold APRS
  (`POST /api/hold`, this machine only) before it retunes the radio for
  Winlink, refuses to retune without it, and releases it only after the
  radio is put back and read back; a restore given up keeps it. Holds
  persist across restarts and show on the packet page with a confirmed
  Release.
- **The round-trip probe** (`aprs_probe.py`; host file
  `packet.aprs.probe {target, text}`): from the Messages tab's Probe
  button (TX ARMED), one message to the target with a 3-character token,
  then five legs judged over t_first + 1,470 s, each yes (with evidence),
  not seen, or unknown (the station couldn't see: off channel, radio down,
  observer down, hub down): sent, digipeated (an H-bit from a digi that
  isn't ours), reached APRS-IS via RF (qAR/qAO/qAr/qAo from someone else's
  iGate), a correlated reply on RF, acked. Refused before the previous
  probe's deadline. The last 20 are kept (`GET api/aprs/probes`; store
  schema 2 adds the `probes` table).
- **The iGate's APRS-IS observer** (`packet.igate.observe`, on by default
  with messaging): the login carries `filter b/<call>* g/<call>*`, and
  what comes back is stored as port `is` and shown "via internet". Nothing
  from APRS-IS is acked, matched or transmitted.

### Fixed

- **A station of one radio answers `/audio`.** A remote listener that took
  audio from the radio's own server at `/audio` got 404 once the station
  stood in front of the radio. The station now hands `/audio` to its only
  radio's audio, the same backend `/r/<id>/audio` reaches. With more than one
  radio it answers 404 and names each radio's `/r/<id>/audio`.
- **A rollback puts back the files it replaced.** It used to run the old
  code's update, which re-rendered the host file; a host file already edited
  for the new code then rendered files that had never run (2026-11-10: the
  radio page came back on plain http, and the station agent stayed loaded,
  crash-looping). An update now writes a journal
  (`~/.local/state/ranch-radio/update-journal.json`) before it writes a file
  or restarts a server. The rollback restores each replaced file byte for
  byte from the backup tree, moves each added file into
  `backups/<time>-rolled-back/`, restarts what the update restarted on the
  restored files, and boots out (on Linux, disables and stops) whatever the
  update added. It never re-renders.
- **A second server no longer ends a running server's listen window.** The
  listen record (`winlink-listen.json`, beside `--addons-state`) now names the
  server that wrote it (`server_pid`). At start, a server finds a record from
  a server that is still running and leaves it alone; only a record whose
  server is gone is ended as before. Every test server reads the default
  state, so a test run during a live window closed the live hub's inbound
  window, stopped the live listening Pat and deleted the record (a live station,
  2026-11-07, twice). Test B-13, with the owner-gone case as its positive
  control.

### Changed

- **CI keeps a time budget.** A run that got much slower than its recorded
  time, a new run over 90 seconds, or shards that drifted apart each raise a
  warning on the pull request. A run that never reported a time fails it. A
  hung test is stopped after 5 minutes instead of 15.

- **CI runs the suite on four runners at once.** The split comes from
  measured test durations, so a pull request's check takes about a quarter
  of the time it did. Each result line now shows how long that test ran.

- **rigctl `T 0` releases only the sender's own PTT**, on every port.
  From a client that isn't keying it answers `RPRT 0` and changes nothing;
  before, any client's `T 0` (or one sent while the radio was keyed at its
  front panel) unkeyed whoever was transmitting.
- **The station-ID ledger hears how every Winlink lane key ended** (the
  owner's own `T 0`, or forced: a disconnect, the keepalive, the transmit
  limit, a disarm, the radio), keeps each ID's source (ardopcf, xara), and
  judges ardopcf's IDs by the ARDOP lane only. ardopcf's log is followed
  across an inode change too.
- **The transmit gate has classes.** `app` (KISS/AGWPE), `msg`, `ack`,
  `digi`, `connected`, set by the hub. Of the overall 300/min, the last 4
  are for acks; UI frames (app, msg, ack) share 12/min with 4 for acks
  (apps 6, messages 2, acks 6) and 15 s of airtime a minute with 3 s for
  acks, by a conservative formula (flags, 1.2x bit stuffing, the radio's
  TX delay). Connected-mode (Winlink) frames count only toward the overall
  cap. Every refusal names its cap.
- **The iGate says `qAO`** (receive-only, per aprs-is.net q.aspx) instead
  of `qAR`, and never gates our own calls' frames heard back (outer or
  inner source).
- The UV-PRO add-on reads `"packet_status": null` as "no link check"; its
  hold goes to `"hub"`, else the base of `packet_status`.

- **The packet hub answers inbound connects only inside an inbound window.**
  A station calling a call an AGWPE client registered (Pat's MyCall, held
  after any packet session) got an automatic UA, then DISCs when Pat closed
  the unwanted connection: transmissions nobody asked for. Such a call is
  now ignored, with no UA and no DM, counted (`inbound_ignored`) and logged,
  unless `POST /api/inbound {"calls", "seconds"}` (this machine only, with
  `X-Ranch-Radio: 1`) opened a window for it; it ends on
  `/api/inbound/close` or by itself, and a restarted hub starts closed.
  Only a listen window (below) opens one.
- **Pat has one lifecycle owner, and a config check before every start.**
  Start, stop, `restart_pat` and the inbound-session cap go through one
  lock; Pat's command line is built from the wanted state when it is
  started; `restart_pat` works whether Pat is running or not and returns
  only once the new Pat is up and has sent its status (else it raises).
  Before every start Pat's config is read for `listen`, `schedule`,
  `ax25.beacon.every` (and `agwpe.addr`) only: if Pat would listen, beacon
  or run scheduled connects on its own, or the file can't be read, Pat is
  not started and the Winlink panel and `doctor` say what to clear. The
  config is found from `--winlink-pat-config` (host file
  `"winlink": {"pat_config": ...}`), else a `--config` in the Pat command,
  else Pat's default for the OS.
- **Winlink knows when Pat is busy, whoever started it.** A session another
  station started (Pat's `Got connect` log line and its Status) is shown as
  "Answering CALL"; while it runs, or while Pat's state is unknown, the
  outbox can't be changed (409) and a connect is refused, re-checked after
  every step of arming, before anything is dialed. Such a session gets the
  same `--winlink-session-max` cap: a graceful disconnect, then (on packet,
  until the hub's link is gone) SIGTERM and SIGKILL, and Pat is started
  again.
- **Packet URLs are checked against the data sub-bands.** 2 m from 144.1
  MHz (47 CFR 97.305(c)), 1.25 m 222-225, 70 cm 420-450 MHz; 144.390 MHz
  (the APRS channel, +-10 kHz) is always refused.

- **The tab bar is grouped: Radios, Messaging, Mesh, Tools.** It had grown
  a flat tab per add-on (eight on a radio with every module). Each radio,
  service and add-on now belongs to one group: Radios (the HF radios, the
  UV-PRO, the MD-UV390), Messaging (Winlink, Packet), Mesh (Meshtastic,
  MeshCore) and Tools (Propagation, and any add-on that names no group).
  The group holding the open view shows its members in place, in a frame
  named for it; every other group is one tab, with its members' most
  urgent dot (on the air, then connected) and their unread counts, that
  opens the member last used there. A group of one is just that member's
  tab, so a small station looks as before. Modules stays last, and the
  station's Overview cards follow the same order. An add-on names its
  group with `Addon.group`; the page lays it out in `static/nav.js`.

### Added

- **Winlink listen windows: let stations that call this one leave mail.**
  Off unless the host file sets one up:
  `"winlink": {"listen": {"ax25+agwpe": {"mhz": 145.090, "max_hours": 4}}}`
  (VHF/UHF packet through the hub only, inside a 47 CFR 97.305(c) data
  sub-band, never 144.390; `max_hours` 0.25 to 12, default 4; checked by
  `render`, passed to a Mac's server as `--winlink-listen` /
  `--winlink-listen-max-h`, kept in station.json for the station). The
  Winlink panel then shows **Listen** with a choice of hours; the confirm
  names the frequency, the hours, and that APRS and the iGate are
  off-frequency meanwhile. Opening a window writes its record, tunes the
  UV-PRO to the frequency, relaunches Pat with `--listen ax25+agwpe` and
  opens the hub's inbound window for MyCall until the window's end, then
  reads all of it back (anything missing is undone). While it is open,
  connect and outbox changes are refused (compose still works). It ends
  on **Stop listening**, at its end, when the hub or an add-on goes away,
  or when the server stops: the hub's window is closed first, a session
  still up is disconnected (then Pat signalled), Pat is relaunched without
  `--listen`, and only once no packet link is left is the radio put back.
  Closing the page does not end it; the hub's own window expiry is the
  bound. After a crash, the next start ends it before Pat runs (closes the
  hub's window, stops a listening Pat that outlived its server, only if it
  is one, and puts the radio back); a window is never resumed. Not offered:
  HF listening, always-on listening, the APRS channel, auto-reply, VARA FM.
- **The UV-PRO's Winlink QSY record survives a restart.** It is written
  beside the add-on state file (`uvpro-qsy.json`) before the radio is first
  changed and removed once it is back; a server that died mid packet
  session leaves it, and the add-on's next start puts the radio back on
  its channel. (It was kept in memory only: a crash left the radio off
  its APRS channel.)
- **Winlink peer-to-peer (P2P).** The session panel's Gateway | Peer switch
  calls another station directly: packet through the hub
  (`ax25+agwpe:///CALL?freq=`), an HF modem the server lists, or telnet
  (`telnet://MYCALL:PASS@host:port/CALL`). A peer is one valid callsign,
  not this station's own (an SSID of it is fine), and a radio peer needs
  its frequency. Compose has **P2P only** (Pat's `X-P2POnly`), shown as a
  P2P badge; Reply and Forward keep it. A session's counts are measured:
  sent is what Pat moved to sent, left what is still in the outbox; the
  "still in the outbox" warning is for gateway sessions only and leaves
  P2P-only messages out. Telnet passwords are masked in logs and pages.

- **The MeshCore tab works like the Mesh tab, and uses what MeshCore has.**
  Same layout and styles as the Mesh tab: channel and Direct tabs with
  unread counts, a To bar, a sender to click for a direct message, ACK
  marks, and the map (`meshmap.js`) of this node and every contact that
  shares a location, pulsing on activity. The node list finds, filters to
  Recent, keeps favourites (the node's own favourite flag) on top and shows
  each node's hops and distance; its card shows the route by repeater name
  and the node's clock when its advert says it is wrong. From the card:
  Find path (drawn on the map), Trace (each hop's SNR), Forget route,
  Remove; and for a repeater or room server, Log in, Status (battery,
  uptime, noise floor, airtime, packet and duplicate counts), Telemetry,
  Neighbours (drawn on the map with their SNR), Owner, Set clock and a
  Console for remote commands. Each request goes on the air, so one per
  node at a time and one every 2 s. A repeater's password can be kept in a
  file (`"logins": {"<key prefix>": "<password file>"}`), read when used and
  never sent to a page or logged; a typed one is not kept either; remote
  commands and replies are never written to the log. The header adds the
  node's own counters (noise floor, last RSSI and SNR, airtime, packets)
  and what it hears on the air. Direct messages are retried until their
  ACK (flooded from the second retry) and report tries and round trip.
  Join a `#hashtag` channel or leave one; with "Add contacts by hand" on,
  nodes heard but not added show up with a + to add them.
- **Reply, reply all, forward, archive and delete in the Winlink panel.** The
  read pane gets an action bar, done the way Pat's own client does it: Reply
  fills To with the sender, `Re: ` the subject (not again when it already
  starts with `re:` in any case, e.g. Winlink Express's `RE:`) and quotes the
  message (`--- <date> <from> wrote: ---`, `>` before each line); Reply all
  adds a Cc of the other recipients, without your own call and the sender
  (the compose form has a new Cc field); Forward leaves To empty, prefixes
  `Fw: ` and re-attaches the original's files (one that can't be read is left
  off, with a note that stays in the form). Archive moves the message to the
  Archive box (not offered in the Outbox or Archive); Delete needs a second
  click within 6 s, bound to the message it was first clicked on. While a
  session arms or runs, the outbox is Pat's: Delete and Archive there are
  disabled on the page, and the server answers 409 to a delete or a move out
  of the outbox, holding a lock across the check and the call to Pat so a
  connect can't snapshot the outbox mid-change. The mailbox routes now
  decode and check the message ID (no path separator, no leading dot) and
  pass Pat a re-encoded one, so an encoded `X%2F..%2F..%2Fout%2F…` source path
  can't reach the outbox dressed as an inbox move; a compose POST carrying
  `X-Pat-SourcePath` is refused instead of becoming a new message.
  `tests/fake_pat.py` gains delete, move, mark-read and attachment routes, a
  log line for every request it receives, and the knobs
  `FAKE_PAT_BROKEN_ATTACH`, `FAKE_PAT_BROKEN_READ` and `FAKE_PAT_SLOW_MID`.
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

- **Auto-tune no longer loops.** On the first VARA trial an IC-7300
  re-tuned 20 m 15 times in 34 s and the page had no way to stop it. A
  frequency report during a tune, or within 1 s of its end, no longer counts
  as a band change; the band is read once the tune is over. As a backstop,
  a band auto-tuned twice within 60 s switches auto-tune off for the run,
  with a warning on the page and in the log.
- **A Winlink session no longer dials under an ATU tune.** Before Pat
  dials a local session, the server puts the radio on the session's
  frequency and waits for any tune to finish, up to 20 s past the settle
  time. On the trial, the band change's tune landed on xara's first key,
  which was refused, and xara exited.
- **The UV-PRO add-on reconnects within seconds after an update, not four
  minutes.** When the packet hub restarts with it, the hub closes its control
  port until its own link to the radio is up; the bridge then waited out its
  240 s connect deadline for a reply that never came. A `tcp:` control connect
  now fails as soon as the hub closes the port (logged as `control port closed
  by the hub`) and tries again a second later.
- **Opening a Winlink message never marked it read.** The page posted the
  body `true`; Pat expects `{"read": true}` and answered 400, which the page
  swallowed, so every inbox message stayed bold. It now posts the object when
  the message opens, un-bolds the row only after Pat's 200, and says so when
  Pat refuses ("Could not mark it read").
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
