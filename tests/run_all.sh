#!/usr/bin/env bash
# The full suite, one test at a time (parallel runs collide on ports), each
# in its own scratch HOME.
# Every tests/*_test.py once, plus the rig-specific second runs, plus the
# Hamlib contract test for both rigs. Logs per run in $LOGDIR.
#   bash tests/run_all.sh            (LOGDIR defaults to /tmp/rr-suite)
#   RR_SKIP="a_test b_test" bash tests/run_all.sh   (skip those, by name)
#   RR_SHARD=2/4 bash tests/run_all.sh   (only shard 2 of 4, split by
#                                         tests/ci/shard.py; CI runs each
#                                         shard on its own runner)
#   RR_LIST=1 bash tests/run_all.sh      (print every run's name, run none)
set -u
cd "$(dirname "$0")/.."
LOGDIR="${LOGDIR:-/tmp/rr-suite}"
mkdir -p "$LOGDIR"
PY="${PYTHON:-.venv/bin/python}"; [[ -x "$PY" ]] || PY=python3
BENLINK="$HOME/.local/share/venvs/benlink/bin/python"   # uvpro_bridge_test needs benlink
MAC_CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
[[ -z "${CHROME:-}" && -x "$MAC_CHROME" ]] && export CHROME="$MAC_CHROME"   # page tests look only on PATH
# Isolation is the runner's job, not each test's: every run gets its own
# scratch HOME and XDG dirs, so no default path reaches the real one, and
# servers default to loopback. RR_SENTINEL=1 (CI, containers) proves it:
# decoy station state planted in the real HOME must be untouched at the end.
REAL_HOME="$HOME"
SCRATCH=$(mktemp -d /tmp/rr-home.XXXXXX)
trap 'rm -rf "$SCRATCH"' EXIT
isolated() {   # name, command...
  local h="$SCRATCH/$1"; shift
  mkdir -p "$h/.config" "$h/.cache" "$h/.local/state" "$h/.local/share"
  [[ -d "$REAL_HOME/.local/share/venvs" ]] && ln -s "$REAL_HOME/.local/share/venvs" "$h/.local/share/venvs"
  env -u CC_OBS_TOKENS_FILE HOME="$h" XDG_CONFIG_HOME="$h/.config" XDG_CACHE_HOME="$h/.cache" \
      XDG_STATE_HOME="$h/.local/state" XDG_DATA_HOME="$h/.local/share" \
      RR_REAL_HOME="$REAL_HOME" RANCH_RADIO_DEFAULT_HOST=127.0.0.1 "$@"
}
pass=0; fail=0; failed=()
pick=""
if [[ -n "${RR_SHARD:-}" && -z "${RR_LIST:-}" ]]; then
  [[ $RR_SHARD =~ ^([0-9]+)/([0-9]+)$ ]] || { echo "RR_SHARD wants I/N, got $RR_SHARD"; exit 2; }
  pick=" $(RR_LIST=1 bash "$0" | python3 tests/ci/shard.py pick "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" | tr '\n' ' ')"
  [[ -n "${pick// /}" ]] || { echo "shard $RR_SHARD has no runs"; exit 2; }
  echo "shard $RR_SHARD:$pick"
fi

if [[ -n "${RR_SENTINEL:-}" && -z "${RR_LIST:-}" ]]; then
  python3 tests/ci/sentinel.py plant "$REAL_HOME" "$SCRATCH/sentinel.json" || exit 2
fi

run() {   # name, command...
  local name="$1" start=$SECONDS rc; shift
  [[ -n "${RR_LIST:-}" ]] && { echo "$name"; return; }
  [[ -z "$pick" || $pick == *" $name "* ]] || return
  [[ " ${RR_SKIP:-} " == *" $name "* ]] && { echo "SKIP $name"; return; }
  if isolated "$name" timeout 900 "$@" >"$LOGDIR/$name.log" 2>&1; then
    echo "PASS $name ($((SECONDS - start))s)"; pass=$((pass + 1))
  else
    rc=$?
    echo "FAIL $name ($((SECONDS - start))s, rc $rc)"; fail=$((fail + 1)); failed+=("$name")
  fi
}

for t in tests/*_test.py; do
  name=$(basename "$t" .py)
  py="$PY"
  [[ $name == uvpro_bridge_test && -x "$BENLINK" ]] && py="$BENLINK"
  run "$name" "$py" "$t"
done
run civ_safety_ic7300 "$PY" tests/civ_safety_test.py ic7300
run winlink_lane_g90 "$PY" tests/winlink_lane_test.py g90
run rigctl_contract bash tests/rigctl_contract.sh
run rigctl_contract_g90 bash tests/rigctl_contract.sh g90

[[ -n "${RR_LIST:-}" ]] && exit 0
if [[ -n "${RR_SENTINEL:-}" ]] && ! python3 tests/ci/sentinel.py check "$REAL_HOME" "$SCRATCH/sentinel.json"; then
  fail=$((fail + 1)); failed+=(sentinel)
fi
echo "$pass passed, $fail failed${failed[*]:+: ${failed[*]}} (logs in $LOGDIR)"
[[ $fail -eq 0 ]]
