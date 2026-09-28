import Foundation

/// 파이썬 스티칭 엔진과의 통신.
///
/// 엔진은 번들 안의 파이썬으로 로컬 주소에서만 돌고, 사용자에게는 보이지 않는다.
/// 앱이 직접 자식 프로세스로 띄우고 끝날 때 함께 정리한다. 브라우저는 쓰지 않는다.
final class Engine {

    enum EngineError: LocalizedError {
        case notRunning
        case http(Int, String)
        case decode(String)
        case job(String)
        case cancelled

        var errorDescription: String? {
            switch self {
            case .notRunning:          return "엔진이 실행되지 않았습니다"
            case .http(let c, let m):   return m.isEmpty ? "서버 오류 (\(c))" : m
            case .decode(let m):        return "응답을 읽지 못했습니다: \(m)"
            case .job(let m):           return m
            case .cancelled:            return "취소했습니다"
            }
        }
    }

    private var process: Process?
    private(set) var port: Int = 0
    private let session: URLSession

    init() {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 600
        cfg.timeoutIntervalForResource = 3600
        session = URLSession(configuration: cfg)
    }

    // MARK: - 수명 주기

    /// 번들 안의 파이썬으로 엔진을 띄우고 응답할 때까지 기다린다.
    func start() async throws {
        let res = Bundle.main.resourceURL!
        let python = res.appendingPathComponent("venv/bin/python")
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            throw EngineError.job("번들 안에서 파이썬을 찾지 못했습니다:\n\(python.path)")
        }

        port = Self.freePort()
        let p = Process()
        p.executableURL = python
        p.arguments = ["-m", "uvicorn", "meridian.server:app",
                       "--host", "127.0.0.1", "--port", "\(port)",
                       "--log-level", "warning"]
        p.currentDirectoryURL = res
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        // OpenBLAS/OpenMP 스레드 충돌로 인한 scipy 최적화 데드락 방지
        env["OPENBLAS_NUM_THREADS"] = "1"
        env["OMP_NUM_THREADS"] = "1"
        // 앱이 강제 종료돼도 엔진이 혼자 남지 않도록 제 pid 를 알려 둔다
        env["MERIDIAN_PARENT_PID"] = "\(ProcessInfo.processInfo.processIdentifier)"
        p.environment = env
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        try p.run()
        process = p

        // 뜰 때까지 기다린다 (첫 실행은 임포트 때문에 몇 초 걸린다)
        for _ in 0..<160 {
            if (try? await get("/api/project") as Project) != nil { return }
            try? await Task.sleep(nanoseconds: 150_000_000)
            if !p.isRunning { throw EngineError.job("엔진이 시작하자마자 종료됐습니다") }
        }
        throw EngineError.job("엔진이 응답하지 않습니다")
    }

    func stop() {
        guard let p = process, p.isRunning else { process = nil; return }
        p.terminate()
        // 곱게 끝나지 않으면 확실히 끊는다
        let deadline = Date().addingTimeInterval(3)
        while p.isRunning && Date() < deadline {
            usleep(80_000)
        }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        process = nil
    }

    private static func freePort() -> Int {
        // 커널에게 빈 포트를 하나 받아 그대로 쓴다
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = 0
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { _ = bind(fd, $0, len) }
        }
        var bound = sockaddr_in()
        withUnsafeMutablePointer(to: &bound) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { _ = getsockname(fd, $0, &len) }
        }
        let p = Int(UInt16(bigEndian: bound.sin_port))
        return p > 0 ? p : 8756
    }

    // MARK: - 요청

    func url(_ path: String) -> URL {
        URL(string: "http://127.0.0.1:\(port)\(path)")!
    }

    private func send(_ method: String, _ path: String, body: Any? = nil) async throws -> Data {
        guard port > 0 else { throw EngineError.notRunning }
        var req = URLRequest(url: url(path))
        req.httpMethod = method
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, resp) = try await session.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
            throw EngineError.http(code, detail ?? "")
        }
        return data
    }

    func get<T: Decodable>(_ path: String) async throws -> T {
        try decode(try await send("GET", path))
    }

    @discardableResult
    func post<T: Decodable>(_ path: String, _ body: Any = [:]) async throws -> T {
        try decode(try await send("POST", path, body: body))
    }

    @discardableResult
    func patch<T: Decodable>(_ path: String, _ body: Any = [:]) async throws -> T {
        try decode(try await send("PATCH", path, body: body))
    }

    /// GET 해서 사전 그대로 받는다. 실패하면 빈 사전 — 부가 정보용 통로다.
    func raw(_ path: String) async -> [String: Any] {
        guard let data = try? await send("GET", path) else { return [:] }
        return ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any]) ?? [:]
    }

    /// JSON 이 아니라 바이트 그대로 받는다 (그림 한 장 등)
    func postData(_ path: String, _ body: Any = [:]) async throws -> Data {
        try await send("POST", path, body: body)
    }

    func postRaw(_ path: String, _ body: Any = [:]) async throws -> [String: Any] {
        let data = try await send("POST", path, body: body)
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    func delete(_ path: String) async throws {
        _ = try await send("DELETE", path)
    }

    private func decode<T: Decodable>(_ data: Data) throws -> T {
        do { return try JSONDecoder().decode(T.self, from: data) }
        catch { throw EngineError.decode("\(error)") }
    }

    // MARK: - 오래 걸리는 작업

    /// 작업을 띄우고 끝날 때까지 진행률을 흘려보낸다.
    @discardableResult
    func run(_ path: String, _ body: Any = [:],
             onStart: ((String) -> Void)? = nil,
             onProgress: @escaping (Double, String) -> Void) async throws -> [String: Any] {
        let started = try await postRaw(path, body)
        if started["cancelled"] as? Bool == true { return [:] }
        guard let jid = started["job"] as? String else { return started }
        // 취소 버튼이 이 id 로 서버에 중단을 알린다
        if let onStart { await MainActor.run { onStart(jid) } }

        while true {
            let data = try await send("GET", "/api/job/\(jid)")
            guard let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw EngineError.decode("작업 상태")
            }
            let frac = (j["frac"] as? Double) ?? 0
            let msg = (j["message"] as? String) ?? ""
            await MainActor.run { onProgress(frac, msg) }
            if (j["done"] as? Bool) == true {
                if (j["cancelled"] as? Bool) == true { throw EngineError.cancelled }
                if let err = j["error"] as? String, !err.isEmpty { throw EngineError.job(err) }
                return (j["result"] as? [String: Any]) ?? [:]
            }
            try await Task.sleep(nanoseconds: 140_000_000)
        }
    }

    /// 돌고 있는 작업을 중단시킨다. 서버는 다음 진행 보고에서 스스로 풀린다.
    func cancel(job jid: String) async {
        _ = try? await send("POST", "/api/job/\(jid)/cancel")
    }
}
