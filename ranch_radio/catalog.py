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
    {"name": "station", "kind": "service", "title": "Every radio on one page, shared rigctl 4532",
     "code": "station.py", "platforms": ["linux"],
     "ships": "Linux: station.service + station.json, rendered from the host file"},
    {"name": "winlink", "kind": "addon", "title": "Winlink over ARDOP (Pat + ardopcf); on/off switch",
     "code": "addons/winlink.py runs Pat (winlink.py); the lane (codec_level.py, station_id.py) is core; "
             "ardop/ builds ardopcf", "platforms": ["macos", "linux"],
     "ships": "flags on the radio's server (macOS, plus an ardopcf agent) or the station's winlink block (Linux), rendered; Pat and ardopcf via doctor --fix"},
    {"name": "mesh", "kind": "addon", "title": "Meshtastic node on the page (channels, DMs, nodes, map); on/off switch",
     "code": "addons/mesh.py over mesh.py, mesh_bridge.py (runs in the meshtastic venv)", "platforms": ["macos", "linux"],
     "ships": "macOS: --mesh-port on the radio's agent; Linux: the station's mesh block; the meshtastic venv via doctor --fix"},
    {"name": "packet", "kind": "service", "title": "Packet hub (APRS + Winlink: Bluetooth KISS, network KISS, "
                                                   "or Direwolf on a USB sound card cable, with an optional iGate)",
     "code": "tnc_hub.py, ax25.py, ax25_link.py, bt_rfcomm.py, aprs.py, aprs_store.py, static/aprs.js; "
             "direwolf_tnc.py runs Direwolf", "platforms": ["macos", "linux"],
     "ships": "rendered from the host file's packet block: Linux uvpro-hub.service or packet-hub.service "
              "(+ direwolf.service); macOS launchd agents for the hub (+ Direwolf)"},

    {"name": "audio", "kind": "service", "title": "Radio sound cards: pinned PipeWire names, never the desktop default",
     "code": "ranch_radio/render.py wireplumber_rules (and wait_audio in each radio unit)", "platforms": ["linux"],
     "ships": "Linux: a WirePlumber rule rendered from each radio's audio_in/audio_out (51-ranch-radio-audio.conf)"},

    {"name": "updates", "kind": "service", "title": "Daily update check: notify, or apply with health check and rollback",
     "code": "ranch_radio/updater.py (ranch-radio update --scheduled)", "platforms": ["macos", "linux"],
     "ships": "on unless the host file says \"updates\": false; macOS: a launchd agent; Linux: a systemd timer, both rendered"},

    # --- add-ons (addons/<name>.py + static/addons/<name>.js)
    {"name": "uvpro", "kind": "addon", "title": "UV-PRO control (channels CHIRP's way, settings, beacon)",
     "code": "addons/uvpro.py, addons/uvpro_bridge.py, addons/chirp.py (benlink venv)", "platforms": ["linux"],
     "ships": "Linux: the station's addons block; borrows the radio from the packet hub when there is one"},
    {"name": "opengd77", "kind": "addon", "title": "TYT MD-UV390 on OpenGD77: front panel, channels, VFOs and Options menus "
                                                  "(written to its memory and restarted on, no CAT exists); CPS-style codeplug editor via qdmr",
     "code": "addons/opengd77.py over opengd77.py (allowlisted link), opengd77_mem.py, qdmr.py (dmrconf) and qdmr_yaml.py; "
             "linux/udev/99-opengd77.rules", "platforms": ["macos", "linux"],
     "ships": "the host file's addons block (macOS: addons.json + --addons on the radio's agent); dmrconf from qdmr"},
    {"name": "propagation", "kind": "addon", "title": "Space weather (NOAA K index, X-ray, solar flux) and what the station "
                                                     "heard per band and the next forecast openings, as badges on the band buttons and a Propagation tab",
     "code": "addons/propagation.py, static/addons/propagation.js (NOAA SWPC JSON; optional skywatch InfluxDB)",
     "platforms": ["macos", "linux"],
     "ships": "the host file's addons block; add \"skywatch\": {\"url\", \"db\"} for per-band decodes and forecast openings"},

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
