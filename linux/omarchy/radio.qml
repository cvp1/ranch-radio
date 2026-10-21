// Omarchy bar module: Radio Remote on/off toggle (Linux twin of macos/widget).
//
// Green antenna  = server running and holding the radio
// Red            = server running, another app holds the radio
// Orange         = starting (web page not answering yet)
// Gray, slashed  = off; the radio is free for other apps
//
// Left-click toggles (off also disables the unit, so it stays off across
// reboots); right-click opens the web page; middle-click restarts.
// Installed by linux/omarchy/install.sh as ~/.config/omarchy/bar/modules/radio.qml
// and placed in shell.json as { "id": "radio", "type": "qml" }.

import QtQuick
import Quickshell.Io
import qs.Ui

WidgetButton {
  id: root

  property string moduleName
  property var settings
  property string radioState: "off"
  property bool busy: false
  readonly property string ctl: (settings && settings.ctl) ? String(settings.ctl) : "~/.local/bin/radio-server"

  readonly property var glyphs: ({
    on: String.fromCodePoint(0xF0003),    // md-access_point
    off: String.fromCodePoint(0xF1511),   // md-access_point_off
    busy: String.fromCodePoint(0xF252)    // fa-hourglass_half
  })
  readonly property var colors: ({
    running: "#3fb950", noradio: "#f85149", starting: "#f0883e", busy: "#d29922"
  })

  text: busy ? glyphs.busy : (radioState === "off" ? glyphs.off : glyphs.on)
  foreground: busy ? colors.busy
            : (colors[radioState] || (bar ? bar.barForeground : "#888888"))
  dimmed: !busy && radioState === "off"
  useActiveColor: false
  fontSize: 14
  tooltipText: "Ranch Radio"

  function parse(raw) {
    try {
      var d = JSON.parse(String(raw).trim())
      radioState = d.state || "off"
      tooltipText = d.tooltip || "Ranch Radio"
    } catch (e) {
      radioState = "off"
      tooltipText = "Ranch Radio: status script failed (" + ctl + ")"
    }
  }

  function act(verb) {
    if (busy) return
    busy = true
    actProc.command = ["bash", "-c", ctl + " " + verb]
    actProc.running = true
  }

  onPressed: function(button) {
    if (button === Qt.RightButton) { if (bar) bar.run(ctl + " open") }
    else if (button === Qt.MiddleButton) act("restart")
    else act("toggle")
  }

  Process {
    id: statusProc
    command: ["bash", "-c", root.ctl + " status"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.parse(text)
    }
  }

  Process {
    id: actProc
    // Give the server a moment to bind its port, then repaint.
    onExited: settle.restart()
  }

  Timer {
    id: settle
    interval: 1500
    onTriggered: { root.busy = false; statusProc.running = true }
  }

  Timer {
    interval: 3000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: if (!statusProc.running) statusProc.running = true
  }
}
