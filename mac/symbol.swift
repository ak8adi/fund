// Zeichnet das App-Symbol (wie mac/fundus.svg) als PNG: swift mac/symbol.swift <ausgabe.png> <pixel>
import AppKit

let args = CommandLine.arguments
let px = CGFloat(Double(args[2]) ?? 1024)
let ctx = CGContext(data: nil, width: Int(px), height: Int(px), bitsPerComponent: 8, bytesPerRow: 0,
                    space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
let k = px / 1024
ctx.translateBy(x: 0, y: px); ctx.scaleBy(x: k, y: -k)            // SVG-Koordinaten: Ursprung oben links
ctx.addPath(CGPath(roundedRect: CGRect(x: 100, y: 100, width: 824, height: 824), cornerWidth: 185, cornerHeight: 185, transform: nil))
ctx.setFillColor(CGColor(srgbRed: 0x11/255, green: 0x11/255, blue: 0x10/255, alpha: 1)); ctx.fillPath()
ctx.translateBy(x: 155.8, y: 149.2); ctx.scaleBy(x: 29.7, y: 29.7)
ctx.setStrokeColor(CGColor(srgbRed: 1, green: 1, blue: 1, alpha: 1)); ctx.setLineCap(.round)
ctx.setLineWidth(2.3); ctx.strokeEllipse(in: CGRect(x: 10.6 - 5.6, y: 10.8 - 5.6, width: 11.2, height: 11.2))
ctx.setLineWidth(2.8); ctx.move(to: CGPoint(x: 14.9, y: 15.1)); ctx.addLine(to: CGPoint(x: 19.4, y: 19.6)); ctx.strokePath()
let rep = NSBitmapImageRep(cgImage: ctx.makeImage()!)
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: args[1]))
