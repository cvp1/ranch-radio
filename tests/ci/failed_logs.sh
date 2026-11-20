#!/usr/bin/env bash
# CI, after a failed shard: each failed run's log, whatever it holds.
#   bash tests/ci/failed_logs.sh SUITE_OUTPUT LOGDIR
# SUITE_OUTPUT is run_all.sh's stdout ("FAIL name (12s, rc 1)" lines). A log
# shows its FAIL lines and tracebacks with context, then its last 40 lines,
# where a one-line SystemExit lands. Capped per run.
set -u
suite="$1"; logs="$2"
names=$(grep -oE '^FAIL [A-Za-z0-9_]+' "$suite" 2>/dev/null | cut -d' ' -f2 | sort -u)
[[ -n "$names" ]] || { echo "no failed runs in $suite"; exit 0; }
for n in $names; do
  f="$logs/$n.log"
  echo "::group::$n.log"
  if [[ ! -s "$f" ]]; then
    echo "(no log, or an empty one: the run printed nothing)"
  else
    grep -nE -B3 -A14 '^FAIL|Traceback' "$f" | head -120
    echo "--- last 40 lines"
    tail -n 40 "$f"
  fi
  echo "::endgroup::"
done
