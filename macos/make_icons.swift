// Draws the app icon (the page's favicon: ring + wave) as PNGs for the web
// app manifest and Safari's Add to Dock / iOS Add to Home Screen.
//
//   swift macos/make_icons.swift static/icons
//
// Full-bleed dark square; the mark sits inside the central 70 % so it survives
// round masks ("maskable") and macOS's rounded-square Dock icon.
import AppKit

let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "static/icons"
try FileManager.default.createDirectory(atPath: out, withIntermediateDirectories: true)

let bg = NSColor(srgbRed: 0x07 / 255.0, green: 0x09 / 255.0, blue: 0x0d / 255.0, alpha: 1)
let teal = NSColor(srgbRed: 0x39 / 255.0, green: 0xe1 / 255.0, blue: 0xc6 / 255.0, alpha: 1)

func render(_ px: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px,
                               bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                               isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    let ctx = NSGraphicsContext(bitmapImageRep: rep)!
    NSGraphicsContext.current = ctx
    let s = CGFloat(px)
    bg.setFill()
    NSRect(x: 0, y: 0, width: s, height: s).fill()

    // SVG viewBox 0..32, y down; map its 2..30 ring into the central 70 %.
    let scale = s * 0.70 / 28
    let off = (s - 28 * scale) / 2 - 2 * scale
    let t = NSAffineTransform()
    t.translateX(by: off, yBy: s - off)   // flip y
    t.scaleX(by: scale, yBy: -scale)
    t.concat()

    teal.setStroke()
    let ring = NSBezierPath(ovalIn: NSRect(x: 2, y: 2, width: 28, height: 28))
    ring.lineWidth = 2
    ring.stroke()

    // M7 18 c3-8 6 4 9-4 s6 4 9-2
    let wave = NSBezierPath()
    wave.move(to: NSPoint(x: 7, y: 18))
    wave.curve(to: NSPoint(x: 16, y: 14), controlPoint1: NSPoint(x: 10, y: 10),
               controlPoint2: NSPoint(x: 13, y: 22))
    wave.curve(to: NSPoint(x: 25, y: 12), controlPoint1: NSPoint(x: 19, y: 6),
               controlPoint2: NSPoint(x: 22, y: 18))
    wave.lineWidth = 2
    wave.lineCapStyle = .round
    wave.stroke()

    ctx.flushGraphics()
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for px in [180, 192, 512] {
    let path = "\(out)/icon-\(px).png"
    try render(px).write(to: URL(fileURLWithPath: path))
    print(path)
}
