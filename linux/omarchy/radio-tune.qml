// Omarchy bar module: one-click ATU tune for a Radio Remote radio.
//
// Click runs `radio-tune <host>` (rigctl G TUNE), which keys the radio's tuner
// under the server's safety: refused while anyone transmits or under --no-tx.
// The label flashes green when the tune starts, red when it is refused or the
// server can't be reached; radio-tune also posts a notification.
// Shown only while an app here (JTDX, JS8Call) holds a rigctl connection to
// that host's server (port 4532, or "rigctlPort"); hidden otherwise. The web
// page has its own TUNE button, and the audio bridge's permanent /audio link
// would make port 7300 always count (2026-10-13).
// One file serves every radio; each bar entry names its host and label:
//   { "id": "radio-tune-ic7300", "type": "qml",
//     "source": "~/ranch-radio/linux/omarchy/radio-tune.qml",
//     "host": "mac-shack", "label": "7300" }

import QtQuick
import Quickshell.Io
import qs.Ui

WidgetButton {
  id: root

  property string moduleName
  property var settings
  readonly property string host: (settings && settings.host) ? String(settings.host) : ""
  readonly property string label: (settings && settings.label) ? String(settings.label) : host
  readonly property string tool: (settings && settings.tool) ? String(settings.tool) : "~/.local/bin/radio-tune"
  readonly property string rigctlPort: (settings && settings.rigctlPort) ? String(settings.rigctlPort) : "4532"
  property string result: ""          // "", "busy", "ok", "fail"
  property bool connected: false

  readonly property string glyph: String.fromCodePoint(0xF0003)   // md-access_point, as radio.qml
  readonly property var colors: ({ busy: "#d29922", ok: "#3fb950", fail: "#f85149" })

  // Empty text hides a WidgetButton without leaving a gap.
  text: (connected || result !== "") ? glyph + " " + label : ""
  foreground: colors[result] || (bar ? bar.barForeground : "#888888")
  useActiveColor: false
  fontSize: 13
  tooltipText: "Tune the " + label + " ATU (" + host + ")"

  onPressed: function(button) {
    if (button !== Qt.LeftButton || result === "busy" || host === "") return
    result = "busy"
    // bash expands the tool's ~; host, port and label go in as arguments, never as code.
    tuneProc.command = ["bash", "-c", tool + ' "$@"', "radio-tune", host, String(rigctlPort), label]
    tuneProc.running = true
  }

  Process {
    id: tuneProc
    onExited: function(code) { root.result = code === 0 ? "ok" : "fail"; clear.restart() }
  }

  Process {
    id: connProc
    command: ["bash", "-c",
      "ip=$(getent ahostsv4 '" + root.host + "' | awk 'NR==1{print $1}'); " +
      "[ -n \"$ip\" ] && ss -Htn state established dst \"$ip\" dport = :" + root.rigctlPort + " | grep -q ."]
    onExited: function(code) { root.connected = code === 0 }
  }

  Timer {
    interval: 3000
    running: root.host !== ""
    repeat: true
    triggeredOnStart: true
    onTriggered: if (!connProc.running) connProc.running = true
  }

  Timer {
    id: clear
    interval: 4000
    onTriggered: root.result = ""
  }
}
