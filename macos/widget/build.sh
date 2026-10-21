#!/bin/bash
# Build ic7300-widget.app, install it to ~/Applications, and register a
# LaunchAgent so the menu bar icon appears at every login.
#   bash macos/widget/build.sh            build + install + start
#   bash macos/widget/build.sh --uninstall
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
app="$HOME/Applications/ic7300-widget.app"
label="org.ranch-radio.ic7300-widget"
agent="$HOME/Library/LaunchAgents/$label.plist"
svc="gui/$(id -u)/$label"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "$svc" 2>/dev/null || true
  stamp=$(date +%F)
  [[ -e "$agent" ]] && mv "$agent" "$HOME/.Trash/$label-$stamp.plist"
  [[ -e "$app" ]] && mv "$app" "$HOME/.Trash/ic7300-widget-$stamp.app"
  echo "widget removed (moved to Trash)"
  exit 0
fi

build="$(mktemp -d)"
mkdir -p "$build/ic7300-widget.app/Contents/MacOS"
swiftc -O -o "$build/ic7300-widget.app/Contents/MacOS/ic7300-widget" "$here/main.swift"

cat > "$build/ic7300-widget.app/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>org.ranch-radio.ic7300-widget</string>
  <key>CFBundleName</key><string>Ranch Radio Widget</string>
  <key>CFBundleExecutable</key><string>ic7300-widget</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
</dict>
</plist>
EOF
codesign --force --sign - "$build/ic7300-widget.app" >/dev/null 2>&1 || true

mkdir -p "$HOME/Applications"
launchctl bootout "$svc" 2>/dev/null || true
if [[ -e "$app" ]]; then mv "$app" "$HOME/.Trash/ic7300-widget-$(date +%F-%H%M%S).app"; fi
mv "$build/ic7300-widget.app" "$app"
rmdir "$build"

cat > "$agent" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array><string>$app/Contents/MacOS/ic7300-widget</string></array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
</dict>
</plist>
EOF
launchctl bootstrap "gui/$(id -u)" "$agent"
echo "installed $app; menu bar icon is live and starts at login"
