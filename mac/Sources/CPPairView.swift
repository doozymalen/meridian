import AppKit

/// 제어점을 찍는 두 장을 한 뷰 안에서 좌우로 나눠 그린다.
///
/// 처음에는 사진마다 뷰를 하나씩 두고 나란히 놓았는데, 두 개를 함께 두면
/// 한쪽이 화면에 나타나지 않는 문제가 되풀이됐다(그리기는 호출되는데 합성에서
/// 빠졌다). 여기서 필요한 것은 좌우 두 칸뿐이므로 뷰 하나가 전부 그린다.
/// 그리면 반드시 보이고, 좌표 계산도 한 곳에 모인다.
@MainActor
final class CPPairView: NSView {

    struct Marker {
        var index: Int          // 프로젝트 전체에서의 제어점 번호
        var number: Int         // 이 쌍 안에서의 순번 (화면에 보이는 숫자)
        var point: CGPoint      // 원본 픽셀 좌표
        var error: Double
        var enabled: Bool
    }

    enum Side { case left, right }

    /// 한쪽 칸의 상태.
    private struct Pane {
        var image: NSImage?
        var srcSize = CGSize(width: 1, height: 1)
        var caption = ""
        var markers: [Marker] = []
        var zoom: CGFloat = 0
        var offset = CGPoint.zero
        var userAdjusted = false
    }

    var onAddPoint: ((Side, CGPoint, Bool) -> Void)?
    var onSelect: ((Int) -> Void)?
    var onMovePoint: ((Int, Side, CGPoint) -> Void)?
    var selectedIndex: Int? { didSet { needsDisplay = true } }
    var leftPick: Int?
    var rightPick: Int?
    var pending: (side: Side, point: CGPoint)? { didSet { needsDisplay = true } }

    private var left = Pane()
    private var right = Pane()

    /// 위쪽 사진 띠. 뷰를 따로 두면 화면에 나타나지 않는 문제를 겪어,
    /// 사진 칸과 같은 뷰가 함께 그린다.
    private struct Thumb {
        let id: Int
        let label: String
        var image: NSImage?
    }
    private var thumbs: [Thumb] = []
    private var thumbOffset: (left: CGFloat, right: CGFloat) = (0, 0)
    private let stripHeight: CGFloat = 74
    private let thumbCell: CGFloat = 60
    private let thumbGap: CGFloat = 5
    private let thumbPad: CGFloat = 6
    var onPickThumb: ((Side, Int) -> Void)?
    private var cursor: CGPoint?
    private var tracking: NSTrackingArea?

    private enum DragState {
        case pan(mouse: CGPoint, offset: CGPoint, side: Side)
        case moveMarker(mouse: CGPoint, markerIndex: Int, arrayIndex: Int, side: Side)
    }
    private var drag: DragState?
    private var dragMoved = false

    override var isFlipped: Bool { true }

    /// 짝을 눈으로 잇기 위한 색. 같은 번호는 양쪽에서 같은 색으로 찍힌다.
    static func markerColor(_ number: Int) -> NSColor {
        let palette: [NSColor] = [
            .systemYellow, .systemBlue, .systemPink, .systemGray, .systemGreen,
            .systemMint, .systemOrange, .systemRed, .systemIndigo, .systemTeal,
            .systemPurple, .systemBrown,
        ]
        return palette[max(0, number - 1) % palette.count]
    }

    // MARK: - 칸 나누기

    private var gap: CGFloat { 8 }

    private func frame(_ side: Side) -> NSRect {
        let w = (bounds.width - gap) / 2
        let top = stripHeight + 1
        return NSRect(x: side == .left ? 0 : w + gap, y: top,
                      width: max(0, w), height: max(0, bounds.height - top))
    }

    private func stripHalf(_ side: Side) -> NSRect {
        let w = (bounds.width - gap) / 2
        return NSRect(x: side == .left ? 0 : w + gap, y: 0, width: max(0, w), height: stripHeight)
    }

