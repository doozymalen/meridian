using System;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.UI.Xaml;

namespace Meridian;

public partial class App : Application
{
    private Window? _window;

    /// <summary>
    /// 창이 뜨기 전에 죽으면 사용자에게는 '눌러도 아무 반응이 없다' 로만 보인다.
    /// 무엇이 잘못됐는지 알 수 있게, 어디서 죽든 기록을 남기고 알림 창을 띄운다.
    /// 인스턴스 생성자(InitializeComponent)보다 먼저 걸어야 하므로 정적 생성자에 둔다.
    /// </summary>
    static App()
    {
        AppDomain.CurrentDomain.UnhandledException += (_, e) => ReportCrash(e.ExceptionObject as Exception);
    }

    public App()
    {
        InitializeComponent();
        UnhandledException += (_, e) => ReportCrash(e.Exception);
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        _window = new MainWindow();
        _window.Activate();
    }

    public static string CrashLogPath =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                     "Meridian", "crash.log");

    private static bool _reported;

    private static void ReportCrash(Exception? ex)
    {
        if (_reported) return;            // 같은 실패가 두 경로로 올 수 있다
        _reported = true;
        string text = $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}] {ex}\n\n";
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(CrashLogPath)!);
            File.AppendAllText(CrashLogPath, text);
        }
        catch { }
        // XAML 이 아직 없을 수도 있으니 운영체제의 기본 알림 창을 쓴다
        MessageBoxW(IntPtr.Zero,
                    $"Meridian 을 여는 중에 문제가 생겼습니다.\n\n{ex?.Message}\n\n자세한 내용: {CrashLogPath}",
                    "Meridian", 0x10 /* MB_ICONERROR */);
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);
}
