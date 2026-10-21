// ic7300-widget: menu bar toggle for the Ranch Radio LaunchAgent.
//
// Green antenna = server running and holding the radio.
// Red           = server running, but another app (e.g. flrig) holds the radio.
// Orange        = service loaded but the web page isn't answering yet.
// Gray, slashed = server off; the radio is free for other apps.
//
// "Off" boots the LaunchAgent out AND disables it, so it stays off across
// reboots until you turn it back on here. The menu also shows which process
// holds the rig's serial port, which is what matters when switching apps.

import AppKit
import Foundation

// The radio server's launchd label: the first argument (the rendered agent
// passes the host file's), else the default init writes.
let label = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "org.ranch-radio.ic7300"
let uid = getuid()
let service = "gui/\(uid)/\(label)"
let plistPath = NSString(string: "~/Library/LaunchAgents/\(label).plist").expandingTildeInPath
let webPort: UInt16 = 7300
let webHost = (ProcessInfo.processInfo.hostName.components(separatedBy: ".").first ?? "localhost").lowercased()
let webURL = URL(string: "https://\(webHost):\(webPort)/")!
/// The server's log, as the agent's plist names it (ranch-radio renders it
/// from hosts/<host>.json), so the widget follows the checkout wherever it lives.
var logPath: String {
    if let dict = NSDictionary(contentsOfFile: plistPath),
       let p = dict["StandardOutPath"] as? String { return p }
    return NSString(string: "~/ranch-radio/server.log").expandingTildeInPath
}

@discardableResult
func run(_ path: String, _ args: [String]) -> (code: Int32, out: String) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: path)
    p.arguments = args
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = pipe
    do { try p.run() } catch { return (-1, "\(error)") }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    return (p.terminationStatus, String(decoding: data, as: UTF8.self))
}

/// Serial device the agent is configured to open (read from the plist's --port arg).
func configuredSerialPort() -> String? {
    guard let dict = NSDictionary(contentsOfFile: plistPath),
          let args = dict["ProgramArguments"] as? [String],
          let i = args.firstIndex(of: "--port"), i + 1 < args.count else { return nil }
    return args[i + 1]
}

/// True if something accepts a TCP connection on localhost:port.
func portOpen(_ port: UInt16) -> Bool {
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    guard fd >= 0 else { return false }
    defer { close(fd) }
    var addr = sockaddr_in()
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = port.bigEndian
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    let r = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    return r == 0
}

struct Status {
    enum State {
        case running   // web port up AND the server holds the rig's serial port
        case noRadio   // web port up, but the rig port is held elsewhere or unplugged
        case starting  // service loaded, web port not answering yet
        case off
    }
    var state: State
    var loaded: Bool
    var serialPort: String?
    var serialPresent: Bool
    var serialHolders: [String]   // other processes holding the rig (names)
}

/// Every device node that can reach the rig's USB-serial chip. macOS can expose
/// one CP2102 twice: Apple's driver (/dev/cu.usbserial-XXXX) and the Silicon
/// Labs driver (/dev/cu.SLAB_USBtoUART). Opening either locks the chip, so
/// flrig on the SLAB node blocks the server on the usbserial node. Check both,
/// plus their tty.* twins.
func rigNodes(configured: String) -> [String] {
    var nodes = [configured]
    let devs = (try? FileManager.default.contentsOfDirectory(atPath: "/dev")) ?? []
    for d in devs where d.hasPrefix("cu.") && (d.contains("usbserial") || d.contains("SLAB_USBtoUART")) {
        nodes.append("/dev/" + d)
    }
    nodes += nodes.map { $0.replacingOccurrences(of: "/dev/cu.", with: "/dev/tty.") }
    return Array(Set(nodes)).filter { FileManager.default.fileExists(atPath: $0) }.sorted()
}

