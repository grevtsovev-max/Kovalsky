import Foundation
import PDFKit
import Vision
import CoreGraphics

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data(message.utf8))
    exit(2)
}

func recognizePage(_ page: PDFPage) throws -> String {
    let bounds = page.bounds(for: .mediaBox)
    guard bounds.width > 0, bounds.height > 0 else { return "" }
    let scale = min(2.5, 2400.0 / max(bounds.width, bounds.height))
    let width = max(1, Int((bounds.width * scale).rounded(.up)))
    let height = max(1, Int((bounds.height * scale).rounded(.up)))
    guard let context = CGContext(data: nil, width: width, height: height,
                                  bitsPerComponent: 8, bytesPerRow: width * 4,
                                  space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue) else {
        return ""
    }
    context.setFillColor(CGColor(gray: 1, alpha: 1))
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    context.saveGState()
    context.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: context)
    context.restoreGState()
    guard let image = context.makeImage() else { return "" }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["ru-RU", "en-US"]
    request.usesLanguageCorrection = true
    try VNImageRequestHandler(cgImage: image).perform([request])
    return (request.results ?? [])
        .sorted { $0.boundingBox.midY > $1.boundingBox.midY }
        .compactMap { $0.topCandidates(1).first?.string }
        .joined(separator: "\n")
}

guard CommandLine.arguments.count == 2 else { fail("PDF_OPEN_FAILED") }
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let document = PDFDocument(url: url), document.pageCount > 0 else {
    fail("PDF_OPEN_FAILED")
}
if document.isEncrypted && !document.unlock(withPassword: "") {
    fail("PDF_ENCRYPTED")
}
var chunks: [String] = []
var usedOCR = false
for index in 0..<min(document.pageCount, 80) {
    guard let page = document.page(at: index) else { continue }
    let layerText = page.string?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    if layerText.count >= 80 {
        chunks.append(layerText)
    } else {
        do {
            let scannedText = try recognizePage(page).trimmingCharacters(in: .whitespacesAndNewlines)
            if scannedText.count >= 40 {
                chunks.append(scannedText)
                usedOCR = true
            } else if !layerText.isEmpty {
                chunks.append(layerText)
            }
        } catch {
            if !layerText.isEmpty { chunks.append(layerText) }
        }
    }
}
let output = chunks.joined(separator: "\n")
if output.trimmingCharacters(in: .whitespacesAndNewlines).count < 80 {
    fail("PDF_HAS_NO_READABLE_TEXT")
}
if usedOCR {
    FileHandle.standardError.write(Data("PDF_OCR_USED".utf8))
}
let prefix = String(output.prefix(12000))
FileHandle.standardOutput.write(Data(prefix.utf8))
