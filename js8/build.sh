#!/bin/bash
# Build the jf8call the JS8 engine runs: Dan Kingshott's jf8call-fork and its
# gfsk8 modem at the commits in js8/UPSTREAM, plus our patches in
# js8/patches/<repo>/ (applied in order). js8_engine.py refuses any other build (+rr.2 or later).
#
#   js8/build.sh [OUT]          default OUT: js8/build/jf8call
#
# Never installs anything. To deploy: copy OUT to the host file's js8.bin
# (default ~/.local/bin/jf8call), check it with
# `python3 js8_engine.py check --bin PATH`, then `ranch-radio install`.
# Needs: git, cmake, ninja, a C++20 compiler, Qt 6 (Core Widgets WebSockets
# Network Sql), Hamlib, PortAudio; on Linux also libpulse.
#   macOS: brew install qt cmake ninja hamlib portaudio
#   Debian/Ubuntu: apt install build-essential cmake ninja-build pkg-config qt6-base-dev
#                  qt6-websockets-dev libqt6sql6-sqlite libhamlib-dev libpulse-dev portaudio19-dev
# JF8CALL_SRC / GFSK8_SRC=/path/to/clone use local clones instead of GitHub.
#
# The optional Olivia, PSK and Codec2 modems are left out: the engine runs
# JS8 only, and every mode jf8call carries is another way to transmit.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HERE/build/jf8call}"
url() { awk -v n="$1" '$1==n{print $2}' "$HERE/UPSTREAM"; }
rev() { awk -v n="$1" '$1==n{print $3}' "$HERE/UPSTREAM"; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/js8-build.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

fetch() {  # name dir src
  git clone -q "$3" "$WORK/$2"
  git -C "$WORK/$2" -c advice.detachedHead=false checkout -q "$(rev "$1")"
}
fetch gfsk8 gfsk8-modem-clean "${GFSK8_SRC:-$(url gfsk8)}"
fetch jf8call jf8call "${JF8CALL_SRC:-$(url jf8call)}"
for r in gfsk8:gfsk8-modem-clean jf8call:jf8call; do
  git -C "$WORK/${r#*:}" -c user.name=build -c user.email=build@localhost \
      am -q --committer-date-is-author-date "$HERE"/patches/"${r%%:*}"/*.patch
done

PREFIX=()
if [[ "$(uname -s)" == Darwin ]]; then
  PREFIX=(-DCMAKE_PREFIX_PATH="$(brew --prefix qt);$(brew --prefix hamlib)")
fi
cmake -S "$WORK/gfsk8-modem-clean" -B "$WORK/gfsk8-modem-clean/build" -G Ninja -DCMAKE_BUILD_TYPE=Release >/dev/null
cmake --build "$WORK/gfsk8-modem-clean/build" --parallel >/dev/null
ctest --test-dir "$WORK/gfsk8-modem-clean/build" --output-on-failure >/dev/null
# Olivia and PSK: pointed at directories that don't exist, so they stay off.
cmake -S "$WORK/jf8call" -B "$WORK/jf8call/build" -G Ninja -DCMAKE_BUILD_TYPE=Release "${PREFIX[@]}" \
      -DGFSK8MODEM_DIR="$WORK/gfsk8-modem-clean" \
      -DOLIVIA_MODEM_DIR="$WORK/none" -DPSK_MODEM_DIR="$WORK/none" >/dev/null
cmake --build "$WORK/jf8call/build" --parallel >/dev/null
ctest --test-dir "$WORK/jf8call/build" --output-on-failure >/dev/null
mkdir -p "$(dirname "$OUT")"
cp "$WORK/jf8call/build/jf8call" "$OUT"
echo "built $OUT ($(QT_QPA_PLATFORM=offscreen "$OUT" --version 2>/dev/null | tail -1); $(git -C "$WORK/jf8call" log --oneline -1))"
