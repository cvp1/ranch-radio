# Changelog

All notable changes to Ranch Radio are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- A new need level, "new grid in this mode": a grid worked before, on this band, but never in this mode, with FT8 and FT4 counted apart (they are one LoTW mode group, so before this an FT4 station in a grid worked only on FT8 showed as nothing new). It ranks below "grid on this band". The Roster, Map, call card, scope labels and station page name it, and it counts as a strong need, so the band strip, the Roster card and Mission Control's Now row may count a few more stations. The Roster's "grid" alert includes it.
- Grids tab: an alert when a missing square comes on the air. It is off by default; pick a chime or a voice that spells the square out. It follows the tab's mode and band, sounds once per square, band and mode in half an hour, and never for what was already on the air when the page opened.
- A Grids tab (the `grids` add-on) for the grid squares you still need, by operating mode and band. Pick a mode (FT8, FT4, SSB, CW, or All), a band, and whether a square counts once worked or only once confirmed. It draws the 488 Lower-48 squares (ARRL's FFMA set) as confirmed, worked or missing, tallies each mode (Lower-48 confirmed, worked and missing, plus squares worked anywhere), and lists the missing squares nearest first. A missing square that a station JTDX is decoding right now goes to the top of the list and is ringed on the lattice. The data comes from the logbook sync's needs index, which now also keeps each square's band and mode. Nothing is written or sent.
- A phone as the station's remote. On a screen 700 px wide or less the station page puts its tabs in a bar at the bottom (Home, Radio, Messages, Mesh, More), clear of the iPhone home indicator; the header is one row and the rig bar the second. Radio opens the focus radio; More lists every tab. A radio's page at 860 px or less puts what you reach for first at the top: frequency, meters, then Listen and PTT, then the scope, band and mode, and levels. On Mesh the node's key, counters and buttons fold behind ⋯ and the conversation strip is one row, so on a 390x844 phone the messages start at 248 px instead of 425.
- Station page: a wall view for a kiosk screen, at `#/wall` or with `?wall` in the address (leaving drops it again). It shows the Map large with its night side on (the Night setting is not changed), a big UTC clock with local time and date, the band strip, the focus radio's spectrum and waterfall from the scope frames the station already receives, and the last eight events this page has seen. It is read only: no radio page is loaded or shown, every pane ignores clicks and keys, and only STOP TX in the rig bar and Exit wall (or Esc) take input. On https or localhost it keeps the screen awake while shown, and says so when the browser refuses.
- Mission Control fits one screen. With two radios, Winlink, Packet, Mesh and five Tools add-ons it no longer scrolls at 1600x1000 or 1440x900. The lanes flow in rows: Messaging and Mesh share one, and a lane's cards fill its row. A radio card puts its ports and Winlink lane on one line and its mini spectrum beside the frequency, S-meter and trace, so it is no taller with the spectrum than without it. A new "Now" row under the band strip shows up to five fresh stations with strong needs (new DXCC, band, mode or grid) on the band JTDX is decoding, counted as the band strip counts them. A click on one is the same Reply a click on its Roster row sends: JTDX answers it only as its own double-click setting allows, and JTDX's outcome shows the same way. Nothing is sent without a click. If the needs index isn't ready, Now says the needs are unknown. A "Log" tile beside it shows QSOs, DXCC and grids worked/confirmed from the needs index, and opens the Logbook tab. On a phone the order is the band strip, then what is on the air and what needs you, then Now, then the radios, then the rest. The rig bar and its STOP TX are unchanged.
- Station page: an Operate tab after Mission Control shows the focus radio, the Roster and the Map side by side, with the band strip above them. It appears when a radio is connected, the Roster or the Map is on, and the window is at least 1100 px wide. From 1500 px the three sit in a row; narrower, the radio is on the left with the Roster above the Map. If the Roster or the Map is switched off, the other panes take its room. The radio is its own page, compacted: STOP TX, power and the connection state stay in its top row, the scope fills the height, and band and mode buttons sit one row each. Levels, the TX panel and Filter/AGC are one click away behind "Controls" (closed on each visit). The page is the same one, so Listen keeps playing and nothing reloads when you come and go. Focusing another radio in the rig bar swaps the pane. In the pane the Map hides its station list without changing your saved choice. The rig bar's STOP TX stays as it is.
- Mission Control: a band strip under the clock, one cell for each band up to 6 m that the focus radio covers. Each cell keeps three things apart, each from its own source: how many decodes this station heard there in the last hour (skywatch), a tick with how many stations reported you (PSKReporter), and the forecast path open now, by region, until when and its peak (the best hour of that window, not the chance now). When a source isn't set up, hasn't answered yet or fails, the strip says so under the cells instead of showing zero; "no decodes" means nobody may have listened. If PSKReporter fails, the last tick stays, struck through as stale. The band JTDX is decoding shows how many recent stations there have strong needs (new DXCC, band, mode or grid) and how many are unconfirmed, or "unknown" while the needs index builds. The Roster card counts the same way, so rows kept from the previous band no longer count. The strip updates on the hour even when nothing new arrives.
- Radio scope: labels for who is on the band. On the station page each radio's spectrum names the stations JTDX decoded on that radio's band, each at its own frequency (JTDX's dial when it was heard plus its audio offset), and the POTA activators the Map add-on has; no new feed is fetched. The most needed go first, in up to three rows, coloured by need in every theme; labels closer than 40 px collapse to one ("FT8 ×23 · 4 new"). Hover shows the details.
- Tune from the bands and the spots. On Mission Control's band strip the band's name tunes the focus radio to that band (where it was last left there, else the band's default); so do the band names in the Propagation tab's Next openings, its band board and its table, on the station page (the focus radio) and on a radio's own page (that radio, as its band buttons do). On a radio's scope in the station page, a click on a spot label tunes the focus radio to it: a JTDX decode to the dial it was decoded on, leaving the mode alone; a POTA spot to its frequency, in CW for CW, the band's usual sideband for SSB, and USB for FT8, FT4 and other digital modes (with data on where the radio has it). A POTA mode the radio doesn't have, or one not in that list (AM, FM), changes the frequency only. A label on a radio that is not the focus tunes nothing and says so. Everything goes through the same checks as typing a frequency in the rig bar: locked, a click flashes the padlock and sends nothing; transmitting, tuning or with a Winlink session armed, nothing is sent and a note says why. Each of these says in its tooltip that, on a station with auto-tune, a band change transmits a tune carrier. Add-ons can ask the page to change band (`api.tune({band})`), through the same checks.
- Mission Control: a band strip under the clock, one cell for each band up to 6 m that the focus radio covers. Each cell keeps three things apart, each from its own source: how many decodes this station heard there in the last hour (skywatch), a tick with how many stations reported you (PSKReporter), and the forecast path open now, by region, until when and its peak (the best hour of that window, not the chance now). When a source isn't set up, hasn't answered yet or fails, the strip says so under the cells instead of showing zero; "no decodes" means nobody may have listened. If PSKReporter fails, the last tick stays, struck through as stale. The band JTDX is decoding shows how many recent stations there have strong needs (new DXCC, band, mode or grid) and how many are unconfirmed, or "unknown" while the needs index builds. The Roster card counts the same way, so rows kept from the previous band no longer count. The strip updates on the hour even when nothing new arrives.
- Radio scope: labels for who is on the band. On the station page each radio's spectrum names the stations JTDX decoded on that radio's band, each at its own frequency (JTDX's dial when it was heard plus its audio offset), and the POTA activators the Map add-on has; no new feed is fetched. The most needed go first, in up to three rows, coloured by need in every theme; labels closer than 40 px collapse to one ("FT8 ×23 · 4 new"). Hover shows the details.
- Station page: type a frequency in the rig bar. Unlocked, a click on the focus radio's frequency opens an entry (MHz, kHz or Hz, as on the radio's page); Enter tunes the radio. A padlock beside it is the same lock as the radio pages', so locking or unlocking either changes both; locked, a click flashes the padlock and does nothing. Just before sending, the bar checks again that tuning is unlocked, the same radio still has focus, it is connected, not transmitting or tuning, and has no Winlink session armed; if any of these fails, nothing is sent and a note says why. A mode the radio refuses after a frequency change it accepted is reported as such. Every control that can change band (the bar's frequency, the radio page's band buttons, its frequency, A/B and swap) now says in its tooltip that, on a station with auto-tune, a band change transmits a tune carrier. The radio page and the bar share one band stack.
- Station page: Listen in the rig bar. A speaker beside the focus radio's name starts and stops its receive audio from any view, Mission Control included; it lights only when the audio is actually playing, and the radio's own Listen button agrees. If the browser won't start audio from there (some want a press on the radio's own page first), a note says so and its tab opens. Volume stays on the radio's page. To make this work the focus radio's page now loads early, hidden; while hidden it stops drawing its scope and meters, and a radio without its own scope (drawn from receive audio) closes that audio stream unless you are listening. Listening is receive only: it never keys, retunes, or takes a radio from another operator or a Winlink session.
- Roster and Map: a call card. An info button beside each call on the Roster (it never sends a Reply) and the Map's station card show what your logbook has with that exact call: the latest QSOs (at most 20) with date, band, mode and a confirmed tick, and what the station still needs from it when it was just decoded. It reads the logbook sync's ledger and changes nothing. A QRZ link opens the call's page in a new tab; the page itself fetches nothing from QRZ.
- Station page: STOP TX for all HF radios at the right of the rig bar. One press sends each radio the same STOP TX its own page sends; the packet hub is not included. Beside it, what each radio confirmed: TX STOPPED only when every HF radio reports the latch, otherwise each one by name (stopped, not reached, unknown, offline). A radio that was down or dropped the message gets it when it reconnects, for 10 minutes, even across a reload; after that the bar says, in red, it was not delivered and sends nothing more, so an old press never undoes a later Resume. Cancel ends a pending press. Latched but still transmitting shows as LATCHED BUT STILL KEYED; a radio whose server restarted (the latch does not survive that) shows as no longer stopped and is not stopped again on its own. Resume stays on each radio's page: its STOPPED tag opens it. Each radio page keeps its own STOP TX.
- Station page: a rig bar under the top bar on every view, Mission Control and the radios' own tabs included (it replaces the radio strip, which only showed on Winlink, Mesh and Packet). Each HF radio shows a state dot, frequency, mode (with -D), band and S-meter (on the air: watts, in red), with ON AIR and Release TX while keyed; a radio that isn't connected reads offline. One radio, the focus, is shown in full (by default the one on the shared rigctl port); a click on another focuses it, and the browser remembers it. A radio's name opens its tab. A refusal or warning from a radio's server now shows as a toast on the station page, named for the radio.
- Mesh conversations can be managed: each one in the rail (and the chat head) has a ⋯ menu to mark read or unread, pin to top, mute (no pop-ups), archive (a folded Archived section; a new message brings it back unless muted), and clear a channel's history or delete a DM conversation. Each message's bar adds Copy and Delete. Deletes take messages off this station only (the mesh cannot unsend; the page says so) and offer Undo; a message still being sent can't be deleted. A search box on the rail finds conversations by name or message text. The station keeps all of it (`mesh-convs.json`, `mesh-trash.jsonl`, mode 600), so unread counts and archives survive a reload and agree across browsers.
- Mesh: nodes show by name everywhere, even after they drop out of the radio's node list. The station keeps every name the node has given (`mesh-names.json` beside the history, newest 3000), so messages, NeighborInfo, traceroutes and who-hears-us name a node rather than showing its `!id`. The Health view's Relay column names the relay when only one known node ends in that byte.
- Mesh and MeshCore maps: a click on the small map opens it big, to pan and zoom around; the big map fills the panel with the node list beside it. ✕ Close map or Esc gives the room back. MeshCore gets the big map too.
- Logbook sync on several hosts: `"adopt_wavelog": false` in a host's `logsync` block makes that host send only the QSOs its own JTDX logged. It still pulls Wavelog to confirm its own sends, but takes in nothing else (no first-start backfill, no hand-typed or other hosts' QSOs), so two hosts never upload the same QSO. One host keeps the default and adopts hand-typed QSOs.
- Mesh and MeshCore messaging redesigned (`static/chat.js`, `static/chat.css`): a rail of conversations (each channel and each DM, with its last message and unread count), a full-height chat with bubbles, names in their own colour, day and "New" lines and a jump-to-latest button, a hover (or tap) bar to react or reply, an emoji picker for the box and for reactions, quotes that jump to the message they answer, and a box that grows and sends on Enter (Shift+Enter for a new line, Escape to drop a reply). The node list, card and map sit on the right; ⤢ still gives the map the room.
- Themes: a picker in the top bar (the palette button) sets the page's look: Midnight blue (the new default), Mint (classic), Amber, Sandstone, Aurora and High contrast. The choice is kept in the browser and reaches the station's radio and packet tabs at once. Status green and the on-air red stay as they were in every theme.
- **Winlink strip: the modem's own state.** Beside the session state, the
  strip shows what the modem the next session uses is doing, read from the
  logs the radio's server already follows for station ID (ardopcf's
  `--winlink-ardop-log`, xara's `--winlink-vara-log`): link state, partner
  and session bandwidth, the S/N of the last decoded frame, speed (ARDOP's
  frame modulation and decode quality; VARA's speed level and rate), BUSY,
  and PTT. A reading older than 90 s says how old it is; a modem whose log
  isn't followed says "not tracked", one followed but silent so far
  says "waiting", and a radio server on older code says "no status", never blanks. Nothing talks
  to the modems: Pat keeps their host ports to itself.
- **Winlink panel, console layout.** A status strip on top (state, the via,
  who the next session calls, how many messages wait in the outbox, unread),
  then three columns: session controls on the left, mailbox and session log
  in the middle, gateways or the P2P directory on the right. Each station row
  has its own Connect, which picks it and asks for Connect's confirm (it never
  transmits by itself). The gateway list loads without a click, the Inbox and
  Outbox tabs carry counts, an "In queue" line names what the next session
  sends, the log colours errors and successes, and Ctrl+Enter queues a
  composed message. The state line names the station and frequency instead of
  the raw connect URL (a telnet URL's password no longer shows there).
- **HaLow tab (`halow` add-on).** Watches and runs an OpenWrt Wi-Fi HaLow bridge over SSH, installing nothing on it: radios, networks, stations (signal, rates, retries, address), box health, services, log, and a DHCP guard that turns red if the bridge starts handing out LAN addresses. Changes (reboot, disconnect a station, channel, power, network name, hidden, on/off, service restarts, DHCP off) are planned server-side, shown as the exact command with what it will drop, and run once on Confirm; an unconfirmed plan expires after two minutes. Every applied change goes to `halow-actions.log`. Wi-Fi keys are never read or shown.
- **Wavelog CAT, in place of WaveLogGate.** A `wavelog_cat` block sends each
  radio's frequency and mode to Wavelog's live QSO form (on every change, and
  every minute while quiet), and a click on a spot in Wavelog's DX cluster or
  bandmap tunes the radio through the station (`/wavelog/qsy/<radio>`). The
  click is taken only from the Wavelog address the block names, never while
  the radio transmits or its Winlink lane is armed. WaveLogGate and the flrig
  it needed can go; JTDX's QSOs were already the logbook sync's.

- **A Meshtastic node over Wi-Fi.** The Mesh module's `port` takes
  `tcp:HOST` or `tcp:HOST:PORT` (port 4403 by default) beside a serial device
  and `ble:NAME`, in the same comma list tried in order. Wi-Fi needs no cable
  or Bluetooth bond and leaves the node's one Bluetooth link free for a
  phone. The node's API port has no login, so keep it on a LAN you trust. A
  malformed `tcp:` entry stops the bridge with a status saying why instead of
  being skipped. The header's tooltip names the link in use. Power saving,
  which turns off Bluetooth and Wi-Fi on an ESP32 node, is now refused over
  Bluetooth as well as Wi-Fi; set it over USB.
- **Mesh: a message the mesh NAKs goes again by itself.** When the node
  reports a failure the mesh may get over (no relay heard it, a timeout, the
  duty-cycle limit), the station sends the message again 15 s, 1 min and
  3 min later, at most three more times. The message shows when it goes
  next and a **Cancel**. Not resent: a reaction, a write error, another NAK
  reason, anything after the node goes away (Retry stays for those). A
  resend's ACK wait is its own, so the first try's no longer marks it
  unknown early. `"auto_resend": false` in the host file's `mesh` block
  leaves it all to Retry. (An idea from Mesh-Client.)
- **Mesh: long messages go in parts.** A text over one packet's 200 bytes is
  split between words into up to nine parts, `[1/3] …`, sent one at a time:
  the next once the one before is ACKed or its wait runs out. A part that
  finally fails drops the rest; Cancel on a part not sent yet drops it and
  those after it. The counter shows how many parts it takes. A reply or a
  reaction is still one packet. After a restart, parts never sent show
  failed.
- **Mesh Health: what this node hears, added up.** A **Health** button in
  the Mesh header opens a view of the last hour and the last day:
  - a status band (Healthy, Attention, Busy), also shown on the button;
  - packets a minute for the last hour, and this node's channel
    utilization and its own air-tx over 24 hours, with the firmware's 25 %
    and 40 % lines;
  - **What looks wrong**, each with what to do: a busy channel, a chatty
    node (by rate, or a quarter of all traffic, with its main port), a
    weak direct link, a node heard only
    over MQTT, one relay carrying most of what reaches us (named from the
    firmware's relay byte), much traffic on channels without a key. Each
    needs enough packets before it says anything. A finding with a node
    opens that node's card;
  - the top talkers with their ports, hop limit, hops and direct SNR, and
    the last 200 packets heard with path, signal, relay and size.

  The node card gains a traffic line (packets in the last hour, mostly what,
  hop limit). The bridge's `rx` events now carry hops, hop limit, SNR,
  RSSI, MQTT, relay byte, channel and size. Counts are in memory only: they
  start over when the station restarts (the view says since when) and
  survive a Mesh switch-off. Pages get an update at most every 10 s. (The
  idea is from Mesh-Client's Diagnostics tab, keeping only what one node's
  receive side can show honestly.)
- **Mission Control replaces the station's Overview.** One look at the
  station: what is on the air (any HF radio keyed, a Winlink session, the
  UV-PRO), with Release TX; what needs you (unread Winlink mail, the outbox,
  new mesh messages, a listen window asking if you're there, gear offline),
  each a link to the tab that deals with it; then every radio and service
  live, in lanes named like the tab bar's groups: a radio's frequency, band,
  mode and S-meter (power while transmitting), Winlink's state and mailbox,
  the mesh's nodes heard and last message. A card opens its tab; keys 1-9
  open the cards in order and 0 comes back.
- **Mission Control, dressed up.** A large UTC clock with local time and
  date; each radio a double-width card with a live mini spectrum and
  waterfall (from the scope frames its server already sends; the tuned
  frequency marked), a segmented S-meter with its scale, and the last two
  minutes of signal (red where it transmitted). Every card has an icon and
  its lane's colour; on the air pulses red. Motion stops with the system's
  reduced-motion setting.
- **Automatic Meshtastic traceroutes (off by default).** With
  `"auto_trace": true` in the host file's `mesh` block, the station traces a
  node by itself when it hears it over relays and has no trace yet, or its
  hop count changed since the last one. Only nodes heard since the node
  connected count (not the node DB's old entries); MQTT, ignored and direct
  nodes are skipped. One trace at a time, 30 s apart, at most
  `auto_trace_per_hour` (default 6, 1..30) and once an hour per node; a
  manual trace always goes first. Traces it sends are marked `auto`.
  Idea from MeshSense, which does this with no cap.
- **Edit a published P2P directory listing.** On the maintainer's station
  the Winlink page names the published listings; a click opens one's text
  in an editor, and Save checks it with the directory's parser and build,
  commits and pushes. The confirmed date stays; an edit over a listing that
  changed since it was opened is refused.
- **The directory follows a publish.** After a Publish or an edit, the
  station refetches the directory (past web caches) until the change is in,
  bounded to about 13 minutes, and the page updates by itself, instead of
  waiting up to 6 hours for the next refresh.
- **Bandwidth for a Winlink peer call.** The Peer row has a bandwidth
  picker beside the frequency for HF modems (VARA HF 500/2300/2750 Hz,
  ARDOP 200/500/1000/2000 Hz; left on Default, the modem picks). A pick goes
  in the URL as `bw=`; a P2P directory row fills it from the listing.

- **Publish P2P directory listings from the Winlink page.** On the
  directory maintainer's station (`winlink.directory.maintain`), listing
  requests in the inbox appear above the directory in Peer mode, showing the
  exact text to be published and the directory's own check of it. One click
  publishes it (written, built, committed and pushed to the directory repo,
  the request archived) or dismisses it. The callsign is always the
  message's sender; internet mail can only be dismissed.

- **A choice of map backgrounds, on every map** (`static/basemaps.js`): a menu
  beside the zoom buttons, the pick kept in the browser (one for the world
  map, one for the local maps).
  - *Earth*: NASA's Blue Marble, relief and sea floor. The Map tab's new
    default, and there the night side is the Earth's own city lights, cut
    along the gray line.
  - *Terrain*: OpenTopoMap, relief with contours, peaks and roads. The new
    default on the Mesh, MeshCore and APRS maps.
  - *Topo (US)*, *Imagery (US)*: USGS The National Map.
  - *Streets*, *Dark*: OpenStreetMap, plain and inverted. Dark is the look
    the local maps had.

  Each map credits the background it is drawing. Tiles come to the browser
  straight from each provider, as OpenStreetMap's always did. Past a
  background's deepest zoom its deepest tiles are drawn larger, so no map goes
  blank zooming in.

- **The Mesh map says much more.**
  - *Who hears whom, by name, beside the map*, as on the Map tab: Two-way,
    Hear you, You hear, each node with its distance, the SNR you hear it at
    (▼) and the SNR it hears you at (▲), farthest first. A click opens the
    node's card and brings it into view. ✕ puts the list away.
  - *Each node's mark*: a router is a diamond; a node more than a hop away
    carries its hops; a node you hear or that hears you wears a ring in that
    link's colour; one heard only over MQTT has a dashed edge. Hover for the
    rest: role, how it reached you, when, how far, battery.
  - *Distance rings* around your node, so the map has a scale.
  - *Names that can be read*: your node, the selected one, favorites and the
    nodes in direct reach are named first, and no name is drawn over another.
    A link's SNR is written on it only where the line has room.
  - *⤢ gives the map the room*: the conversation steps aside, and comes back
    when you ask for one (Message on a node's card) or press ⤢ again.

- **The Map tab shows who you hear, who hears you and what is on the air.**
  - *Hears you*: the stations that reported your callsign to PSKReporter, one
    by one at their own locators (diamonds), with the SNR each gave you and
    when. They come from the Propagation add-on's poll, so it needs your
    callsign there (`"pskreporter": {"call"}`); without it the map says what
    to set. PSKReporter is asked every ten minutes, so this is minutes old by
    nature and every station carries its age; a failed poll keeps the last
    answer and says so.
  - *You hear*: a decoded station now stays on the map for an hour (the roster
    forgets after two minutes), fainter as it ages, bigger when stronger, with
    its best SNR and how often it was decoded.
  - *Two-way*: a station in both lists on one band gets a white ring. Those
    are the ones you can work.
  - A great-circle path to each station (blue in, amber out, white both ways),
    drawn as one line across the Pacific. Everything is placed in the copy of
    the world centred on you.
  - *On the air*, top left, pushed the moment it changes: each radio by its
    own server's word (keyed or not, frequency, mode) and JTDX by its Status
    (transmitting, listening, calling whom). While you transmit, rings leave
    your marker and a beam runs to the station JTDX is calling, and the tab's
    dot goes red; a fresh decode pulses at its station and runs in along its
    path. A JTDX that goes silent mid-transmission reads "not heard from"
    after 45 s, never "transmitting" for ever.
  - A list of the stations by name (two-way, hear you, you hear; farthest
    first), a switch per layer, a 15 / 30 / 60 minute window, Fit and Me
    buttons, zoom at the pointer.

- **The Mesh map shows who hears this node and who it hears, without
  selecting anything.** With no node selected, this node's own links are
  drawn in the colours a selected node's have: green to a node that hears
  you (its NeighborInfo lists you, or it was a traceroute's first hop), blue
  to one you hear directly (a packet from it arrived with no hop used in the
  last day), purple both ways, each with its SNR; the map's note is the key.
  Before, your own node could not be selected, so the map never answered
  this for you. A message of yours pulses red at your node as it goes out.

### Changed

- **The tab bar no longer reshuffles as you move around.** A group with more than one member
  (Radios, Messaging, Mesh, Tools) is now always one split button: its name opens the member you
  used last, and the small arrow beside it opens a menu of every member with its dot and badge.
  The open group reads "Tools · Map". Nothing expands in place, so the bar has the same tabs in
  the same places on every page.
- **One STOP TX press no longer stacks a toast per radio saying it worked.** Each radio's "STOP
  TX: transmitter released, Winlink stopped..." confirmation repeated what the rig bar's stop
  state already shows, and on a phone the stack covered half the screen. Those stay in the bar,
  and so does each radio's "Transmitter released: STOP TX pressed", which the radio server sends
  on every press whether or not the radio was on the air. A STOP TX step that failed still toasts.
- **A radio's tab no longer repeats the station's header.** Inside the station page the radio
  page drops its Connected pill and theme picker (the station's bar has both); its STOP TX,
  latency and power button stay, and its pill comes back when the radio is offline or not
  responding. On a phone the station's pill shrinks to its dot (the words are in its tooltip),
  so the name, dot, clock and theme picker fit one row above the tabs.
- **The Map's station list opens on a tab that has stations.** With nobody reporting you it
  showed an empty Two-way list; it now opens on the first of Two-way, Hear you, You hear that has
  rows, and follows as they fill. A click on a tab is remembered and wins from then on; a click on
  the map still moves the list to that station's tab while it is selected.
- **The Roster reads at a glance.** A line under its bar says what a click does (JTDX answers a CQ;
  other messages only if its settings allow); it was only a tooltip. "New call", the lowest need and
  the most common, is shown quietly so the higher needs stand out (the order is unchanged). The
  Roster's checkboxes and alert menu and the Map's band and window menus share one look. On a phone or
  a narrow pane the table keeps Call, To, Distance, dB and Need and drops Entity, Grid, DF, Message
  and Age, so Need is on screen.
- **Mission Control's cards say more.** The Logbook card shows the sync's counts per logbook
  (sent, waiting, held), the latest QSO and then the logbook's address; before, only the address. The
  packet card is short ("Packet · UV-PRO") with its full title on hover. The station's callsign and
  grid sit beside its name in the top bar, taken from the logbook sync, else Winlink, else
  PSKReporter (the host file gains no field), and follow a change in the Logbook settings without a
  reload.
- **A click on a Map station opens its card; Answer is a button on it.** The
  card has both directions, distance and bearing, and what the station is to
  you. Before, a click on a dot sent JTDX the Reply at once; the Roster tab
  still answers on one click.
- **The station watches every radio, not only those with a Winlink lane**, to
  know what each is doing for the Map tab. The watch only reads.

### Fixed

- The radio page shows another client's change made just after its own. For 700 ms after you change a setting the page ignores the server's states for it, so a late echo of your own edit cannot snap the dial back; it used to ignore every state, so a retune from a second page or a phone in that window left this page showing the old frequency until the next change. Now only a value this page had or sent in the window is ignored.

- **A browser can no longer retune a radio whose Winlink lane is armed.** Between the session's
  transmissions, a band button or VFO swap on the radio page moved the dial under Pat, and the
  band change then started an ATU tune (a carrier). While armed, the radio server now refuses a
  page's frequency, mode, filter, VFO, split or tuner change and VFO swap with a notice and puts
  the page back on the real setting, as it does while transmitting. The one change it lets
  through is the session's own frequency (within 10 Hz), as the rigctl Winlink lane already did.
- **`tests/affected.py` names a test after its longest module stem.** `station_id_test` belongs to
  `station_id.py`, so a change to `station.py` or `static/station.css` no longer selects it by name
  (19 tests had a shorter-stem owner too, e.g. `mesh_convs_test` under `mesh.*`). Imports and path
  mentions still select as before.
- A page message whose type is not a string no longer closes that page's connection; it is refused with a warning.
- Every map (the Map tab, Mesh, MeshCore, APRS): the wheel no longer runs away. A sideways trackpad swipe zoomed out a whole level each time (five swipes went from zoom 5 to 1) and a momentum tail did too; now a sideways swipe pans, a vertical scroll adds up to a level per mouse notch or deliberate swipe, and a pinch zooms in finer steps.
- Chat: a conversation hidden and shown again with no frame between (a quick tab switch) could still open on an old message.
- Mesh / MeshCore chat: a conversation drawn while its tab was hidden opened on its oldest message; it now opens on the newest.
- **Mission Control cards keep their own height.** A card in the same row as
  the wide radio card no longer stretches to match it, and on a phone the
  clock's UTC label sits beside the time instead of drifting to the right.

- **The first VARA HF call after a radio-server restart no longer fails.**
  xara keeps its PTT connection to its lane open and doesn't reconnect it, so
  after any restart of the radio's server (an update or a crash) its first key
  hit a dead socket, xara exited and launchd restarted it, and that call
  failed within seconds. The server now restarts xara's agent
  (`--winlink-vara-label`, rendered from `winlink.xara.label`) as soon as the
  lane listens again.
- **A Winlink session stuck in Pat now ends.** If Pat hasn't returned 10 s
  after a Stop pressed twice or the page closing, the station ends the
  session itself and says the modem may be stuck (a Mac station, 2026-11-17: Pat
  blocked on a wedged ardopcf before dialing, so its disconnect had nothing
  to stop and the page showed a session for minutes).
- **ARDOP with no sound card says so.** During an ARDOP session the station
  follows ardopcf's log; a "cannot open playback/capture device" line ends
  the session at once with "ARDOP is not on the air" and ardopcf's error,
  instead of a connect timeout that read as "no answer".
- **Mesh: a node with a big node list works again.** The node list goes to the
  server as one line, and asyncio's default 64 KiB cap per line stopped the
  server reading the node for good, quietly: no nodes, sends and asks never
  answered, while the page said connected. Seen live with 179 nodes (81 KB);
  the cap is now 4 MiB, and a line over it restarts the link with an error in
  the log.

- **Mesh: Reboot node, or saving a node setting, no longer leaves a dead link
  that says "connected".** The node restarts after both; the bridge now starts
  over on a new link 15 s later instead of waiting for the old one to notice.
  Seen live: after a Neighbors save the Bluetooth link died silently and every
  later command (a Reboot, two Neighbours asks) hung; over USB the save itself
  hung.

- **A mesh message the node never reports on no longer says "sent" forever.** After two minutes
  with neither an ACK nor a failure it shows "?" (no ACK came back) and offers Retry; a late ACK
  still settles it. Seen live: a reaction lost on the air stayed "sent" and nothing said so.

- **The Map tab centres on your grid and fills with tiles.** A 6-character station grid (as JTDX
  reports it) was refused, so the map had no "you" and a default centre; and a map drawn while its
  tab was hidden kept a zero size (one tile). Found on the first live look.

### Added

- **The station knows who its own node hears.** It asks its own node for its
  NeighborInfo a little after connecting and every 30 minutes (answered inside
  the node; nothing goes on the air), when the node has the module on; a page
  can ask too (Ask for → Neighbours on its own card). With every station
  holding its own node's list, the map's green "hears it" lines appear: who
  hears a selected node. The card no longer calls nodes at one position
  "not on the map": the map draws them apart now.

- **The Mesh map shows which way a node's links go, and nodes sharing a spot
  apart.** Select a node: green lines are the nodes that hear it, blue the
  ones it hears, purple both, each labelled with its SNR (both ways when both)
  and a tooltip saying who hears whom; the map's note gives the key. Nodes at
  the very same position (a channel shares positions only to a grid cell, so a
  site's nodes all land on its centre) are drawn on a small ring around it, so
  each one, and the links between them, can be seen.

- **A node's card lists who it hears and who hears it, by name.** Each with its
  SNR and a click through to that node; one the map can't draw says why (no
  position, the same spot, not in this node's list). The ranch's own nodes
  share a position, so their links were never visible lines.

- **Ask a node for its neighbours.** A node's card has Ask for → Neighbours: one
  small packet, and the answer lists every node it hears directly with SNR,
  which fills the map's links and "heard by" when yours is among them. Only a
  node with its NeighborInfo module on answers, at most once every 3 minutes
  (the firmware's limit, so the card waits that long too); a NO_RESPONSE says so.

- **The Mesh tab shows who hears your node.** "heard by N" in the header opens
  a list, one row a node: those whose NeighborInfo lists yours (with the SNR
  they hear you at), the first hop of a traceroute, and apart from them, nodes
  that ACKed your DM (they got it, maybe through relays). Nothing is sent to
  find out. NeighborInfo and traceroutes are now kept on disk for 7 days, so a
  restart no longer empties the map's links (nodes send NeighborInfo hours
  apart). Not in it: who rebroadcast your packet; the firmware drops that as
  a duplicate and tells the app only that someone did.

- **The Mesh tab does what the Meshtastic apps do.** Direct is now a list of
  conversations, one per node, each with its unread count; replies quote what
  they answer, and a message takes an emoji reaction (👍 ❤️ 😂 ‼️ ❓ 🎉, the
  apps' own `emoji` flag, so phones show them as reactions too); a failed send
  has Retry. A node's card adds air time, uptime, environment sensors,
  distance and bearing, when its position was fixed, whether it has a PKI key
  and who it hears (NeighborInfo, also drawn on the map with each link's SNR);
  from it, ask the node for its position, telemetry, environment or user info
  (one request per node at a time, each kind once a minute: every ask is
  airtime; a position ask sends an empty position, never ours), and Ignore or
  Remove it. The header shows the node's own counters (channel use, air time,
  packets, nodes online, noise floor, uptime), and Settings has Reboot node.
  A restart of the node's link no longer drops you from the channel you were
  reading to Direct.

- **The Map tab** (`addons/map.py`, `static/addons/map.js`): worked/confirmed grids by band, live
  decodes by need (click to answer), the gray line (sun position checked against NOAA reference
  points in a browser), POTA spots on the band.

- **The Roster tab** (`addons/roster.py`, `logsync_roster.py`): JTDX's decodes, live, marked by need,
  LoTW use, distance and bearing; click to answer (a Reply to JTDX, as a double-click). JTDX's Decode
  and Status are parsed to the layout of a real JTDX 2.2 capture (its short Status, null strings,
  padded messages). Reference data never fetched under `RANCH_RADIO_OFFLINE=1` (the tests set it).

- **The needs index** (`logsync_cty.py`, `logsync_needs.py`, `logsync_refdata.py`): DXCC entity
  from cty.csv (exact calls, longest prefix, portable forms, KG4), worked/confirmed by entity, band,
  mode group and grid from the ledger, LoTW users; `ranch-radio logsync needs`. On a real ledger the
  lookup agrees with Wavelog's DXCC on 99.6% of QSOs.
- **`tests/affected.py` sees `.csv` and `.bin` fixtures**, so a fixture change selects the test that
  reads it (the OpenGD77 `.bin` fixture selected nothing before).

- **Logbook sync settings on the Logbook tab.** Paths, addresses, the Wavelog profile and the
  TQSL location, planned as a host-file diff and applied with a sync restart (no radio restart);
  the three credentials write-only (set, length and date shown, never the value); a live
  read-only *Test* per destination.

### Fixed

- **logsync keeps JTDX's QSO times in UTC.** JTDX labels the times in its QSO Logged
  message as local time, but the values are UTC; logsync converted them from local and
  shifted every QSO by the station's UTC offset. The test JTDX now sends what real JTDX sends,
  and the time check runs in a non-UTC zone (CI runs in UTC, where the bug can't show).
- **`doctor` no longer reports a working TQSL as missing.** Real TQSL 2.8.6 prints its
  version for `-v` and exits 255; the check now reads the version line, not the exit code.
  The test's fake TQSL exits 255 the same way.
- **logsync matches phone QSOs on LoTW and QRZ.** The identity key carried SSB's sideband
  (USB/LSB), but LoTW and QRZ return phone QSOs as plain `SSB`, so every USB/LSB QSO looked
  missing and would have been re-signed. The sideband is no longer part of the key; a ledger
  built before this fix should be rebuilt (move `logsync.db` aside and restart).

### Changed

- **Winlink features are off the radio server's import path.** The server
  takes only `winlink_tail.py` (the end-of-session station-ID wait, moved
  verbatim from `winlink.py`), and `render.py` loads the P2P directory and
  listen-plan checkers by name from stdlib-only modules. A change to
  `p2p_directory.py` or `listen_plan.py` now selects 27 and 26 runs instead
  of 77 and 76, none of them transmit-safety or rig-contract tests;
  `tests/affected_test.py` guards the seam. `tests/run_all.sh` refuses a
  bare local run of the whole suite (`--full` to insist; CI is unaffected).

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

- **Logbook sync (`logsync`).** The station logs every QSO JTDX reports to
  Wavelog, QRZ and LoTW, and pulls receipts and confirmations back, so
  GridTracker's uploads can be switched off. It hears JTDX's UDP beside
  GridTracker (a multicast group) or as a unicast relay in front of it;
  GridTracker's click-to-reply keeps working either way. Every QSO lands in a
  ledger (`~/.local/state/radio-remote/logsync.db`, with an ADIF safety copy)
  with a state per destination; the Logbook tab shows the counts, holds and a
  tick per destination for recent QSOs. The first start imports the Wavelog
  log as held: `ranch-radio logsync backfill` reports it, and only
  `--apply HASH` releases it. Credentials are files (mode 600) the block
  names; TQSL runs without `-p` and is killed after 60 s.
  **Host file:** a `logsync` block (see `examples/hosts/mac-shack.json`).

- **Attended HF listening.** Answer HF peer-to-peer Winlink calls (ARDOP or
  VARA HF) while you are at the page: pick a channel from the host file's
  `winlink.listen_hf`, confirm, and the radio's lane is armed on that
  channel's dial frequency with Pat listening. It ends when the page that
  opened it closes, when nobody presses **I'm here** (asked every
  `presence_minutes`, default 15), at `max_minutes` (default 60), or on
  STOP TX. Channels must sit wholly inside an HF data sub-band at General
  privileges; the page shows which are inside a 47 CFR 97.221(b) segment.

- **P2P directory in Winlink's Peer mode.** A list of stations that asked to
  be listed for peer-to-peer Winlink; a click fills the callsign, dial
  frequency and via, and never connects. On by default where Winlink is on
  (one public read every 6 hours, the last copy kept); `"directory": false`
  in the `winlink` block turns it off.

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
