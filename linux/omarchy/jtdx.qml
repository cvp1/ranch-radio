// Omarchy bar module: start JTDX for whichever radio is plugged in.
//
// Green  = JTDX running
// Normal = a radio (G90 or QMX) is plugged in; click to start JTDX for it
// Dimmed = no radio plugged in
//
// Left-click runs `jtdx-radio` (starts JTDX with that radio's profile, or
// focuses it if it's already open; asks when both radios are plugged in);
// right-click always asks which radio.
// Installed by linux/omarchy/install.sh as ~/.config/omarchy/bar/modules/jtdx.qml
// and placed in shell.json as { "id": "jtdx", "type": "qml" }.

import QtQuick
import Quickshell.Io
import qs.Ui

WidgetButton {
  id: root

  property string moduleName
  property var settings
  property string jtdxState: "none"
  readonly property string tool: (settings && settings.tool) ? String(settings.tool) : "~/.local/bin/jtdx-radio"

  text: "JTDX"
  foreground: jtdxState === "running" ? "#3fb950" : (bar ? bar.barForeground : "#888888")
  dimmed: jtdxState === "none"
  useActiveColor: false
  tooltipText: "JTDX"

  function parse(raw) {
    try {
      var d = JSON.parse(String(raw).trim())
      jtdxState = d.state || "none"
      tooltipText = d.tooltip || "JTDX"
    } catch (e) {
      jtdxState = "none"
      tooltipText = "JTDX: status script failed (" + tool + ")"
    }
  }

  onPressed: function(button) {
    if (bar) bar.run(tool + (button === Qt.RightButton ? " pick" : ""))
    settle.restart()
  }

  Process {
    id: statusProc
    command: ["bash", "-c", root.tool + " status"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.parse(text)
    }
  }

  Timer {
    id: settle
    interval: 2000
    onTriggered: statusProc.running = true
  }

  Timer {
    interval: 3000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: if (!statusProc.running) statusProc.running = true
  }
}
