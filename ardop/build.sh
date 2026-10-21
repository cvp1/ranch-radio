#!/bin/bash
# Build the ardopcf that Radio Remote runs on mac-shack: pflarue's ardopcf at a
# pinned upstream commit, plus our patches in ardop/patches (applied in order).
#
#   ardop/build.sh [OUT]        default OUT: ardop/build/ardopcf
#
# Never installs anything. To deploy: copy OUT over ~/bin/ardopcf (back the old
# one up first) and restart the org.ranch-radio.ardopcf LaunchAgent, radio idle.
# Needs: git, make, cc; on macOS `brew install portaudio`, on Linux alsa-lib.
# ARDOP_SRC=/path/to/clone uses a local clone instead of GitHub.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM_URL="https://github.com/pflarue/ardop.git"
UPSTREAM_COMMIT="$(awk '/^commit /{print $2}' "$HERE/UPSTREAM")"
OUT="${1:-$HERE/build/ardopcf}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/ardop-build.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

git clone -q "${ARDOP_SRC:-$UPSTREAM_URL}" "$WORK/src"
git -C "$WORK/src" -c advice.detachedHead=false checkout -q "$UPSTREAM_COMMIT"
git -C "$WORK/src" -c user.name=build -c user.email=build@localhost \
    am -q --committer-date-is-author-date "$HERE"/patches/*.patch

# The host ports (8515/8516) can key the radio: loopback only on every OS.
# Patch 0001 sets this for macOS; Linux (linux-station) gets it here.
if [[ "$(uname -s)" == Darwin ]]; then
  make -C "$WORK/src" -s ardopcf
else
  # GCC 14+ makes upstream's pointer/int mismatches errors (lib/rawhid passes a
  # hid_device* to close(); CM108 HID PTT only, which Radio Remote never uses:
  # Pat keys through the rigctl lane). Warnings, as patch 0001 does on macOS.
  # -std=gnu17: GCC 15+ defaults to C23, where upstream's old-style `f()`
  # declarations mean "no arguments" (lib/ws_server). The Makefile assigns CFLAGS (=), so it goes on the command line, keeping
  # its own -g -MMD; CPPFLAGS it appends to (+=), so the environment works.
  CPPFLAGS="${CPPFLAGS:-} -DARDOP_HOST_LOOPBACK_ONLY" make -C "$WORK/src" -s ardopcf \
    CFLAGS="-g -MMD -std=gnu17 -Wno-error=incompatible-pointer-types -Wno-error=int-conversion"
fi
mkdir -p "$(dirname "$OUT")"
cp "$WORK/src/ardopcf" "$OUT"
echo "built $OUT ($(git -C "$WORK/src" log --oneline -1))"
