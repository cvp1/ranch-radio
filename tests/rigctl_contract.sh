#!/usr/bin/env bash
# Contract test: the rigctl listener against Hamlib's real netrigctl client
# (rigctl -m 2, the same backend JTDX/WSJT-X use), with the server simulating
# a rig. Needs hamlib's rigctl on PATH.
#   bash tests/rigctl_contract.sh [rig]      (default ic7300; also g90)
set -u
cd "$(dirname "$0")/.."
RIG="${1:-ic7300}"
PY="${PYTHON:-.venv/bin/python}"; [[ -x "$PY" ]] || PY=python3
PORT=$((14000 + RANDOM % 1000)); WEB=$((17000 + RANDOM % 1000))
LOG=$(mktemp)
"$PY" server.py --sim --rig "$RIG" --no-audio --no-tx --listen "$WEB" --rigctl-port "$PORT" >"$LOG" 2>&1 &
SP=$!
trap 'kill $SP 2>/dev/null' EXIT
for _ in $(seq 1 50); do (exec 3<>/dev/tcp/127.0.0.1/$PORT) 2>/dev/null && break; sleep 0.1; done

r() { rigctl -m 2 -r "127.0.0.1:$PORT" "$@" 2>&1 | tr '\n' ' ' | sed 's/ *$//'; }
fail=0
check() {  # name, got, expected-regex
  if [[ "$2" =~ $3 ]]; then printf 'ok    %-28s %s\n' "$1" "$2"
  else printf 'FAIL  %-28s got [%s] want /%s/\n' "$1" "$2" "$3"; fail=1; fi
}

check "open + get freq"        "$(r f VFOA)"                 '^[0-9]+$'
r F VFOA 7074000 >/dev/null
check "set/get freq"           "$(r f VFOA)"                 '^7074000$'
r M VFOA PKTUSB 0 >/dev/null
check "set PKTUSB -> data on"  "$(r m VFOA)"                 '^PKTUSB [0-9]+$'
r M VFOA USB 0 >/dev/null
if [[ "$RIG" == g90 ]]; then
  check "USB keeps DATA (sticky)" "$(r m VFOA)"              '^PKTUSB [0-9]+$'
else
  check "set USB -> data off"  "$(r m VFOA)"                 '^USB [0-9]+$'
fi
r M VFOA LSB 0 >/dev/null
check "set LSB"                "$(r m VFOA)"                 '^(PKT)?LSB [0-9]+$'
check "get vfo"                "$(r v)"                 '^VFOA$'
check "get ptt"                "$(r t VFOA)"                 '^0$'
check "PTT on is refused"      "$(r T VFOA 1)"               'error|Feature not available|-11'
check "ptt still off"          "$(r t VFOA)"                 '^0$'
check "PTT off accepted"       "$(r T VFOA 0)"               '^$'
check "get split"              "$(r s VFOA)"                 '^0 VFOA$'
check "bad mode refused"       "$(r M VFOA WFM 0)"           'error|Invalid'
check "several in one session" "$(r f VFOA m VFOA t VFOA s VFOA)"           '^[0-9]+ (PKT)?LSB [0-9]+ 0 0 VFOA$'

# The listener runs in rigctld --vfo mode (chk_vfo 1), so the CLI names VFOs.
# A set aimed at VFO B is the split TX frequency and must never move VFO A
# (rig_set_freq(VFO_B) in non-VFO mode arrived as a bare F and landed on A).
before="$(r f VFOA)"
# One session: the G90's emulated split TX frequency is cleared when the last
# client leaves (F9), and each r call is its own connection.
check "F VFOB -> TX freq"      "$(r F VFOB 14075500 f VFOB)" '^14075500$'
check "F VFOB leaves VFO A"    "$(r f VFOA)"            "^${before}\$"

echo "--- libhamlib split API (as JTDX/WSJT-X call it):"
"$PY" tests/hamlib_split_api.py "$PORT" > "$LOG.api" 2>&1 || fail=1
sed 's/^/      /' "$LOG.api"

grep -E "Traceback|ERROR" "$LOG" && fail=1
echo; [[ $fail == 0 ]] && echo "PASS ($RIG)" || { echo "FAILED ($RIG) — server log: $LOG"; exit 1; }