    private func thumbRect(_ side: Side, at index: Int) -> NSRect {
        let h = stripHalf(side)
        let off = side == .left ? thumbOffset.left : thumbOffset.right
        return NSRect(x: h.minX + thumbPad + CGFloat(index) * (thumbCell + thumbGap) - off,
                      y: (stripHeight - thumbCell) / 2, width: thumbCell, height: thumbCell)
    }

    private func clampThumbOffset(_ side: Side, _ v: CGFloat) -> CGFloat {
        let content = thumbPad * 2 + CGFloat(thumbs.count) * thumbCell
            + CGFloat(max(0, thumbs.count - 1)) * thumbGap
        return min(max(0, v), max(0, content - stripHalf(side).width))
    }

    func setThumbnails(_ images: [SourceImage], thumb: (Int) -> NSImage?) {
        if thumbs.map(\.id) != images.map(\.id) {
            thumbs = images.map { Thumb(id: $0.id, label: "\($0.id + 1)", image: thumb($0.id)) }
        }
        needsDisplay = true
    }

    /// 고른 사진이 띠에서 보이도록 민다.
    func revealPicks(left leftID: Int?, right rightID: Int?) {
        for (side, pick) in [(Side.left, leftID), (.right, rightID)] {
            guard let id = pick, let i = thumbs.firstIndex(where: { $0.id == id }) else { continue }
            let r = thumbRect(side, at: i)
            let h = stripHalf(side)
            var off = side == .left ? thumbOffset.left : thumbOffset.right
            if r.minX < h.minX + thumbPad { off -= (h.minX + thumbPad - r.minX) }
            else if r.maxX > h.maxX - thumbPad { off += (r.maxX - (h.maxX - thumbPad)) }
            let clamped = clampThumbOffset(side, off)
            if side == .left { thumbOffset.left = clamped } else { thumbOffset.right = clamped }
        }
        needsDisplay = true
    }

    private func pane(_ side: Side) -> Pane { side == .left ? left : right }
    private func setPane(_ side: Side, _ p: Pane) { if side == .left { left = p } else { right = p } }

    private func side(at point: CGPoint) -> Side {
        point.x < bounds.width / 2 ? .left : .right
    }

    // MARK: - 내용

    func set(_ side: Side, image: NSImage?, srcSize: CGSize, caption: String) {
        var p = pane(side)
        p.image = image
        p.srcSize = srcSize.width > 0 ? srcSize : CGSize(width: 1, height: 1)
        p.caption = caption
        p.markers = []
        p.userAdjusted = false
        p.zoom = 0
        setPane(side, p)
        fit(side)
        needsDisplay = true
    }

    func setMarkers(_ side: Side, _ markers: [Marker]) {
        var p = pane(side)
        p.markers = markers
        setPane(side, p)
        needsDisplay = true
    }

    func image(_ side: Side) -> NSImage? { pane(side).image }

    // MARK: - 좌표

    private func proxyScale(_ side: Side) -> CGFloat {
        let p = pane(side)
        guard let img = p.image, p.srcSize.width > 0 else { return 1 }
        return img.size.width / p.srcSize.width
    }

    private func viewPoint(_ side: Side, _ src: CGPoint) -> CGPoint {
        let p = pane(side)
        let f = frame(side)
        let k = proxyScale(side)
        return CGPoint(x: f.minX + src.x * k * p.zoom + p.offset.x,
                       y: f.minY + src.y * k * p.zoom + p.offset.y)
    }

    private func sourcePoint(_ side: Side, _ view: CGPoint) -> CGPoint {
        let p = pane(side)
        let f = frame(side)
        let k = proxyScale(side)
        guard p.zoom > 0, k > 0 else { return .zero }
        return CGPoint(x: (view.x - f.minX - p.offset.x) / p.zoom / k,
                       y: (view.y - f.minY - p.offset.y) / p.zoom / k)
    }

    // MARK: - 확대

