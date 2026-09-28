import AppKit

/// 파노라마 미리보기 화면.
///
/// 상태가 넷이다. 사진이 없을 때, 사진은 있는데 아직 정렬하지 않았을 때,
/// 그리고 결과가 있을 때. 가운데 칸은 늘 '지금 할 일'을 보여 준다.
///
/// 하나 더 있는 이유는 미리보기 합성이 29장 기준 십수 초씩 걸리기 때문이다.
/// 그동안 정렬 안내 화면이 그대로 떠 있으면 정렬이 실패한 것처럼 보인다.
@MainActor
final class PreviewViewController: NSViewController {

    enum State {
        case empty
        case needsAlign(count: Int)
        case rendering
        case panorama
    }

    var onAlign: (() -> Void)?
    /// 미리보기를 끌 때. dx·dy 는 화면에 보이는 그림 폭에 대한 비율(오른쪽·아래가 +),
    /// roll 은 화면에서 반시계로 돈 각도(도).
    ///
    /// 화소가 아니라 '보이는 폭의 몇 배' 로 넘기는 이유: 끄는 중에는 해상도가 다른
    /// 그림이 번갈아 들어온다. 화소로 재면 어떤 그림이 떠 있느냐에 따라 같은 손놀림이
    /// 다른 각도가 된다. 화각이 고정돼 있으니 보이는 폭에 대한 비율은 늘 같은 각도다.
    var onOrientDrag: ((OrientDragPhase, Double, Double, Double) -> Void)?

    private let scroll = NSScrollView()
    private let imageView = OrientDragImageView()
    private let emptyBox = NSStackView()
    private let alignBox = NSStackView()
    private let alignTitle = NSTextField(labelWithString: "")
    /// Return 키는 이 안내 화면이 떠 있을 때만 이 버튼에 준다. 숨겨진 채로 창의
    /// 기본 버튼으로 남아 있으면, 다른 곳에서 누른 Return 이 정렬을 다시 돌린다.
    private let alignButton = NSButton(title: "", target: nil, action: nil)
    private let statusLabel = NSTextField(labelWithString: "")
    private let renderBox = NSStackView()
    private let renderBar = NSProgressIndicator()
    private let renderLabel = NSTextField(labelWithString: "")
    private var state: State = .empty
    private var dragShownWidth: CGFloat = 1
    /// 화면에 맞춘 상태인지. 사용자가 직접 확대·축소하면 풀린다. 맞춘 상태면
    /// 그림이 바뀌어도, 창 크기가 바뀌어도 늘 다시 맞춘다.
    private var fitted = true

