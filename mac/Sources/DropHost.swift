import AppKit

/// 창 전체를 사진 받는 자리로 만든다.
///
/// 드롭은 '창'이 받는다. 분할 뷰를 가로채면 안 된다 — NSSplitViewController 는
/// 제가 만든 분할 뷰에 칸을 붙이므로, loadView 에서 다른 뷰로 바꿔치기하면
/// 세 칸이 어디에도 붙지 못해 본문이 통째로 빈다(실제로 그렇게 됐다).
/// NSWindow 는 그 자체로 드래그를 받을 수 있으니 여기서 처리한다.
@MainActor
final class DropWindow: NSWindow, NSDraggingDestination {

    /// 받아들일 파일. 엔진이 읽을 수 있는 확장자만 통과시킨다.
    static let imageExtensions: Set<String> = [
        "jpg", "jpeg", "png", "tif", "tiff", "bmp", "webp",
        "arw", "srf", "sr2", "cr2", "cr3", "nef", "nrw", "dng", "raf",
        "orf", "rw2", "pef", "srw", "erf", "kdc", "dcr", "3fr", "iiq",
    ]

    var onDrop: (([String]) -> Void)?

    private let tint = CALayer()
    private let frameLayer = CAShapeLayer()
    private let label = CATextLayer()
    private var hintReady = false

    func enableDrop() {
        registerForDraggedTypes([.fileURL])
    }

    // MARK: - 안내 표시 (자식 뷰가 아니라 레이어로 얹는다)

    private func prepareHint() {
        guard !hintReady, let host = contentView else { return }
        host.wantsLayer = true
        tint.zPosition = 900
        frameLayer.zPosition = 901
        label.zPosition = 902
        frameLayer.fillColor = nil
        frameLayer.lineWidth = 2
        frameLayer.lineDashPattern = [9, 6]
        label.alignmentMode = .center
        label.fontSize = 17
        label.font = NSFont.systemFont(ofSize: 17, weight: .medium)
        for l in [tint, frameLayer, label] {
            l.isHidden = true
            host.layer?.addSublayer(l)
        }
        hintReady = true
    }

    private func showHint(count: Int) {
        prepareHint()
        guard let host = contentView else { return }
        let accent = NSColor.controlAccentColor
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        let b = host.bounds
        tint.frame = b
        tint.backgroundColor = accent.withAlphaComponent(0.12).cgColor
        frameLayer.frame = b
        frameLayer.path = CGPath(roundedRect: b.insetBy(dx: 18, dy: 18),
                                 cornerWidth: 12, cornerHeight: 12, transform: nil)
        frameLayer.strokeColor = accent.cgColor
        label.string = count == 1 ? "사진 1장 추가" : "사진 \(count)장 추가"
        label.foregroundColor = accent.cgColor
        label.contentsScale = backingScaleFactor
        label.frame = NSRect(x: 0, y: b.midY - 14, width: b.width, height: 28)
        [tint, frameLayer, label].forEach { $0.isHidden = false }
        CATransaction.commit()
    }

    private func hideHint() {
        [tint, frameLayer, label].forEach { $0.isHidden = true }
    }

    // MARK: - 드래그

    private func acceptableFiles(_ sender: NSDraggingInfo) -> [String] {
        guard let items = sender.draggingPasteboard.readObjects(
            forClasses: [NSURL.self],
            options: [.urlReadingFileURLsOnly: true]) as? [URL] else { return [] }

        var out: [String] = []
        for url in items {
            var isDir: ObjCBool = false
            guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir) else { continue }
            if isDir.boolValue {
                // 폴더를 놓으면 그 안의 사진을 훑는다 (한 겹만)
                let inner = (try? FileManager.default.contentsOfDirectory(
                    at: url, includingPropertiesForKeys: nil)) ?? []
                out += inner.filter {
                    Self.imageExtensions.contains($0.pathExtension.lowercased())
                }.map(\.path)
            } else if Self.imageExtensions.contains(url.pathExtension.lowercased()) {
                out.append(url.path)
            }
        }
        return out.sorted()
    }

    func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        let files = acceptableFiles(sender)
        guard !files.isEmpty else { return [] }
        showHint(count: files.count)
        return .copy
    }

    func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        tint.isHidden ? [] : .copy
    }

    func draggingExited(_ sender: NSDraggingInfo?) { hideHint() }
    func draggingEnded(_ sender: NSDraggingInfo) { hideHint() }

    func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        hideHint()
        let files = acceptableFiles(sender)
        guard !files.isEmpty else { return false }
        onDrop?(files)
        return true
    }
}
