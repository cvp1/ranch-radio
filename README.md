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

You need Python 3.12 or newer and git. Plug the radio in first.

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
ranch-radio update --check    # is there an update? (exit 2 when there is)
ranch-radio update            # what an update would pull and change (pulls nothing)
ranch-radio update --apply    # pull, install, restart what changed (never a radio in use)
ranch-radio add mesh          # switch a module on (dry run; --apply writes the host file)
ranch-radio remove mesh       # switch it off
ranch-radio modules           # every module and what it needs
ranch-radio drift             # installed files vs. the host file
```

## Updates

`ranch-radio update --apply` is the one update command, and it is safe to run
any time:

- **Packages first.** It checks that the host's Python has what
  `requirements.txt` asks for, and installs it into the checkout's `.venv`. If
  the host runs another Python (a system one), it stops and says what to
  install, and the code goes back where it was. Nothing restarts.
- **Radios in use are left alone.** A radio that is transmitting or in a
  Winlink session keeps running. It is restarted on the new code at the next
  update, and `--check` reports it until then. A radio you switched off stays
  off.
- **Checked, and rolled back.** After restarting, every server that was serving
  must serve again within a minute. If one doesn't, the code goes back to the
  commit it came from, the old code is restarted, and the update confirms
  those servers serve again. The same happens if the new code crashes before
  it can check itself. That commit is not tried again until a newer one lands
  (`--force` tries it anyway).
- **It knows what the servers run.** If the checkout moves without an update,
  for example by a hand `git pull`, `--check` says so. The next
  `update --apply` restarts the servers on it, with the same checks.

**The daily check.** Every host runs `ranch-radio update --scheduled` at 10:00:
a launchd agent on macOS, a systemd timer on Linux. It shows a desktop
notification once per new commit. To have it apply updates itself, with the
same checks and rollback, or to change the time, add to the host file:

```json
"updates": {"apply": true, "at": "10:00"}
```

`"updates": false` turns the daily check off. An installed job then does
nothing; to remove it, run `launchctl bootout gui/$(id -u)/org.ranch-radio.update`
on a Mac or `systemctl --user disable --now ranch-radio-update.timer` on Linux.
When nothing new has landed, the job leaves host-file edits for you to install
with `update --apply`. A code update installs everything the host file
renders. The job skips a run when no server is serving, because it would
have nothing to check the new code against. On a Mac its log
is `~/Library/Logs/ranch-radio-update.log`; on Linux,
`journalctl --user -u ranch-radio-update`.

## Modules

| Module | What it adds | Needs |
|---|---|---|
| `winlink` | Winlink email over HF (ARDOP), from the page | Pat; ardopcf (built from `ardop/`) |
| `mesh` | A Meshtastic node on the page: channels, DMs, nodes, map | a node over USB or Bluetooth; the `meshtastic` package |
| `packet` | A network KISS TNC (Direwolf) or UV-PRO over Bluetooth as an APRS and Winlink packet hub (Linux) | Network or BlueZ |
| `uvpro` | UV-PRO control: channels, settings, beacon (Linux) | `benlink` |
| `opengd77` | A TYT MD-UV390 on OpenGD77 firmware: front panel, channels, codeplug editor | `dmrconf` (qdmr) |
| `propagation` | Space weather (K index, X-ray, solar flux) under the band buttons; per-band decodes heard and who heard you, as badges and a Propagation tab | Internet for NOAA; optional skywatch InfluxDB and PSKReporter callsign |
| `macos-widget` | A menu-bar on/off switch for the radio server | Xcode command-line tools |
| `omarchy-bar` | Bar toggle, tune button and JTDX launcher on Omarchy Linux | Omarchy |

Add one with `ranch-radio add <module>` or from the page's **Modules** tab;
either shows the change to the host file first. Then run
`ranch-radio doctor --fix` and `ranch-radio update --apply`. Each add-on also
has an on/off switch on the page.

The `propagation` add-on reads NOAA's public space weather feeds with no
setup. Two optional sources fill in the bands:

```json
"addons": {"propagation": {"enabled": true,
                           "skywatch": {"url": "http://192.168.1.20:8086", "db": "skywatch"},
                           "pskreporter": {"call": "N0CALL"},
                           "window_min": 60}}
