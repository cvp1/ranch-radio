# Ranch Radio

A browser control panel for amateur radio transceivers on your local network.
Plug a radio into a Mac or a Linux machine, and any phone, tablet or laptop on
the LAN can tune it, see its spectrum, hear it and talk through it. A Linux
machine with several radios puts them all on one station page. Add-on modules
bring in Winlink, Meshtastic, APRS packet and handheld-radio control.

```
radio ──USB──> host (Ranch Radio) ──LAN──> browser(s)
       CAT serial + USB audio
```

## What you get

- **The radio on a page.** A big tunable frequency display, the radio's own
  spectrum and waterfall (where it has one), S-meter and transmit meters,
  band, mode and filter, AF/RF/squelch/power, DSP toggles and the tuner.
- **Audio both ways.** Receive audio in the browser, and push-to-talk with the
  device's microphone, with a per-band transmit level.
- **Apps keep working.** Each radio answers Hamlib's rigctl protocol on its
  own port, so WSJT-X, JTDX and other apps share the radio with the page.
- **Modules** you switch on per machine, from a **Modules** tab on the page or
  the command line (below).

## Supported radios

| Rig | Radio | Link | Status |
|---|---|---|---|
| `ic7300` | ICOM IC-7300 | CI-V over USB | **Tested** |
| `g90` | Xiegu G90 | CI-V (via a Digirig or similar) | **Tested** |
| `qmx` | QRP Labs QMX / QMX+ | Kenwood-style CAT over USB | **Tested** |
| `ic705` | ICOM IC-705 | CI-V over USB | Built from the CI-V reference |
| `ic7610` | ICOM IC-7610 | CI-V over USB | Built from the CI-V reference |
| `ic9700` | ICOM IC-9700 | CI-V over USB | Built from the CI-V reference |
| `ic7100` | ICOM IC-7100 | CI-V over USB | Built from the CI-V reference |
| `x6100` | Xiegu X6100 | CI-V | Built from the CI-V reference |
| `hamlib` | anything `rigctl -l` lists | via rigctld | Generic |

"Tested" means run against the real radio. The page shows only the controls
a radio has.

## Install (macOS or Linux)

You need Python 3.10 or newer and git. Plug the radio in first.

```sh
git clone https://github.com/cvp1/ranch-radio.git ~/ranch-radio
cd ~/ranch-radio && ./install.sh --call YOURCALL
```

`install.sh` is safe to run again. It:

1. Makes a Python venv (`.venv`) and puts `ranch-radio` on your PATH
   (`~/.local/bin`).
2. Runs `ranch-radio init`, which finds the radio's serial port, guesses the
   rig from its USB names, pairs its sound card on Linux, and writes this
   machine's host file to `~/.config/ranch-radio/hosts/<host>.json`. If it
   can't tell, it lists what it found: pass `--rig` and `--port`.
3. Runs `ranch-radio doctor --fix`, which installs what needs no root.
4. Installs and starts the services: a launchd agent on macOS, systemd user
   units on Linux.
5. Runs `ranch-radio doctor`, which lists anything left with its fix. Steps
   that need root, like joining the serial port's group, are printed for you.

Then open `https://<this-machine>:7300/`. The certificate is self-signed; on a
Mac, `macos/install-webapp.sh` trusts it and installs the page as an app.

**Without `--call` the radio is receive-only.** To allow transmit later:
`ranch-radio init --force --call YOURCALL`, then `ranch-radio update --apply`.

## Everyday commands

```sh
ranch-radio doctor            # what this machine's modules need, and whether it has it
ranch-radio update --apply    # git pull, install, restart what changed (never a radio in use)
ranch-radio add mesh          # switch a module on (dry run; --apply writes the host file)
ranch-radio remove mesh       # switch it off
ranch-radio modules           # every module and what it needs
ranch-radio drift             # installed files vs. the host file
```

## Modules

| Module | What it adds | Needs |
|---|---|---|
| `winlink` | Winlink email over HF (ARDOP), from the page | Pat; ardopcf (built from `ardop/`) |
| `mesh` | A Meshtastic node on the page: channels, DMs, nodes, map | a node over USB or Bluetooth; the `meshtastic` package |
| `packet` | A network KISS TNC (Direwolf) or UV-PRO over Bluetooth as an APRS and Winlink packet hub (Linux) | Network or BlueZ |
| `uvpro` | UV-PRO control: channels, settings, beacon (Linux) | `benlink` |
| `opengd77` | A TYT MD-UV390 on OpenGD77 firmware: front panel, channels, codeplug editor | `dmrconf` (qdmr) |
| `macos-widget` | A menu-bar on/off switch for the radio server | Xcode command-line tools |
| `omarchy-bar` | Bar toggle, tune button and JTDX launcher on Omarchy Linux | Omarchy |

Add one with `ranch-radio add <module>` or from the page's **Modules** tab;
either shows the change to the host file first. Then run
`ranch-radio doctor --fix` and `ranch-radio update --apply`. Each add-on also
has an on/off switch on the page.

## Safety

- **Receive-only until you give a callsign** (`init --call`).
- **Transmit limits.** Every transmission has a time limit (60 s in the host
  file `init` writes), and the radio unkeys if the operator's browser goes
  quiet.
- **Winlink.** The modem's rigctl lane keys only during an armed session, and
  sessions end with a station ID.
- **LAN only.** The servers refuse requests from web pages on other sites
  (Host and Origin checks), so a page from the internet, open in a browser on
  your LAN, can't reach the radio. There is no login: run it on a network you
  trust, or start the server with `--token`.

You are responsible for operating within your license.

### Linux Sound Card Node Stability & WirePlumber

On Linux, Ranch Radio pairs USB audio sound cards via PipeWire. Replugging a device
or system reboot can cause dynamic ALSA enumeration shifts. Ranch Radio's installer
renders WirePlumber rules (`~/.config/wireplumber/wireplumber.conf.d/51-ranch-radio-audio.conf`)
from each radio's configured `audio_in` and `audio_out` node names, matching nodes
and enforcing persistent node naming and session priority.

Verify active sound card node names at any time with:
```sh
wpctl status
# or
pw-cli list-objects Node | grep -E 'node.name =|media.class ='
```

### Network KISS TNC & Packet Hub

For packet radio (APRS & Winlink), Ranch Radio supports network KISS TNCs (such as
Direwolf on `tcp:host:port`) as well as Bluetooth TNCs. When using a network KISS TNC:
- Set `"tnc": "tcp:<host>:<port>"` in the host configuration's `"packet"` block.
- Ranch Radio renders a dedicated `packet-hub.service` systemd unit, entirely decoupled
  from Bluetooth, BlueZ, or UV-PRO dependencies.
- The station dashboard presents the packet hub with its configured name (e.g. `Packet (Direwolf)`).

## How it fits together

- `server.py` runs one radio: its page, audio, rigctl port and add-ons.
- `station.py` (Linux) puts every radio on one page, with a shared rigctl port.
- `ranch_radio/` is the shipping layer. Each machine's radios, services and
  modules are declared in one host file, and `ranch-radio install` renders the
  launchd agents or systemd units from it. `examples/hosts/` has a Mac with
  one radio, a two-radio Linux station and a Linux laptop.
- `addons/` holds the switchable modules; `static/` is the page (no build step).

## Development

```sh
.venv/bin/python tests/<name>_test.py     # one suite
bash tests/run_all.sh                      # everything (page tests need Chrome or Chromium)
```

Tests use simulated radios and stand-in service managers; none touches a real
radio.

## License

MIT. See [LICENSE](LICENSE).
