#!/usr/bin/env bash
# Publish this checkout's committed code (HEAD, via git archive: tracked files
# only, no history) to the public repository.
#
#   tools/export-public.sh sync [-m MESSAGE | --from-head] [--push]
#       Clone the public repo, replace its tree with HEAD's (deletions too),
#       check it, and commit it as one new commit. Shows the change; pushes
#       only with --push. Nothing changed: says so and stops. --from-head:
#       the message is HEAD's subject, or a generic one if that carries
#       anything personal (what the publish workflow uses).
#   tools/export-public.sh snapshot OUT
#       A fresh one-commit repository in OUT (the first publish).
#
# Every export is checked before it can leave, and fails closed:
#   - tests/public_hygiene_test.py (license, no host files, home paths, LAN
#     addresses, personal labels)
#   - the private leak scan (RANCH_RADIO_CONFIG, default ~/.config/ranch-radio:
#     tests/leak_scan.py + leaks.txt), over the tree and the commit message.
#     Without it nothing is exported.
# Uncommitted changes are not exported (HEAD is); they are listed.
#
# RANCH_RADIO_PUBLIC: the public repo (default https://github.com/cvp1/ranch-radio.git).
set -euo pipefail
cd "$(dirname "$0")/.."
DEV="$PWD"
PUBLIC="${RANCH_RADIO_PUBLIC:-https://github.com/cvp1/ranch-radio.git}"
CONF="${RANCH_RADIO_CONFIG:-$HOME/.config/ranch-radio}"
SCAN="$CONF/tests/leak_scan.py"
NAME="Ranch Radio"
EMAIL="ranch-radio@users.noreply.github.com"

die() { echo "export-public: $*" >&2; exit 1; }
usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

[[ -f "$SCAN" && -f "$CONF/leaks.txt" ]] || die "no private leak scan at $SCAN (set RANCH_RADIO_CONFIG); nothing exported"
dirty=$(git status --porcelain --untracked-files=no)
[[ -z "$dirty" ]] || { echo "Not exported (uncommitted; HEAD is what goes out):"; echo "$dirty" | sed 's/^/  /'; }

check_tree() {   # $1: a repository whose staged tree is the export
  python3 "$1/tests/public_hygiene_test.py" >/dev/null \
    || { python3 "$1/tests/public_hygiene_test.py" | grep -v '^ok'; die "public hygiene test failed"; }
  python3 "$SCAN" "$1" >/dev/null || { python3 "$SCAN" "$1" | tail -20; die "private leak scan found personal data"; }
}

message_ok() {   # $1: a commit message; true when the leak scan finds nothing in it
  local t; t=$(mktemp -d)
  printf '%s\n' "$1" > "$t/COMMIT_MESSAGE"
  python3 "$SCAN" "$t" >/dev/null
}

check_message() {
  message_ok "$1" || die "the commit message carries personal data"
}

fill() {   # $1: a directory; replace its tree with HEAD's (keeps .git), then stage it
  find "$1" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
  git -C "$DEV" archive HEAD | tar -x -C "$1"
  git -C "$1" add -A
}

cmd="${1:-}"; shift || true
case "$cmd" in
  snapshot)
    OUT="${1:-}"; [[ -n "$OUT" ]] || usage
    [[ -e "$OUT" ]] && die "$OUT exists; pick another or move it aside"
    mkdir -p "$OUT"
    git -C "$OUT" init -q -b main
    fill "$OUT"
    check_tree "$OUT"
    git -C "$OUT" -c user.name="$NAME" -c user.email="$EMAIL" commit -q -m "Ranch Radio"
    echo "Snapshot: $OUT (one commit, checked; nothing pushed)"
    ;;
  sync)
    MSG="Update, $(date +%Y-%m-%d)"; PUSH=0
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -m) MSG="${2:?-m needs a message}"; shift 2 ;;
        --from-head)
          MSG=$(git -C "$DEV" log -1 --format=%s)
          message_ok "$MSG" || { echo "(HEAD's subject isn't publishable; a generic message instead)"; MSG="Update, $(date +%Y-%m-%d)"; }
          shift ;;
        --push) PUSH=1; shift ;;
        *) usage ;;
      esac
    done
    check_message "$MSG"
    WORK=$(mktemp -d "${TMPDIR:-/tmp}/ranch-radio-public.XXXXXX")
    git clone -q "$PUBLIC" "$WORK" || die "can't clone $PUBLIC"
    fill "$WORK"
    check_tree "$WORK"
    if git -C "$WORK" diff --cached --quiet; then
      echo "The public repo is current: nothing to publish."
      exit 0
    fi
    git -C "$WORK" -c user.name="$NAME" -c user.email="$EMAIL" commit -q -m "$MSG"
    echo "Ready to publish to $PUBLIC:"
    git -C "$WORK" show --stat --format='  %s' HEAD | sed '1d'
    if [[ "$PUSH" == 1 ]]; then
      git -C "$WORK" push -q origin HEAD || die "push failed"
      echo "Published: $(git -C "$WORK" log -1 --format=%h) on $PUBLIC"
    else
      echo "Not pushed (dry run). Publish with: tools/export-public.sh sync -m \"$MSG\" --push"
      echo "(the checked commit is in $WORK)"
    fi
    ;;
  *) usage ;;
esac
