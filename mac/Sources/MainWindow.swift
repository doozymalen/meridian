import AppKit
import UniformTypeIdentifiers

/// 창 하나, 세 칸 — 사진 목록 · 미리보기 · 속성.
///
/// 툴바는 제목 줄과 하나로 합쳐(unified) 창을 넓게 쓰고, 양 옆 칸은 접을 수 있다.
@MainActor
final class MainWindowController: NSWindowController, NSToolbarDelegate {

    private let engine = Engine()
    private let split = NSSplitViewController()
    private let sidebar = SidebarViewController()
    private let content = ContentContainerViewController()
    private var preview: PreviewViewController { content.preview }
    private let inspector = InspectorViewController()
    private let modeControl = NSSegmentedControl(
        labels: ["미리보기", "제어점"], trackingMode: .selectOne, target: nil, action: nil)

    private var project = Project.empty
    private var previewResult: PreviewResult?
    private var busy = false
    private var previewTask: Task<Void, Never>?

    /// 설정이 바뀌었지만 아직 미리보기에 반영하지 않은 상태.
    ///
    /// 사진이 많으면 미리보기 한 번에 여러 초가 걸린다. 설정을 하나 만질 때마다
    /// 다시 그리면 이것저것 바꿔 보는 일이 사실상 불가능하다. 바뀐 것은 모아
    /// 두었다가 사용자가 갱신을 누를 때 한 번에 그린다.
    /// 미리보기를 끄는 동안의 상태. 시작 방향, 지금 가리키는 방향, 요청 중인지.
    private var dragBase: (Double, Double, Double)?
    /// 빠른 미리보기로 보여 줄 값: 좌우, 상하, 기울기, 가로 화각, 세로 화각 (도)
    private var liveTarget: (Double, Double, Double, Double, Double)?
    /// 끌기나 슬라이더 조작이 진행 중인지. 끝나면 늦게 온 빠른 미리보기를 버린다.
    private var liveActive = false
    private var liveInFlight = false
    /// 방향 적용이 렌더 중에 들어오면 버리지 않고 이어서 그린다
    private var queuedOrientation: (Double, Double, Double)?
    private var orientationRunning = false
    /// 사용자가 마지막으로 정한 방향 (도). 서버의 배치는 렌더가 끝나야 따라오므로,
    /// 그 사이에 다시 끌면 옛 방향에서 시작해 튀었다. 앱이 이 값을 기준으로 삼는다.
    private var intendedOrientation: (Double, Double, Double)?
    /// 방향을 새로 정할 때마다 오른다. 끝난 렌더가 이보다 옛것이면 화면에 올리지 않는다.
    private var orientationGen = 0
    /// 지금 돌고 있는 미리보기 작업 — 끌기를 시작하면 취소해 CPU 를 비운다
    private var previewJobID: String?
    private var cancelledRenderForLive = false

    /// 지금 설정으로 나올 출력 크기. 렌더와 무관하게 서버에서 바로 받는다.
    private var layoutInfo: LayoutInfo?
    private var lastPreviewStatus = ""

    private var pendingChanges = false {
        didSet { if pendingChanges != oldValue { updateStatus() } }
    }

    private var sheet: ProgressSheet?

    // MARK: - 창

    convenience init() {
        let w = DropWindow(contentRect: NSRect(x: 0, y: 0, width: 1240, height: 800),
                           styleMask: [.titled, .closable, .miniaturizable, .resizable,
                                       .fullSizeContentView],
                           backing: .buffered, defer: false)
        w.title = "Meridian"
        w.titlebarAppearsTransparent = false
        w.setFrameAutosaveName("MeridianMainWindow")
        w.minSize = NSSize(width: 900, height: 560)
        self.init(window: w)
        build()
    }

    private func build() {
        let side = NSSplitViewItem(sidebarWithViewController: sidebar)
        side.minimumThickness = 208
        side.maximumThickness = 340
        side.canCollapse = true

        let main = NSSplitViewItem(viewController: content)
        main.minimumThickness = 460
        if #available(macOS 11.0, *) {
            // 툴바 아래로 본문을 가로지르는 구분선을 긋지 않는다. 미리보기는
            // 가운데에 그림 한 장만 놓는 화면이라 경계를 그어 줄 것이 없고,
            // 선만 남아 눈에 걸린다.
            main.titlebarSeparatorStyle = .none
        }

