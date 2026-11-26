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
| Radio server | `server.py` (`Station`) `rigctl_server.py` `audio.py` `tx_audio.py` `codec_level.py` `station_id.py` `mercury_id.py` `cw_id.py` `modem_status.py` `webguard.py` `logcap.py` `winlink_tail.py` | one process per radio: its page, WebSocket, audio, rigctl port, the Winlink lane and TX gates, the modems' status from their logs; `winlink_tail.py` holds a Winlink transmit armed until the modem's station ID is out (split from `winlink.py` so this process doesn't load the Winlink stack); `mercury_id.py`: the station's own CW ID on Mercury HF's lane (Mercury sends none), rendered by `cw_id.py` (pure) |
| Station | `station.py` (`Station`) `station_peers.py` `station_peers_config.py` | one per host, both OSes: the page, Pat (sessions arm a radio's lane over its WebSocket: `RemoteLane`), Mesh, packet tab, add-ons, Modules; Linux adds a shared rigctl port. `station_peers.py`: other stations' radios on the LAN (`station.peers`), listed as `<radio>@<peer>` and proxied through that station with its certificate pinned, never this station's rigctl port, Winlink or JS8 radio; `station_peers_config.py`: their host-file check (stdlib only, because `ranch_radio/render.py` checks it) |
| Packet | `tnc_hub.py` `ax25*.py` `aprs*.py` `digi.py` `igate*.py` `bt_rfcomm.py` `direwolf_tnc.py` | the APRS/Winlink packet hub and its TNCs |
| Winlink | `winlink.py` `pat_config.py` `listen_plan.py` `p2p_directory.py` `p2p_directory_config.py` `p2p_intake.py` `ardop/` | runs Pat; the lane and codec level above are core. `p2p_directory*.py`: the read-only P2P directory behind the Peer picker, and its host-file block (stdlib only, because `ranch_radio/render.py` checks it); `p2p_intake.py`: the maintainer's listing requests from Pat's inbox; `ardop/`: the ARDOP modem build and patches |
| Mesh | `mesh.py` `mesh_convs.py` `mesh_bridge.py` `mesh_feed.py` `meshcore_bridge.py` `mesh_stats.py` `mesh_scan.py` | Meshtastic and MeshCore bridges (own venvs); `mesh_convs.py` keeps the Mesh tab's conversation state (archive, mute, pin, read) and its delete trash; `mesh_stats.py` adds up what the node hears for the Health view (pure, no I/O); `mesh_scan.py` lists the radios in USB and Bluetooth reach for the tab's "Find radios" (the nodes' venv; opens nothing) |
| Logbook sync | `logsync.py` `logsync_config.py` `logsync_udp.py` `logsync_ledger.py` `logsync_adif.py` `logsync_settings.py` `logsync_cty.py` `logsync_refdata.py` `logsync_needs.py` `logsync_roster.py` `grid_targets.py` | the station's JTDX -> Wavelog/QRZ/LoTW uploads and pulls; the Logbook tab shows its status and edits `logsync_settings.py`. What's still needed (new DXCC, band, grid) for the roster and map: `logsync_needs.py`, from `logsync_cty.py` (callsign -> DXCC) and `logsync_refdata.py` (cached cty.csv and LoTW users); `logsync_roster.py`: who JTDX hears now; `grid_targets.py`: the grid sets the Grids tab counts against |
| JS8 | `js8_engine.py` `js8_bridge.py` `js8/` | `js8_engine.py`: one jf8call engine per radio, keyed through that radio's rigctl port (it transmits); `js8_bridge.py`: the station's client for each engine, which also arms JS8 transmit from the page; `js8/`: the jf8call build and patches |
| DMR handhelds | `opengd77.py` `opengd77_mem.py` `opengd77_screen.py` `opengd77_font.py` `qdmr.py` `qdmr_yaml.py` | OpenGD77 radios over USB, which have no CAT: identity and screen over the CPS protocol, settings and channels written to flash (`opengd77_mem.py`, pure), screen text read with the firmware's own fonts (`opengd77_font.py`, generated); `qdmr.py` and `qdmr_yaml.py` read and write codeplugs with qdmr's dmrconf |
| Imaging | `picture.py` `sstv.py` `wefax.py` | the Imaging add-on's decoders, pure and numpy-only (the core never imports them): `picture.py` the tone discriminator, sample buffer, pixel sampler and PNG writer; `sstv.py` VIS header to slant-corrected colour picture (Martin, Scottie, Robot, PD); `wefax.py` HF radiofax, start/stop tones, phasing (line start; the period is nominal plus the host file's `clock_ppm`). Tested against independently encoded signals (`tests/imaging_synth.py`) |
| Wavelog CAT | `wavelog_cat.py` | the station's radio frequency/mode to Wavelog, and Wavelog's spot clicks back (`/wavelog/qsy/`, Origin-pinned); replaces WaveLogGate |
| Add-ons | `addons/<name>.py` + `static/addons/<name>.js` `halow_ap.py` | switchable modules; contract in `addons/__init__.py`. `halow_ap.py` is the HaLow add-on's access-point side: one fixed read script over SSH, and the exact command each change runs |
| Ops | `ranch_radio/` | the `ranch-radio` CLI: host file, render, doctor, install, drift, update, add/remove, init, status/stop/restart (idle only), logs, backup |
| Web | `static/` | `index.html` (radio page), `station.html`, `packet.html`; `nav.js` groups, `addons.js` registry, `modules.js` |
| Tests | `tests/` | standalone scripts, `tests/run_all.sh` runs them; `tests/code_map_test.py` fails when a root module or folder is missing from this table |
| The rest | `linux/` `macos/` `tools/` `examples/` `docs/` `changelog.d/` | OS extras (udev rules, Omarchy, the macOS web app and widget); maintainer tools (`tools/render_golden.py`, `tools/changelog_fold.py`, firmware and fixture helpers); example host files and Direwolf configs; README images; changelog fragments |

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
  limit. STOP TX latches and, like the hub's Off, survives a restart (an unreadable
  state file starts stopped). Don't loosen any of these.
- **Nothing personal in the tree.** `tests/public_hygiene_test.py` is required: no
  real host files, no home paths but `/Users/you` or `/home/you`, no LAN addresses
  but `192.168.1.x`, no logs, captures or certificates. Example callsigns are
  `N0CALL` / `N9ABC`; example hosts are `mac-shack` / `linux-station`.
- **An add-on that breaks must not take the server down.** Import or start
  failures show as "not loaded" with the reason; the radio runs on.
- **Headless pages, CORS and Host checks** live in `webguard.py`. Every new HTTP
  route goes through `authorised`.
- **Operator or read-only** (`webguard.AuthPolicy`, host file `auth`): every
  WebSocket command a read-only page may send is in a `READONLY_OK` set
  (server.py, station.py), and every mutating HTTP route is closed to it by
  `auth_guard`. A new command is operator-only unless it only stops. A
  connection this host makes to its own radio sends `auth.bearer_headers()`.
- **Pages run under `webguard.CSP`**: no inline `<script>`, no `on*=` attributes,
  no `eval` (`tests/xss_hygiene_test.py`). Text from the air goes through `esc()`
  or `textContent`. A page test that evals inside a frame calls
  `Page.setBypassCSP` (see `operate_page_test`).
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

Then the README's Modules table, and a `changelog.d/<branch>.md` fragment. An add-on that
owns a device runs in one process only (`addons/__init__.py`). Groups are
`radios`, `messaging`, `mesh`, `tools` (`static/nav.js`).

## Running tests

```sh
python3 tests/affected.py                  # which runs your change touches (vs main)
python3 tests/affected.py --run            # run just those
python3 tests/<name>_test.py               # one test, directly
python3 tests/public_hygiene_test.py       # required, seconds
uvx ruff check .                           # the linter CI runs (pyproject.toml)
bash tests/run_all.sh --full               # everything, serial, ~30 min (CI runs it)
RR_SHARD=2/4 bash tests/run_all.sh         # one CI shard
bash tests/flake_hunt.sh                   # the suite at CI's CPU budget (Docker)
```

Page tests need Chrome or Chromium (`CHROME=`). The rigctl contract needs Hamlib.
Each run gets its own scratch `HOME`; a test must never read the real one.

Test style: a standalone script with a docstring that lists what it covers, a
`check(name, got, want)` function, fakes that behave like the hardware, and
`testkit.wait_for` instead of `sleep`. Ports come from `testkit.free_port()`.

- **After `location.reload()`, wait for the new page.** Until it unloads, the
  old page still matches whatever the test waits for, so the wait can pass at
  once and the next check reads the new page mid-load. Set a mark only the old
  page carries, and wait for it to be gone:
  `b.js("window.__beforeReload = true; location.reload()")`, then
  `b.wait("!window.__beforeReload && document.readyState === 'complete' && ...")`
  (`mesh_kept_page_test`, 2026-11-24). Note that `b.wait` returns a falsy value
  on timeout instead of failing.
- **A flaky test: compare against main before blaming the branch or
  rerunning.** `ONLY="<test>" bash tests/flake_hunt.sh` on the branch, then the
  same on a worktree of `origin/main`, at CI's CPU budget. The same rate on
  both means the flake was already there.
- **A failed shard's test output is in its "Logs of failed tests" step.**
  `gh run view --log-failed` leaves that step out, because the step itself
  passes; read the job's full log (`gh run view <run> --job <id> --log`).

## Conventions

- Comments carry the **why**, often with the date and host it was learned on.
  Keep that; it is the project's memory.
- Anything an operator would notice gets a changelog line, in `changelog.d/<branch>.md`, never
  `CHANGELOG.md` itself (parallel PRs all editing its top conflicted and re-ran CI; see
  `changelog.d/README.md`). `tools/changelog_fold.py --apply` folds the fragments in.
- One concern per pull request; CI must be green; merge with a merge commit.
- New Python dependencies go in `requirements.txt` with a reason in the PR,
  and each one, with what it pulls in, gets an exact pin in `constraints.txt`
  (`constraints_test` checks).
  Add-ons with heavy dependencies (meshtastic, benlink) get their own venv
  through `doctor --fix`, never the core one.
- `tests/ci/durations.json` is refreshed from a green CI run
  (`gh run view <id> --log | python3 tests/ci/shard.py refresh`) when a test's
  time changes a lot.
