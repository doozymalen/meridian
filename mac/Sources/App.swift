import AppKit

/// 진입점.
///
/// main.swift 의 최상위 코드는 메인 액터가 아니라서, AppKit 객체를 거기서
/// 만들면 격리 검사에 걸린다. @main 진입점을 메인 액터로 두면 깔끔하다.
@main
struct MeridianApp {
    @MainActor
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.run()
    }
}