        // 속성 칸은 전용 생성자로 만든다. 일반 칸으로 만들면 AppKit 이 이걸
        // 속성 패널로 인식하지 못해, 툴바의 추적 구분자가 엉뚱한 자리에 붙고
        // 진짜 분할 구분선은 툴바를 그대로 뚫고 올라온다 — 선이 둘로 보인다.
        let insp: NSSplitViewItem
        if #available(macOS 14.0, *) {
            insp = NSSplitViewItem(inspectorWithViewController: inspector)
        } else {
            insp = NSSplitViewItem(viewController: inspector)
        }
        insp.minimumThickness = 260
        insp.maximumThickness = 340
        // 속성은 작업 내내 보면서 쓰는 것이라 접지 않는다
        insp.canCollapse = false
        if #available(macOS 11.0, *) {
            // 속성 패널은 스크롤되므로 내용이 툴바 밑으로 들어갈 때만 선이
            // 필요하다. 그 판단은 AppKit 에 맡긴다.
            insp.titlebarSeparatorStyle = .automatic
        }

        split.addSplitViewItem(side)
        split.addSplitViewItem(main)
        split.addSplitViewItem(insp)

        window?.contentViewController = split

        // 드롭은 창이 받는다. 분할 뷰 구조는 건드리지 않는다.
        if let w = window as? DropWindow {
            w.enableDrop()
            w.onDrop = { [weak self] paths in self?.addPaths(paths) }
        }

        Self.healToolbarConfiguration()
        let tb = NSToolbar(identifier: "MeridianToolbar")
        tb.delegate = self
        tb.displayMode = .iconOnly
        tb.allowsUserCustomization = true
        tb.autosavesConfiguration = true
        window?.toolbar = tb
        if #available(macOS 11.0, *) { window?.toolbarStyle = .unified }

        wireCallbacks()
    }

    private func wireCallbacks() {
        sidebar.thumbnailURL = { [weak self] id in
            self?.engine.url("/api/thumb/\(id)") ?? URL(fileURLWithPath: "/")
        }
        sidebar.onToggleEnabled = { [weak self] id, on in
            self?.mutate { try await $0.patch("/api/images/\(id)", ["enabled": on]) as Project }
        }
        sidebar.onSetAnchor = { [weak self] id in
            self?.mutate { try await $0.patch("/api/images/\(id)", ["anchor": true]) as Project }
        }
        sidebar.onRemove = { [weak self] id in self?.removeImage(id) }
        inspector.onProjection = { [weak self] p in
            self?.apply(setting: "projection", value: p.rawValue)
        }
        inspector.onSetting = { [weak self] key, value in
            self?.apply(setting: key, value: value)
        }
        inspector.onPlanet = { [weak self] pitch in self?.renderPlanet(pitch: pitch) }
        inspector.onOrient = { [weak self] yaw, pitch, roll in
            _ = self?.endLive()
            self?.applyOrientation(yaw: yaw, pitch: pitch, roll: roll)
        }
        inspector.onExport = { [weak self] in self?.export() }

        preview.onAlign = { [weak self] in self?.alignPhotos(nil) }
        preview.onOrientDrag = { [weak self] phase, dx, dy, roll in
            self?.orientDrag(phase, dx, dy, roll)
        }
        inspector.onOrientPreview = { [weak self] y, p, r in self?.orientSliderPreview(y, p, r) }
        inspector.onFov = { [weak self] h, v in self?.applyFov(h, v) }
        inspector.onFovPreview = { [weak self] h, v in self?.fovPreview(h, v) }
        inspector.onFovFit = { [weak self] in self?.fitFov() }
        content.editor.engine = engine
        content.editor.onChanged = { [weak self] in
            Task { await self?.refresh() }
        }
        modeControl.selectedSegment = 0
        modeControl.target = self
        modeControl.action = #selector(modeChanged(_:))
        modeControl.segmentStyle = .texturedRounded
    }

    // MARK: - 엔진 시작

    func launch() {
        showWindow(nil)
        // 지난번 크기를 복원하되, 쓸 수 없을 만큼 작으면 기본 크기로 되돌린다
        if let w = window, w.frame.width < 1000 || w.frame.height < 640 {
            w.setContentSize(NSSize(width: 1240, height: 800))
        }
        window?.center()
        Task {
            beginSheet(title: "엔진 시작 중", message: "잠시만 기다려 주세요…")
            do {
                try await engine.start()
                endSheet()
                await refresh()
            } catch {
                endSheet()
                report(error)
            }
        }
    }

    func shutdown() { engine.stop() }

    /// 진단용 — 앱이 실제로 쓰는 추가 경로를 그대로 태운다.
    /// `MERIDIAN_TEST_ADD` 에 콜론으로 이어 붙인 경로를 주면 파일 선택창 없이 넣는다.
    func addPathsForTesting(_ paths: [String]) { addPaths(paths) }

    /// 화면을 눈으로 볼 수 없을 때 배치를 확인하는 통로.
    /// `MERIDIAN_DUMP_LAYOUT=1` 로 실행하면 각 칸이 실제로 어떤 크기로 잡혔는지 찍는다.
    /// 창이 멀쩡히 떠 있는데도 내용이 비는 종류의 문제는 이걸 봐야 알 수 있다.
    func dumpLayout() {
        var out = "[layout] window=\(window?.frame.size ?? .zero)\n"
        for (i, item) in split.splitViewItems.enumerated() {
            let v = item.viewController.view
            out += "  pane[\(i)] \(type(of: item.viewController)) size=\(v.frame.size)"
            out += " collapsed=\(item.isCollapsed)\n"
        }
        // 속성 패널 안쪽까지 — 내용이 위에서부터 쌓였는지 본다
        if let scroll = inspector.view as? NSScrollView, let doc = scroll.documentView {
            out += "  inspector doc=\(doc.frame) clipFlipped=\(scroll.contentView.isFlipped)\n"
            out += "  inspector insets=\(scroll.contentInsets) safeTop=\(scroll.safeAreaInsets.top)\n"
            if let first = (doc as? NSStackView)?.arrangedSubviews.first {
                out += "  inspector 첫 항목 y=\(first.frame.minY) (문서 맨 위에서)\n"
            }
        }
        out += "  inspector collapsible=\(split.splitViewItems.last?.canCollapse ?? true)\n"
        out += "  sidebar safeTop=\(sidebar.safeTopInset())\n"
        content.show(.controlPoints)
        out += "  editor: \(content.editor.dumpState())\n"
        out += "  project: 사진 \(project.images.count)장 정렬됨=\(project.optimized) "
        out += "제어점 \(project.cpStats.total)개\n"
        // 제어점 화면을 눈으로 확인하려면 MERIDIAN_START_MODE=cp 로 띄운다
        if ProcessInfo.processInfo.environment["MERIDIAN_START_MODE"] != "cp" {
            content.show(.preview)
        } else {
            modeControl.selectedSegment = 1
        }
        FileHandle.standardError.write(out.data(using: .utf8)!)
    }

    // MARK: - 상태 갱신

    private func refresh() async {
        do {
            project = try await engine.get("/api/project")
            // 크기 계산은 화소를 건드리지 않아 순식간이다. 매번 같이 읽어 두면
            // 설정을 바꾼 직후에도 속성 패널의 숫자가 항상 맞는다.
            layoutInfo = LayoutInfo(await engine.raw("/api/layout"))
            sidebar.update(images: project.orderedImages, anchor: project.anchor)
            content.editor.isAligned = project.optimized
            content.editor.update(images: project.orderedImages)
            inspector.update(project: project, preview: previewResult, layout: layoutInfo)
            restoreControlValues()
            updateStatus()
            if project.images.isEmpty {
                preview.apply(.empty)
                previewResult = nil
            } else if !project.optimized {
                preview.apply(.needsAlign(count: project.images.count))
            }
        } catch {
            report(error)
        }
    }

    private func updateStatus() {
        let n = project.images.count
        guard n > 0 else { return setStatus("") }

        var parts = ["사진 \(n)장"]
        // 정렬하기 전의 제어점 수나 '끊김' 경고는 뜻이 없다. 정렬한 뒤에만 알린다.
        if project.optimized {
            parts.append("제어점 \(project.cpStats.enabled)")
            parts.append(String(format: "RMS %.2fpx", project.lastRMS))
            let groups = project.cpStats.groups
            if groups.count > 1 { parts.append("\(groups.count)조각으로 끊김") }
        }
        let dirty = pendingChanges && project.optimized
        if dirty {
            parts.append("변경사항 있음 — ⌘Y로 미리보기 갱신")
        }
        // 툴바 버튼도 같이 바뀐다. 제목 줄 한 줄보다 이쪽이 먼저 눈에 띈다.
        refreshItem?.label = dirty ? "갱신 필요" : "미리보기 갱신"
        refreshItem?.image = UI.symbol(dirty ? "arrow.clockwise.circle.fill"
                                             : "arrow.clockwise", "미리보기 갱신")
        setStatus(parts.joined(separator: " · "))
    }

    /// 창 제목 아래 한 줄. macOS 11 아래에서는 제목에 붙인다.
    private func setStatus(_ text: String) {
        if #available(macOS 11.0, *) {
            window?.subtitle = text
        } else {
            window?.title = text.isEmpty ? "Meridian" : "Meridian — \(text)"
        }
    }

    private func mutate(_ work: @escaping (Engine) async throws -> Project?) {
        Task {
            do { _ = try await work(engine); await refresh(); pendingChanges = true }
            catch { report(error) }
        }
    }

    private func removeImage(_ id: Int) {
        Task {
            do { try await engine.delete("/api/images/\(id)"); await refresh(); pendingChanges = true }
            catch { report(error) }
        }
    }

    /// 미리보기 그림 자체는 그대로인 설정들 — 굳이 다시 그리게 하지 않는다
    private static let exportOnlySettings: Set<String> =
        ["out_width", "scale_percent", "format", "quality", "gpano", "interpolation"]

    private func apply(setting key: String, value: Any) {
        Task {
            do {
                let _: Settings = try await engine.patch("/api/settings", [key: value])
                await refresh()
                if !Self.exportOnlySettings.contains(key) {
                    pendingChanges = true      // 그리는 것은 사용자가 정한다
                }
            } catch { report(error) }
        }
    }

    // MARK: - 동작

    @objc func addPhotos(_ sender: Any?) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = true
        panel.prompt = "추가"
        panel.message = "파노라마로 이을 사진을 고르세요"
        if #available(macOS 11.0, *) {
            var types: [UTType] = [.jpeg, .png, .tiff]
            for ext in ["arw", "cr2", "cr3", "nef", "dng", "raf", "orf", "rw2", "pef", "srw"] {
                if let t = UTType(filenameExtension: ext) { types.append(t) }
            }
            panel.allowedContentTypes = types
        }
        guard panel.runModal() == .OK, !panel.urls.isEmpty else { return }

        var paths: [String] = []
        for url in panel.urls {
            var isDir: ObjCBool = false
            FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir)
            if isDir.boolValue {
                let items = (try? FileManager.default.contentsOfDirectory(
                    at: url, includingPropertiesForKeys: nil)) ?? []
                paths += items.map(\.path)
            } else {
                paths.append(url.path)
            }
        }
        addPaths(paths)
    }

    /// 파일 선택창과 드래그&드롭이 함께 쓰는 경로.
    private func addPaths(_ paths: [String]) {
        guard !paths.isEmpty else { return }
        runJob(title: "사진 불러오는 중", path: "/api/images/add", body: ["paths": paths]) { result in
            let added = (result["added"] as? [[String: Any]])?.count ?? 0
            if added == 0 {
                self.report(message: "추가된 사진이 없습니다. 이미 들어 있거나 읽을 수 없는 형식입니다.")
            }
            self.schedulePreview()
        }
    }

    @objc func alignPhotos(_ sender: Any?) {
        guard project.images.count >= 2 else {
            return report(message: "사진이 두 장 이상 필요합니다")
        }
        runJob(title: "자동 정렬", path: "/api/align",
               body: ["detect": true, "mode": "full", "straighten": true]) { result in
            if let groups = result["groups"] as? [Int], groups.count > 1 {
                self.report(message: "정렬했지만 \(groups.count)조각으로 끊겼습니다. "
                            + "겹치는 사진이 충분한지 확인해 주세요.")
            }
            self.intendedOrientation = nil
            self.orientationGen += 1
            self.schedulePreview(after: 0)
        }
    }

    @objc func optimize(_ sender: Any?) {
        runJob(title: "최적화", path: "/api/optimize", body: ["mode": "full"]) { _ in
            self.schedulePreview()
        }
    }

    @objc func straighten(_ sender: Any?) {
        Task {
            do {
                _ = try await engine.postRaw("/api/straighten")
                await refresh(); schedulePreview()
            } catch { report(error) }
        }
    }

    @objc func toggleSidebar(_ sender: Any?) {
        split.splitViewItems.first?.animator().isCollapsed.toggle()
    }

    @objc private func modeChanged(_ sender: NSSegmentedControl) {
        content.show(sender.selectedSegment == 0 ? .preview : .controlPoints)
    }

    @objc func showPreviewMode(_ sender: Any?) {
        modeControl.selectedSegment = 0
        content.show(.preview)
    }

    @objc func showControlPointMode(_ sender: Any?) {
        guard project.images.count >= 2 else {
            return report(message: "사진이 두 장 이상 있어야 제어점을 볼 수 있습니다")
        }
        modeControl.selectedSegment = 1
        content.show(.controlPoints)
    }

    @objc func refreshPreviewNow(_ sender: Any?) {
        guard project.optimized else {
            return report(message: "먼저 자동 정렬을 해주세요")
        }
        Task { await renderPreview() }
    }

    @objc func zoomFit(_ sender: Any?) { preview.zoomToFit() }
    @objc func zoomActual(_ sender: Any?) { preview.zoomToActual() }
    @objc func zoomIn(_ sender: Any?) { preview.zoom(by: 1.3) }
    @objc func zoomOut(_ sender: Any?) { preview.zoom(by: 1 / 1.3) }

    private func runJob(title: String, path: String, body: Any,
                        cancellable: Bool = false,
                        done: @escaping ([String: Any]) -> Void) {
        guard !busy else { return }
        busy = true
        beginSheet(title: title, message: "…", cancellable: cancellable)
        Task {
            do {
                let result = try await engine.run(path, body, onStart: { jid in
                    self.sheet?.onCancel = {
                        Task { await self.engine.cancel(job: jid) }
                    }
                }) { frac, msg in
                    self.sheet?.update(progress: frac, message: msg)
                }
                endSheet(); busy = false
                await refresh()
                done(result)
            } catch Engine.EngineError.cancelled {
                // 사용자가 스스로 끊은 것이니 오류 창을 띄우지 않는다
                endSheet(); busy = false
                await refresh()
                setStatus("내보내기를 취소했습니다")
            } catch {
                endSheet(); busy = false
                report(error)
            }
        }
    }

    // MARK: - 미리보기

    private func schedulePreview(after seconds: Double = 0.25) {
        previewTask?.cancel()
        previewTask = Task {
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            guard !Task.isCancelled else { return }
            await renderPreview()
        }
    }

    private func renderPreview(center: (Double, Double, Double)? = nil,
                               centerPitch: Double? = nil) async {
        guard !project.images.isEmpty, project.optimized, !busy else { return }
        busy = true
        // 29장짜리 미리보기는 십수 초가 걸린다. 그동안 제목 줄만 바뀌고 가운데는
        // 정렬 안내 화면 그대로면, 정렬이 실패한 것으로 보인다. 진행 상황을
        // 화면 한가운데에 내놓는다.
        setStatus("미리보기 만드는 중…")
        preview.beginRender()
        let gen = orientationGen
        defer { busy = false; previewJobID = nil; updateStatus() }
        var body: [String: Any] = ["max_dim": 1700]
        if let c = center {
            body["center_yaw"] = c.0; body["center_pitch"] = c.1; body["center_roll"] = c.2
        } else if let p = centerPitch {
            body["center_pitch"] = p; body["center_yaw"] = 0; body["center_roll"] = 0
        }
        do {
            let result = try await engine.run("/api/preview", body, onStart: { jid in
                self.previewJobID = jid
            }) { frac, msg in
                self.sheet?.update(progress: frac, message: msg)
                if self.liveActive { return }        // 끄는 중에는 진행 표시로 덮지 않는다
                self.preview.renderProgress(frac, msg)
                self.setStatus(msg.isEmpty ? "미리보기 만드는 중…"
                                           : "\(msg)  \(Int(frac * 100))%")
            }
            guard let r = PreviewResult(result) else { return }
            previewResult = r
            // 그사이 방향이 또 바뀌었거나 끄는 중이면 이 그림은 이미 옛것이다.
            // 올리면 새 방향으로 가 있던 화면이 잠깐 뒤로 튄다.
            let stale = gen != orientationGen || liveActive
            if !stale { pendingChanges = false }
            let url = engine.url(r.url)
            if !stale, let data = try? Data(contentsOf: url), let img = NSImage(data: data) {
                preview.show(image: img,
                             status: "출력 \(r.fullSize.0)×\(r.fullSize.1) · "
                                   + String(format: "%.1f MP", r.megapixels))
                lastPreviewStatus = "출력 \(r.fullSize.0)×\(r.fullSize.1) · "
                                  + String(format: "%.1f MP", r.megapixels)
            }
            // 방향을 바꿨으면 서버 쪽 배치가 달라졌다. 최신 상태를 다시 읽지
            // 않으면 속성 패널이 옛 값으로 되돌아가, 적용이 안 된 것처럼 보인다.
            await refresh()
            // 끌기용 원판을 뒤에서 미리 만들어 둔다. 정렬이나 보정 설정이 그대로면
            // 서버가 있는 걸 그대로 쓰므로 방향만 바꿀 때는 비용이 없다.
            Task { _ = try? await engine.postRaw("/api/live/prepare") }
        } catch Engine.EngineError.cancelled {
            // 더 새로운 방향이 오고 있어서 스스로 끊은 것 — 알릴 일이 아니다
        } catch {
            if previewResult == nil {
                preview.apply(.needsAlign(count: project.images.count))
            }
            report(error)
        }
    }

    /// 파노라마 전체를 주어진 각도로 돌린다.
    private func applyOrientation(yaw: Double, pitch: Double, roll: Double) {
        // 방향은 눌러서 보는 것이라 바로 그린다. 앞선 렌더가 돌고 있으면
        // renderPreview 가 조용히 돌아가 버리므로, 마지막 값만 기억했다가 이어서 그린다.
        intendedOrientation = (yaw, pitch, roll)
        orientationGen += 1
        queuedOrientation = (yaw, pitch, roll)
        // 돌고 있는 렌더는 어차피 버릴 그림이니 끊는다
        cancelPreviewJob()
        guard !orientationRunning else { return }
        orientationRunning = true
        Task {
            while let q = queuedOrientation {
                queuedOrientation = nil
                while busy { try? await Task.sleep(nanoseconds: 120_000_000) }
                await renderPreview(center: q)
            }
            orientationRunning = false
        }
    }

    // MARK: - 끌어서 방향 돌리기 · 화각

    /// 끄는 동안은 서버에 미리 만들어 둔 전방위 원판을 새 방향·화각으로 펼쳐 받아
    /// 곧바로 보여 주고, 손을 떼면 정식 미리보기를 그린다.
    ///
    /// 방향 규칙은 '잡아 끄는' 느낌에 맞췄다 (서버에서 재 보고 정했다).
    /// 좌우 +면 그림이 왼쪽으로, 상하 +면 아래로, 기울기 +면 반시계로 돈다.
    private func orientDrag(_ phase: OrientDragPhase, _ dx: Double, _ dy: Double,
                            _ droll: Double) {
        guard project.optimized, let r = previewResult else { return }
        switch phase {
        case .began:
            dragBase = currentOrientation()
            beginLive()
        case .changed:
            guard let b = dragBase else { return }
            // 보이는 폭 전체가 몇 도인지 (그림 가운데의 화소당 각도 기준)
            let span = r.degPerPx * Double(r.width)
            let yaw = wrap180(b.0 - dx * span)
            let pitch = min(90, max(-90, b.1 + dy * span))
            let roll = wrap180(b.2 + droll)
            inspector.showOrientation(yaw: yaw, pitch: pitch, roll: roll)
            let f = currentFov()
            showLive((yaw, pitch, roll, f.0, f.1))
        case .ended:
            dragBase = nil
            guard let t = endLive() else {
                // 눌렀다 떼기만 했는데 돌던 렌더를 끊었다면 다시 그린다
                if cancelledRenderForLive {
                    let o = currentOrientation()
                    applyOrientation(yaw: o.0, pitch: o.1, roll: o.2)
                }
                return
            }
            applyOrientation(yaw: t.0, pitch: t.1, roll: t.2)
        }
    }

    private func currentOrientation() -> (Double, Double, Double) {
        if let o = intendedOrientation { return o }
        let deg = 180.0 / Double.pi
        let l = project.layout
        return ((l?.centerYaw ?? 0) * deg, (l?.centerPitch ?? 0) * deg, (l?.centerRoll ?? 0) * deg)
    }

    private func currentFov() -> (Double, Double) {
        (project.settings.hfov, project.settings.vfov)
    }

    private func wrap180(_ a: Double) -> Double {
        (a + 540).truncatingRemainder(dividingBy: 360) - 180
    }

    private func beginLive() {
        guard !liveActive else { return }
        liveActive = true
        // 돌던 정식 렌더는 손을 떼면 어차피 새로 그린다. 끊어야 끄는 동안 빠르다.
        cancelledRenderForLive = previewJobID != nil
        cancelPreviewJob()
        Task { _ = try? await engine.postRaw("/api/live/prepare") }
    }

    /// 조작을 끝내고 마지막으로 보여 준 값을 돌려준다.
    private func endLive() -> (Double, Double, Double, Double, Double)? {
        liveActive = false
        defer { liveTarget = nil }
        return liveTarget
    }

    private func showLive(_ target: (Double, Double, Double, Double, Double)) {
        guard project.optimized, previewResult != nil else { return }
        beginLive()
        liveTarget = target
        pumpLive()
    }

    /// 방향 슬라이더를 끄는 동안
    private func orientSliderPreview(_ yaw: Double, _ pitch: Double, _ roll: Double) {
        let f = currentFov()
        showLive((yaw, pitch, roll, f.0, f.1))
    }

    /// 화각 슬라이더를 끄는 동안
    private func fovPreview(_ h: Double, _ v: Double) {
        let o = currentOrientation()
        showLive((o.0, o.1, o.2, h, v))
    }

    /// 화각 적용. 방향과 같이 바로 그린다.
    private func applyFov(_ h: Double, _ v: Double) {
        _ = endLive()
        inspector.showFov(h: h, v: v)
        Task {
            do {
                let _: Settings = try await engine.patch("/api/settings", ["hfov": h, "vfov": v])
                await refresh()
                let o = currentOrientation()
                applyOrientation(yaw: o.0, pitch: o.1, roll: o.2)
            } catch { report(error) }
        }
    }

    /// 화각을 비워 두면 다음 미리보기가 지금 방향에서 사진이 다 들어가게 맞춘다.
    private func fitFov() {
        guard project.optimized else { return report(message: "먼저 자동 정렬을 해주세요") }
        Task {
            do {
                let _: Settings = try await engine.patch("/api/settings", ["hfov": 0, "vfov": 0])
                await refresh()
                let o = currentOrientation()
                applyOrientation(yaw: o.0, pitch: o.1, roll: o.2)
            } catch { report(error) }
        }
    }

    /// 한 번에 요청 하나만 보낸다. 응답이 오면 그사이 움직인 마지막 값으로 다시 보낸다.
    /// 요청을 쌓으면 손은 멈췄는데 그림이 한참 뒤따라오게 된다.
    private func pumpLive() {
        guard !liveInFlight, liveActive, let t = liveTarget else { return }
        liveInFlight = true
        Task {
            var shown = false
            if let data = try? await engine.postData(
                    "/api/live/frame",
                    ["yaw": t.0, "pitch": t.1, "roll": t.2, "hfov": t.3, "vfov": t.4]),
               let img = NSImage(data: data), liveActive {
                preview.showLive(img)
                shown = true
            }
            if !shown && liveActive {
                setStatus("움직임 미리보기 준비 중… 손을 떼면 그 값으로 그립니다")
            }
            liveInFlight = false
            if liveActive, let now = liveTarget, now != t {
                // 원판이 아직 없으면 너무 자주 두드리지 않는다
                if !shown { try? await Task.sleep(nanoseconds: 300_000_000) }
                pumpLive()
            }
        }
    }

    private func renderPlanet(pitch: Double) {
        Task {
            do {
                let _: Settings = try await engine.patch("/api/settings",
                                                         ["projection": "stereographic"])
                await refresh()
                applyOrientation(yaw: 0, pitch: pitch, roll: 0)
            } catch { report(error) }
        }
    }

    private func cancelPreviewJob() {
        guard let jid = previewJobID else { return }
        previewJobID = nil
        Task { await engine.cancel(job: jid) }
    }

    /// 새로고침이 속성 패널을 서버 값(아직 옛 방향일 수 있는)으로 덮은 뒤 되돌린다
    private func restoreControlValues() {
        if liveActive, let t = liveTarget {
            inspector.showOrientation(yaw: t.0, pitch: t.1, roll: t.2)
            if t.3 > 0, t.4 > 0 { inspector.showFov(h: t.3, v: t.4) }
        } else if let o = intendedOrientation {
            inspector.showOrientation(yaw: o.0, pitch: o.1, roll: o.2)
        }
    }

    // MARK: - 내보내기

    @objc func exportFromMenu(_ sender: Any?) { export() }

    private func export() {
        guard project.optimized else { return report(message: "먼저 자동 정렬을 해주세요") }
        let panel = NSSavePanel()
        panel.prompt = "내보내기"
        panel.nameFieldStringValue = "\(project.name).\(project.settings.format)"
        if let l = layoutInfo {
            panel.message = String(format: "%d × %d 픽셀 · %.1f MP 로 내보냅니다",
                                   l.fullSize.0, l.fullSize.1, l.megapixels)
        }
        guard panel.runModal() == .OK, let url = panel.url else { return }
        runJob(title: "내보내는 중", path: "/api/render", body: ["path": url.path],
               cancellable: true) { result in
            if let p = result["path"] as? String {
                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: p)])
            }
        }
    }

    // MARK: - 시트와 알림

    private func beginSheet(title: String, message: String,
                           cancellable: Bool = false) {
        guard let window else { return }
        let s = ProgressSheet(title: title, message: message, cancellable: cancellable)
        sheet = s
        window.beginSheet(s.window)
    }

    private func endSheet() {
        if let s = sheet { window?.endSheet(s.window) }
        sheet = nil
    }

    private func report(_ error: Error) {
        report(message: error.localizedDescription)
    }

    private func report(message: String) {
        guard let window else { return }
        let a = NSAlert()
        a.alertStyle = .warning
        a.messageText = "문제가 생겼습니다"
        a.informativeText = message
        a.addButton(withTitle: "확인")
        a.beginSheetModal(for: window)
    }

    // MARK: - 툴바

    /// 변경사항이 쌓이면 이 버튼 모양을 바꿔 눌러야 한다는 걸 알린다
    private weak var refreshItem: NSToolbarItem?

    /// 툴바에 항목을 새로 넣었을 때 그게 실제로 보이게 한다.
    ///
    /// autosavesConfiguration 이 켜져 있으면 macOS 는 사용자가 마지막으로 본
    /// 항목 목록을 저장해 두고, 다음 실행부터 코드의 기본 목록보다 그걸
    /// 우선한다. 그래서 새 버튼을 추가해도 기존 사용자에게는 영영 안 뜬다
    /// (조용히 빠지기만 해서 알아채기도 어렵다). 구성이 바뀐 걸 확인하면
    /// 저장본을 버려 기본값으로 되돌린다 — 그대로면 사용자 배치는 지킨다.
    private static func healToolbarConfiguration() {
        let stamp = "add,align,optimize,straighten,refresh,mode,zoom,export/insp"
        let d = UserDefaults.standard
        guard d.string(forKey: "MeridianToolbarItemSet") != stamp else { return }
        d.removeObject(forKey: "NSToolbar Configuration MeridianToolbar")
        d.set(stamp, forKey: "MeridianToolbarItemSet")
    }

    private enum Item {
        static let add = NSToolbarItem.Identifier("add")
        static let align = NSToolbarItem.Identifier("align")
        static let optimize = NSToolbarItem.Identifier("optimize")
        static let straighten = NSToolbarItem.Identifier("straighten")
        static let zoom = NSToolbarItem.Identifier("zoom")
        static let mode = NSToolbarItem.Identifier("mode")
        static let refresh = NSToolbarItem.Identifier("refresh")
        static let export = NSToolbarItem.Identifier("export")
    }

    func toolbarDefaultItemIdentifiers(_ t: NSToolbar) -> [NSToolbarItem.Identifier] {
        var ids: [NSToolbarItem.Identifier] = [.toggleSidebar]
        // 사이드바 경계에 맞춰 툴바를 나눠 주는 구분자는 macOS 11 부터 있다
        if #available(macOS 11.0, *) { ids.append(.sidebarTrackingSeparator) }

        // 빈칸(flexibleSpace)은 넣지 않는다. 이 구분자가 있으면 뒤쪽 칸의
        // 항목이 무조건 오른쪽으로 정렬되고 빈칸은 늘어나지 않는다 — 넣는
        // 자리를 바꿔 가며 직접 재 보고 확인했다. 제목은 왼쪽, 조작은 오른쪽이
        // 지금 macOS 의 관례이므로 그대로 따르고, 묶음 사이만 띄운다.
        //
        // 속성 패널 경계에 맞추는 추적 구분자(inspectorTrackingSeparator)도
        // 쓰지 않는다. 경계보다 100pt 쯤 왼쪽에 자리를 잡는 바람에, 그 오른쪽이
        // 아무것도 놓을 수 없는 죽은 칸으로 남았다.
        ids += [Item.add, Item.align, Item.optimize, Item.straighten, Item.refresh,
                .space, Item.mode, .space, Item.zoom]
        if #available(macOS 14.0, *) { ids.append(.inspectorTrackingSeparator) }
        ids.append(Item.export)
        return ids
    }

    func toolbarAllowedItemIdentifiers(_ t: NSToolbar) -> [NSToolbarItem.Identifier] {
        toolbarDefaultItemIdentifiers(t) + [.flexibleSpace, .space]
    }

    func toolbar(_ toolbar: NSToolbar, itemForItemIdentifier id: NSToolbarItem.Identifier,
                 willBeInsertedIntoToolbar flag: Bool) -> NSToolbarItem? {
        if #available(macOS 14.0, *), id == .inspectorTrackingSeparator {
            return NSTrackingSeparatorToolbarItem(
                identifier: id, splitView: split.splitView, dividerIndex: 1)
        }
        func button(_ symbol: String, _ label: String, _ tip: String,
                    _ action: Selector) -> NSToolbarItem {
            let item = NSToolbarItem(itemIdentifier: id)
            item.label = label
            item.paletteLabel = label
            item.toolTip = tip
            item.image = UI.symbol(symbol, label)
            item.target = self
            item.action = action
            item.isBordered = true
            return item
        }

        switch id {
        case Item.add:
            return button("plus", "사진 추가", "사진이나 폴더를 추가합니다 (⌘O)",
                          #selector(addPhotos(_:)))
        case Item.align:
            return button("wand.and.stars", "자동 정렬",
                          "특징점을 찾아 카메라와 렌즈를 한꺼번에 풉니다 (⌘R)",
                          #selector(alignPhotos(_:)))
        case Item.optimize:
            return button("slider.horizontal.3", "최적화",
                          "지금 제어점으로 다시 맞춥니다", #selector(optimize(_:)))
        case Item.straighten:
            return button("level", "수평", "지평선을 수평으로 맞춥니다",
                          #selector(straighten(_:)))
        case Item.refresh:
            let item = button("arrow.clockwise", "미리보기 갱신",
                              "바꾼 설정을 미리보기에 반영합니다 (⌘Y)",
                              #selector(refreshPreviewNow(_:)))
            refreshItem = item
            return item
        case Item.export:
            let item = button("square.and.arrow.up", "내보내기",
                              "파노라마를 파일로 씁니다 (⌘E)",
                              #selector(exportFromMenu(_:)))
            return item
        case Item.zoom:
            let seg = NSSegmentedControl(labels: ["−", "맞춤", "+"],
                                         trackingMode: .momentary,
                                         target: self, action: #selector(zoomSegment(_:)))
            seg.segmentStyle = .texturedRounded
            let item = NSToolbarItem(itemIdentifier: id)
            item.label = "확대"
            item.view = seg
            return item
        case Item.mode:
            let item = NSToolbarItem(itemIdentifier: id)
            item.label = "보기 모드"
            item.toolTip = "미리보기와 제어점 편집을 오갑니다"
            item.view = modeControl
            return item
        default:
            return nil
        }
    }

    @objc private func zoomSegment(_ sender: NSSegmentedControl) {
        switch sender.selectedSegment {
        case 0: preview.zoom(by: 1 / 1.3)
        case 1: preview.zoomToFit()
        default: preview.zoom(by: 1.3)
        }
    }
}

/// 오래 걸리는 작업을 알리는 시트.
@MainActor
final class ProgressSheet {
    let window: NSWindow
    private let bar = NSProgressIndicator()
    private let message = NSTextField(labelWithString: "")
    private let cancelButton = NSButton(title: "취소", target: nil, action: nil)
    /// 값이 있으면 시트에 취소 버튼이 붙는다
    var onCancel: (() -> Void)?

    init(title: String, message msg: String, cancellable: Bool = false) {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 360, height: 118),
                          styleMask: [.titled], backing: .buffered, defer: false)
        let titleLabel = NSTextField(labelWithString: title)
        titleLabel.font = .systemFont(ofSize: 13, weight: .semibold)

        message.stringValue = msg
        message.font = .systemFont(ofSize: 11)
        message.textColor = .secondaryLabelColor
        message.lineBreakMode = .byTruncatingTail

        bar.isIndeterminate = false
        bar.minValue = 0
        bar.maxValue = 1
        bar.style = .bar

        cancelButton.bezelStyle = .rounded
        cancelButton.target = self
        cancelButton.action = #selector(cancelTapped)
        cancelButton.keyEquivalent = "\u{1b}"          // Esc
        cancelButton.isHidden = !cancellable

        // 버튼은 오른쪽 끝에 붙인다 — 맥에서 취소는 늘 그 자리에 있다
        let buttonRow = NSStackView(views: [NSView(), cancelButton])
        buttonRow.orientation = .horizontal
        buttonRow.distribution = .fill

        let stack = NSStackView(views: cancellable
                                ? [titleLabel, message, bar, buttonRow]
                                : [titleLabel, message, bar])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.edgeInsets = NSEdgeInsets(top: 20, left: 22, bottom: 20, right: 22)
        stack.translatesAutoresizingMaskIntoConstraints = false

        let content = NSView()
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: content.topAnchor),
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            bar.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -44),
        ])
        if cancellable {
            content.addConstraint(
                buttonRow.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -44))
        }
        window.contentView = content
    }

    @objc private func cancelTapped() {
        cancelButton.isEnabled = false
        cancelButton.title = "중단하는 중…"
        onCancel?()
    }

    func update(progress: Double, message msg: String) {
        bar.doubleValue = progress
        message.stringValue = msg
    }
}
