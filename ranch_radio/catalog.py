"""The module catalog: every module Ranch Radio has, in this repo.

Each entry says what a module is, where its code lives, and how it reaches a
host. Modules are rendered from the host file (~/.config/ranch-radio/hosts/<host>.json,
or the checkout's hosts/); manifest.py says what each needs.
"""

# kind: radio | service | addon | integration
MODULES = [
    # --- radios: one server.py per radio, picked by its rigs.py profile
    {"name": "ic7300", "kind": "radio", "title": "ICOM IC-7300 (CI-V, scope, USB audio)",
     "code": "server.py --rig ic7300 (civ.py)", "platforms": ["macos", "linux"],
     "ships": "macOS: a launchd agent; Linux: a systemd unit + socket, both rendered from the host file"},
    {"name": "g90", "kind": "radio", "title": "Xiegu G90 (CI-V via Digirig)",
     "code": "server.py --rig g90 (civ.py)", "platforms": ["linux"],
     "ships": "Linux: a systemd unit + socket (+ mixer drop-ins), rendered; on a Digirig, give it a stable device name (udev) or its /dev/serial/by-id path"},
    {"name": "qmx", "kind": "radio", "title": "QRP Labs QMX (Kenwood CAT, USB audio)",
     "code": "server.py --rig qmx (qmx.py)", "platforms": ["linux"],
     "ships": "Linux: a systemd unit + socket, rendered, bound to its /dev/serial/by-id path; macOS: a launchd agent"},
    {"name": "hamlib", "kind": "radio", "title": "Anything rigctld drives",
     "code": "server.py --rig hamlib (hamlib_rig.py)", "platforms": ["macos", "linux"],
     "ships": "as any radio, with rigctld running for the rig"},

    # --- services the station or a radio server runs
    {"name": "station", "kind": "service", "title": "The page: every radio, Winlink, Mesh, packet, the add-ons and "
                                                    "Modules; a shared rigctl port when asked (4532)",
     "code": "station.py", "platforms": ["macos", "linux"],
     "ships": "Linux: station.service + station.json; macOS: a launchd agent + station.json, both rendered from the host file"},
    {"name": "winlink", "kind": "addon", "title": "Winlink over ARDOP (Pat + ardopcf); on/off switch",
     "code": "addons/winlink.py runs Pat (winlink.py); the lane (codec_level.py, station_id.py) is core; "
             "ardop/ builds ardopcf", "platforms": ["macos", "linux"],
     "ships": "the station's winlink block (station.json), both OSes; each radio's lane is a flag on its server; macOS adds an ardopcf agent; Pat and ardopcf via doctor --fix; "
              "winlink.directory: the P2P directory the Peer picker reads (p2p_directory.py), on unless false"},
    {"name": "vara", "kind": "service", "title": "Winlink over VARA HF (xara, unregistered), beside ARDOP",
     "code": "station_id.py VaraLogTail (xara's CW ID), the VARA lane in server.py / rigctl_server.py, "
             "pat_config.py vara_refusal; xara itself is built by the operator (doctor prints how)",
     "platforms": ["macos"],
     "ships": "macOS: a xara launchd agent and three --winlink-vara-* flags on the radio's server, with the station arming VARA sessions over its lane, rendered "
              "from the host file's winlink.xara block"},
    {"name": "mercury", "kind": "service", "title": "Winlink over Mercury HF (Rhizomatica, VARA-TNC compatible), beside ARDOP",
     "code": "mercury_id.py (the station's own CW ID), the Mercury lane in server.py / rigctl_server.py, "
             "mercury_tnc.py (listener check, pinned INI); mercury/ carries the bind patch; Mercury itself "
             "is built by the operator (doctor prints how)",
     "platforms": ["macos"],
     "ships": "macOS: a ranch-radio.mercury launchd agent and its INI, and --winlink-mercury-* flags on the radio's "
              "server, rendered from the host file's winlink.mercury block while winlink.vara_modem is mercury "
              "(ranch-radio vara-backend switches it)"},
    {"name": "js8", "kind": "service", "title": "JS8 engine: jf8call (JS8Call-compatible) headless beside each radio, "
                                                 "receive-first, keyed only through the radio's own rigctl port",
     "code": "js8_engine.py (the launcher: version gate, safe settings put back at every start); jf8call is built "
             "by the operator from the Ranch Radio patches (doctor prints how)",
     "platforms": ["macos", "linux"],
     "ships": "a launchd agent (Mac) or <id>-js8.service (Linux) per radio with a js8_port, rendered from the "
              "host file's js8 block; its WebSocket on loopback only, for the station"},
    {"name": "js8call", "kind": "addon", "title": "The JS8 tab: band activity, messages and stations heard from each radio's "
                                                   "JS8 engine, and sending while armed (per radio, time-boxed)",
     "code": "addons/js8call.py (relays js8_bridge.py), static/addons/js8call.js, js8call.css",
     "platforms": ["macos", "linux"],
     "ships": "the host file's addons block; needs the js8 module for anything to show"},
    {"name": "mesh", "kind": "addon", "title": "Meshtastic node on the page (channels, conversations, replies and reactions, "
                                                 "nodes with telemetry and requests, map with neighbour links, who hears us, settings); "
                                                 "on/off switch",
     "code": "addons/mesh.py over mesh.py, mesh_bridge.py (runs in the meshtastic venv)", "platforms": ["macos", "linux"],
     "ships": "the station's mesh block (station.json), both OSes; the meshtastic venv via doctor --fix"},
    {"name": "packet", "kind": "service", "title": "Packet hub (APRS + Winlink: Bluetooth KISS, network KISS, "
                                                   "or Direwolf on a USB sound card cable, with an optional iGate)",
     "code": "tnc_hub.py, ax25.py, ax25_link.py, bt_rfcomm.py, aprs.py, aprs_store.py, static/aprs.js; "
             "direwolf_tnc.py runs Direwolf", "platforms": ["macos", "linux"],
     "ships": "rendered from the host file's packet block: Linux uvpro-hub.service or packet-hub.service "
              "(+ direwolf.service); macOS launchd agents for the hub (+ Direwolf)"},
    {"name": "logsync", "kind": "service", "title": "Logbook sync: every QSO JTDX logs, to Wavelog, QRZ and LoTW, "
                                                     "with confirmations back (replaces GridTracker's uploads)",
     "code": "logsync.py (senders, pulls), logsync_udp.py (JTDX's UDP: multicast beside GridTracker, or a unicast "
             "relay), logsync_ledger.py (SQLite ledger), logsync_adif.py, logsync_config.py "
             "(the block's checks, stdlib only: what render imports); the Logbook tab shows its status",
     "platforms": ["macos", "linux"],
     "ships": "the host file's logsync block, run by the station (station.json); TQSL for LoTW; credentials in "
              "files (mode 600) the block names; `ranch-radio logsync backfill` reviews the first import"},

    {"name": "wavelog_cat", "kind": "service", "title": "Wavelog CAT: each radio's frequency and mode to Wavelog's "
                                                         "live QSO form, and Wavelog's spot clicks back to the radio "
                                                         "(replaces WaveLogGate and the flrig it needed)",
     "code": "wavelog_cat.py (push, the /wavelog/qsy route; its block check is stdlib only: what render imports)",
     "platforms": ["macos", "linux"],
     "ships": "the host file's wavelog_cat block, run by the station (station.json); the Wavelog API key file "
              "logsync.wavelog names, or its own"},

    {"name": "audio", "kind": "service", "title": "Radio sound cards: pinned PipeWire names, never the desktop default",
     "code": "ranch_radio/render.py wireplumber_rules (and wait_audio in each radio unit)", "platforms": ["linux"],
     "ships": "Linux: a WirePlumber rule rendered from each radio's audio_in/audio_out (51-ranch-radio-audio.conf)"},

    {"name": "updates", "kind": "service", "title": "Daily update check: notify, or apply with health check and rollback",
     "code": "ranch_radio/updater.py (ranch-radio update --scheduled)", "platforms": ["macos", "linux"],
     "ships": "on unless the host file says \"updates\": false; macOS: a launchd agent; Linux: a systemd timer, both rendered"},

    # --- add-ons (addons/<name>.py + static/addons/<name>.js)
    {"name": "uvpro", "kind": "addon", "title": "UV-PRO / UV-50PRO control (channels CHIRP's way, settings, beacon)",
     "code": "addons/uvpro.py, addons/uvpro_bridge.py, addons/chirp.py (benlink venv)", "platforms": ["macos", "linux"],
     "ships": "the station's addons block (station.json), both OSes; "
              "through the packet hub's control port when there is one, else borrows the radio from it"},
    {"name": "opengd77", "kind": "addon", "title": "TYT MD-UV390 on OpenGD77: front panel, channels, VFOs and Options menus "
                                                  "(written to its memory and restarted on, no CAT exists); CPS-style codeplug editor via qdmr",
     "code": "addons/opengd77.py over opengd77.py (allowlisted link), opengd77_mem.py, qdmr.py (dmrconf) and qdmr_yaml.py; "
             "linux/udev/99-opengd77.rules", "platforms": ["macos", "linux"],
     "ships": "the host file's addons block, run by the station; dmrconf from qdmr"},
    {"name": "propagation", "kind": "addon", "title": "Space weather (NOAA K index, X-ray, solar flux) and what the station "
                                                     "heard per band and the next forecast openings, as badges on the band buttons and a Propagation tab",
     "code": "addons/propagation.py, static/addons/propagation.js (NOAA SWPC JSON; optional skywatch InfluxDB)",
     "platforms": ["macos", "linux"],
     "ships": "the host file's addons block; add \"skywatch\": {\"url\", \"db\"} for per-band decodes and forecast openings"},
    {"name": "map", "kind": "addon", "title": "Who you hear, who hears you (PSKReporter) and what is on the air, with the path to each station; "
                                              "grids worked and confirmed, the gray line and POTA spots",
     "code": "addons/map.py, static/addons/map.js (a choice of backgrounds, static/basemaps.js, fetched by the browser; POTA from api.pota.app; "
             "who hears you from the propagation add-on's PSKReporter poll; the radios' state from station.py's watches)",
     "platforms": ["macos", "linux"],
     "ships": "the host file's addons block; needs the logbook sync (logsync)"},
    {"name": "grids", "kind": "addon", "title": "The grid squares you still need, by operating mode and band: Lower-48 (FFMA) "
                                                "worked, confirmed and missing, the missing ones on the air now, grids worked worldwide",
     "code": "addons/grids.py, static/addons/grids.js; logsync_needs.py (grid_cells) and grid_targets.py (the 488 squares)",
     "platforms": ["macos", "linux"],
     "ships": "the host file's addons block; needs the logbook sync (logsync)"},
    {"name": "roster", "kind": "addon", "title": "Who JTDX hears now, coloured by what you still need (new DXCC, band, mode, grid); click to answer",
     "code": "addons/roster.py, static/addons/roster.js; logsync_roster.py and logsync_needs.py (the sync's intake and ledger)",
     "platforms": ["macos", "linux"],
     "ships": "the host file's addons block; needs the logbook sync (logsync) in unicast relay mode"},
    {"name": "halow", "kind": "addon", "title": "An OpenWrt Wi-Fi HaLow bridge (cameras on long-range Wi-Fi): who is on it and how well, "
                                                "on a map with which way each link is proven, and an OpenMANET mesh's nodes with the dBm each end hears the other at, "
                                                "the box's health, its DHCP guard, and its settings, changed by confirmed plans",
     "code": "addons/halow.py, halow_ap.py, halow_mesh.py, static/addons/halow.js (over SSH; nothing installed on the bridge)",
     "platforms": ["macos", "linux"],
     "ships": "the host file's addons block: \"host\" and \"key\"; \"position\" and \"places\" for the map; \"mesh\" (gate, nodes) for an OpenMANET mesh"},
    {"name": "imaging", "kind": "addon", "title": "Pictures off the air: SSTV (hams, the ISS) and HF weather fax, "
                                                  "decoded from a radio's receive audio; receive only",
     "code": "addons/imaging.py, static/addons/imaging.js; sstv.py, wefax.py and picture.py (the decoders, numpy)",
     "platforms": ["macos", "linux"],
     "ships": "the host file's addons block; numpy in the station's Python"},
    {"name": "logbook", "kind": "addon", "title": "Your web logbook (Wavelog, Cloudlog) in a Logbook tab beside Propagation",
     "code": "addons/logbook.py, static/addons/logbook.js (the browser frames the logbook; the server never talks to it)",
     "platforms": ["macos", "linux"],
     "ships": "the host file's addons block: \"url\", or \"port\" for a logbook on the radio's own machine"},
    {"name": "qso", "kind": "addon", "title": "Log a phone or CW contact by hand: frequency and mode from the radio, "
                                              "the call looked up on QRZ, the POTA park filled from the spots; "
                                              "QSOs wait in the logbook sync until Wavelog and QRZ can be reached",
     "code": "addons/qso.py, qso_lookup.py, static/addons/qso.js",
     "platforms": ["macos", "linux"],
     "ships": "the host file's addons block; the QRZ login is set from the tab (a QRZ XML subscription)"},

    # --- desktop integrations
    {"name": "macos-widget", "kind": "integration", "title": "Menu bar on/off for the radio agent",
     "code": "macos/widget (Swift)", "platforms": ["macos"],
     "ships": "macos/widget/build.sh builds the app; its agent is rendered from the host file"},
    {"name": "macos-webapp", "kind": "integration", "title": "Trust the cert, install the page as a Brave app",
     "code": "macos/install-webapp.sh", "platforms": ["macos"], "ships": "run by hand per Mac"},
    {"name": "omarchy-bar", "kind": "integration", "title": "Bar toggle, ATU tune and JTDX button",
     "code": "linux/omarchy (radio-server, jtdx-radio, *.qml, install.sh, git hook)", "platforms": ["linux"],
     "ships": "linux/omarchy/install.sh, re-run by a git hook after pulls"},
    {"name": "remote-audio", "kind": "integration", "title": "JTDX on another machine (audio over SSH/WebSocket)",
     "code": "linux/remote-audio", "platforms": ["linux"], "ships": "by hand: PipeWire pipe tunnels plus linux/remote-audio/ws_audio_bridge.py"},
]

KINDS = ["radio", "service", "addon", "integration"]


def by_name():
    return {m["name"]: m for m in MODULES}