func readStatus() -> Status {
    let lc = run("/bin/launchctl", ["print", service])
    let loaded = lc.code == 0
    var serverPid: Int32? = nil
    if loaded, let line = lc.out.split(separator: "\n").first(where: {
        $0.trimmingCharacters(in: .whitespaces).hasPrefix("pid = ") }) {
        serverPid = Int32(line.split(separator: "=").last!.trimmingCharacters(in: .whitespaces))
    }
    let listening = loaded && portOpen(webPort)

    let serial = configuredSerialPort()
    var present = false
    var others: [String] = []
    var serverHoldsRig = false
    if let s = serial {
        present = FileManager.default.fileExists(atPath: s)
        let nodes = rigNodes(configured: s)
        if !nodes.isEmpty {
            // lsof -F pc prints "p<pid>" then "c<command>" for each holder.
            let out = run("/usr/sbin/lsof", ["-F", "pc"] + nodes).out
            var pid: Int32 = -1
            for line in out.split(separator: "\n") {
                if line.hasPrefix("p") { pid = Int32(line.dropFirst()) ?? -1 }
                else if line.hasPrefix("c") {
                    if pid == serverPid { serverHoldsRig = true }
                    else { others.append(String(line.dropFirst())) }
                }
            }
            others = Array(Set(others)).sorted()
        }
    }

    let state: Status.State
    if !loaded { state = .off }
    else if !listening { state = .starting }
    else if serverHoldsRig { state = .running }
    else { state = .noRadio }
    return Status(state: state, loaded: loaded, serialPort: serial,
                  serialPresent: present, serialHolders: others)
}