    func fit(_ side: Side) {
        var p = pane(side)
        guard let img = p.image, img.size.width > 0 else { return }
        let f = frame(side)
        guard f.width > 8, f.height > 8 else { return }
        let pad: CGFloat = 16
        p.zoom = max(0.02, min((f.width - pad) / img.size.width,
                               (f.height - pad) / img.size.height))
        p.offset = CGPoint(x: (f.width - img.size.width * p.zoom) / 2,
                           y: (f.height - img.size.height * p.zoom) / 2)
        setPane(side, p)
    }

    func fitAll() {
        for s in [Side.left, .right] where !pane(s).userAdjusted { fit(s) }
        needsDisplay = true
    }

    /// 어떤 지점을 그 칸 한가운데로 가져온다.
    func center(_ side: Side, on src: CGPoint, zoom newZoom: CGFloat? = nil) {
        var p = pane(side)
        if let z = newZoom { p.zoom = z }
        p.userAdjusted = true
        let f = frame(side)
        let k = proxyScale(side)
        p.offset = CGPoint(x: f.width / 2 - src.x * k * p.zoom,
                           y: f.height / 2 - src.y * k * p.zoom)
        setPane(side, p)
        needsDisplay = true
    }

    override func layout() {
        super.layout()
        fitAll()
    }

    // MARK: - 그리기

    override func draw(_ dirty: NSRect) {
        NSColor.underPageBackgroundColor.setFill()
        dirty.fill()

        for s in [Side.left, .right] { drawStrip(side: s) }
        NSColor.separatorColor.setFill()
        NSRect(x: 0, y: stripHeight, width: bounds.width, height: 1).fill()

        for s in [Side.left, .right] { draw(side: s) }

        // 가운데 가르는 선
        NSColor.separatorColor.setFill()
        NSRect(x: bounds.width / 2 - 0.5, y: 0, width: 1, height: bounds.height).fill()

        if let c = cursor { drawLoupe(at: c) }
    }

    private func drawStrip(side: Side) {
        let h = stripHalf(side)
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(rect: h).addClip()
        let pick = side == .left ? leftPick : rightPick
        for (i, t) in thumbs.enumerated() {
            let r = thumbRect(side, at: i)
            guard r.maxX > h.minX, r.minX < h.maxX else { continue }
            let box = NSBezierPath(roundedRect: r, xRadius: 5, yRadius: 5)
            NSColor.windowBackgroundColor.setFill()
            box.fill()
            if let img = t.image {
                NSGraphicsContext.saveGraphicsState()
                box.addClip()
                let scale = max(r.width / img.size.width, r.height / img.size.height)
                let w = img.size.width * scale, hh = img.size.height * scale
                img.draw(in: NSRect(x: r.midX - w / 2, y: r.midY - hh / 2, width: w, height: hh))
                NSGraphicsContext.restoreGraphicsState()
            }
            let selected = t.id == pick
            (selected ? NSColor.controlAccentColor : NSColor.separatorColor).setStroke()
            box.lineWidth = selected ? 3 : 1
            box.stroke()

            let attrs: [NSAttributedString.Key: Any] = [
                .font: NSFont.systemFont(ofSize: 10, weight: .semibold),
                .foregroundColor: NSColor.white,
            ]
            let size = (t.label as NSString).size(withAttributes: attrs)
            let badge = NSRect(x: r.minX + 3, y: r.minY + 3, width: size.width + 8, height: size.height + 2)
            (selected ? NSColor.controlAccentColor : NSColor.black.withAlphaComponent(0.6)).setFill()
            NSBezierPath(roundedRect: badge, xRadius: 3, yRadius: 3).fill()
            (t.label as NSString).draw(at: CGPoint(x: badge.minX + 4, y: badge.minY + 1), withAttributes: attrs)
        }
        NSGraphicsContext.restoreGraphicsState()
    }

