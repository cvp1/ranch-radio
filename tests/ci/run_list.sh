#!/usr/bin/env bash
# Run the named tests (tests/<name>.py) one at a time, each with its own
# scratch HOME and XDG dirs as run_all.sh gives them; exit 1 if any failed. For the CI
# lanes that run a fixed subset (py312, macos).
set -u
cd "$(dirname "$0")/../.."
PY="${PYTHON:-.venv/bin/python}"; [[ -x "$PY" ]] || PY=python3
failed=()
for t in "$@"; do
  home=$(mktemp -d)
  mkdir -p "$home/.config" "$home/.cache" "$home/.local/state" "$home/.local/share"
  start=$SECONDS
  # The same isolation run_all.sh gives: scratch XDG dirs too, loopback servers.
  if env -u CC_OBS_TOKENS_FILE HOME="$home" XDG_CONFIG_HOME="$home/.config" XDG_CACHE_HOME="$home/.cache" \
      XDG_STATE_HOME="$home/.local/state" XDG_DATA_HOME="$home/.local/share" \
      RANCH_RADIO_DEFAULT_HOST=127.0.0.1 "$PY" "tests/$t.py" >"$home.log" 2>&1; then
    echo "ok    $t ($((SECONDS - start)) s)"
  else
    echo "FAIL  $t ($((SECONDS - start)) s)"; tail -40 "$home.log"; failed+=("$t")
  fi
done
[[ ${#failed[@]} -eq 0 ]] || { echo "failed: ${failed[*]}"; exit 1; }
