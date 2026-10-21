#!/usr/bin/env bash
# Install the Radio Remote toggle and the JTDX button into the Omarchy
# (Quickshell) bar.
#   bash linux/omarchy/install.sh             install / update
#   bash linux/omarchy/install.sh --uninstall
# Links radio-server and jtdx-radio into ~/.local/bin and radio.qml and
# jtdx.qml into the bar's module dir (symlinks, so `git pull` updates them),
# adds {"id":"radio"} and {"id":"jtdx"} (type qml) to the right side of the
# bar in ~/.config/omarchy/shell.json (backed up first, if it changes), and points the JTDX
# menu entry at jtdx-radio. Unit/serial/port defaults live in radio-server;
# each radio's JTDX profile in jtdx-radio.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cfg="$HOME/.config/omarchy"
shell_json="$cfg/shell.json"
bin="$HOME/.local/bin/radio-server"
mod="$cfg/bar/modules/radio.qml"
jbin="$HOME/.local/bin/jtdx-radio"
jmod="$cfg/bar/modules/jtdx.qml"
# Same file name as the package's jtdx.desktop, so the menu shows one JTDX.
jdesk="$HOME/.local/share/applications/jtdx.desktop"

edit_layout() {   # $1 = add | remove
  [[ -f "$shell_json" ]] || { echo "no $shell_json; place {\"id\":\"radio\",\"type\":\"qml\"} and {\"id\":\"jtdx\",\"type\":\"qml\"} by hand" >&2; return; }
  # Backed up and rewritten only when the layout changes: the git hook runs
  # this after every pull that touches linux/omarchy/.
  python3 - "$shell_json" "$1" <<'PY'
import copy, json, shutil, sys, time
path, op = sys.argv[1], sys.argv[2]
cfg = json.load(open(path))
before = copy.deepcopy(cfg)
layout = cfg.setdefault("bar", {}).setdefault("layout", {})
for section in layout.values():
    section[:] = [e for e in section if e.get("id") not in ("radio", "jtdx")]
if op == "add":
    right = layout.setdefault("right", [])
    at = next((i + 1 for i, e in enumerate(right) if e.get("id") == "omarchy.tray"), 0)
    right[at:at] = [{"id": "radio", "type": "qml"}, {"id": "jtdx", "type": "qml"}]
if cfg == before:
    sys.exit(0)
shutil.copy2(path, path + ".bak." + time.strftime("%Y%m%d-%H%M%S"))
tmp = path + ".tmp"
json.dump(cfg, open(tmp, "w"), indent=2); open(tmp, "a").write("\n")
import os; os.replace(tmp, path)
PY
}

if [[ "${1:-}" == "--uninstall" ]]; then
  edit_layout remove
  rm -f "$bin" "$mod" "$jbin" "$jmod" "$jdesk"
  echo "radio toggle and JTDX button removed from the bar"
  exit 0
fi

chmod +x "$here/radio-server" "$here/jtdx-radio" "$here/git-hook"
mkdir -p "$(dirname "$bin")" "$(dirname "$mod")" "$(dirname "$jdesk")"
# The shell only notices a new file in bar/modules/ after a restart ("File
# name case mismatch" until then), so note whether any module is new.
new_module=0
[[ -e "$mod" && -e "$jmod" ]] || new_module=1
ln -sfn "$here/radio-server" "$bin"
ln -sfn "$here/radio.qml" "$mod"
ln -sfn "$here/jtdx-radio" "$jbin"
ln -sfn "$here/jtdx.qml" "$jmod"
# Re-run this script after every `git pull` that changes linux/omarchy/.
# Leaves a hook alone if it's someone else's. Ours is a link to this
# git-hook, possibly by an old path: after the move from ~/ic7300-remote to
# ~/ranch-radio (2026-10-16) the links still named the old path, and a
# literal compare called them "not ours". So compare the resolved files, and
# also take a link (even a dangling one) to any copy's linux/omarchy/git-hook.
ours() {   # $1 = hook path
  [[ "$(readlink -f "$1" 2>/dev/null)" == "$(readlink -f "$here/git-hook")" ]] && return 0
  [[ -L "$1" && "$(readlink "$1")" == */linux/omarchy/git-hook ]]
}
hooks="$(git -C "$here" rev-parse --absolute-git-dir 2>/dev/null)/hooks"
if [[ -d "$hooks" ]]; then
  for h in post-merge post-rewrite; do
    if [[ ! -e "$hooks/$h" && ! -L "$hooks/$h" ]] || ours "$hooks/$h"; then
      ln -sfn "$here/git-hook" "$hooks/$h"
    else
      echo "left $hooks/$h alone (not ours): install.sh won't re-run after pulls" >&2
    fi
  done
fi
cat >"$jdesk" <<DESKTOP
[Desktop Entry]
Version=1.0
Name=JTDX
Comment=JTDX for the radio that's plugged in (G90 or QMX)
Exec=$jbin
Icon=jtdx_icon
Terminal=false
Type=Application
Categories=AudioVideo;Audio;HamRadio;
StartupNotify=true
DESKTOP
edit_layout add
echo "installed: $bin, $mod, $jbin, $jmod, $jdesk; bar layout checked (the shell reloads shell.json live)"
if (( new_module )); then
  # omarchy refuses to restart a shell holding the lock screen; say so.
  if [[ -n "${WAYLAND_DISPLAY:-}" ]] && command -v omarchy >/dev/null; then
    if omarchy restart shell >/dev/null 2>&1; then
      echo "restarted the shell to load the new bar module"
    else
      echo "couldn't restart the shell (screen locked?): run 'omarchy restart shell' if the new bar module doesn't show"
    fi
  else
    echo "no desktop session here: run 'omarchy restart shell' to show the new bar module"
  fi
fi
"$bin" status
"$jbin" status
