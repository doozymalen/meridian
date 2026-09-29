import AppKit

/// 오른쪽 속성 패널.
///
/// 항목은 '레이블 : 컨트롤' 두 열로 맞춘다. 맥 앱의 설정 패널이 대부분 이 모양이라
/// 눈이 세로 한 줄을 따라 내려가며 읽을 수 있다.
@MainActor
final class InspectorViewController: NSViewController {

    var onProjection: ((Projection) -> Void)?
    var onPlanet: ((Double) -> Void)?          // 중심 상하각(도)
    var onOrient: ((Double, Double, Double) -> Void)?   // yaw, pitch, roll (도)
    /// 방향 슬라이더를 끄는 동안 — 적용하지 않고 빠른 미리보기만
    var onOrientPreview: ((Double, Double, Double) -> Void)?
    var onFov: ((Double, Double) -> Void)?              // 가로, 세로 화각 (도)
    var onFovPreview: ((Double, Double) -> Void)?
    var onFovFit: (() -> Void)?
    var onSetting: ((String, Any) -> Void)?
    var onExport: (() -> Void)?

    private let stack = NSStackView()
    private let projectionPopup = NSPopUpButton()
    private let yawField = NSTextField()
    private let pitchField = NSTextField()
    private let rollField = NSTextField()
    private let hfovField = NSTextField()
    private let vfovField = NSTextField()
    private let hfovSlider = NSSlider()
    private let vfovSlider = NSSlider()
    private let yawSlider = NSSlider()
    private let pitchSlider = NSSlider()
    private let rollSlider = NSSlider()
    private let seamPopup = NSPopUpButton()
    private let blendPopup = NSPopUpButton()
    private let exposurePopup = NSPopUpButton()
    private let wbCheck = NSButton(checkboxWithTitle: "화이트밸런스까지 맞추기", target: nil, action: nil)
    private let vigCheck = NSButton(checkboxWithTitle: "비네팅 보정", target: nil, action: nil)
    private let patchNadirCheck = NSButton(checkboxWithTitle: "천정/삼각대 자국 자동 복구", target: nil, action: nil)
    private let fillGapsCheck = NSButton(checkboxWithTitle: "빈 곳 채우기", target: nil, action: nil)
    private let lowFreqSlider = NSSlider()
    private let lowFreqValue = NSTextField(labelWithString: "100%")
    private let lensBox = NSTextField(wrappingLabelWithString: "")
    private let widthField = NSTextField()
    private let heightField = NSTextField()
    private let formatPopup = NSPopUpButton()
    private let qualitySlider = NSSlider()
    private let qualityValue = NSTextField(labelWithString: "94")
    private let sizeSummary = NSTextField(labelWithString: "—")

    private var aspect: Double = 2.0
    private var suppress = false
    private var sections: [NSView] = []

