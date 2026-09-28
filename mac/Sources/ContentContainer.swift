import AppKit

/// 가운데 칸에서 미리보기와 제어점 편집기를 갈아 끼우는 그릇.
///
/// 두 화면을 같은 자리에 겹쳐 두고 isHidden 으로만 바꾸다가, 숨긴 쪽이 화면에
/// 계속 남아 보이는 문제를 겪었다(그리기는 제대로 되는데 합성이 어긋났다).
/// 지금은 쓰는 쪽만 실제로 붙이고 나머지는 떼어 낸다. 상태는 각 화면이
/// 스스로 들고 있으므로 떼었다 붙여도 잃는 것이 없다.
@MainActor
final class ContentContainerViewController: NSViewController {

    enum Mode: Int { case preview, controlPoints }

    let preview = PreviewViewController()
    let editor = CPEditorViewController()
    private(set) var mode: Mode = .preview

    override func loadView() {
        view = NSView()
        addChild(preview)
        addChild(editor)
        show(.preview)
    }

    func show(_ m: Mode) {
        mode = m
        let target = m == .preview ? preview.view : editor.view
        let other = m == .preview ? editor.view : preview.view

        if other.superview != nil { other.removeFromSuperview() }
        if target.superview !== view {
            target.translatesAutoresizingMaskIntoConstraints = false
            view.addSubview(target)
            NSLayoutConstraint.activate([
                target.topAnchor.constraint(equalTo: view.topAnchor),
                target.leadingAnchor.constraint(equalTo: view.leadingAnchor),
                target.trailingAnchor.constraint(equalTo: view.trailingAnchor),
                target.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            ])
        }
        target.needsLayout = true
        target.layoutSubtreeIfNeeded()

        if m == .controlPoints {
            editor.viewBecameVisible()
            view.window?.makeFirstResponder(editor)
        }
    }
}