final class App: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    var status = readStatus()
    var timer: Timer?
    var busy = false

    func applicationDidFinishLaunching(_ n: Notification) {
        let menu = NSMenu()
        menu.delegate = self
        item.menu = menu
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            self?.refresh()
        }
    }

    func refresh() {
        DispatchQueue.global(qos: .utility).async {
            let s = readStatus()
            DispatchQueue.main.async {
                self.status = s
                self.paintIcon()
            }
        }
    }

    func paintIcon() {
        let (symbol, color, tip): (String, NSColor, String)
        switch status.state {
        case .running:  (symbol, color, tip) = ("antenna.radiowaves.left.and.right", .systemGreen, "Ranch Radio: running")
        case .noRadio:  (symbol, color, tip) = ("antenna.radiowaves.left.and.right", .systemRed, "Ranch Radio: running but can't reach the radio")
        case .starting: (symbol, color, tip) = ("antenna.radiowaves.left.and.right", .systemOrange, "Ranch Radio: loaded, not answering")
        case .off:      (symbol, color, tip) = ("antenna.radiowaves.left.and.right.slash", .systemGray, "Ranch Radio: off")
        }
        if busy {
            let cfg = NSImage.SymbolConfiguration(pointSize: 14, weight: .semibold)
                .applying(NSImage.SymbolConfiguration(paletteColors: [.systemYellow]))
            item.button?.image = NSImage(systemSymbolName: "hourglass", accessibilityDescription: "working")?
                .withSymbolConfiguration(cfg)
            item.button?.imagePosition = .imageOnly
            item.button?.title = ""
            return
        }
        // Bake the color into the image. A template image + contentTintColor
        // gets flattened to monochrome by the menu bar on recent macOS.
        let cfg = NSImage.SymbolConfiguration(pointSize: 14, weight: .semibold)
            .applying(NSImage.SymbolConfiguration(paletteColors: [color]))
        let img = NSImage(systemSymbolName: symbol, accessibilityDescription: tip)?
            .withSymbolConfiguration(cfg)
        img?.isTemplate = false
        item.button?.image = img
        item.button?.imagePosition = .imageOnly
        item.button?.title = ""
        item.button?.toolTip = tip
    }

    // Rebuild the menu each time it opens so it reflects live state.
    func menuNeedsUpdate(_ menu: NSMenu) {
        status = readStatus()
        paintIcon()
        menu.removeAllItems()

        let headline: String
        switch status.state {
        case .running:  headline = "● Ranch Radio is ON"
        case .noRadio:  headline = "● Ranch Radio is ON but can't reach the radio"
        case .starting: headline = "● Ranch Radio is starting / not answering"
        case .off:      headline = "○ Ranch Radio is OFF"
        }
        let h = NSMenuItem(title: headline, action: nil, keyEquivalent: "")
        h.isEnabled = false
        menu.addItem(h)

        let portLine: String
        if let p = status.serialPort {
            let name = (p as NSString).lastPathComponent
            if !status.serialPresent {
                portLine = "Radio: not connected (\(name) missing)"
            } else if !status.serialHolders.isEmpty {
                portLine = "Radio in use by: \(status.serialHolders.joined(separator: ", "))"
            } else if status.state == .running {
                portLine = "Radio in use by: Ranch Radio"
            } else {
                portLine = "Radio: free for other apps"
            }
        } else {
            portLine = "Rig port: unknown (plist missing?)"
        }
        let pl = NSMenuItem(title: portLine, action: nil, keyEquivalent: "")
        pl.isEnabled = false
        menu.addItem(pl)
        menu.addItem(.separator())

        let on = status.loaded
        let toggle = NSMenuItem(title: on ? "Turn Server Off (free the radio)" : "Turn Server On",
                                action: #selector(toggle), keyEquivalent: "t")
        toggle.target = self
        toggle.isEnabled = !busy
        menu.addItem(toggle)

        if on {
            let r = NSMenuItem(title: "Restart Server", action: #selector(restart), keyEquivalent: "r")
            r.target = self
            r.isEnabled = !busy
            menu.addItem(r)
        }

        let open = NSMenuItem(title: "Open Ranch Radio in Browser", action: #selector(openWeb), keyEquivalent: "o")
        open.target = self
        open.isEnabled = status.state == .running || status.state == .noRadio
        menu.addItem(open)

        let log = NSMenuItem(title: "Show Server Log", action: #selector(openLog), keyEquivalent: "l")
        log.target = self
        menu.addItem(log)

        menu.addItem(.separator())
        let q = NSMenuItem(title: "Quit Widget", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(q)
    }

    func work(_ steps: @escaping () -> Void) {
        busy = true
        paintIcon()
        DispatchQueue.global(qos: .userInitiated).async {
            steps()
            // Give the server a moment to bind its port before repainting.
            for _ in 0..<10 {
                Thread.sleep(forTimeInterval: 0.5)
                let s = readStatus()
                if s.state != .starting { break }
            }
            DispatchQueue.main.async {
                self.busy = false
                self.refresh()
            }
        }
    }

    @objc func toggle() {
        if status.loaded {
            work {
                run("/bin/launchctl", ["bootout", service])
                run("/bin/launchctl", ["disable", service])
            }
        } else {
            work {
                run("/bin/launchctl", ["enable", service])
                run("/bin/launchctl", ["bootstrap", "gui/\(uid)", plistPath])
            }
        }
    }

    @objc func restart() {
        work { run("/bin/launchctl", ["kickstart", "-k", service]) }
    }

    @objc func openWeb() { NSWorkspace.shared.open(webURL) }

    @objc func openLog() {
        NSWorkspace.shared.open([URL(fileURLWithPath: logPath)],
                                withApplicationAt: URL(fileURLWithPath: "/System/Applications/Utilities/Console.app"),
                                configuration: NSWorkspace.OpenConfiguration())
    }
}

// `ic7300-widget --status` prints the same status the menu shows, then exits.
if CommandLine.arguments.contains("--status") {
    let s = readStatus()
    print("server: \(s.state)  loaded: \(s.loaded)")
    print("rig port: \(s.serialPort ?? "?")  present: \(s.serialPresent)  other holders: \(s.serialHolders)")
    exit(0)
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)   // menu bar only, no Dock icon
app.run()
