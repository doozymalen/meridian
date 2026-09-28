using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Net.Http.Json;
using System.Net.Sockets;
using System.Net;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace Meridian;

/// <summary>
/// 파이썬 스티칭 엔진과의 통신.
///
/// 맥 앱과 똑같은 구조다. 엔진은 같은 폴더의 engine\Meridian.exe 로 들어 있고,
/// 이 창이 자식 프로세스로 띄운다. 로컬 주소에서만 듣고, 화면은 HTTP 로만
/// 대화한다. 창이 죽으면 엔진도 같이 끝난다 (MERIDIAN_PARENT_PID).
/// </summary>
public sealed class Engine : IDisposable
{
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromMinutes(30) };
    private Process? _proc;

    public int Port { get; private set; }
    public string Base => $"http://127.0.0.1:{Port}";

    /// <summary>비어 있는 포트를 커널에게 하나 받아 그대로 쓴다.</summary>
    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        int port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }

    private static string EngineExe()
    {
        string dir = AppContext.BaseDirectory;
        string bundled = Path.Combine(dir, "engine", "Meridian.exe");
        if (File.Exists(bundled)) return bundled;
        // 개발 중에는 소스 폴더에서 바로 돌린다
        string dev = Path.GetFullPath(Path.Combine(dir, "..", "..", "..", "..", "dist", "Meridian", "Meridian.exe"));
        return dev;
    }

    public async Task StartAsync()
    {
        Port = FreePort();
        string exe = EngineExe();
        if (!File.Exists(exe))
            throw new FileNotFoundException($"엔진을 찾지 못했습니다:\n{exe}");

        var psi = new ProcessStartInfo(exe)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            WorkingDirectory = Path.GetDirectoryName(exe)!,
        };
        psi.Environment["MERIDIAN_ENGINE_ONLY"] = "1";
        psi.Environment["MERIDIAN_PORT"] = Port.ToString();
        psi.Environment["MERIDIAN_PARENT_PID"] = Environment.ProcessId.ToString();
        _proc = Process.Start(psi);

        // 엔진이 대답하기 시작할 때까지 기다린다. 첫 실행은 풀어 놓는 데 시간이 걸린다.
        for (int i = 0; i < 400; i++)
        {
            if (_proc is { HasExited: true })
                throw new Exception("엔진이 시작하자마자 종료됐습니다");
            try
            {
                using var r = await _http.GetAsync($"{Base}/api/project");
                if (r.IsSuccessStatusCode) return;
            }
            catch { /* 아직 안 떴다 */ }
            await Task.Delay(150);
        }
        throw new TimeoutException("엔진이 응답하지 않습니다");
    }

    // ---------------------------------------------------------------- 기본 통신

    public async Task<JsonNode?> GetAsync(string path)
        => JsonNode.Parse(await _http.GetStringAsync(Base + path));

    public async Task<JsonNode?> PostAsync(string path, object? body = null)
    {
        using var r = await _http.PostAsJsonAsync(Base + path, body ?? new { });
        r.EnsureSuccessStatusCode();
        string s = await r.Content.ReadAsStringAsync();
        return string.IsNullOrWhiteSpace(s) ? null : JsonNode.Parse(s);
    }

    public async Task<JsonNode?> PatchAsync(string path, object body)
    {
        using var req = new HttpRequestMessage(HttpMethod.Patch, Base + path)
        {
            Content = JsonContent.Create(body),
        };
        using var r = await _http.SendAsync(req);
        r.EnsureSuccessStatusCode();
        string s = await r.Content.ReadAsStringAsync();
        return string.IsNullOrWhiteSpace(s) ? null : JsonNode.Parse(s);
    }

    public async Task DeleteAsync(string path)
        => (await _http.DeleteAsync(Base + path)).EnsureSuccessStatusCode();

    public Task<byte[]> BytesAsync(string path) => _http.GetByteArrayAsync(Base + path);

    /// <summary>끄는 동안 쓰는 한 장. 작업으로 돌리지 않고 그림이 바로 온다.</summary>
    public async Task<byte[]?> LiveFrameAsync(object body)
    {
        using var r = await _http.PostAsJsonAsync($"{Base}/api/live/frame", body);
        if (!r.IsSuccessStatusCode) return null;      // 409 = 원판 준비 중
        return await r.Content.ReadAsByteArrayAsync();
    }

    // ---------------------------------------------------------------- 오래 걸리는 작업

    public sealed class JobCancelledException : Exception { }

    /// <summary>작업을 띄우고 끝날 때까지 진행률을 흘려보낸다. 취소는 CancelJobAsync.</summary>
    public async Task<JsonNode?> RunAsync(string path, object? body,
                                          Action<double, string>? onProgress = null,
                                          Action<string>? onStart = null,
                                          CancellationToken ct = default)
    {
        var started = await PostAsync(path, body);
        if (started?["cancelled"]?.GetValue<bool>() == true) return null;
        string? jid = started?["job"]?.GetValue<string>();
        if (jid is null) return started;
        onStart?.Invoke(jid);

        while (true)
        {
            ct.ThrowIfCancellationRequested();
            var j = await GetAsync($"/api/job/{jid}");
            double frac = j?["frac"]?.GetValue<double>() ?? 0;
            string msg = j?["message"]?.GetValue<string>() ?? "";
            onProgress?.Invoke(frac, msg);
            if (j?["done"]?.GetValue<bool>() == true)
            {
                if (j["cancelled"]?.GetValue<bool>() == true) throw new JobCancelledException();
                string? err = j["error"]?.GetValue<string>();
                if (!string.IsNullOrEmpty(err)) throw new Exception(err);
                return j["result"];
            }
            await Task.Delay(140, ct);
        }
    }

    public async Task CancelJobAsync(string jid)
    {
        try { await PostAsync($"/api/job/{jid}/cancel"); } catch { }
    }

    public void Dispose()
    {
        try { if (_proc is { HasExited: false }) _proc.Kill(entireProcessTree: true); } catch { }
        _http.Dispose();
    }
}