    override func loadView() {
        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        // 스크롤 뷰의 기본 좌표계는 아래에서 위로 간다. 그대로 두면 내용이
        // 패널 아래쪽에 붙고 위에 큰 빈칸이 남는다. 뒤집어야 위에서부터 쌓인다.
        scroll.contentView = FlippedClipView()

        stack.orientation = .vertical
        stack.alignment = .leading
        stack.distribution = .fill
        stack.spacing = 16
        // 위쪽은 툴바 아래 안전 영역이 이미 자리를 잡아 준다. 여기서 또 띄우면
        // 첫 항목이 한참 아래로 밀려 패널 윗부분이 비어 보인다.
        stack.edgeInsets = NSEdgeInsets(top: 2, left: 14, bottom: 18, right: 14)
        stack.translatesAutoresizingMaskIntoConstraints = false
        stack.setHuggingPriority(.defaultHigh, for: .vertical)

        buildProjection()
        buildBlending()
        buildLens()
        buildOutput()

        // 스택을 바로 documentView 로 둔다. 빈 NSView 를 끼워 넣으면 그 뷰의
        // 높이가 정해지지 않아 스크롤 영역이 0 이 되고, 패널이 통째로 빈다.
        scroll.documentView = stack
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: scroll.contentView.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: scroll.contentView.trailingAnchor),
            stack.topAnchor.constraint(equalTo: scroll.contentView.topAnchor),
        ])
        view = scroll
    }

    // MARK: - 구성 요소

    private func header(_ text: String) -> NSView {
        let l = NSTextField(labelWithString: text)
        l.font = .systemFont(ofSize: 11, weight: .semibold)
        l.textColor = .secondaryLabelColor
        return l
    }

    private func row(_ label: String, _ control: NSView) -> NSView {
        let l = NSTextField(labelWithString: label)
        l.font = .systemFont(ofSize: 11)
        l.textColor = .secondaryLabelColor
        l.alignment = .right
        l.setContentHuggingPriority(.required, for: .horizontal)
        l.setContentCompressionResistancePriority(.required, for: .horizontal)
        l.widthAnchor.constraint(equalToConstant: 68).isActive = true

        control.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let h = NSStackView(views: [l, control])
        h.orientation = .horizontal
        h.alignment = .centerY
        h.spacing = 8
        h.distribution = .fill
        return h
    }

    /// 섹션 하나. 제목 아래에 항목들을 쌓고, 가로는 패널 폭을 꽉 채운다.
    private func section(_ title: String, _ rows: [NSView]) -> NSView {
        let v = NSStackView(views: [header(title)] + rows)
        v.orientation = .vertical
        v.alignment = .leading
        v.spacing = 8
        v.distribution = .fill
        v.translatesAutoresizingMaskIntoConstraints = false
        sections.append(v)
        for r in rows where r is NSStackView {
            r.translatesAutoresizingMaskIntoConstraints = false
            r.widthAnchor.constraint(equalTo: v.widthAnchor).isActive = true
        }
        return v
    }

    private func buildProjection() {
        projectionPopup.addItems(withTitles: Projection.allCases.map(\.title))
        projectionPopup.target = self
        projectionPopup.action = #selector(projectionChanged)

        let planet = NSButton(title: "리틀 플래닛", target: self, action: #selector(littlePlanet))
        let tunnel = NSButton(title: "터널", target: self, action: #selector(tunnel))
        [planet, tunnel].forEach { $0.bezelStyle = .rounded; $0.controlSize = .small }
        let presets = NSStackView(views: [planet, tunnel])
        presets.spacing = 6

        addSection(section("투영", [
            row("방식", projectionPopup),
            row("프리셋", presets),
        ]))
        buildOrientation()
        buildFov()
    }

    /// 파노라마 전체를 각도로 돌린다.
    ///
    /// 미리보기를 끌어서 맞추는 것보다 숫자로 넣는 편이 정확하고, 같은 값을
    /// 다시 쓰기도 좋다. 수평이 틀어졌을 때 roll 로 바로잡는 쓰임이 많다.
    private func buildOrientation() {
        for f in [yawField, pitchField, rollField] {
            f.font = UI.monoFont(12)
            f.alignment = .right
            f.placeholderString = "0"
            f.target = self
            f.action = #selector(fieldEdited)
            f.widthAnchor.constraint(equalToConstant: 54).isActive = true
        }
        for (slider, range) in [(yawSlider, 180.0), (pitchSlider, 90.0), (rollSlider, 180.0)] {
            slider.minValue = -range
            slider.maxValue = range
            slider.doubleValue = 0
            slider.target = self
            slider.action = #selector(sliderMoved(_:))
            // 끄는 동안에는 숫자만 따라가고, 손을 떼면 그때 그린다.
            // 매번 다시 그리면 사진이 많을 때 한참 기다려야 한다.
            slider.isContinuous = true
            slider.widthAnchor.constraint(greaterThanOrEqualToConstant: 96).isActive = true
        }

        let apply = NSButton(title: "적용", target: self, action: #selector(applyOrientation))
        let reset = NSButton(title: "되돌리기", target: self, action: #selector(resetOrientation))
        [apply, reset].forEach { $0.bezelStyle = .rounded; $0.controlSize = .small }

        func line(_ slider: NSSlider, _ field: NSTextField) -> NSView {
            let deg = NSTextField(labelWithString: "°")
            deg.textColor = .tertiaryLabelColor
            let h = NSStackView(views: [slider, field, deg])
            h.orientation = .horizontal
            h.alignment = .centerY
            h.spacing = 4
            return h
        }
        let buttons = NSStackView(views: [apply, reset])
        buttons.spacing = 6

        addSection(section("파노라마 방향", [
            row("좌우", line(yawSlider, yawField)),
            row("상하", line(pitchSlider, pitchField)),
            row("기울기", line(rollSlider, rollField)),
            row("", buttons),
        ]))
    }

    /// 슬라이더를 끄는 동안에는 숫자만 바꾸고, 손을 뗀 순간에 적용한다.
    @objc private func sliderMoved(_ sender: NSSlider) {
        let field: NSTextField = sender === yawSlider ? yawField
                               : sender === pitchSlider ? pitchField : rollField
        field.stringValue = String(format: "%.1f", sender.doubleValue)
        if NSApp.currentEvent?.type == .leftMouseUp {
            applyOrientation()
        } else {
            onOrientPreview?(Double(yawField.stringValue) ?? 0,
                             Double(pitchField.stringValue) ?? 0,
                             Double(rollField.stringValue) ?? 0)
        }
    }

    // MARK: 화각

    /// 출력 틀의 가로·세로 화각. 숫자로도 넣을 수 있고, '맞춤' 은 사진이 다
    /// 들어가는 가장 작은 화각으로 다시 잰다.
    private func buildFov() {
        for f in [hfovField, vfovField] {
            f.font = UI.monoFont(12)
            f.alignment = .right
            f.placeholderString = "자동"
            f.target = self
            f.action = #selector(fovFieldEdited)
            f.widthAnchor.constraint(equalToConstant: 54).isActive = true
        }
        for s in [hfovSlider, vfovSlider] {
            s.minValue = 1
            s.maxValue = 360
            s.target = self
            s.action = #selector(fovSliderMoved(_:))
            s.isContinuous = true
            s.widthAnchor.constraint(greaterThanOrEqualToConstant: 96).isActive = true
        }
        let fit = NSButton(title: "맞춤", target: self, action: #selector(fovFit))
        fit.bezelStyle = .rounded
        fit.controlSize = .small
        fit.toolTip = "사진이 전부 들어가는 가장 작은 화각으로 맞춥니다"

        func line(_ slider: NSSlider, _ field: NSTextField) -> NSView {
            let deg = NSTextField(labelWithString: "°")
            deg.textColor = .tertiaryLabelColor
            let h = NSStackView(views: [slider, field, deg])
            h.orientation = .horizontal
            h.alignment = .centerY
            h.spacing = 4
            return h
        }
        // 버튼을 스택에 한 번 감싸야 제 크기를 지킨다. 행에 바로 넣으면 폭 전체로 늘어난다.
        let fitRow = NSStackView(views: [fit])
        addSection(section("화각", [
            row("가로", line(hfovSlider, hfovField)),
            row("세로", line(vfovSlider, vfovField)),
            row("", fitRow),
        ]))
    }

    private var fovValues: (Double, Double) {
        (Double(hfovField.stringValue) ?? hfovSlider.doubleValue,
         Double(vfovField.stringValue) ?? vfovSlider.doubleValue)
    }

    @objc private func fovSliderMoved(_ sender: NSSlider) {
        let field = sender === hfovSlider ? hfovField : vfovField
        field.stringValue = String(format: "%.1f", sender.doubleValue)
        if NSApp.currentEvent?.type == .leftMouseUp {
            onFov?(fovValues.0, fovValues.1)
        } else {
            onFovPreview?(fovValues.0, fovValues.1)
        }
    }

    @objc private func fovFieldEdited() {
        let h = min(max(Double(hfovField.stringValue) ?? hfovSlider.doubleValue, 1), hfovSlider.maxValue)
        let v = min(max(Double(vfovField.stringValue) ?? vfovSlider.doubleValue, 1), vfovSlider.maxValue)
        hfovField.stringValue = String(format: "%.1f", h)
        vfovField.stringValue = String(format: "%.1f", v)
        hfovSlider.doubleValue = h
        vfovSlider.doubleValue = v
        onFov?(h, v)
    }

    @objc private func fovFit() { onFovFit?() }

    /// 숫자를 직접 고치면 슬라이더도 따라 움직인다.
    @objc private func fieldEdited() {
        syncSlidersFromFields()
        applyOrientation()
    }

    /// 적용 전 화각 값을 그대로 둔다 (다른 갱신이 옛 값으로 덮지 않게)
    func showFov(h: Double, v: Double) {
        hfovField.stringValue = String(format: "%.1f", h)
        vfovField.stringValue = String(format: "%.1f", v)
        hfovSlider.doubleValue = h
        vfovSlider.doubleValue = v
    }

    /// 미리보기를 끄는 동안 값만 따라 보여 준다. 적용은 하지 않는다.
    func showOrientation(yaw: Double, pitch: Double, roll: Double) {
        yawField.stringValue = String(format: "%.1f", yaw)
        pitchField.stringValue = String(format: "%.1f", pitch)
        rollField.stringValue = String(format: "%.1f", roll)
        syncSlidersFromFields()
    }

    private func syncSlidersFromFields() {
        yawSlider.doubleValue = Double(yawField.stringValue) ?? 0
        pitchSlider.doubleValue = Double(pitchField.stringValue) ?? 0
        rollSlider.doubleValue = Double(rollField.stringValue) ?? 0
    }

    @objc private func applyOrientation() {
        onOrient?(Double(yawField.stringValue) ?? 0,
                  Double(pitchField.stringValue) ?? 0,
                  Double(rollField.stringValue) ?? 0)
    }

    @objc private func resetOrientation() {
        for f in [yawField, pitchField, rollField] { f.stringValue = "0" }
        for s in [yawSlider, pitchSlider, rollSlider] { s.doubleValue = 0 }
        onOrient?(0, 0, 0)
    }

    private func buildBlending() {
        seamPopup.addItems(withTitles: ["거리 분할 + 동적계획", "거리 분할 + 그래프컷",
                                        "거리 분할만", "쓰지 않음"])
        seamPopup.target = self; seamPopup.action = #selector(seamChanged)
        blendPopup.addItems(withTitles: ["멀티밴드", "페더", "쓰지 않음"])
        blendPopup.target = self; blendPopup.action = #selector(blendChanged)
        exposurePopup.addItems(withTitles: ["자동", "쓰지 않음"])
        exposurePopup.target = self; exposurePopup.action = #selector(exposureChanged)

        wbCheck.target = self; wbCheck.action = #selector(wbChanged)
        vigCheck.target = self; vigCheck.action = #selector(vigChanged)
        patchNadirCheck.target = self; patchNadirCheck.action = #selector(patchNadirChanged)
        fillGapsCheck.target = self; fillGapsCheck.action = #selector(fillGapsChanged)
        fillGapsCheck.toolTip = "사진이 없는 곳을 주변 색으로 메웁니다. "
            + "실제로 찍힌 내용이 아니므로, 어디를 못 찍었는지 보려면 끄세요."

        lowFreqSlider.minValue = 0; lowFreqSlider.maxValue = 100
        lowFreqSlider.widthAnchor.constraint(greaterThanOrEqualToConstant: 90).isActive = true
        lowFreqSlider.target = self; lowFreqSlider.action = #selector(lowFreqChanged)
        lowFreqSlider.isContinuous = true
        lowFreqValue.font = UI.monoFont(13)
        let lf = NSStackView(views: [lowFreqSlider, lowFreqValue])
        lf.spacing = 8

        addSection(section("합성", [
            row("이음선", seamPopup),
            row("블렌딩", blendPopup),
            row("노출", exposurePopup),
            row("밝기 기울기", lf),
            wbCheck, vigCheck, patchNadirCheck, fillGapsCheck
        ]))
    }

    private func buildLens() {
        lensBox.font = UI.font(.callout)
        lensBox.textColor = .secondaryLabelColor
        lensBox.preferredMaxLayoutWidth = 220
        addSection(section("렌즈", [lensBox]))
    }

    private func buildOutput() {
        [widthField, heightField].forEach {
            $0.font = UI.monoFont(12)
            $0.alignment = .right
            $0.target = self
            $0.action = #selector(sizeEdited(_:))
            $0.widthAnchor.constraint(equalToConstant: 76).isActive = true
        }
        let times = NSTextField(labelWithString: "×")
        times.textColor = .tertiaryLabelColor
        let reset = NSButton(title: "원본", target: self, action: #selector(resetSize))
        reset.bezelStyle = .rounded; reset.controlSize = .small
        let sizeRow = NSStackView(views: [widthField, times, heightField, reset])
        sizeRow.spacing = 5

        formatPopup.addItems(withTitles: ["JPEG", "TIFF", "PNG"])
        formatPopup.target = self; formatPopup.action = #selector(formatChanged)

        qualitySlider.minValue = 60; qualitySlider.maxValue = 100
        qualitySlider.widthAnchor.constraint(greaterThanOrEqualToConstant: 90).isActive = true
        qualitySlider.target = self; qualitySlider.action = #selector(qualityChanged)
        qualityValue.font = UI.monoFont(11)
        qualityValue.textColor = .secondaryLabelColor
        let q = NSStackView(views: [qualitySlider, qualityValue])
        q.spacing = 8
        qualityValue.widthAnchor.constraint(equalToConstant: 28).isActive = true

        sizeSummary.font = UI.monoFont(11)
        sizeSummary.textColor = .secondaryLabelColor

        let export = NSButton(title: "내보내기…", target: self, action: #selector(exportTapped))
        export.bezelStyle = .rounded
        // Return 을 이 버튼에 주지 않는다. 속성 패널은 숫자를 넣고 Return 으로
        // 확정하는 곳이라, 기본 버튼이 있으면 값을 넣을 때마다 저장 창이 뜬다.
        // 파란 강조만 남긴다 (내보내기는 ⌘E).
        export.bezelColor = .controlAccentColor

        addSection(section("출력", [
            row("크기", sizeRow),
            row("형식", formatPopup),
            row("품질", q),
            sizeSummary,
            export,
        ]))
    }

    private func addSection(_ v: NSView) {
        stack.addArrangedSubview(v)
        v.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -28).isActive = true
    }

    // MARK: - 상태 반영

    func update(project: Project, preview: PreviewResult?,
                layout: LayoutInfo? = nil) {
        suppress = true
        defer { suppress = false }
        let s = project.settings

        // 화각 — 투영마다 한계가 달라 슬라이더 범위도 같이 바꾼다
        let lim = FovLimit.max(for: s.projection)
        hfovSlider.maxValue = lim.0
        vfovSlider.maxValue = lim.1
        if ![hfovField, vfovField].contains(where: { $0.currentEditor() != nil }) {
            hfovField.stringValue = s.hfov > 0 ? String(format: "%.1f", s.hfov) : ""
            vfovField.stringValue = s.vfov > 0 ? String(format: "%.1f", s.vfov) : ""
            if s.hfov > 0 { hfovSlider.doubleValue = s.hfov }
            if s.vfov > 0 { vfovSlider.doubleValue = s.vfov }
        }

        if let p = Projection(rawValue: s.projection),
           let idx = Projection.allCases.firstIndex(of: p) {
            projectionPopup.selectItem(at: idx)
        }
        seamPopup.selectItem(at: ["dp_color_grad", "graphcut", "distance", "none"]
            .firstIndex(of: s.seam) ?? 0)
        blendPopup.selectItem(at: ["multiband", "feather", "none"]
            .firstIndex(of: s.blender) ?? 0)
        exposurePopup.selectItem(at: s.exposure == "auto" ? 0 : 1)
        wbCheck.state = s.perChannel ? .on : .off
        vigCheck.state = s.vignetting ? .on : .off
        patchNadirCheck.state = s.patchNadir ? .on : .off
        fillGapsCheck.state = s.fillGaps ? .on : .off
        lowFreqSlider.doubleValue = s.lowFreq * 100
        lowFreqValue.stringValue = "\(Int(s.lowFreq * 100))%"
        formatPopup.selectItem(at: ["jpg", "tif", "png"].firstIndex(of: s.format) ?? 0)
        qualitySlider.integerValue = s.quality
        qualityValue.stringValue = "\(s.quality)"
        qualitySlider.isEnabled = s.format == "jpg"

        // 렌즈 — EXIF 에서 읽은 이름과 추정 화각
        var lines: [String] = []
        for (lid, lens) in project.lenses.sorted(by: { $0.key < $1.key }) {
            let users = project.lensUsers[lid] ?? []
            let first = users.first.flatMap { project.images["\($0)"] }
            let name = first?.exif.lens
                ?? [first?.exif.make, first?.exif.model].compactMap { $0 }.joined(separator: " ")
            lines.append(name.isEmpty ? "이름 없는 렌즈" : name)
            var detail = [String(format: "화각 %.2f°", lens.fov)]
            if let f = first?.exif.focalLength { detail.append(String(format: "%.0fmm", f)) }
            detail.append("\(users.count)장")
            lines.append("   " + detail.joined(separator: " · "))
            if let v = project.vignetting?[lid] {
                lines.append(String(format: "   가장자리 밝기 %.1f%%", v.cornerFalloff * 100))
            }
        }
        lensBox.stringValue = lines.isEmpty ? "사진을 추가하면 렌즈를 읽습니다"
                                            : lines.joined(separator: "\n")

        if let l = project.layout,
           ![yawField, pitchField, rollField].contains(where: { $0.currentEditor() != nil }) {
            let deg = 180.0 / Double.pi
            yawField.stringValue = String(format: "%.1f", l.centerYaw * deg)
            pitchField.stringValue = String(format: "%.1f", l.centerPitch * deg)
            rollField.stringValue = String(format: "%.1f", l.centerRoll * deg)
            syncSlidersFromFields()
        }
        // 크기는 미리보기가 아니라 layout 에서 받는다. 설정을 바꾸고 아직
        // 그리지 않았어도 나올 크기는 바로 알 수 있어야 하기 때문이다.
        if let l = layout {
            aspect = l.fullSize.1 > 0 ? Double(l.fullSize.0) / Double(l.fullSize.1) : 2
            if widthField.currentEditor() == nil { widthField.stringValue = "\(l.fullSize.0)" }
            if heightField.currentEditor() == nil { heightField.stringValue = "\(l.fullSize.1)" }
            sizeSummary.stringValue = String(format: "%.1f 메가픽셀 · 원본 %d×%d",
                                             l.megapixels, l.nativeSize.0, l.nativeSize.1)
        } else {
            sizeSummary.stringValue = "정렬 후 계산됩니다"
        }
    }

    // MARK: - 동작

    @objc private func projectionChanged() {
        guard !suppress else { return }
        onProjection?(Projection.allCases[projectionPopup.indexOfSelectedItem])
    }
    @objc private func littlePlanet() { onPlanet?(-90) }
    @objc private func tunnel() { onPlanet?(90) }
    @objc private func seamChanged() {
        guard !suppress else { return }
        onSetting?("seam", ["dp_color_grad", "graphcut", "distance", "none"][seamPopup.indexOfSelectedItem])
    }
    @objc private func blendChanged() {
        guard !suppress else { return }
        onSetting?("blender", ["multiband", "feather", "none"][blendPopup.indexOfSelectedItem])
    }
    @objc private func exposureChanged() {
        guard !suppress else { return }
        onSetting?("exposure", exposurePopup.indexOfSelectedItem == 0 ? "auto" : "none")
    }
    @objc private func wbChanged() {
        guard !suppress else { return }
        onSetting?("per_channel", wbCheck.state == .on)
    }
    @objc private func vigChanged() {
        guard !suppress else { return }
        onSetting?("vignetting", vigCheck.state == .on)
    }
    @objc private func fillGapsChanged() {
        guard !suppress else { return }
        onSetting?("fill_gaps", fillGapsCheck.state == .on)
    }

    @objc private func patchNadirChanged() {
        guard !suppress else { return }
        onSetting?("patch_nadir", patchNadirCheck.state == .on)
    }
    @objc private func lowFreqChanged() {
        lowFreqValue.stringValue = "\(Int(lowFreqSlider.doubleValue))%"
        guard !suppress, NSApp.currentEvent?.type != .leftMouseDragged else { return }
        onSetting?("low_freq", lowFreqSlider.doubleValue / 100)
    }
    @objc private func formatChanged() {
        guard !suppress else { return }
        let f = ["jpg", "tif", "png"][formatPopup.indexOfSelectedItem]
        qualitySlider.isEnabled = f == "jpg"
        onSetting?("format", f)
    }
    @objc private func qualityChanged() {
        qualityValue.stringValue = "\(qualitySlider.integerValue)"
        guard !suppress, NSApp.currentEvent?.type != .leftMouseDragged else { return }
        onSetting?("quality", qualitySlider.integerValue)
    }
    @objc private func sizeEdited(_ sender: NSTextField) {
        guard !suppress else { return }
        if sender === widthField, let w = Int(widthField.stringValue), w > 63 {
            heightField.stringValue = "\(Int(Double(w) / aspect))"
            onSetting?("out_width", w)
        } else if sender === heightField, let h = Int(heightField.stringValue), h > 63 {
            let w = Int(Double(h) * aspect)
            widthField.stringValue = "\(w)"
            onSetting?("out_width", w)
        }
    }
    @objc private func resetSize() { onSetting?("out_width", 0) }
    @objc private func exportTapped() { onExport?() }
}


/// 위에서부터 내용을 쌓는 클립 뷰.
@MainActor
final class FlippedClipView: NSClipView {
    override var isFlipped: Bool { true }
}
