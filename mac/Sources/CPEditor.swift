import AppKit

/// 제어점 편집 화면.
///
/// 두 사진을 나란히 놓고, 왼쪽에서 한 점을 찍으면 오른쪽에서 짝을 찾아 준다.
/// 자동 정렬이 실패한 자리를 사람이 직접 이어 주는 곳이라, 확대경과 잔차 색을
/// 함께 보여 주는 것이 중요하다.
@MainActor
final class CPEditorViewController: NSViewController {

    var engine: Engine?
    var onChanged: (() -> Void)?          // 제어점이 바뀌어 프로젝트를 다시 읽어야 할 때

    private let kindPopup = NSPopUpButton()
    private let hint = NSTextField(labelWithString: "")
    private let summary = NSTextField(labelWithString: "")
    private let pair = CPPairView()

    private var images: [SourceImage] = []
    private var points: [ControlPoint] = []
    private var pairA: Int?
    private var pairB: Int?
    private var pendingLeft: CGPoint?
    private var busy = false
    private var thumbCache: [Int: NSImage] = [:]

    // MARK: - 화면

    override func loadView() {
        let root = NSView()
        let bar = buildToolStrip()
        let underStrip = NSBox(); underStrip.boxType = .separator
        let aboveBar = NSBox(); aboveBar.boxType = .separator

        for v in [pair, bar, underStrip, aboveBar] as [NSView] {
            v.translatesAutoresizingMaskIntoConstraints = false
            root.addSubview(v)
        }

        let topGuide: NSLayoutYAxisAnchor = {
            if #available(macOS 11.0, *) { return root.safeAreaLayoutGuide.topAnchor }
            return root.topAnchor
        }()

        NSLayoutConstraint.activate([
            underStrip.topAnchor.constraint(equalTo: topGuide),
            underStrip.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            underStrip.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            underStrip.heightAnchor.constraint(equalToConstant: 0),

            pair.topAnchor.constraint(equalTo: topGuide),
            pair.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            pair.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            pair.bottomAnchor.constraint(equalTo: aboveBar.topAnchor),

            aboveBar.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            aboveBar.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            aboveBar.bottomAnchor.constraint(equalTo: bar.topAnchor),

            bar.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            bar.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            bar.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            bar.heightAnchor.constraint(equalToConstant: 40),
        ])
        view = root
        wire()
    }