```

A band with no decodes reads "no data", never closed: a receiver hears only
the band it's tuned to. PSKReporter is asked at most every 10 minutes.

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

For packet radio (APRS & Winlink), Ranch Radio's packet hub can use a network
KISS TNC such as [Direwolf](https://github.com/wb2osz/direwolf) as well as a
Bluetooth TNC. Direwolf owns the sound card and modem; the hub connects to its
KISS-over-TCP port and adds the APRS store, the web page, transmit safety, and
AGWPE and KISS ports for Pat and other clients. A host with no radios of its own
can run it.

**1. Direwolf.** Enable its KISS port in `direwolf.conf`:

```
MYCALL N9ABC-10
MODEM 1200
KISSPORT 8001
```

[`examples/direwolf/direwolf.conf`](examples/direwolf/direwolf.conf) is a complete
example with every transmitting line (beacon, digipeater, iGate) commented out.

**2. Host file.** Point the `"packet"` block at it with `tcp:<host>:<port>`:

```json
"packet": {"url": "http://127.0.0.1:7310", "agwpe": "127.0.0.1:8000",
           "tnc": "tcp:direwolf.local:8001", "mycall": "N9ABC",
           "allow_calls": ["N9ABC"], "tx": false}
```

[`examples/hosts/linux-direwolf.json`](examples/hosts/linux-direwolf.json) is a
whole host file. Then `ranch-radio install --apply` renders a dedicated
`packet-hub.service` systemd unit, with no Bluetooth, BlueZ or UV-PRO
dependency. The station dashboard shows the hub under its configured name (for
example `Packet (Direwolf)`).

**3. Check it.** The hub page is at `http://127.0.0.1:7310/`, and
`curl http://127.0.0.1:7310/api/status` shows `"radio": {"up": true}` once it
has connected. Received frames appear in the heard list and the APRS store.

- **Receive-only until you say so.** `"tx": false` (or leaving it out) never
  transmits. Set `"tx": true` to allow it. Every frame must come from one of
  `allow_calls` (any SSID); anything else is refused and logged.
- **No authentication.** Direwolf's KISS and AGWPE ports have none, and anyone who
  can reach the KISS port can transmit through your radio. Firewall it to the
  machine that runs Ranch Radio. The hub's own AGWPE and KISS ports bind to
  `127.0.0.1` by default.
- **Same machine?** The hub listens on `127.0.0.1:8000` (AGWPE) and `:8001` (KISS).
  If Direwolf runs on the same machine, give Direwolf other ports (for example
  `KISSPORT 8011`) and use that port in `"tnc"`.
- **If Direwolf goes away,** the hub keeps running and reconnects by itself,
  retrying after 2, 4, 8, 16, then every 30 seconds.

### Direwolf on a USB sound card cable (AIOC), and an iGate

A VHF/UHF radio with no built-in TNC can do packet through a sound card
cable, such as an [AIOC](https://github.com/skuep/AIOC), a Digirig, or a CM108
dongle, with Direwolf as the modem. Ranch Radio runs both Direwolf and the
packet hub, on macOS or Linux:

```json
"packet": {"url": "http://127.0.0.1:7310", "agwpe": "127.0.0.1:8000",
           "tnc": "direwolf", "mycall": "N9ABC", "allow_calls": ["N9ABC"], "tx": false,
           "direwolf": {"usb": "1209:7388", "audio": "AIOC Audio", "ptt": "off"}}
```

- **`usb`** is the cable's USB ID, VID:PID; an AIOC is `1209:7388`.
  Direwolf waits until the cable is plugged in. If the cable drops off USB,
  Direwolf restarts and waits for it again, because it can't recover a sound
  card that went away.
- **`audio`** is the cable's sound card as Direwolf names it. An AIOC is
  `AIOC Audio` on a Mac. Direwolf's log lists every name it sees.
- **Direwolf has no network ports.** The hub talks to it through a
  pseudo-terminal (`/tmp/kisstnc`). Direwolf's KISS and AGWPE ports would
  listen on every interface with no authentication, which would let anyone
  transmit past the hub's transmit gate.
- **Transmit needs both switches.** `"ptt": "serial"` lets Direwolf key the
  radio through the cable's serial port: DTR on, RTS off, which is the AIOC's
  default. `"tx": true` lets the hub send. Both are off by default.
- **iGate, receive only:** add
  `"igate": {"login": "N9ABC-10", "passcode": "~/.config/ranch-radio/aprs-is-passcode"}`
  to the `direwolf` block, and put your APRS-IS passcode in that file.
  Received packets go to APRS-IS. Nothing from APRS-IS is ever sent on the
  air.
- **On a Mac, allow the microphone.** macOS asks whether the service may use
  the microphone the first time Direwolf opens the cable's sound card. Answer
  Allow, or Direwolf hears silence.

[`examples/hosts/mac-aioc.json`](examples/hosts/mac-aioc.json) is a whole
host file. `ranch-radio doctor` checks that Direwolf is installed.

## How it fits together

- `server.py` runs one radio: its page, audio, rigctl port and add-ons.
- `station.py` (Linux) puts every radio on one page, with a shared rigctl port.
- `ranch_radio/` is the shipping layer. Each machine's radios, services and
  modules are declared in one host file, and `ranch-radio install` renders the
  launchd agents or systemd units from it. `examples/hosts/` has a Mac with
  one radio, a two-radio Linux station, a Linux laptop and a radio-less packet
  hub on a Direwolf TNC.
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