    override func loadView() {
        let root = NSView()

        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = true
        scroll.autohidesScrollers = true
        scroll.allowsMagnification = true
        scroll.minMagnification = 0.03
        scroll.maxMagnification = 12
        scroll.automaticallyAdjustsContentInsets = false
        scroll.drawsBackground = true
        scroll.backgroundColor = .underPageBackgroundColor

        // 그림이 화면보다 작을 때 가운데에 놓이게 한다. 기본 클립 뷰는 늘
        // 왼쪽 위로 붙여서, 작은 파노라마가 한쪽으로 쏠린 채 보인다.
        let clip = CenteringClipView()
        clip.drawsBackground = false
        scroll.contentView = clip

        imageView.imageScaling = .scaleProportionallyUpOrDown
        imageView.imageAlignment = .alignCenter
        scroll.documentView = imageView
        imageView.onDrag = { [weak self] phase, delta, roll in
            guard let self else { return }
            if phase == .began {
                let w = (self.imageView.image?.size.width ?? 1) * self.scroll.magnification
                self.dragShownWidth = max(w, 1)
            }
            // 창 좌표는 위가 + 이므로 세로를 뒤집는다
            let w = Double(self.dragShownWidth)
            self.onOrientDrag?(phase, Double(delta.x) / w, -Double(delta.y) / w, roll)
        }
        // 트랙패드로 직접 확대하면 '맞춤' 상태를 푼다
        NotificationCenter.default.addObserver(
            forName: NSScrollView.didEndLiveMagnifyNotification, object: scroll, queue: .main
        ) { [weak self] _ in MainActor.assumeIsolated { self?.fitted = false } }

        root.addSubview(scroll)
        buildEmptyBox(in: root)
        buildAlignBox(in: root)
        buildRenderBox(in: root)

        statusLabel.font = UI.monoFont(11)
        statusLabel.textColor = .tertiaryLabelColor
        statusLabel.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(statusLabel)

        NSLayoutConstraint.activate([
            // 툴바 아래에서 시작한다. 툴바 밑까지 깔면 가려진 높이까지 '보이는 공간' 으로
            // 셈해서, 맞춤을 해도 그림 위아래가 툴바와 창 끝에 잘렸다.
            scroll.topAnchor.constraint(equalTo: safeTop(root)),
            scroll.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            scroll.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            statusLabel.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -12),
            statusLabel.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -8),
        ])
        view = root
        apply(.empty)
    }

    private func buildEmptyBox(in root: NSView) {
        let icon = NSImageView()
        icon.image = UI.symbol("photo.on.rectangle.angled", "사진")
        if #available(macOS 11.0, *) {
            icon.symbolConfiguration = .init(pointSize: 40, weight: .light)
        }
        icon.contentTintColor = .tertiaryLabelColor

        let title = NSTextField(labelWithString: "사진을 끌어다 놓으세요")
        title.font = .systemFont(ofSize: 16, weight: .medium)
        title.textColor = .secondaryLabelColor
        title.alignment = .center

        let sub = NSTextField(labelWithString: "폴더째 놓아도 되고, 툴바의 + 로 골라도 됩니다")
        sub.font = UI.font(.callout)
        sub.textColor = .tertiaryLabelColor
        sub.alignment = .center

        emptyBox.orientation = .vertical
        emptyBox.alignment = .centerX
        emptyBox.spacing = 6
        emptyBox.translatesAutoresizingMaskIntoConstraints = false
        [icon, title, sub].forEach { emptyBox.addArrangedSubview($0) }
        emptyBox.setCustomSpacing(14, after: icon)
        root.addSubview(emptyBox)
        NSLayoutConstraint.activate([
            emptyBox.centerXAnchor.constraint(equalTo: root.centerXAnchor),
            emptyBox.centerYAnchor.constraint(equalTo: root.centerYAnchor),
        ])
    }

    /// 사진은 들어왔는데 아직 정렬하지 않은 상태 — 다음에 할 일을 크게 내건다.
    private func buildAlignBox(in root: NSView) {
        let icon = NSImageView()
        icon.image = UI.symbol("wand.and.stars", "자동 정렬")
        if #available(macOS 11.0, *) {
            icon.symbolConfiguration = .init(pointSize: 38, weight: .light)
        }
        icon.contentTintColor = .controlAccentColor

        alignTitle.font = .systemFont(ofSize: 17, weight: .semibold)
        alignTitle.textColor = .labelColor
        alignTitle.alignment = .center

        let sub = NSTextField(labelWithString:
            "겹치는 부분을 찾아 카메라 자세와 렌즈를 한꺼번에 풉니다")
        sub.font = UI.font(.callout)
        sub.textColor = .secondaryLabelColor
        sub.alignment = .center

        let button = alignButton
        button.title = "자동 정렬"
        button.target = self
        button.action = #selector(alignTapped)
        button.bezelStyle = .rounded
        button.controlSize = .large
        button.font = .systemFont(ofSize: 15, weight: .medium)
        if #available(macOS 11.0, *) {
            button.image = UI.symbol("wand.and.stars", "")
            button.imagePosition = .imageLeading
        }
        button.widthAnchor.constraint(greaterThanOrEqualToConstant: 190).isActive = true

        let hint = NSTextField(labelWithString: "⌘R")
        hint.font = UI.font(.caption)
        hint.textColor = .tertiaryLabelColor
        hint.alignment = .center

        alignBox.orientation = .vertical
        alignBox.alignment = .centerX
        alignBox.spacing = 6
        alignBox.translatesAutoresizingMaskIntoConstraints = false
        [icon, alignTitle, sub, button, hint].forEach { alignBox.addArrangedSubview($0) }
        alignBox.setCustomSpacing(16, after: icon)
        alignBox.setCustomSpacing(20, after: sub)
        root.addSubview(alignBox)
        NSLayoutConstraint.activate([
            alignBox.centerXAnchor.constraint(equalTo: root.centerXAnchor),
            alignBox.centerYAnchor.constraint(equalTo: root.centerYAnchor),
        ])
    }

    private func buildRenderBox(in root: NSView) {
        let title = NSTextField(labelWithString: "미리보기 만드는 중")
        title.font = .systemFont(ofSize: 15, weight: .semibold)
        title.alignment = .center

        renderBar.isIndeterminate = false
        renderBar.minValue = 0
        renderBar.maxValue = 1
        renderBar.style = .bar
        renderBar.widthAnchor.constraint(equalToConstant: 240).isActive = true

        renderLabel.font = UI.font(.callout)
        renderLabel.textColor = .secondaryLabelColor
        renderLabel.alignment = .center
        renderLabel.lineBreakMode = .byTruncatingTail
        renderLabel.widthAnchor.constraint(equalToConstant: 260).isActive = true

        renderBox.orientation = .vertical
        renderBox.alignment = .centerX
        renderBox.spacing = 10
        renderBox.isHidden = true
        renderBox.translatesAutoresizingMaskIntoConstraints = false
        [title, renderBar, renderLabel].forEach { renderBox.addArrangedSubview($0) }
        root.addSubview(renderBox)
        NSLayoutConstraint.activate([
            renderBox.centerXAnchor.constraint(equalTo: root.centerXAnchor),
            renderBox.centerYAnchor.constraint(equalTo: root.centerYAnchor),
        ])
    }

    @objc private func alignTapped() { onAlign?() }

    // MARK: - 상태

    func apply(_ s: State) {
        state = s
        alignButton.keyEquivalent = ""
        switch s {
        case .empty:
            emptyBox.isHidden = false
            alignBox.isHidden = true
            renderBox.isHidden = true
            scroll.isHidden = true
            statusLabel.stringValue = ""
        case .needsAlign(let n):
            alignButton.keyEquivalent = "\r"
            emptyBox.isHidden = true
            alignBox.isHidden = false
            renderBox.isHidden = true
            scroll.isHidden = true
            alignTitle.stringValue = "사진 \(n)장 준비됨"
            statusLabel.stringValue = ""
        case .rendering:
            emptyBox.isHidden = true
            alignBox.isHidden = true
            renderBox.isHidden = false
            scroll.isHidden = true
        case .panorama:
            emptyBox.isHidden = true
            alignBox.isHidden = true
            renderBox.isHidden = true
            scroll.isHidden = false
        }
    }

    /// 합성이 시작됐다. 이미 그림이 있으면 그걸 치우지 않는다 — 설정 하나
    /// 바꿀 때마다 화면이 비면 비교할 수가 없다. 첫 합성일 때만 자리를 내준다.
    func beginRender() {
        renderBar.doubleValue = 0
        renderLabel.stringValue = "…"
        if imageView.image == nil { apply(.rendering) }
    }

    func renderProgress(_ frac: Double, _ message: String) {
        renderBar.doubleValue = frac
        renderLabel.stringValue = message
        if imageView.image != nil {
            statusLabel.stringValue = message.isEmpty
                ? "미리보기 만드는 중…" : "\(message)  \(Int(frac * 100))%"
        }
    }

    func show(image: NSImage, status: String) {
        statusLabel.stringValue = status
        apply(.panorama)
        replace(image)
    }

    /// 그림만 갈아 끼운다. 맞춘 상태면 다시 맞추고, 사용자가 확대해 둔 상태면
    /// 보이는 크기와 보던 자리를 그대로 지킨다.
    private func replace(_ image: NSImage) {
        let old = imageView.image
        let clip = scroll.contentView
        var focus = CGPoint(x: 0.5, y: 0.5)       // 보고 있던 지점 (그림에 대한 비율)
        if let o = old, o.size.width > 0, o.size.height > 0 {
            focus = CGPoint(x: clip.bounds.midX / o.size.width,
                            y: clip.bounds.midY / o.size.height)
        }
        let shown = (old?.size.width ?? image.size.width) * scroll.magnification

        imageView.image = image
        imageView.frame = NSRect(origin: .zero, size: image.size)
        guard !fitted, old != nil, image.size.width > 0 else {
            zoomToFit()
            return
        }
        scroll.magnification = max(scroll.minMagnification,
                                   min(scroll.maxMagnification, shown / image.size.width))
        let b = clip.bounds.size
        clip.scroll(to: NSPoint(x: focus.x * image.size.width - b.width / 2,
                                y: focus.y * image.size.height - b.height / 2))
        scroll.reflectScrolledClipView(clip)
    }

    /// 끄는 동안 들어오는 저해상도 한 장
    func showLive(_ image: NSImage) {
        guard imageView.image != nil else {
            show(image: image, status: statusLabel.stringValue)
            return
        }
        replace(image)
    }

    // MARK: - 확대

    func zoomToFit() {
        fitted = true
        guard let img = imageView.image, img.size.width > 0 else { return }
        // 배율의 영향을 받지 않는 실제 뷰 크기로 잰다. contentView.bounds 는 배율로
        // 나뉜 크기라서, 그걸 쓰면 호출할 때마다 '맞춤' 과 '100%' 를 번갈아 오갔다 —
        // 창이 다시 배치될 때마다 불리므로 그림이 확대됐다 줄었다 했다.
        let avail = scroll.contentView.frame.size
        guard avail.width > 1, avail.height > 1 else { return }
        let factor = min(avail.width / img.size.width, avail.height / img.size.height)
        let m = max(scroll.minMagnification, min(factor, 1.0))
        if abs(scroll.magnification - m) > 1e-4 { scroll.magnification = m }
    }

    func zoomToActual() {
        fitted = false
        scroll.magnification = 1.0
    }

    func zoom(by factor: CGFloat) {
        fitted = false
        scroll.magnification = max(scroll.minMagnification,
                                   min(scroll.maxMagnification, scroll.magnification * factor))
    }

    override func viewDidLayout() {
        super.viewDidLayout()
        if case .panorama = state, fitted { zoomToFit() }
    }
}

