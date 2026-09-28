import AppKit

/// 낮은 macOS 에서도 컴파일되고 동작하도록 감싸 두는 것들.
///
/// Apple Silicon 맥은 macOS 11 이 최소라 지금 빌드는 11 아래로 내려갈 일이 없다.
/// 그래도 11 전용 API 를 직접 부르지 않고 여기서 한 번 걸러 두면, Intel 이나
/// 더 낮은 버전을 대상으로 다시 빌드할 때 코드를 건드리지 않아도 된다.
enum UI {

    /// 본문 글자 크기. preferredFont(forTextStyle:) 은 macOS 11 부터라 쓰지 않는다.
    enum TextRole {
        case title, body, callout, caption

        var size: CGFloat {
            switch self {
            case .title:   return 15
            case .body:    return 13
            case .callout: return 12
            case .caption: return 10
            }
        }

        var weight: NSFont.Weight {
            self == .title ? .semibold : .regular
        }
    }

    static func font(_ role: TextRole) -> NSFont {
        .systemFont(ofSize: role.size, weight: role.weight)
    }

    static func monoFont(_ size: CGFloat = 11) -> NSFont {
        .monospacedDigitSystemFont(ofSize: size, weight: .regular)
    }

    /// SF Symbols 는 macOS 11 부터다. 그 아래에서는 이름표만 남긴다.
    static func symbol(_ name: String, _ description: String) -> NSImage? {
        if #available(macOS 11.0, *) {
            return NSImage(systemSymbolName: name, accessibilityDescription: description)
        }
        return nil
    }

    /// 사이드바용 표 스타일. 낮은 버전에서는 기본 모양을 쓴다.
    static func applySourceListStyle(_ table: NSTableView) {
        if #available(macOS 11.0, *) {
            table.style = .sourceList
        } else {
            table.selectionHighlightStyle = .sourceList
        }
    }
}