    private func buildToolStrip() -> NSView {
        let strip = NSView()

        let swap = NSButton(title: "⇄ 좌우 바꾸기", target: self, action: #selector(swapPair))
        swap.bezelStyle = .rounded
        swap.controlSize = .small

        kindPopup.addItems(withTitles: ["보통", "수직선", "수평선"])
        kindPopup.toolTip = "수직선·수평선은 같은 사진 안에서 두 점을 찍습니다"
        kindPopup.controlSize = .small
        kindPopup.widthAnchor.constraint(equalToConstant: 92).isActive = true

        let prune = NSButton(title: "나쁜 점 정리", target: self, action: #selector(pruneBad))
        prune.bezelStyle = .rounded
        prune.controlSize = .small
        prune.toolTip = "잔차가 큰 제어점을 꺼서 최적화 품질을 올립니다"

        summary.font = UI.monoFont(11)
        summary.textColor = .secondaryLabelColor
        hint.font = UI.font(.callout)
        hint.textColor = .secondaryLabelColor
        hint.lineBreakMode = .byTruncatingTail
        hint.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        
        summary.lineBreakMode = .byTruncatingTail
        summary.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let spacerA = NSView()
        spacerA.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let spacerB = NSView()
        spacerB.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let row = NSStackView(views: [swap, kindPopup, prune, spacerA, hint, spacerB, summary])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 10
        row.translatesAutoresizingMaskIntoConstraints = false
        strip.addSubview(row)
        NSLayoutConstraint.activate([
            row.leadingAnchor.constraint(equalTo: strip.leadingAnchor, constant: 12),
            row.trailingAnchor.constraint(equalTo: strip.trailingAnchor, constant: -12),
            row.centerYAnchor.constraint(equalTo: strip.centerYAnchor),
        ])
        return strip
    }

    private func wire() {
        pair.onPickThumb = { [weak self] side, id in
            guard let self else { return }
            if side == .left {
                if id == self.pairB { self.pairB = self.pairA }   // 같은 장을 두 번 고르지 않게
                self.pairA = id
            } else {
                if id == self.pairA { self.pairA = self.pairB }
                self.pairB = id
            }
            self.syncStrips()
            self.loadPair()
        }
        pair.onAddPoint = { [weak self] side, pt, alt in
            guard let self else { return }
            if side == .left { self.leftClicked(pt, option: alt) } else { self.rightClicked(pt) }
        }
        pair.onMovePoint = { [weak self] idx, side, pt in
            self?.movePoint(index: idx, side: side, point: pt)
        }
        pair.onSelect = { [weak self] idx in self?.pair.selectedIndex = idx }
    }

    /// 아직 정렬하지 않았으면 그 사실을 알려 준다. 제어점이 비어 있는 게
    /// 고장이 아니라 순서의 문제라는 걸 알아야 한다.
    var isAligned: Bool = false {
        didSet { if isAligned != oldValue { refreshHint() } }
    }

    private func refreshHint() {
        if !isAligned {
            hint.stringValue = "아직 정렬하지 않았습니다 — 툴바의 자동 정렬(⌘R)을 먼저 누르세요"
        } else if points.isEmpty {
            hint.stringValue = "이 쌍에는 제어점이 없습니다. 왼쪽 사진을 클릭해 직접 찍을 수 있습니다"
        } else {
            hint.stringValue = ""
        }
    }

    func update(images: [SourceImage]) {
        let newImages = images.filter { $0.enabled }
        let changed = self.images.map(\.id) != newImages.map(\.id)
        self.images = newImages
        
        var pairChanged = false
        if pairA == nil || !self.images.contains(where: { $0.id == pairA }) {
            pairA = self.images.first?.id
            pairChanged = true
        }
        if pairB == nil || pairB == pairA || !self.images.contains(where: { $0.id == pairB }) {
            pairB = self.images.first(where: { $0.id != pairA })?.id
            pairChanged = true
        }
        
        syncStrips()
        if changed || pairChanged || pair.image(.left) == nil {
            loadPair()
        } else {
            reloadPoints()
        }
    }

    private func syncStrips() {
        guard let engine else { return }
        let thumb: (Int) -> NSImage? = { [weak self] id in
            guard let self, let engine = self.engine else { return nil }
            if let c = self.thumbCache[id] { return c }
            let data = try? Data(contentsOf: engine.url("/api/thumb/\(id)"))
            let img = data.flatMap(NSImage.init(data:))
            self.thumbCache[id] = img
            return img
        }
        _ = engine
        pair.leftPick = pairA
        pair.rightPick = pairB
        pair.setThumbnails(images, thumb: thumb)
        pair.revealPicks(left: pairA, right: pairB)
    }

    private func image(with id: Int) -> SourceImage? { images.first { $0.id == id } }

    /// 프록시 사진을 읽는다.
    ///
    /// NSImage(contentsOf:) 로 http 주소를 바로 읽으면 이따금 빈 값이 온다.
    /// 자료를 먼저 받아 이미지로 만들고, 한 번은 다시 시도한다. 여기서 실패하면
    /// 칸이 통째로 비어 무엇이 잘못됐는지 알기 어려워진다.
    private func loadProxy(_ id: Int) -> NSImage? {
        guard let engine else { return nil }
        let url = engine.url("/api/proxy/\(id)")
        for _ in 0..<2 {
            if let data = try? Data(contentsOf: url), let img = NSImage(data: data) {
                return img
            }
            Thread.sleep(forTimeInterval: 0.15)
        }
        return nil
    }

    func loadPair() {
        guard let engine, let a = pairA, let b = pairB, a != b,
              let ia = image(with: a), let ib = image(with: b) else {
            Diag.log("loadPair 건너뜀 pair=(\(pairA.map(String.init) ?? "nil"), \(pairB.map(String.init) ?? "nil"))")
            return
        }
        Diag.log("loadPair 실행 pair=(\(a), \(b))")
        pendingLeft = nil
        pair.pending = nil

        pair.set(.left, image: loadProxy(ia.id),
                 srcSize: CGSize(width: ia.width, height: ia.height),
                 caption: "\(ia.id + 1). \(ia.name)")
        pair.set(.right, image: loadProxy(ib.id),
                 srcSize: CGSize(width: ib.width, height: ib.height),
                 caption: "\(ib.id + 1). \(ib.name)")
        reloadPoints()
    }

    private func reloadPoints() {
        guard let engine, let a = pairA, let b = pairB else { return }
        Task {
            do {
                let list: ControlPointList = try await engine.get(
                    "/api/control-points?pair=\(a),\(b)")
                points = list.points
                applyMarkers()
            } catch {
                hint.stringValue = error.localizedDescription
            }
        }
    }

    private func applyMarkers() {
        guard let a = pairA else { return }
        // 화면에 보이는 번호는 이 쌍 안에서의 순번이다. 양쪽이 같은 번호·같은
        // 색을 쓰므로 어느 점과 어느 점이 짝인지 눈으로 바로 이어진다.
        pair.setMarkers(.left, points.enumerated().map { n, c in
            .init(index: c.index, number: n + 1,
                  point: c.imgA == a ? CGPoint(x: c.xa, y: c.ya) : CGPoint(x: c.xb, y: c.yb),
                  error: c.error, enabled: c.enabled)
        })
        pair.setMarkers(.right, points.enumerated().map { n, c in
            .init(index: c.index, number: n + 1,
                  point: c.imgA == a ? CGPoint(x: c.xb, y: c.yb) : CGPoint(x: c.xa, y: c.ya),
                  error: c.error, enabled: c.enabled)
        })
        refreshHint()
        let errs = points.filter { $0.enabled && $0.error > 0 }.map(\.error)
        if errs.isEmpty {
            summary.stringValue = "이 쌍의 제어점 \(points.count)개"
        } else {
            let rms = (errs.map { $0 * $0 }.reduce(0, +) / Double(errs.count)).squareRoot()
            summary.stringValue = String(format: "이 쌍의 제어점 %d개 · RMS %.2fpx · 최대 %.2fpx",
                                         points.count, rms, errs.max() ?? 0)
        }
    }

    /// 화면에 드러난 뒤 한 번 불린다. 숨어 있는 동안 크기가 0 이었다면
    /// 확대 비율이 잡히지 않았으므로 여기서 다시 맞춘다.
    func viewBecameVisible() {
        pair.fitAll()
        pair.revealPicks(left: pairA, right: pairB)
        pair.needsDisplay = true
    }

    /// 진단용 — 두 칸에 사진이 제대로 물렸는지 본다.
    func dumpState() -> String {
        let a = pairA.map(String.init) ?? "nil"
        let b = pairB.map(String.init) ?? "nil"
        return "pair=(\(a), \(b)) 사진목록=\(images.count) "
             + "\(pair.dumpState()) 제어점=\(points.count)"
    }

    // MARK: - 점 찍기

    private var kind: String {
        ["manual", "vertical", "horizontal"][kindPopup.indexOfSelectedItem]
    }

    private func leftClicked(_ pt: CGPoint, option: Bool) {
        guard !busy else { return }
        if kind != "manual" {
            // 수직·수평선은 같은 사진 안의 두 점을 잇는다
            if let first = pendingLeft {
                addPoint(a: pairA!, b: pairA!, p1: first, p2: pt, kind: kind)
                pendingLeft = nil
                pair.pending = nil
            } else {
                pendingLeft = pt
                pair.pending = (.left, pt)
                hint.stringValue = "같은 사진에서 두 번째 점을 찍으세요"
            }
            return
        }

        if option {
            pendingLeft = pt
            pair.pending = (.left, pt)
            hint.stringValue = "오른쪽 사진에서 짝이 될 지점을 클릭하세요"
            return
        }
        suggestMatch(for: pt)
    }

    private func rightClicked(_ pt: CGPoint) {
        guard !busy, let first = pendingLeft, let a = pairA, let b = pairB else {
            hint.stringValue = "왼쪽 사진을 먼저 클릭하세요"
            return
        }
        addPoint(a: a, b: b, p1: first, p2: pt, kind: "manual")
        pendingLeft = nil
        pair.pending = nil
    }

    /// 현재 카메라 파라미터로 짝을 예측하고 상호상관으로 다듬어 준다.
    private func suggestMatch(for pt: CGPoint) {
        guard let engine, let a = pairA, let b = pairB else { return }
        busy = true
        hint.stringValue = "짝을 찾는 중…"
        Task {
            defer { busy = false }
            do {
                let raw = try await engine.postRaw("/api/control-points/suggest",
                                                   ["img_a": a, "img_b": b,
                                                    "xa": pt.x, "ya": pt.y])
                guard let s = SuggestResult(raw) else {
                    hint.stringValue = "짝을 찾지 못했습니다"
                    return
                }
                if s.score < 0.45 {
                    // 자신 없으면 사람에게 맡긴다. 예측 위치로 화면만 옮겨 준다.
                    pendingLeft = pt
                    pair.pending = (.left, pt)
                    pair.center(.right, on: CGPoint(x: s.x, y: s.y), zoom: 1.2)
                    hint.stringValue = String(format: "자신이 없습니다 (일치도 %.2f). 직접 찍어 주세요", s.score)
                    return
                }
                addPoint(a: a, b: b, p1: pt, p2: CGPoint(x: s.x, y: s.y), kind: "manual",
                         note: String(format: "추가됨 (일치도 %.2f)", s.score))
            } catch {
                hint.stringValue = error.localizedDescription
            }
        }
    }

    private func addPoint(a: Int, b: Int, p1: CGPoint, p2: CGPoint,
                          kind: String, note: String = "추가됨") {
        guard let engine else { return }
        Task {
            do {
                _ = try await engine.postRaw("/api/control-points",
                                             ["img_a": a, "img_b": b,
                                              "xa": p1.x, "ya": p1.y,
                                              "xb": p2.x, "yb": p2.y, "kind": kind])
                hint.stringValue = note
                reloadPoints()
                onChanged?()
            } catch {
                hint.stringValue = error.localizedDescription
            }
        }
    }

    private func movePoint(index: Int, side: CPPairView.Side, point: CGPoint) {
        guard let engine else { return }
        Task {
            do {
                let payload: [String: Any] = side == .left ? ["xa": point.x, "ya": point.y] : ["xb": point.x, "yb": point.y]
                let _: ControlPoint = try await engine.patch("/api/control-points/\(index)", payload)
                reloadPoints()
                onChanged?()
            } catch {
                hint.stringValue = error.localizedDescription
            }
        }
    }

    func deleteSelected() {
        guard let engine, let idx = pair.selectedIndex else { return }
        Task {
            do {
                try await engine.delete("/api/control-points/\(idx)")
                pair.selectedIndex = nil
                hint.stringValue = "제어점 #\(idx) 삭제"
                reloadPoints()
                onChanged?()
            } catch {
                hint.stringValue = error.localizedDescription
            }
        }
    }

    // MARK: - 동작

    @objc private func swapPair() {
        let a = pairA; pairA = pairB; pairB = a
        syncStrips()
        loadPair()
    }

    @objc private func pruneBad() {
        guard let engine else { return }
        Task {
            do {
                let r = try await engine.postRaw("/api/control-points/prune", ["threshold": 0])
                let n = r["disabled"] as? Int ?? 0
                let thr = r["threshold"] as? Double ?? 0
                hint.stringValue = n > 0
                    ? String(format: "잔차 %.1fpx 초과 %d개를 껐습니다", thr, n)
                    : "정리할 점이 없습니다"
                reloadPoints()
                onChanged?()
            } catch {
                hint.stringValue = error.localizedDescription
            }
        }
    }

    override func keyDown(with e: NSEvent) {
        // ⌫ 로 고른 제어점을 지운다
        if e.keyCode == 51 || e.keyCode == 117 { deleteSelected() } else { super.keyDown(with: e) }
    }
}
