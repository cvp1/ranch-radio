#!/usr/bin/env bash
# Ranch Radio installer, macOS and Linux. From a checkout:
#
#   git clone https://github.com/cvp1/ranch-radio.git ~/ranch-radio
#   cd ~/ranch-radio && ./install.sh [--rig RIG] [--port DEVICE] [--call CALL]
#
# 1. Python 3.12+ and a venv in .venv with requirements.txt
# 2. `ranch-radio` on your PATH (~/.local/bin)
# 3. `ranch-radio init` (first time only): finds the radio, writes
#    ~/.config/ranch-radio/hosts/<host>.json; receive-only until --call
# 4. `ranch-radio doctor --fix`: what needs no root (never sudo)
# 5. `ranch-radio install --apply`, then `ranch-radio start`
# 6. `ranch-radio doctor`: anything left (root steps are printed for you)
#
# Safe to run again: each step skips what's already done. To update later:
#   ranch-radio update --apply      (pull, packages, install, restart idle radios only;
#                                    checks they serve, rolls back if not)
# A daily check (ranch-radio update --scheduled) notifies when there is one.
set -euo pipefail
cd "$(dirname "$0")"
REPO="$PWD"
say() { printf '\n== %s\n' "$*"; }

say "Python"
PY=""
for c in python3.14 python3.13 python3.12 python3; do
  p=$(command -v "$c" 2>/dev/null) || continue
  if "$p" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then PY="$p"; break; fi
done
if [[ -z "$PY" ]]; then
  echo "Ranch Radio needs Python 3.12 or newer."
  [[ "$(uname -s)" == Darwin ]] && echo "  brew install python" || echo "  install python3 (3.12+) with your package manager"
  exit 1
fi
echo "$PY ($("$PY" -c 'import platform; print(platform.python_version())'))"

say "Packages (.venv)"
[[ -x .venv/bin/python ]] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
echo "ok"

say "ranch-radio on your PATH"
mkdir -p "$HOME/.local/bin"
ln -sf "$REPO/ranch-radio" "$HOME/.local/bin/ranch-radio"
case ":$PATH:" in *":$HOME/.local/bin:"*) echo "~/.local/bin/ranch-radio" ;;
  *) echo "~/.local/bin/ranch-radio (add ~/.local/bin to your PATH)" ;; esac
RR=("$REPO/.venv/bin/python" "$REPO/ranch-radio")

say "Host file"
if hf=$("${RR[@]}" where 2>/dev/null); then
  echo "using $hf"
else
  "${RR[@]}" init "$@"
fi

say "Fixes that need no root"
"${RR[@]}" doctor --fix || true      # exit 1 while something is missing; the check below says what

say "Install and start"
"${RR[@]}" install --apply
"${RR[@]}" start

say "Check"
"${RR[@]}" doctor || true
