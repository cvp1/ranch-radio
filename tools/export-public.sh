#!/usr/bin/env bash
# Make a public snapshot of this checkout: HEAD's files only (git archive), as
# a fresh repository with a single commit, so no history comes along. Checks
# it with tests/public_hygiene_test.py and, when the private config repo is
# here, its leak scan. Never pushes: publishing is a separate, deliberate step.
#
#   tools/export-public.sh [OUT]       default OUT: /tmp/ranch-radio-public
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-/tmp/ranch-radio-public}"
[[ -e "$OUT" ]] && { echo "$OUT exists; pick another or move it aside" >&2; exit 1; }
mkdir -p "$OUT"
git archive HEAD | tar -x -C "$OUT"
git -C "$OUT" init -q -b main
git -C "$OUT" add -A
git -C "$OUT" -c user.name="Ranch Radio" -c user.email="ranch-radio@users.noreply.github.com" \
    commit -q -m "Ranch Radio"
python3 "$OUT/tests/public_hygiene_test.py"
SCAN="${RANCH_RADIO_CONFIG:-$HOME/.config/ranch-radio}/tests/leak_scan.py"
if [[ -f "$SCAN" ]]; then
  python3 "$SCAN" "$OUT"
else
  echo "(no private leak scan at $SCAN; skipped)"
fi
echo "Public snapshot: $OUT (one commit, nothing pushed)"
