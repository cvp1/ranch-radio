#!/usr/bin/env bash
# Run the named tests (tests/<name>.py) one at a time, each with its own
# scratch HOME as run_all.sh gives them; exit 1 if any failed. For the CI
# lanes that run a fixed subset (py312, macos).
set -u
cd "$(dirname "$0")/../.."
PY="${PYTHON:-.venv/bin/python}"; [[ -x "$PY" ]] || PY=python3
failed=()
for t in "$@"; do
  home=$(mktemp -d)
  start=$SECONDS
  if HOME="$home" "$PY" "tests/$t.py" >"$home.log" 2>&1; then
    echo "ok    $t ($((SECONDS - start)) s)"
  else
    echo "FAIL  $t ($((SECONDS - start)) s)"; tail -40 "$home.log"; failed+=("$t")
  fi
done
[[ ${#failed[@]} -eq 0 ]] || { echo "failed: ${failed[*]}"; exit 1; }