@MainActor
private func safeTop(_ v: NSView) -> NSLayoutYAxisAnchor {
    if #available(macOS 11.0, *) { return v.safeAreaLayoutGuide.topAnchor }
    return v.topAnchor
}

/// 내용이 화면보다 작으면 가운데에 두는 클립 뷰.
@MainActor
final class CenteringClipView: NSClipView {
    override func constrainBoundsRect(_ proposedBounds: NSRect) -> NSRect {
        var rect = super.constrainBoundsRect(proposedBounds)
        guard let doc = documentView else { return rect }
        let frame = doc.frame
        if rect.width > frame.width {
            rect.origin.x = (frame.width - rect.width) / 2
        }
        if rect.height > frame.height {
            rect.origin.y = (frame.height - rect.height) / 2
        }
        return rect
    }
}


enum OrientDragPhase { case began, changed, ended }

/// 끌어서 파노라마 방향을 돌리는 그림 뷰.
///
/// 왼쪽 끌기는 좌우·상하, 오른쪽 끌기나 ⌥ 끌기는 기울기. PTGui 파노라마 편집기와
/// 같은 손버릇이다. 기울기는 그림 가운데를 축으로 마우스가 돈 각도를 쓴다.
final class OrientDragImageView: NSImageView {
    var onDrag: ((OrientDragPhase, NSPoint, Double) -> Void)?

