import Foundation

/// 엔진이 돌려주는 프로젝트 상태. 서버의 project.to_dict() 와 짝이 맞는다.
struct Project: Decodable {
    var name: String
    var images: [String: SourceImage]
    var lenses: [String: Lens]
    var settings: Settings
    var anchor: Int?
    var optimized: Bool
    var lastRMS: Double
    var cpStats: CPStats
    var lensUsers: [String: [Int]]
    var vignetting: [String: Vignetting]?
    var layout: PanoLayout?
    var path: String?

    enum CodingKeys: String, CodingKey {
        case name, images, lenses, settings, anchor, optimized, vignetting, layout, path
        case lastRMS = "last_rms"
        case cpStats = "cp_stats"
        case lensUsers = "lens_users"
    }

    /// 번호 순으로 정렬한 사진 목록.
    var orderedImages: [SourceImage] {
        images.values.sorted { $0.id < $1.id }
    }

    static let empty = Project(
        name: "", images: [:], lenses: [:], settings: .init(),
        anchor: nil, optimized: false, lastRMS: 0,
        cpStats: .init(total: 0, enabled: 0, groups: [], orphans: []),
        lensUsers: [:], vignetting: nil, layout: nil, path: nil)
}

struct SourceImage: Decodable {
    var id: Int
    var path: String
    var name: String
    var width: Int
    var height: Int
    var enabled: Bool
    var isRaw: Bool
    var fovHint: Double
    var fovSource: String
    var exif: EXIF

    enum CodingKeys: String, CodingKey {
        case id, path, name, width, height, enabled, exif
        case isRaw = "is_raw"
        case fovHint = "fov_hint"
        case fovSource = "fov_source"
    }
}

struct EXIF: Decodable {
    var make: String?
    var model: String?
    var lens: String?
    var focalLength: Double?
    var focal35: Double?
    var fnumber: Double?
    var iso: Double?

    enum CodingKeys: String, CodingKey {
        case make, model, lens, iso, fnumber
        case focalLength = "focal_length"
        case focal35 = "focal_35"
    }
}

struct Lens: Decodable {
    var fov: Double
    var projection: String
    var a: Double
    var b: Double
    var c: Double
}

struct Vignetting: Decodable {
    var a: Double
    var b: Double
    var c: Double

    /// 반경 1.0(짧은 변 끝)에서의 밝기 비율.
    var cornerFalloff: Double { 1 + a + b + c }
}

struct CPStats: Decodable {
    var total: Int
    var enabled: Int
    var groups: [[Int]]
    var orphans: [Int]
}

struct Settings: Decodable {
    var projection: String = "equirect"
    /// 출력 틀의 가로·세로 화각(도). 0 이면 아직 안 정한 것 — 처음 그릴 때 채워진다
    var hfov: Double = 0
    var vfov: Double = 0
    var blender: String = "multiband"
    var seam: String = "dp_color_grad"
    var exposure: String = "auto"
    var perChannel: Bool = true
    var vignetting: Bool = true
    var lowFreq: Double = 1.0
    var scalePercent: Double = 100
    var outWidth: Int = 0
    var format: String = "jpg"
    var quality: Int = 94
    var patchNadir: Bool = false
    var fillGaps: Bool = false

    enum CodingKeys: String, CodingKey {
        case projection, blender, seam, exposure, vignetting, format, quality, hfov, vfov
        case perChannel = "per_channel"
        case lowFreq = "low_freq"
        case scalePercent = "scale_percent"
        case outWidth = "out_width"
        case patchNadir = "patch_nadir"
        case fillGaps = "fill_gaps"
    }
}

/// 미리보기 렌더 결과.
struct PreviewResult {
    var url: String
    var width: Int
    var height: Int
    var fullSize: (Int, Int)
    var megapixels: Double
    var nativeSize: (Int, Int)
    /// 미리보기 그림 가운데에서 화소 하나가 몇 도인지 — 끈 거리를 각도로 바꾼다
    var degPerPx: Double

