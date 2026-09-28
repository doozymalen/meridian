import AppKit

/// 왼쪽 사진 목록.
///
/// 사이드바 재질(NSVisualEffectView .sidebar)을 쓰면 창 뒤 배경이 은은히 비치고
/// 라이트·다크 전환도 시스템이 알아서 한다. 직접 색을 칠하면 그 둘 다 잃는다.
@MainActor
final class SidebarViewController: NSViewController {

    var onSelect: ((Int) -> Void)?
    var onToggleEnabled: ((Int, Bool) -> Void)?
    var onSetAnchor: ((Int) -> Void)?
    var onRemove: ((Int) -> Void)?
    var thumbnailURL: ((Int) -> URL)?

    private let table = NSTableView()
    private let scroll = NSScrollView()
    private var images: [SourceImage] = []
    private var anchor: Int?
    private var cache: [Int: NSImage] = [:]
    private let emptyLabel = NSTextField(labelWithString: "사진 없음")

    override func loadView() {
        let effect = NSVisualEffectView()
        effect.material = .sidebar
        effect.blendingMode = .behindWindow
        effect.state = .followsWindowActiveState

        table.headerView = nil
        UI.applySourceListStyle(table)
        table.backgroundColor = .clear
        table.rowHeight = 62
        table.intercellSpacing = NSSize(width: 0, height: 4)
        table.allowsMultipleSelection = false
        table.dataSource = self
        table.delegate = self
        table.menu = contextMenu()
        let col = NSTableColumn(identifier: .init("photo"))
        col.resizingMask = .autoresizingMask
        table.addTableColumn(col)

        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.translatesAutoresizingMaskIntoConstraints = false
        effect.addSubview(scroll)

        emptyLabel.font = UI.font(.callout)
        emptyLabel.textColor = .tertiaryLabelColor
        emptyLabel.alignment = .center
        emptyLabel.translatesAutoresizingMaskIntoConstraints = false
        effect.addSubview(emptyLabel)

        NSLayoutConstraint.activate([
            scroll.topAnchor.constraint(equalTo: effect.topAnchor),
            scroll.leadingAnchor.constraint(equalTo: effect.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: effect.trailingAnchor),
            scroll.bottomAnchor.constraint(equalTo: effect.bottomAnchor),
            emptyLabel.centerXAnchor.constraint(equalTo: effect.centerXAnchor),
        ])
        // 사이드바는 제목 줄 뒤까지 올라간다. 그냥 위에 붙이면 신호등 버튼 옆에
        // 글자가 얹힌다. 안전 영역 아래에서 시작해야 한다.
        if #available(macOS 11.0, *) {
            emptyLabel.topAnchor.constraint(
                equalTo: effect.safeAreaLayoutGuide.topAnchor, constant: 14).isActive = true
        } else {
            emptyLabel.topAnchor.constraint(equalTo: effect.topAnchor, constant: 52).isActive = true
        }
        view = effect
    }

    func update(images: [SourceImage], anchor: Int?) {
        let changed = images.map(\.id) != self.images.map(\.id)
        self.images = images
        self.anchor = anchor
        if changed { cache.removeAll() }
        emptyLabel.isHidden = !images.isEmpty
        table.reloadData()
    }

    /// 진단용 — 안내 글이 제목 줄을 피해 앉았는지 본다.
    func safeTopInset() -> CGFloat {
        guard let l = emptyLabel.superview else { return -1 }
        return emptyLabel.frame.minY - l.frame.minY
    }

    private func contextMenu() -> NSMenu {
        let m = NSMenu()
        m.addItem(withTitle: "사용 안 함", action: #selector(toggleItem(_:)), keyEquivalent: "")
        m.addItem(withTitle: "기준 사진으로", action: #selector(anchorItem(_:)), keyEquivalent: "")
        m.addItem(.separator())
        m.addItem(withTitle: "프로젝트에서 제거", action: #selector(removeItem(_:)), keyEquivalent: "")
        m.items.forEach { $0.target = self }
        return m
    }

    private var clickedImage: SourceImage? {
        let row = table.clickedRow
        return images.indices.contains(row) ? images[row] : nil
    }

    @objc private func toggleItem(_ s: Any) {
        guard let im = clickedImage else { return }
        onToggleEnabled?(im.id, !im.enabled)
    }
    @objc private func anchorItem(_ s: Any) {
        if let im = clickedImage { onSetAnchor?(im.id) }
    }
    @objc private func removeItem(_ s: Any) {
        if let im = clickedImage { onRemove?(im.id) }
    }
}

extension SidebarViewController: NSTableViewDataSource, NSTableViewDelegate {

    func numberOfRows(in tableView: NSTableView) -> Int { images.count }

    func tableView(_ t: NSTableView, viewFor col: NSTableColumn?, row: Int) -> NSView? {
        guard images.indices.contains(row) else { return nil }
        let im = images[row]
        let id = NSUserInterfaceItemIdentifier("cell")
        let cell = (t.makeView(withIdentifier: id, owner: self) as? PhotoCell) ?? PhotoCell()
        cell.identifier = id
        cell.configure(image: im, isAnchor: im.id == anchor, thumb: thumbnail(for: im.id))
        return cell
    }

    func tableViewSelectionDidChange(_ n: Notification) {
        let row = table.selectedRow
        if images.indices.contains(row) { onSelect?(images[row].id) }
    }

    private func thumbnail(for id: Int) -> NSImage? {
        if let c = cache[id] { return c }
        guard let url = thumbnailURL?(id) else { return nil }
        // 썸네일은 작아서 바로 읽어도 화면이 걸리지 않는다
        guard let img = NSImage(contentsOf: url) else { return nil }
        cache[id] = img
        return img
    }
}

/// 사진 한 줄: 작은 미리보기 + 이름 + 상태 배지.
@MainActor
final class PhotoCell: NSTableCellView {

    private let thumb = ThumbView()
    private let title = NSTextField(labelWithString: "")
    private let subtitle = NSTextField(labelWithString: "")
    private let badge = NSTextField(labelWithString: "")

    override init(frame: NSRect) {
        super.init(frame: frame)
        build()
    }
    required init?(coder: NSCoder) { fatalError() }

    private func build() {
        thumb.wantsLayer = true
        thumb.layer?.cornerRadius = 4
        thumb.layer?.masksToBounds = true

        title.font = UI.font(.body)
        title.lineBreakMode = .byTruncatingMiddle
        subtitle.font = UI.font(.caption)
        subtitle.textColor = .secondaryLabelColor

        badge.font = .systemFont(ofSize: 9, weight: .semibold)
        badge.textColor = .white
        badge.alignment = .center
        badge.wantsLayer = true
        badge.layer?.cornerRadius = 3
        badge.layer?.backgroundColor = NSColor.controlAccentColor.cgColor
        badge.isHidden = true

        let text = NSStackView(views: [title, subtitle])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 1

        let row = NSStackView(views: [thumb, text])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 8
        row.translatesAutoresizingMaskIntoConstraints = false
        addSubview(row)
        addSubview(badge)
        badge.translatesAutoresizingMaskIntoConstraints = false

        NSLayoutConstraint.activate([
            row.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 8),
            row.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -8),
            row.centerYAnchor.constraint(equalTo: centerYAnchor),
            thumb.widthAnchor.constraint(equalToConstant: 52),
            thumb.heightAnchor.constraint(equalToConstant: 52),
            badge.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -8),
            badge.topAnchor.constraint(equalTo: topAnchor, constant: 4),
            badge.widthAnchor.constraint(greaterThanOrEqualToConstant: 30),
            badge.heightAnchor.constraint(equalToConstant: 14),
        ])
        textField = title
    }

    func configure(image im: SourceImage, isAnchor: Bool, thumb img: NSImage?) {
        thumb.image = img
        title.stringValue = im.name
        let mp = Double(im.width * im.height) / 1_000_000
        var parts = [String(format: "%.0fMP", mp)]
        if im.isRaw { parts.append("RAW") }
        if let f = im.exif.focalLength { parts.append(String(format: "%.0fmm", f)) }
        subtitle.stringValue = parts.joined(separator: " · ")

        badge.isHidden = !isAnchor
        badge.stringValue = isAnchor ? " 기준 " : ""
        alphaValue = im.enabled ? 1.0 : 0.4
    }
}


/// 정사각 칸을 가득 채우는 작은 미리보기.
///
/// NSImageView 의 비례 축소는 세로 사진을 넣으면 좌우에 큰 여백을 남긴다.
/// 목록에서는 가장자리를 잘라서라도 크기를 맞추는 편이 훑어보기 좋다.
@MainActor
final class ThumbView: NSView {

    var image: NSImage? { didSet { needsDisplay = true } }

    override func draw(_ dirty: NSRect) {
        NSColor.quaternaryLabelColor.withAlphaComponent(0.18).setFill()
        bounds.fill()
        guard let img = image, img.size.width > 0, img.size.height > 0 else { return }
        let scale = max(bounds.width / img.size.width, bounds.height / img.size.height)
        let w = img.size.width * scale
        let h = img.size.height * scale
        img.draw(in: NSRect(x: (bounds.width - w) / 2, y: (bounds.height - h) / 2,
                            width: w, height: h))
    }
}