    private var start = NSPoint.zero
    private var rolling = false
    private var active = false

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func resetCursorRects() {
        if image != nil { addCursorRect(visibleRect, cursor: .openHand) }
    }

    override func mouseDown(with e: NSEvent) { begin(e, roll: e.modifierFlags.contains(.option)) }
    override func mouseDragged(with e: NSEvent) { move(e) }
    override func mouseUp(with e: NSEvent) { end() }
    override func rightMouseDown(with e: NSEvent) { begin(e, roll: true) }
    override func rightMouseDragged(with e: NSEvent) { move(e) }
    override func rightMouseUp(with e: NSEvent) { end() }

    private func begin(_ e: NSEvent, roll: Bool) {
        // 끄는 중에 두 번째 누름이 들어오면 무시한다 — 시작점이 새로 잡혀 튄다
        guard image != nil, !active else { return }
        start = e.locationInWindow
        rolling = roll
        active = true
        NSCursor.closedHand.push()
        onDrag?(.began, .zero, 0)
    }

    private func move(_ e: NSEvent) {
        guard active else { return }
        let p = e.locationInWindow
        if rolling {
            // 그림 가운데를 축으로 시작점에서 지금 점까지 돈 각도 (창 좌표, 반시계 +)
            let c = convert(NSPoint(x: visibleRect.midX, y: visibleRect.midY), to: nil)
            let a0 = atan2(Double(start.y - c.y), Double(start.x - c.x))
            let a1 = atan2(Double(p.y - c.y), Double(p.x - c.x))
            var d = (a1 - a0) * 180 / .pi
            if d > 180 { d -= 360 } else if d < -180 { d += 360 }
            onDrag?(.changed, .zero, d)
        } else {
            onDrag?(.changed, NSPoint(x: p.x - start.x, y: p.y - start.y), 0)
        }
    }

    private func end() {
        guard active else { return }
        active = false
        NSCursor.pop()
        onDrag?(.ended, .zero, 0)
    }
}
