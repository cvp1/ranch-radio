# Ranch Radio — agent brief

Read this before touching code. It is the map the README does not give:
where things live, the rules that bite, and how to run only what you changed.
The README is for operators; CONTRIBUTING.md is the pull-request flow.

## What this is

A browser control panel for amateur radio transceivers on a LAN, plus optional
modules (Winlink, Meshtastic, MeshCore, APRS packet, handheld control). Python
3.12+, aiohttp, plain JS, no build step. Two hosts in the wild: a Mac with one
radio, a Linux station with several; both run the same two processes: one
`server.py` per radio (loopback, behind the station) and one `station.py` (the
page at 7300). Changes reach a radio only when the operator runs
`ranch-radio update --apply` there; nothing pulls on its own.

## The map

| Layer | Files | Role |
|---|---|---|
| Rig control | `radio.py` `rigs.py` `civ.py` `qmx.py` `hamlib_rig.py` `sim.py` | state model, rig profiles (`rigs.PROFILES`), CAT backends, the simulator |
| Radio server | `server.py` (`Station`) `rigctl_server.py` `audio.py` `tx_audio.py` `codec_level.py` `station_id.py` `webguard.py` | one process per radio: its page, WebSocket, audio, rigctl port, the Winlink lane and TX gates |
| Station | `station.py` (`Station`) | one per host, both OSes: the page, Pat (sessions arm a radio's lane over its WebSocket: `RemoteLane`), Mesh, packet tab, add-ons, Modules; Linux adds a shared rigctl port |
| Packet | `tnc_hub.py` `ax25*.py` `aprs*.py` `digi.py` `igate*.py` `bt_rfcomm.py` `direwolf_tnc.py` | the APRS/Winlink packet hub and its TNCs |
| Winlink | `winlink.py` `pat_config.py` `listen_plan.py` | runs Pat; the lane and codec level above are core |
| Mesh | `mesh.py` `mesh_bridge.py` `mesh_feed.py` `meshcore_bridge.py` | Meshtastic and MeshCore bridges (own venvs) |
| Logbook sync | `logsync.py` `logsync_config.py` `logsync_udp.py` `logsync_ledger.py` `logsync_adif.py` | the station's JTDX -> Wavelog/QRZ/LoTW uploads and pulls; the Logbook tab shows its status |
| Add-ons | `addons/<name>.py` + `static/addons/<name>.js` | switchable modules; contract in `addons/__init__.py` |
| Ops | `ranch_radio/` | the `ranch-radio` CLI: host file, render, doctor, install, drift, update, add/remove, init |
| Web | `static/` | `index.html` (radio page), `station.html`, `packet.html`; `nav.js` groups, `addons.js` registry, `modules.js` |
| Tests | `tests/` | 110+ standalone scripts, `tests/run_all.sh` runs them |

`ranch_radio/catalog.py` describes every module in prose; `ranch_radio/manifest.py`
says what each needs in a form `doctor` checks. Both are hand-maintained.

## The host file is the one source of truth

`~/.config/ranch-radio/hosts/<host>.json` (outside the checkout; `ranch_radio/paths.py`).
`render.py` turns it into launchd plists or systemd units, `install` writes them,
`drift` compares, `doctor` checks what the machine lacks. Never write a rendered
file by hand. Example host files: `examples/hosts/`.

## Rules that bite

- **The station orchestrates; the radio server keys.** Winlink sessions (Pat),
  listen windows, Mesh, the packet tab, the Modules tab and the host's add-ons are
  the station's, on both OSes. The radio server owns everything that touches the
  transmitter: the lane, arming, codec level, station ID, STOP TX. A session
  reaches a radio over `station.RemoteLane` (its WebSocket: `winlink_arm` with
  the scheme and QSY, `winlink_end`); between sessions `station.RadioWatch` reads
  the radio's lane state for the panel. Do not add orchestration to `server.py`
  (its `--addons` is for panel add-ons on a radio's own page only); do not move a
  TX gate into `station.py`.
- **Tests that need Pat use `tests/winlink_test_lib.start()`**, which runs a
  simulated radio behind a station and routes server.py-style flags to the right
  process (`split_flags`). Radio-side messages (`winlink_arm`, `tx_stop`) go to
  the radio's page (`L.RWEB`); sessions and the mailbox to the station's (`L.WEB`).
- **Render is the only way a host gets its processes.** `render.py` emits the
  station agent/unit, `station.json` and one agent/unit per radio from the host
  file; `tests/fixtures/render-golden.json` pins the examples' output and
  `tools/render_golden.py` refreshes it on a deliberate change only.
- **No transmitting in tests.** The suite drives fakes (`tests/fake_*.py`). Anything
  that changes how or when a radio keys needs a test proving it stays safe
  (`civ_safety_test`, `tx_stop_test`, `tx_state_test`, `winlink_lane_test`).
- **Receive-only by default.** No callsign, no TX. Every transmission has a time
  limit. STOP TX latches. The hub's Off survives a restart. Don't loosen any of these.
- **Nothing personal in the tree.** `tests/public_hygiene_test.py` is required: no
  real host files, no home paths but `/Users/you` or `/home/you`, no LAN addresses
  but `192.168.1.x`, no logs, captures or certificates. Example callsigns are
  `N0CALL` / `N9ABC`; example hosts are `mac-shack` / `linux-station`.
- **An add-on that breaks must not take the server down.** Import or start
  failures show as "not loaded" with the reason; the radio runs on.
- **Headless pages, CORS and Host checks** live in `webguard.py`. Every new HTTP
  route goes through `authorised`.
- **Legacy name.** State and config paths still say `radio-remote`
  (`~/.config/radio-remote/`, `~/.local/state/radio-remote/`). Keep them; a rename
  needs a migration.

## Adding an add-on (five touchpoints)

`ranch-radio new-addon NAME --label "Label" --title "What it does" [--group tools] [--css]`
writes the first three and prints the other two:

1. `addons/NAME.py` — an `Addon` subclass, `ADDON = ...`; check `opts` in
   `__init__` and raise `ValueError` naming the field.
2. `static/addons/NAME.js` — `addons.register(NAME, {mount, hello, event, off, tab, card})`.
3. `tests/NAME_addon_test.py` — loads it in the registry; fails until 4 and 5 exist.
4. `ranch_radio/catalog.py` — a `MODULES` entry (kind `addon`).
5. `ranch_radio/manifest.py` — a `MANIFESTS` entry (`family: addon`, `block: ("addons", NAME)`,
   a template, `ASK` fields with `rules`, `needs`).

Then the README's Modules table, and `CHANGELOG.md` under Unreleased. An add-on that
owns a device runs in one process only (`addons/__init__.py`). Groups are
`radios`, `messaging`, `mesh`, `tools` (`static/nav.js`).

## Running tests

```sh
python3 tests/affected.py                  # which runs your change touches (vs main)
python3 tests/affected.py --run            # run just those
python3 tests/<name>_test.py               # one test, directly
python3 tests/public_hygiene_test.py       # required, seconds
bash tests/run_all.sh                      # everything, serial, ~30 min locally
RR_SHARD=2/4 bash tests/run_all.sh         # one CI shard
bash tests/flake_hunt.sh                   # the suite at CI's CPU budget (Docker)
```

Page tests need Chrome or Chromium (`CHROME=`). The rigctl contract needs Hamlib.
Each run gets its own scratch `HOME`; a test must never read the real one.

Test style: a standalone script with a docstring that lists what it covers, a
`check(name, got, want)` function, fakes that behave like the hardware, and
`testkit.wait_for` instead of `sleep`. Ports come from `testkit.free_port()`.

## Conventions

- Comments carry the **why**, often with the date and host it was learned on.
  Keep that; it is the project's memory.
- `CHANGELOG.md` Unreleased gets a line for anything an operator would notice.
- One concern per pull request; CI must be green; merge with a merge commit.
- New Python dependencies go in `requirements.txt` with a reason in the PR.
  Add-ons with heavy dependencies (meshtastic, benlink) get their own venv
  through `doctor --fix`, never the core one.
- `tests/ci/durations.json` is refreshed from a green CI run
  (`gh run view <id> --log | python3 tests/ci/shard.py refresh`) when a test's
  time changes a lot.
