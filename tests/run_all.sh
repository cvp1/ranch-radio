#!/usr/bin/env bash
# The full suite, one test at a time (parallel runs collide on ports).
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
pass=0; fail=0; failed=()
pick=""
if [[ -n "${RR_SHARD:-}" && -z "${RR_LIST:-}" ]]; then
  [[ $RR_SHARD =~ ^([0-9]+)/([0-9]+)$ ]] || { echo "RR_SHARD wants I/N, got $RR_SHARD"; exit 2; }
  pick=" $(RR_LIST=1 bash "$0" | python3 tests/ci/shard.py pick "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" | tr '\n' ' ')"
  [[ -n "${pick// /}" ]] || { echo "shard $RR_SHARD has no runs"; exit 2; }
  echo "shard $RR_SHARD:$pick"
fi

run() {   # name, command...
  local name="$1" start=$SECONDS rc; shift
  [[ -n "${RR_LIST:-}" ]] && { echo "$name"; return; }
  [[ -z "$pick" || $pick == *" $name "* ]] || return
  [[ " ${RR_SKIP:-} " == *" $name "* ]] && { echo "SKIP $name"; return; }
  if timeout 900 "$@" >"$LOGDIR/$name.log" 2>&1; then
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
echo "$pass passed, $fail failed${failed[*]:+: ${failed[*]}} (logs in $LOGDIR)"
[[ $fail -eq 0 ]]
