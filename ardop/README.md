# ardopcf for Radio Remote (mac-shack, linux-station)

Radio Remote's Winlink panel runs Pat over **ardopcf** (pflarue/ardop). We do not
keep a copy of that project here, only what we change in it:

- `UPSTREAM`: the pflarue/ardop commit we build on.
- `patches/0001-*`: native macOS build (CoreAudio/PortAudio sound, host port bound
  to 127.0.0.1 only via `ARDOP_HOST_LOOPBACK_ONLY`, embedded Info.plist, ad-hoc
  codesign as `org.ranch-radio.ardopcf`).
- `patches/0002-*`: station ID (47 CFR 97.119). Every ARQ attempt that
  transmitted ends with an IDFrame + CW ID before the host is told the call is
  over, including an unanswered connect, a host abort, and unanswered DISC
  repeats. Stock ardopcf sends no ID on those paths and waits for its 10-minute
  ID instead, which Radio Remote's gated lane refuses outside a session. This
  matches the original ARDOP TNC spec: "An ID frame with optional CW ID is sent
  following the last Connect Request." Includes `test/python/test_final_id.py`
  (stock ardopcf passes 2/7, patched 7/7).

## Build

    ardop/build.sh            # -> ardop/build/ardopcf (git-ignored)

Clones upstream at the pinned commit into a temp dir, applies the patches with
`git am`, runs `make`. Needs `brew install portaudio`. It never installs.

Verified 2026-10-15: a build from this folder matches the deployed
`~/bin/ardopcf` section for section (code, data, strings identical; only the
linker UUID and its ad-hoc signature differ, which every macOS build does).

## Linux (linux-station)

`ardop/build.sh` builds the same pin and patches with ALSA (PipeWire's ALSA
plugin on Omarchy); needs `alsa-lib`. On Linux it adds what patch 0001 does
only for macOS: host ports on 127.0.0.1 only (`ARDOP_HOST_LOOPBACK_ONLY`),
plus `-std=gnu17` and pointer/int mismatches as warnings, which GCC 14-16
reject in upstream's `lib/ws_server` and `lib/rawhid` (CM108 HID PTT, never
used here: Pat keys through Radio Remote's rigctl lane).

Verified on linux-station 2026-10-16 (GCC 16): host ports bind 127.0.0.1 only, and
`test_final_id.py` passes 7/7. Installed as `~/.local/bin/ardopcf`.

## Deploy on mac-shack (radio idle)

    cp -p ~/bin/ardopcf ~/bin/ardopcf.bak-$(date +%Y%m%d)
    cp ardop/build/ardopcf ~/bin/ardopcf
    launchctl kickstart -k gui/$(id -u)/org.ranch-radio.ardopcf

## Changing a patch

Work in a clone at `UPSTREAM` with the patches applied (`git am`), commit, then
regenerate: `git format-patch -o ardop/patches <upstream-commit>..HEAD`.
Moving to a newer upstream: update `UPSTREAM`, re-apply, fix conflicts, rerun
`test_final_id.py` and the Radio Remote suites before deploying.