    init?(_ d: [String: Any]) {
        guard let url = d["url"] as? String,
              let w = d["width"] as? Int, let h = d["height"] as? Int else { return nil }
        self.url = url
        self.width = w
        self.height = h
        let full = d["full_size"] as? [Int] ?? [w, h]
        let native = d["native_size"] as? [Int] ?? full
        self.fullSize = (full.first ?? w, full.count > 1 ? full[1] : h)
        self.nativeSize = (native.first ?? w, native.count > 1 ? native[1] : h)
        self.megapixels = d["megapixels"] as? Double ?? 0
        self.degPerPx = d["deg_per_px"] as? Double ?? 0.2
    }
}

/// 지금 설정으로 나올 출력 크기. 렌더하지 않고 서버가 바로 계산해 준다.
///
/// 크기 표시를 미리보기 결과에서 가져오면, 설정만 바꾸고 아직 안 그린 동안
/// 옛 숫자가 남는다. 사용자 눈에는 '입력해도 안 바뀐다' 로 보인다.
struct LayoutInfo {
    var fullSize: (Int, Int)
    var nativeSize: (Int, Int)
    var megapixels: Double

    init?(_ d: [String: Any]) {
        guard let full = d["full_size"] as? [Int], full.count > 1,
              full[0] > 0, full[1] > 0 else { return nil }
        let native = d["native_size"] as? [Int] ?? full
        self.fullSize = (full[0], full[1])
        self.nativeSize = (native.first ?? full[0], native.count > 1 ? native[1] : full[1])
        self.megapixels = d["megapixels"] as? Double ?? 0
    }
}

/// 투영마다 담을 수 있는 화각 한계(가로, 세로). 엔진의 warp.FOV_MAX 와 같다.
enum FovLimit {
    static func max(for projection: String) -> (Double, Double) {
        switch projection {
        case "cylindrical", "mercator": return (360, 170)
        case "rectilinear": return (170, 170)
        case "stereographic": return (358, 358)
        case "fisheye": return (360, 360)
        default: return (360, 180)
        }
    }
}

/// 투영 방식 — 화면에 보일 이름과 엔진이 쓰는 값.
enum Projection: String, CaseIterable {
    case equirect, cylindrical, rectilinear, mercator, stereographic, fisheye

    var title: String {
        switch self {
        case .equirect:      return "구면"
        case .cylindrical:   return "원통"
        case .rectilinear:   return "직선"
        case .mercator:      return "메르카토르"
        case .stereographic: return "스테레오"
        case .fisheye:       return "어안"
        }
    }
}


/// 제어점 한 쌍. 좌표는 원본 픽셀 기준이다.
struct ControlPoint: Decodable {
    var index: Int
    var imgA: Int
    var imgB: Int
    var xa: Double
    var ya: Double
    var xb: Double
    var yb: Double
    var kind: String
    var error: Double
    var enabled: Bool
    var weight: Double

    enum CodingKeys: String, CodingKey {
        case index, xa, ya, xb, yb, kind, error, enabled, weight
        case imgA = "img_a"
        case imgB = "img_b"
    }

    var kindLabel: String {
        switch kind {
        case "auto":       return "자동"
        case "manual":     return "수동"
        case "vertical":   return "수직선"
        case "horizontal": return "수평선"
        default:           return kind
        }
    }
}

struct ControlPointList: Decodable {
    var points: [ControlPoint]
}

/// 짝점 추천 결과.
struct SuggestResult {
    var x: Double
    var y: Double
    var score: Double

    init?(_ d: [String: Any]) {
        guard let x = d["xb"] as? Double, let y = d["yb"] as? Double else { return nil }
        self.x = x
        self.y = y
        self.score = d["score"] as? Double ?? 0
    }
}


/// 파노라마 캔버스의 방향과 크기.
struct PanoLayout: Decodable {
    var projection: String
    var centerYaw: Double
    var centerPitch: Double
    var centerRoll: Double
    var w: Int
    var h: Int

    enum CodingKeys: String, CodingKey {
        case projection, w, h
        case centerYaw = "center_yaw"
        case centerPitch = "center_pitch"
        case centerRoll = "center_roll"
    }
}
