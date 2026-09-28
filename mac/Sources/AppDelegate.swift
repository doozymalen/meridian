import AppKit

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {

    private var controller: MainWindowController?

    func applicationDidFinishLaunching(_ n: Notification) {
        NSApp.setActivationPolicy(.regular)
        buildMenu()
        let c = MainWindowController()
        controller = c
        c.launch()
        if let list = ProcessInfo.processInfo.environment["MERIDIAN_TEST_ADD"], !list.isEmpty {
            let paths = list.split(separator: ":").map(String.init)
            DispatchQueue.main.asyncAfter(deadline: .now() + 4) {
                c.addPathsForTesting(paths)
            }
        }
        if ProcessInfo.processInfo.environment["MERIDIAN_START_MODE"] == "cp" {
            DispatchQueue.main.asyncAfter(deadline: .now() + 6) {
                c.showControlPointMode(nil)
            }
        }
        if ProcessInfo.processInfo.environment["MERIDIAN_DUMP_LAYOUT"] == "1" {
            // 두 번 찍는다. 두 번째는 사진을 넣은 뒤 상태를 보기 위한 것.
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { c.dumpLayout() }
            DispatchQueue.main.asyncAfter(deadline: .now() + 26) { c.dumpLayout() }
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationWillTerminate(_ n: Notification) {
        controller?.shutdown()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }

    /// 메뉴 막대. 맥 앱이라면 있어야 할 항목은 비워 두지 않는다 —
    /// 단축키와 접근성 도구가 여기를 먼저 본다.
    private func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Meridian 정보", action: #selector(about), keyEquivalent: "")
            .target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Meridian 가리기", action: #selector(NSApplication.hide(_:)),
                        keyEquivalent: "h")
        let others = appMenu.addItem(withTitle: "다른 앱 가리기",
                                     action: #selector(NSApplication.hideOtherApplications(_:)),
                                     keyEquivalent: "h")
        others.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Meridian 종료", action: #selector(NSApplication.terminate(_:)),
                        keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let fileItem = NSMenuItem()
        let fileMenu = NSMenu(title: "파일")
        fileMenu.addItem(withTitle: "사진 추가…", action: #selector(MainWindowController.addPhotos(_:)),
                         keyEquivalent: "o")
        fileMenu.addItem(.separator())
        fileMenu.addItem(withTitle: "내보내기…", action: #selector(exportProxy), keyEquivalent: "E")
            .target = self
        fileItem.submenu = fileMenu
        main.addItem(fileItem)

        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "편집")
        editMenu.addItem(withTitle: "실행 취소", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "다시 실행", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "복사", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "붙여넣기", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "모두 선택", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        main.addItem(editItem)

        let panoItem = NSMenuItem()
        let panoMenu = NSMenu(title: "파노라마")
        panoMenu.addItem(withTitle: "자동 정렬",
                         action: #selector(MainWindowController.alignPhotos(_:)), keyEquivalent: "r")
        panoMenu.addItem(withTitle: "최적화",
                         action: #selector(MainWindowController.optimize(_:)), keyEquivalent: "R")
        panoMenu.addItem(withTitle: "수평 맞추기",
                         action: #selector(MainWindowController.straighten(_:)), keyEquivalent: "")
        panoItem.submenu = panoMenu
        main.addItem(panoItem)

        let viewItem = NSMenuItem()
        let viewMenu = NSMenu(title: "보기")
        viewMenu.addItem(withTitle: "미리보기",
                         action: #selector(MainWindowController.showPreviewMode(_:)),
                         keyEquivalent: "1")
        viewMenu.addItem(withTitle: "제어점 편집",
                         action: #selector(MainWindowController.showControlPointMode(_:)),
                         keyEquivalent: "2")
        viewMenu.addItem(withTitle: "미리보기 갱신",
                         action: #selector(MainWindowController.refreshPreviewNow(_:)),
                         keyEquivalent: "y")
        viewMenu.addItem(.separator())
        viewMenu.addItem(withTitle: "실제 크기",
                         action: #selector(MainWindowController.zoomActual(_:)), keyEquivalent: "0")
        viewMenu.addItem(withTitle: "화면에 맞추기",
                         action: #selector(MainWindowController.zoomFit(_:)), keyEquivalent: "9")
        viewMenu.addItem(withTitle: "확대",
                         action: #selector(MainWindowController.zoomIn(_:)), keyEquivalent: "+")
        viewMenu.addItem(withTitle: "축소",
                         action: #selector(MainWindowController.zoomOut(_:)), keyEquivalent: "-")
        viewMenu.addItem(.separator())
        viewMenu.addItem(withTitle: "사이드바 가리기/보기",
                         action: #selector(MainWindowController.toggleSidebar(_:)), keyEquivalent: "s")
        viewMenu.addItem(.separator())
        viewMenu.addItem(withTitle: "전체 화면 시작",
                         action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
            .keyEquivalentModifierMask = [.command, .control]
        viewItem.submenu = viewMenu
        main.addItem(viewItem)

        let windowItem = NSMenuItem()
        let windowMenu = NSMenu(title: "윈도우")
        windowMenu.addItem(withTitle: "최소화", action: #selector(NSWindow.miniaturize(_:)),
                           keyEquivalent: "m")
        windowMenu.addItem(withTitle: "확대/축소", action: #selector(NSWindow.zoom(_:)),
                           keyEquivalent: "")
        windowItem.submenu = windowMenu
        main.addItem(windowItem)

        NSApp.mainMenu = main
        NSApp.windowsMenu = windowMenu
    }

    @objc private func about() {
        NSApp.orderFrontStandardAboutPanel(options: [
            .applicationName: "Meridian",
            .credits: NSAttributedString(
                string: "파노라마 이미지 스티칭\n\n"
                      + "특징점 정렬 · 번들 조정 · 비네팅 보정 · 멀티밴드 블렌딩",
                attributes: [.font: NSFont.systemFont(ofSize: 11),
                             .foregroundColor: NSColor.secondaryLabelColor]),
        ])
    }

    /// 메뉴에서도 내보내기를 부를 수 있게 응답 체인으로 넘긴다.
    @objc private func exportProxy() {
        NSApp.sendAction(Selector(("exportFromMenu")), to: nil, from: nil)
    }
}
