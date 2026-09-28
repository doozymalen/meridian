import Foundation

/// 화면을 눈으로 확인할 수 없을 때 쓰는 기록 통로.
/// `MERIDIAN_TRACE=1` 을 줬을 때만 남긴다.
enum Diag {
    static let enabled = ProcessInfo.processInfo.environment["MERIDIAN_TRACE"] == "1"

    static func log(_ message: @autoclosure () -> String) {
        guard enabled else { return }
        FileHandle.standardError.write("[trace] \(message())\n".data(using: .utf8)!)
    }
}