    private func draw(side: Side) {
        let p = pane(side)
        let f = frame(side)
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(rect: f).addClip()

        guard let img = p.image else {
            drawCentered("사진을 고르세요", in: f, color: .tertiaryLabelColor)
            NSGraphicsContext.restoreGraphicsState()
            return
        }

        let rect = NSRect(x: f.minX + p.offset.x, y: f.minY + p.offset.y,
                          width: img.size.width * p.zoom, height: img.size.height * p.zoom)
        NSGraphicsContext.current?.imageInterpolation = p.zoom > 2.5 ? .none : .high
        img.draw(in: rect)

        drawCaption(p.caption, in: f)
        for m in p.markers { drawMarker(m, side: side) }
        if let pend = pending, pend.side == side {
            let v = viewPoint(side, pend.point)
            NSColor.controlAccentColor.setStroke()
            let path = NSBezierPath(ovalIn: NSRect(x: v.x - 8, y: v.y - 8, width: 16, height: 16))
            path.lineWidth = 2
            path.setLineDash([4, 3], count: 2, phase: 0)
            path.stroke()
        }
        NSGraphicsContext.restoreGraphicsState()
    }

    private func drawCentered(_ text: String, in f: NSRect, color: NSColor) {
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 12), .foregroundColor: color,
        ]
        let size = (text as NSString).size(withAttributes: attrs)
        (text as NSString).draw(at: CGPoint(x: f.midX - size.width / 2, y: f.midY - size.height / 2),
                                withAttributes: attrs)
    }

    private func drawCaption(_ text: String, in f: NSRect) {
        guard !text.isEmpty else { return }
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 11, weight: .medium),
            .foregroundColor: NSColor.white,
        ]
        let size = (text as NSString).size(withAttributes: attrs)
        let box = NSRect(x: f.minX + 10, y: f.minY + 8, width: size.width + 14, height: size.height + 6)
        NSColor.black.withAlphaComponent(0.55).setFill()
        NSBezierPath(roundedRect: box, xRadius: 5, yRadius: 5).fill()
        (text as NSString).draw(at: CGPoint(x: box.minX + 7, y: box.minY + 3), withAttributes: attrs)
    }

    private func drawMarker(_ m: Marker, side: Side) {
        let p = viewPoint(side, m.point)
        let isSel = m.index == selectedIndex
        let tint = m.enabled ? Self.markerColor(m.number) : NSColor.disabledControlTextColor

        NSColor.white.withAlphaComponent(0.9).setStroke()
        let cross = NSBezierPath()
        cross.lineWidth = 1
        cross.move(to: CGPoint(x: p.x - 5, y: p.y)); cross.line(to: CGPoint(x: p.x + 5, y: p.y))
        cross.move(to: CGPoint(x: p.x, y: p.y - 5)); cross.line(to: CGPoint(x: p.x, y: p.y + 5))
        cross.stroke()

        let text = "\(m.number)"
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 10, weight: .bold),
            .foregroundColor: tint.brightnessComponentSafe > 0.62 ? NSColor.black : NSColor.white,
        ]
        let size = (text as NSString).size(withAttributes: attrs)
        let badge = NSRect(x: p.x + 4, y: p.y + 4, width: size.width + 8, height: size.height + 3)
        tint.setFill()
        NSBezierPath(roundedRect: badge, xRadius: 2.5, yRadius: 2.5).fill()
        if m.error > 12 {
            NSColor.systemRed.setStroke()
            let r = NSBezierPath(roundedRect: badge.insetBy(dx: -1.5, dy: -1.5), xRadius: 4, yRadius: 4)
            r.lineWidth = 1.5
            r.stroke()
        }
        if isSel {
            NSColor.controlAccentColor.setStroke()
            let r = NSBezierPath(roundedRect: badge.insetBy(dx: -3, dy: -3), xRadius: 5, yRadius: 5)
            r.lineWidth = 2
            r.stroke()
        }
        (text as NSString).draw(at: CGPoint(x: badge.minX + 4, y: badge.minY + 1), withAttributes: attrs)
    }

    /// 커서 둘레를 크게 비춰 주는 확대경 — 한 화소를 다투는 작업이라 꼭 필요하다.
    private func drawLoupe(at c: CGPoint) {
        let s = side(at: c)
        let p = pane(s)
        guard let img = p.image, p.zoom < 6 else { return }
        let size: CGFloat = 132, mag: CGFloat = 6
        var origin = CGPoint(x: c.x + 22, y: c.y - size - 22)
        origin.x = min(max(8, origin.x), bounds.width - size - 8)
        origin.y = min(max(8, origin.y), bounds.height - size - 8)
        let box = NSRect(origin: origin, size: CGSize(width: size, height: size))

        let clip = NSBezierPath(ovalIn: box)
        NSGraphicsContext.saveGraphicsState()
        clip.addClip()
        NSColor.black.setFill()
        box.fill()
        let src = sourcePoint(s, c)
        let k = proxyScale(s)
        let drawn = NSRect(x: box.midX - src.x * k * mag, y: box.midY - src.y * k * mag,
                           width: img.size.width * mag, height: img.size.height * mag)
        NSGraphicsContext.current?.imageInterpolation = .none
        img.draw(in: drawn)
        NSColor.controlAccentColor.setStroke()
        let cross = NSBezierPath()
        cross.lineWidth = 1
        cross.move(to: CGPoint(x: box.midX, y: box.midY - 12)); cross.line(to: CGPoint(x: box.midX, y: box.midY - 4))
        cross.move(to: CGPoint(x: box.midX, y: box.midY + 4));  cross.line(to: CGPoint(x: box.midX, y: box.midY + 12))
        cross.move(to: CGPoint(x: box.midX - 12, y: box.midY)); cross.line(to: CGPoint(x: box.midX - 4, y: box.midY))
        cross.move(to: CGPoint(x: box.midX + 4, y: box.midY));  cross.line(to: CGPoint(x: box.midX + 12, y: box.midY))
        cross.stroke()
        NSGraphicsContext.restoreGraphicsState()
        NSColor.separatorColor.setStroke()
        clip.lineWidth = 1
        clip.stroke()
    }

    // MARK: - 입력

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let t = tracking { removeTrackingArea(t) }
        let t = NSTrackingArea(rect: bounds,
                               options: [.mouseMoved, .mouseEnteredAndExited, .activeInKeyWindow],
                               owner: self)
        addTrackingArea(t)
        tracking = t
    }

    override func mouseMoved(with e: NSEvent) {
        cursor = convert(e.locationInWindow, from: nil)
        needsDisplay = true
    }

    override func mouseExited(with e: NSEvent) {
        cursor = nil
        needsDisplay = true
    }

    override func mouseDown(with e: NSEvent) {
        let p = convert(e.locationInWindow, from: nil)
        let s = side(at: p)
        if p.y < stripHeight {
            for (i, t) in thumbs.enumerated() where thumbRect(s, at: i).contains(p) {
                onPickThumb?(s, t.id)
                return
            }
            return
        }
        dragMoved = false
        if let (hit, arrayIdx) = marker(at: p, side: s) {
            selectedIndex = hit.index
            onSelect?(hit.index)
            drag = .moveMarker(mouse: p, markerIndex: hit.index, arrayIndex: arrayIdx, side: s)
        } else {
            drag = .pan(mouse: p, offset: pane(s).offset, side: s)
        }
    }

    override func mouseDragged(with e: NSEvent) {
        guard let d = drag else { return }
        let p = convert(e.locationInWindow, from: nil)
        
        switch d {
        case .pan(let mouse, let offset, let side):
            if abs(p.x - mouse.x) + abs(p.y - mouse.y) > 3 { dragMoved = true }
            var pn = pane(side)
            pn.offset = CGPoint(x: offset.x + (p.x - mouse.x), y: offset.y + (p.y - mouse.y))
            pn.userAdjusted = true
            setPane(side, pn)
        case .moveMarker(_, _, let arrayIdx, let side):
            dragMoved = true
            let src = sourcePoint(side, p)
            var pn = pane(side)
            pn.markers[arrayIdx].point = src
            setPane(side, pn)
        }
        
        cursor = p
        needsDisplay = true
    }

    override func mouseUp(with e: NSEvent) {
        defer { drag = nil }
        guard let d = drag else { return }
        let p = convert(e.locationInWindow, from: nil)
        
        switch d {
        case .pan(_, _, let side):
            if !dragMoved {
                let src = sourcePoint(side, p)
                let size = pane(side).srcSize
                if src.x >= 0, src.y >= 0, src.x < size.width, src.y < size.height {
                    onAddPoint?(side, src, e.modifierFlags.contains(.option))
                }
            }
        case .moveMarker(_, let markerIdx, let arrayIdx, let side):
            if dragMoved {
                let pt = pane(side).markers[arrayIdx].point
                onMovePoint?(markerIdx, side, pt)
            }
        }
    }

    override func scrollWheel(with e: NSEvent) {
        let p = convert(e.locationInWindow, from: nil)
        let s = side(at: p)
        if p.y < stripHeight {
            let d = abs(e.scrollingDeltaX) > abs(e.scrollingDeltaY) ? e.scrollingDeltaX : e.scrollingDeltaY
            let step = d * (e.hasPreciseScrollingDeltas ? 1.0 : 6.0)
            if s == .left { thumbOffset.left = clampThumbOffset(.left, thumbOffset.left - step) }
            else { thumbOffset.right = clampThumbOffset(.right, thumbOffset.right - step) }
            needsDisplay = true
            return
        }
        var pn = pane(s)
        guard pn.image != nil else { return }
        let delta = e.hasPreciseScrollingDeltas ? e.scrollingDeltaY * 0.004 : e.deltaY * 0.06
        let next = max(0.05, min(20, pn.zoom * exp(delta)))
        let f = frame(s)
        let local = CGPoint(x: p.x - f.minX, y: p.y - f.minY)
        let k = next / max(pn.zoom, 0.0001)
        pn.offset = CGPoint(x: local.x - (local.x - pn.offset.x) * k,
                            y: local.y - (local.y - pn.offset.y) * k)
        pn.zoom = next
        pn.userAdjusted = true
        setPane(s, pn)
        cursor = p
        needsDisplay = true
    }

    private func marker(at p: CGPoint, side s: Side) -> (Marker, Int)? {
        for (i, m) in pane(s).markers.enumerated().reversed() {
            let v = viewPoint(s, m.point)
            if hypot(v.x - p.x, v.y - p.y) < 11 { return (m, i) }
            if NSRect(x: v.x + 2, y: v.y + 2, width: 26, height: 18).contains(p) { return (m, i) }
        }
        return nil
    }

    override var acceptsFirstResponder: Bool { true }

    // MARK: - 접근성·진단

    override func isAccessibilityElement() -> Bool { true }
    override func accessibilityRole() -> NSAccessibility.Role? { .group }
    override func accessibilityLabel() -> String? {
        "왼쪽 \(left.caption), 오른쪽 \(right.caption)"
    }

    func dumpState() -> String {
        func d(_ p: Pane) -> String {
            let sz = p.image.map { "\(Int($0.size.width))x\(Int($0.size.height))" } ?? "없음"
            return "[그림=\(sz) 배율=\(String(format: "%.3f", p.zoom)) 표=\(p.caption)]"
        }
        return "칸크기=\(Int(frame(.left).width))x\(Int(frame(.left).height)) 왼쪽\(d(left)) 오른쪽\(d(right))"
    }
}

extension NSColor {
    /// 글자색을 정할 때 쓰는 밝기. 색공간이 달라 실패해도 안전하게 중간값을 준다.
    var brightnessComponentSafe: CGFloat {
        usingColorSpace(.deviceRGB)?.brightnessComponent ?? 0.5
    }
}
