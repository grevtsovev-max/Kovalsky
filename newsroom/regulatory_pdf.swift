import Foundation
import PDFKit
import Vision
import CoreGraphics

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data(message.utf8)); exit(2)
}
guard CommandLine.arguments.count == 2,
      let doc = PDFDocument(url: URL(fileURLWithPath: CommandLine.arguments[1])) else { fail("PDF_OPEN_FAILED") }
guard doc.pageCount > 0 && doc.pageCount <= 500 else { fail("PDF_PAGE_LIMIT") }
var pages: [String] = []
var ocrPages: [Int] = []
for index in 0..<doc.pageCount {
    guard let page = doc.page(at: index) else { fail("PDF_PAGE_UNREADABLE") }
    var text = page.string?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    if text.count < 30 {
        let bounds = page.bounds(for: .mediaBox)
        let scale = min(2.5, 2400.0 / max(bounds.width, bounds.height))
        guard let context = CGContext(data: nil, width: Int(bounds.width * scale), height: Int(bounds.height * scale), bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { fail("PDF_RENDER_FAILED") }
        context.setFillColor(CGColor(gray: 1, alpha: 1)); context.fill(CGRect(x: 0, y: 0, width: context.width, height: context.height))
        context.scaleBy(x: scale, y: scale); page.draw(with: .mediaBox, to: context)
        guard let image = context.makeImage() else { fail("PDF_RENDER_FAILED") }
        let request = VNRecognizeTextRequest(); request.recognitionLevel = .accurate
        request.recognitionLanguages = ["ru-RU", "en-US"]; request.usesLanguageCorrection = true
        do { try VNImageRequestHandler(cgImage: image).perform([request]) }
        catch { fail("PDF_OCR_FAILED") }
        text = (request.results ?? []).sorted { $0.boundingBox.midY > $1.boundingBox.midY }.compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
        ocrPages.append(index + 1)
    }
    pages.append(text)
}
let result: [String: Any] = ["pages": pages, "ocr_pages": ocrPages]
guard let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]) else { fail("PDF_JSON_FAILED") }
FileHandle.standardOutput.write(data)
