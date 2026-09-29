using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices.WindowsRuntime;   // byte[] -> IBuffer
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;   // RangeBaseValueChangedEventArgs
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media.Imaging;
using Windows.ApplicationModel.DataTransfer;
using Windows.Storage;
using Windows.Storage.Pickers;
using Windows.Storage.Streams;

namespace Meridian;

/// <summary>사진 목록 한 줄.</summary>
public sealed class PhotoItem
{
    public int Id { get; init; }
    public string Name { get; init; } = "";
    public string Detail { get; init; } = "";
    public BitmapImage? Thumb { get; init; }
    public int Width { get; init; }
    public int Height { get; init; }
    public bool Enabled { get; init; } = true;
    public bool IsAnchor { get; init; }
    public double Opacity => Enabled ? 1.0 : 0.4;
    public Visibility AnchorVisibility => IsAnchor ? Visibility.Visible : Visibility.Collapsed;
}

public sealed partial class MainWindow : Window
{
    private readonly Engine _engine = new();
    private readonly ObservableCollection<PhotoItem> _photos = new();
    private readonly Dictionary<int, BitmapImage?> _thumbs = new();

    private JsonNode? _project;
    private JsonNode? _preview;          // 마지막 미리보기 결과
    private JsonNode? _layout;           // 지금 설정으로 나올 크기

    /// <summary>화면을 코드로 채우는 동안에는 사용자의 조작으로 오해하지 않게 막는다.</summary>
    private bool _suppress;
    private bool _busy;
    private bool _pending;               // 바꾼 설정이 아직 미리보기에 반영되지 않음

    // 방향: 서버 값은 렌더가 끝나야 따라오므로, 마지막으로 정한 값을 화면이 들고 있는다.
    private (double Yaw, double Pitch, double Roll)? _intended;
    private int _orientGen;
    private string? _previewJob;

    // 끌기·슬라이더 조작 중 빠른 미리보기
    private bool _liveActive, _liveInFlight;
    private (double Yaw, double Pitch, double Roll, double H, double V)? _liveTarget;
    private (double Yaw, double Pitch, double Roll) _dragBase;
    private Windows.Foundation.Point _dragStart;
    private bool _dragging, _dragRoll;
    private double _dragShownWidth = 1;

    private static readonly string[] SeamKeys = { "dp_color_grad", "graphcut", "distance", "none" };
    private static readonly string[] BlendKeys = { "multiband", "feather", "none" };
    private static readonly string[] ProjKeys =
        { "equirect", "cylindrical", "rectilinear", "mercator", "stereographic", "fisheye" };

    public MainWindow()
    {
        InitializeComponent();
        Title = "Meridian";
        PhotoList.ItemsSource = _photos;

        ProjectionCombo.ItemsSource = new[] { "구면", "원통", "직선", "메르카토르", "스테레오", "어안" };
        SeamCombo.ItemsSource = new[] { "거리 분할 + 동적계획", "거리 분할 + 그래프컷", "거리 분할만", "쓰지 않음" };
        BlendCombo.ItemsSource = new[] { "멀티밴드", "페더", "쓰지 않음" };
        ExposureCombo.ItemsSource = new[] { "자동", "쓰지 않음" };
        FormatCombo.ItemsSource = new[] { "JPEG", "TIFF", "PNG" };

        Editor.Engine = _engine;
        Editor.Changed += async () => { try { await RefreshAsync(); } catch { } };

        Root.KeyDown += OnKeyDown;
        Activated += OnFirstActivated;
        Closed += (_, _) => _engine.Dispose();
    }

    private bool _started;
    private async void OnFirstActivated(object sender, WindowActivatedEventArgs e)
    {
        if (_started) return;
        _started = true;
        try
        {
            SetStatus("엔진을 켜는 중…");
            await _engine.StartAsync();
            await RefreshAsync();
            SetStatus("");
        }
        catch (Exception ex) { await ReportAsync(ex.Message); }
    }

    private void OnKeyDown(object sender, KeyRoutedEventArgs e)
    {
        bool ctrl = Microsoft.UI.Input.InputKeyboardSource
            .GetKeyStateForCurrentThread(Windows.System.VirtualKey.Control)
            .HasFlag(Windows.UI.Core.CoreVirtualKeyStates.Down);
        if (!ctrl) return;
        if (e.Key == Windows.System.VirtualKey.Y) RefreshPreview_Click(this, new RoutedEventArgs());
        else if (e.Key == Windows.System.VirtualKey.R) Align_Click(this, new RoutedEventArgs());
        else if (e.Key == Windows.System.VirtualKey.O) AddPhotos_Click(this, new RoutedEventArgs());
        else if (e.Key == Windows.System.VirtualKey.E) Export_Click(this, new RoutedEventArgs());
    }

    // ---------------------------------------------------------------- 상태 읽기

    private bool Optimized => _project?["optimized"]?.GetValue<bool>() == true;
    private JsonObject Settings => _project?["settings"]?.AsObject() ?? new JsonObject();
    private double Setting(string k, double d = 0) => Settings[k]?.GetValue<double>() ?? d;
    private string SettingStr(string k, string d = "") => Settings[k]?.GetValue<string>() ?? d;
    private bool SettingBool(string k) => Settings[k]?.GetValue<bool>() == true;

    private async Task RefreshAsync()
    {
        _project = await _engine.GetAsync("/api/project");
        // 크기 계산은 화소를 건드리지 않아 순식간이다. 설정을 바꾼 직후에도 숫자가 맞는다.
        try { _layout = await _engine.GetAsync("/api/layout"); } catch { _layout = null; }
        await FillPhotosAsync();
        FillInspector();
        UpdatePanels();
        UpdateStatus();
        if (Editor.Visibility == Visibility.Visible) await UpdateEditorAsync();
    }

    private async Task FillPhotosAsync()
    {
        var images = _project?["images"]?.AsObject();
        if (images is null) { _photos.Clear(); return; }
        var ids = images.Select(kv => int.Parse(kv.Key)).OrderBy(i => i).ToList();
        int? anchor = _project?["anchor"]?.GetValue<int>();
        bool Enabled(int id) => images[id.ToString()]?["enabled"]?.GetValue<bool>() != false;
        // 켜고 끄기·기준 바꾸기도 줄 모양이 바뀌므로 함께 비교한다
        if (_photos.Count == ids.Count
            && _photos.Select(p => (p.Id, p.Enabled, p.IsAnchor))
                      .SequenceEqual(ids.Select(i => (i, Enabled(i), i == anchor)))) return;

        int? selected = (PhotoList.SelectedItem as PhotoItem)?.Id;
        foreach (int gone in _thumbs.Keys.Except(ids).ToList()) _thumbs.Remove(gone);
        _photos.Clear();
        foreach (int id in ids)
        {
            var im = images[id.ToString()]!;
            string path = im["path"]?.GetValue<string>() ?? "";
            int w = (int)(im["width"]?.GetValue<double>() ?? 0), h = (int)(im["height"]?.GetValue<double>() ?? 0);
            var exif = im["exif"];
            string focal = exif?["focal_length"] is JsonNode f ? $" · {f.GetValue<double>():0}mm" : "";
            if (!_thumbs.TryGetValue(id, out var thumb))
            {
                try { thumb = await ToBitmapAsync(await _engine.BytesAsync($"/api/thumb/{id}")); }
                catch { thumb = null; }
                _thumbs[id] = thumb;
            }
            _photos.Add(new PhotoItem
            {
                Id = id,
                Name = Path.GetFileName(path),
                Detail = $"{w * (double)h / 1e6:0}MP{focal}",
                Thumb = thumb,
                Width = w,
                Height = h,
                Enabled = Enabled(id),
                IsAnchor = id == anchor,
            });
        }
        if (selected is int sel) PhotoList.SelectedItem = _photos.FirstOrDefault(p => p.Id == sel);
    }

    internal static async Task<BitmapImage> ToBitmapAsync(byte[] bytes)
    {
        var bmp = new BitmapImage();
        using var ms = new InMemoryRandomAccessStream();
        await ms.WriteAsync(bytes.AsBuffer());
        ms.Seek(0);
        await bmp.SetSourceAsync(ms);
        return bmp;
    }

    private void FillInspector()
    {
        _suppress = true;
        try
        {
            ProjectionCombo.SelectedIndex = Math.Max(0, Array.IndexOf(ProjKeys, SettingStr("projection", "equirect")));
            SeamCombo.SelectedIndex = Math.Max(0, Array.IndexOf(SeamKeys, SettingStr("seam", "dp_color_grad")));
            BlendCombo.SelectedIndex = Math.Max(0, Array.IndexOf(BlendKeys, SettingStr("blender", "multiband")));
            ExposureCombo.SelectedIndex = SettingStr("exposure", "none") == "auto" ? 0 : 1;
            WbCheck.IsChecked = SettingBool("per_channel");
            VigCheck.IsChecked = SettingBool("vignetting");
            NadirCheck.IsChecked = SettingBool("patch_nadir");
            FillGapsCheck.IsChecked = SettingBool("fill_gaps");
            QualitySlider.Value = Setting("quality", 94);
            FormatCombo.SelectedIndex = SettingStr("format", "jpg") switch { "tif" => 1, "png" => 2, _ => 0 };

            // 방향 — 화면이 들고 있는 값이 우선이다 (서버는 렌더가 끝나야 따라온다)
            var o = CurrentOrientation();
            YawSlider.Value = YawBox.Value = Math.Round(o.Yaw, 1);
            PitchSlider.Value = PitchBox.Value = Math.Round(o.Pitch, 1);
            RollSlider.Value = RollBox.Value = Math.Round(o.Roll, 1);

            // 화각 — 투영마다 한계가 다르다
            var (hmax, vmax) = FovLimit(SettingStr("projection", "equirect"));
            HfovSlider.Maximum = hmax; VfovSlider.Maximum = vmax;
            HfovBox.Maximum = hmax; VfovBox.Maximum = vmax;
            double h = Setting("hfov"), v = Setting("vfov");
            if (h > 0) { HfovSlider.Value = h; HfovBox.Value = h; }
            if (v > 0) { VfovSlider.Value = v; VfovBox.Value = v; }

            if (_layout?["full_size"] is JsonArray fs && fs.Count > 1)
            {
                WidthBox.Value = fs[0]!.GetValue<double>();
                HeightBox.Value = fs[1]!.GetValue<double>();
                var ns = _layout["native_size"] as JsonArray;
                double mp = _layout["megapixels"]?.GetValue<double>() ?? 0;
                SizeSummary.Text = ns is { Count: > 1 }
                    ? $"{mp:0.0} 메가픽셀 · 원본 {ns[0]!.GetValue<int>()}×{ns[1]!.GetValue<int>()}"
                    : $"{mp:0.0} 메가픽셀";
            }

            LensText.Text = BuildLensText();
        }
        finally { _suppress = false; }
    }

    private static (double, double) FovLimit(string projection) => projection switch
    {
        "cylindrical" or "mercator" => (360, 170),
        "rectilinear" => (170, 170),
        "stereographic" => (358, 358),
        "fisheye" => (360, 360),
        _ => (360, 180),
    };

    private string BuildLensText()
    {
        var lenses = _project?["lenses"]?.AsObject();
        var users = _project?["lens_users"]?.AsObject();
        var images = _project?["images"]?.AsObject();
        if (lenses is null || lenses.Count == 0) return "사진을 추가하면 렌즈를 읽습니다";

        var lines = new List<string>();
        foreach (var kv in lenses.OrderBy(k => k.Key))
        {
            double fov = kv.Value?["fov"]?.GetValue<double>() ?? 0;
            var ids = users?[kv.Key] as JsonArray;
            string name = "이름 없는 렌즈";
            double? focal = null;
            if (ids is { Count: > 0 } && images?[ids[0]!.GetValue<int>().ToString()] is JsonNode im)
            {
                var exif = im["exif"];
                string? lens = exif?["lens"]?.GetValue<string>();
                string? make = exif?["make"]?.GetValue<string>();
                string? model = exif?["model"]?.GetValue<string>();
                if (!string.IsNullOrWhiteSpace(lens)) name = lens!;
                else if (!string.IsNullOrWhiteSpace(model)) name = $"{make} {model}".Trim();
                focal = exif?["focal_length"]?.GetValue<double>();
            }
            string detail = $"화각 {fov:0.00}°";
            if (focal is > 0) detail += $" · {focal:0}mm";
            detail += $" · {ids?.Count ?? 0}장";
            lines.Add(name);
            lines.Add("   " + detail);
        }
        return string.Join("\n", lines);
    }

    private void UpdatePanels()
    {
        bool hasPhotos = _photos.Count > 0;
        EmptyPanel.Visibility = hasPhotos ? Visibility.Collapsed : Visibility.Visible;
        bool needAlign = hasPhotos && !Optimized;
        AlignPanel.Visibility = needAlign ? Visibility.Visible : Visibility.Collapsed;
        if (needAlign) AlignTitle.Text = $"사진 {_photos.Count}장 준비됨";
        PreviewScroll.Visibility = PreviewImage.Source != null && !needAlign
            ? Visibility.Visible : Visibility.Collapsed;
    }

    private void UpdateStatus()
    {
        if (_photos.Count == 0) { SetStatus(""); return; }
        var parts = new List<string> { $"사진 {_photos.Count}장" };
        if (Optimized)
        {
            int cps = _project?["cp_stats"]?["enabled"]?.GetValue<int>() ?? 0;
            parts.Add($"제어점 {cps}");
            parts.Add($"RMS {_project?["last_rms"]?.GetValue<double>() ?? 0:0.00}px");
            if (_project?["cp_stats"]?["groups"] is JsonArray g && g.Count > 1)
                parts.Add($"{g.Count}조각으로 끊김");
        }
        bool dirty = _pending && Optimized;
        if (dirty) parts.Add("변경사항 있음 — Ctrl+Y로 미리보기 갱신");
        RefreshLabel.Text = dirty ? "갱신 필요" : "미리보기 갱신";
        SetStatus(string.Join(" · ", parts));
    }

    private void SetStatus(string text) => StatusText.Text = text;

    private async Task ReportAsync(string message)
    {
        var dlg = new ContentDialog
        {
            XamlRoot = Root.XamlRoot,
            Title = "문제가 생겼습니다",
            Content = message,
            CloseButtonText = "확인",
        };
        await dlg.ShowAsync();
    }

    // ---------------------------------------------------------------- 사진과 작업

    private nint Hwnd => WinRT.Interop.WindowNative.GetWindowHandle(this);

    /// <summary>엔진이 읽을 수 있는 확장자. 드롭한 폴더를 훑을 때도 쓴다.</summary>
    private static readonly HashSet<string> ImageExtensions = new(StringComparer.OrdinalIgnoreCase)
    {
        ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp",
        ".arw", ".srf", ".sr2", ".cr2", ".cr3", ".nef", ".nrw", ".dng", ".raf",
        ".orf", ".rw2", ".pef", ".srw", ".erf", ".kdc", ".dcr", ".3fr", ".iiq",
    };

    private async void AddPhotos_Click(object sender, RoutedEventArgs e)
    {
        var picker = new FileOpenPicker { ViewMode = PickerViewMode.Thumbnail };
        foreach (var ext in ImageExtensions) picker.FileTypeFilter.Add(ext);
        WinRT.Interop.InitializeWithWindow.Initialize(picker, Hwnd);
        var files = await picker.PickMultipleFilesAsync();
        if (files is null || files.Count == 0) return;
        await AddPathsAsync(files.Select(f => f.Path).ToList());
    }

    /// <summary>파일 선택창과 끌어다 놓기가 함께 쓰는 경로.</summary>
    private async Task AddPathsAsync(IReadOnlyList<string> paths)
    {
        if (paths.Count == 0) return;
        try
        {
            var r = await RunJobAsync("사진 읽는 중", "/api/images/add", new { paths });
            await RefreshAsync();
            if ((r?["added"] as JsonArray)?.Count is null or 0)
                await ReportAsync("추가된 사진이 없습니다. 이미 들어 있거나 읽을 수 없는 형식입니다.");
        }
        catch (Exception ex) { await ReportAsync(ex.Message); }
    }

    // ---------------------------------------------------------------- 끌어다 놓기

    /// <summary>놓인 것 중 사진만 고른다. 폴더는 그 안을 한 겹만 훑는다.</summary>
    private static List<string> AcceptableFiles(IEnumerable<IStorageItem> items)
    {
        var out_ = new List<string>();
        foreach (var item in items)
        {
            if (string.IsNullOrEmpty(item.Path)) continue;
            if (item is IStorageFolder)
            {
                try
                {
                    out_.AddRange(Directory.EnumerateFiles(item.Path)
                        .Where(f => ImageExtensions.Contains(Path.GetExtension(f))));
                }
                catch { }
            }
            else if (ImageExtensions.Contains(Path.GetExtension(item.Path)))
            {
                out_.Add(item.Path);
            }
        }
        out_.Sort(StringComparer.OrdinalIgnoreCase);
        return out_;
    }

    private async void Root_DragOver(object sender, DragEventArgs e)
    {
        if (!e.DataView.Contains(StandardDataFormats.StorageItems)) return;
        var deferral = e.GetDeferral();
        try
        {
            var files = AcceptableFiles(await e.DataView.GetStorageItemsAsync());
            if (files.Count == 0)
            {
                e.AcceptedOperation = DataPackageOperation.None;
                DropOverlay.Visibility = Visibility.Collapsed;
                return;
            }
            e.AcceptedOperation = DataPackageOperation.Copy;
            e.DragUIOverride.Caption = "사진 추가";
            DropLabel.Text = files.Count == 1 ? "사진 1장 추가" : $"사진 {files.Count}장 추가";
            DropOverlay.Visibility = Visibility.Visible;
        }
        catch { }
        finally { deferral.Complete(); }
    }

    private void Root_DragLeave(object sender, DragEventArgs e) => DropOverlay.Visibility = Visibility.Collapsed;

    private async void Root_Drop(object sender, DragEventArgs e)
    {
        DropOverlay.Visibility = Visibility.Collapsed;
        if (!e.DataView.Contains(StandardDataFormats.StorageItems)) return;
        List<string> files;
        var deferral = e.GetDeferral();
        try { files = AcceptableFiles(await e.DataView.GetStorageItemsAsync()); }
        catch { files = new(); }
        finally { deferral.Complete(); }
        await AddPathsAsync(files);
    }

    // ---------------------------------------------------------------- 사진 목록 메뉴

    private void PhotoList_ContextRequested(UIElement sender, ContextRequestedEventArgs e)
    {
        // 오른쪽 클릭한 줄을 찾는다. 선택과는 따로다.
        DependencyObject? d = e.OriginalSource as DependencyObject;
        while (d is not null and not ListViewItem) d = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetParent(d);
        if (d is not ListViewItem lvi || PhotoList.ItemFromContainer(lvi) is not PhotoItem item) return;

        var menu = new MenuFlyout();
        var toggle = new MenuFlyoutItem { Text = item.Enabled ? "사용 안 함" : "다시 사용" };
        toggle.Click += (_, _) => _ = MutateAsync(() =>
            _engine.PatchAsync($"/api/images/{item.Id}", new { enabled = !item.Enabled }));
        var anchor = new MenuFlyoutItem { Text = "기준 사진으로", IsEnabled = !item.IsAnchor };
        anchor.Click += (_, _) => _ = MutateAsync(() =>
            _engine.PatchAsync($"/api/images/{item.Id}", new { anchor = true }));
        var remove = new MenuFlyoutItem { Text = "프로젝트에서 제거", Icon = new FontIcon { Glyph = "\uE74D" } };
        remove.Click += (_, _) => _ = MutateAsync(() => _engine.DeleteAsync($"/api/images/{item.Id}"));
        menu.Items.Add(toggle);
        menu.Items.Add(anchor);
        menu.Items.Add(new MenuFlyoutSeparator());
        menu.Items.Add(remove);

        if (e.TryGetPosition(lvi, out var pos)) menu.ShowAt(lvi, pos);
        else menu.ShowAt(lvi);
        e.Handled = true;
    }

    /// <summary>
    /// 사진을 끄거나 빼거나 기준을 바꾼다. 그림에 영향이 있으니 갱신 필요로 표시하되,
    /// 그리는 것은 사용자가 정한다 (다른 설정과 같다).
    /// </summary>
    private async Task MutateAsync(Func<Task> work)
    {
        try
        {
            await work();
            _pending = true;
            await RefreshAsync();
        }
        catch (Exception ex) { await ReportAsync(ex.Message); }
    }

    // ---------------------------------------------------------------- 미리보기 / 제어점 전환

    private void Mode_Changed(SelectorBar sender, SelectorBarSelectionChangedEventArgs args)
    {
        // XAML 이 처음 고른 항목으로도 불린다. 그때는 아직 칸들이 연결되기 전이다.
        if (Editor is null || PreviewPane is null || !_started) return;
        _ = ShowModeAsync(sender.SelectedItem == CPModeItem);
    }

    private async Task ShowModeAsync(bool controlPoints)
    {
        if (controlPoints && _photos.Count(p => p.Enabled) < 2)
        {
            ModeBar.SelectedItem = PreviewModeItem;
            await ReportAsync("켜진 사진이 두 장 이상 있어야 제어점을 볼 수 있습니다");
            return;
        }
        Editor.Visibility = controlPoints ? Visibility.Visible : Visibility.Collapsed;
        PreviewPane.Visibility = controlPoints ? Visibility.Collapsed : Visibility.Visible;
        if (controlPoints)
        {
            await UpdateEditorAsync();
            Editor.Focus(FocusState.Programmatic);
        }
    }

    private Task UpdateEditorAsync()
        => Editor.UpdateAsync(_photos.Where(p => p.Enabled)
                                     .Select(p => new EditorImage(p.Id, p.Name, p.Width, p.Height))
                                     .ToList(), Optimized);

    private async void Align_Click(object sender, RoutedEventArgs e)
    {
        if (_photos.Count < 2) { await ReportAsync("사진이 두 장 이상 필요합니다"); return; }
        try
        {
            await RunJobAsync("자동 정렬", "/api/align",
                              new { detect = true, mode = "full", straighten = true });
            _intended = null;
            _orientGen++;
            await RefreshAsync();
            await RenderPreviewAsync();
        }
        catch (Exception ex) { await ReportAsync(ex.Message); }
    }

    private async void Optimize_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            await RunJobAsync("최적화", "/api/optimize", new { mode = "full" });
            await RefreshAsync();
            await RenderPreviewAsync();
        }
        catch (Exception ex) { await ReportAsync(ex.Message); }
    }

    private async void Straighten_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            await _engine.PostAsync("/api/straighten");
            await RefreshAsync();
            await RenderPreviewAsync();
        }
        catch (Exception ex) { await ReportAsync(ex.Message); }
    }

    /// <summary>진행 창을 띄우고 작업이 끝날 때까지 기다린다. 취소를 받을 수도 있다.</summary>
    private async Task<JsonNode?> RunJobAsync(string title, string path, object? body,
                                              bool cancellable = false)
    {
        var bar = new ProgressBar { Minimum = 0, Maximum = 1, Width = 280 };
        var msg = new TextBlock { Text = "…", Opacity = 0.7 };
        var dlg = new ContentDialog
        {
            XamlRoot = Root.XamlRoot,
            Title = title,
            Content = new StackPanel { Spacing = 10, Children = { bar, msg } },
        };
        string? jid = null;
        if (cancellable)
        {
            dlg.CloseButtonText = "취소";
            dlg.CloseButtonClick += (_, _) => { if (jid != null) _ = _engine.CancelJobAsync(jid); };
        }
        _ = dlg.ShowAsync();
        try
        {
            return await _engine.RunAsync(path, body,
                (f, m) => { bar.Value = f; msg.Text = m; },
                id => jid = id);
        }
        finally { dlg.Hide(); }
    }

    // ---------------------------------------------------------------- 미리보기

    private void RefreshPreview_Click(object sender, RoutedEventArgs e) => _ = RenderPreviewAsync();

    private async Task RenderPreviewAsync((double, double, double)? center = null)
    {
        if (_photos.Count == 0 || !Optimized || _busy) return;
        _busy = true;
        int gen = _orientGen;
        if (PreviewImage.Source is null) { RenderPanel.Visibility = Visibility.Visible; }
        RenderBar.Value = 0;
        RenderMsg.Text = "…";
        try
        {
            object body = center is { } c
                ? new { max_dim = 1700, center_yaw = c.Item1, center_pitch = c.Item2, center_roll = c.Item3 }
                : new { max_dim = 1700 };
            var r = await _engine.RunAsync("/api/preview", body,
                (f, m) =>
                {
                    if (_liveActive) return;      // 끄는 중에는 진행 표시로 덮지 않는다
                    RenderBar.Value = f;
                    RenderMsg.Text = m;
                    SetStatus(string.IsNullOrEmpty(m) ? "미리보기 만드는 중…" : $"{m}  {f * 100:0}%");
                },
                id => _previewJob = id);
            if (r is null) return;
            _preview = r;

            // 그사이 방향이 또 바뀌었거나 끄는 중이면 이미 옛 그림이다. 올리면 화면이 뒤로 튄다.
            bool stale = gen != _orientGen || _liveActive;
            if (!stale)
            {
                _pending = false;
                string url = r["url"]!.GetValue<string>();
                var bytes = await _engine.BytesAsync(url);
                PreviewImage.Source = await ToBitmapAsync(bytes);
                RenderPanel.Visibility = Visibility.Collapsed;
                PreviewStatus.Text = $"출력 {r["full_size"]![0]!.GetValue<int>()}×{r["full_size"]![1]!.GetValue<int>()}"
                                   + $" · {r["megapixels"]!.GetValue<double>():0.0} MP";
                ZoomFit();
            }
            await RefreshAsync();
            _ = _engine.PostAsync("/api/live/prepare");   // 끌기용 원판을 뒤에서 미리 만든다
        }
        catch (Engine.JobCancelledException) { }
        catch (Exception ex) { await ReportAsync(ex.Message); }
        finally
        {
            _busy = false;
            _previewJob = null;
            RenderPanel.Visibility = PreviewImage.Source is null && Optimized && _photos.Count > 0
                ? RenderPanel.Visibility : Visibility.Collapsed;
            UpdatePanels();
            UpdateStatus();
        }
    }

    private (double Yaw, double Pitch, double Roll) CurrentOrientation()
    {
        if (_intended is { } o) return o;
        var l = _project?["layout"];
        const double deg = 180.0 / Math.PI;
        return ((l?["center_yaw"]?.GetValue<double>() ?? 0) * deg,
                (l?["center_pitch"]?.GetValue<double>() ?? 0) * deg,
                (l?["center_roll"]?.GetValue<double>() ?? 0) * deg);
    }

    private void ApplyOrientation(double yaw, double pitch, double roll)
    {
        _intended = (yaw, pitch, roll);
        _orientGen++;
        if (_previewJob is { } j) { _ = _engine.CancelJobAsync(j); }   // 버릴 그림은 끊는다
        _ = ApplyOrientationLoopAsync();
    }

    private bool _orientRunning;
    private async Task ApplyOrientationLoopAsync()
    {
        if (_orientRunning) return;
        _orientRunning = true;
        try
        {
            while (_intended is { } q)
            {
                int gen = _orientGen;
                while (_busy) await Task.Delay(120);
                await RenderPreviewAsync((q.Yaw, q.Pitch, q.Roll));
                if (gen == _orientGen) break;    // 그사이 또 바뀌었으면 한 번 더
            }
        }
        finally { _orientRunning = false; }
    }

    // ---------------------------------------------------------------- 끌어서 방향 돌리기

    private void Preview_PointerPressed(object sender, PointerRoutedEventArgs e)
    {
        if (!Optimized || _preview is null) return;
        var pt = e.GetCurrentPoint(Root);
        _dragRoll = pt.Properties.IsRightButtonPressed ||
                    Microsoft.UI.Input.InputKeyboardSource
                        .GetKeyStateForCurrentThread(Windows.System.VirtualKey.Menu)
                        .HasFlag(Windows.UI.Core.CoreVirtualKeyStates.Down);
        _dragStart = pt.Position;
        _dragBase = CurrentOrientation();
        _dragShownWidth = Math.Max(1, PreviewImage.ActualWidth * PreviewScroll.ZoomFactor);
        _dragging = true;
        PreviewImage.CapturePointer(e.Pointer);
        BeginLive();
    }

    private void Preview_PointerMoved(object sender, PointerRoutedEventArgs e)
    {
        if (!_dragging) return;
        var p = e.GetCurrentPoint(Root).Position;
        double h = HfovBox.Value, v = VfovBox.Value;
        if (_dragRoll)
        {
            // 그림 가운데를 축으로 시작점에서 지금 점까지 돈 각도 (화면 좌표는 아래가 +)
            var c = new Windows.Foundation.Point(
                PreviewScroll.ActualWidth / 2, PreviewScroll.ActualHeight / 2);
            double a0 = Math.Atan2(-(_dragStart.Y - c.Y), _dragStart.X - c.X);
            double a1 = Math.Atan2(-(p.Y - c.Y), p.X - c.X);
            double d = (a1 - a0) * 180 / Math.PI;
            if (d > 180) d -= 360; else if (d < -180) d += 360;
            ShowLive((_dragBase.Yaw, _dragBase.Pitch, Wrap180(_dragBase.Roll + d), h, v));
        }
        else
        {
            // 보이는 폭 전체가 몇 도인지로 끈 거리를 각도로 바꾼다.
            double span = (_preview?["deg_per_px"]?.GetValue<double>() ?? 0.2)
                        * (_preview?["width"]?.GetValue<double>() ?? 1700);
            double dx = (p.X - _dragStart.X) / _dragShownWidth;
            double dy = (p.Y - _dragStart.Y) / _dragShownWidth;
            double yaw = Wrap180(_dragBase.Yaw - dx * span);
            double pitch = Math.Clamp(_dragBase.Pitch + dy * span, -90, 90);
            ShowLive((yaw, pitch, _dragBase.Roll, h, v));
        }
    }

    private void Preview_PointerReleased(object sender, PointerRoutedEventArgs e)
    {
        if (!_dragging) return;
        _dragging = false;
        PreviewImage.ReleasePointerCapture(e.Pointer);
        var t = EndLive();
        if (t is { } v) ApplyOrientation(v.Yaw, v.Pitch, v.Roll);
    }

    private static double Wrap180(double a) => (a + 540) % 360 - 180;

    private void BeginLive()
    {
        if (_liveActive) return;
        _liveActive = true;
        _ = _engine.PostAsync("/api/live/prepare");
    }

    private (double Yaw, double Pitch, double Roll, double H, double V)? EndLive()
    {
        _liveActive = false;
        var t = _liveTarget;
        _liveTarget = null;
        return t;
    }

    private void ShowLive((double Yaw, double Pitch, double Roll, double H, double V) target)
    {
        if (!Optimized || _preview is null) return;
        BeginLive();
        _liveTarget = target;
        _suppress = true;
        YawSlider.Value = YawBox.Value = Math.Round(target.Yaw, 1);
        PitchSlider.Value = PitchBox.Value = Math.Round(target.Pitch, 1);
        RollSlider.Value = RollBox.Value = Math.Round(target.Roll, 1);
        if (target.H > 0) { HfovSlider.Value = HfovBox.Value = target.H; }
        if (target.V > 0) { VfovSlider.Value = VfovBox.Value = target.V; }
        _suppress = false;
        _ = PumpLiveAsync();
    }

    /// <summary>한 번에 요청 하나만. 쌓으면 손은 멈췄는데 그림이 한참 뒤따라온다.</summary>
    private async Task PumpLiveAsync()
    {
        if (_liveInFlight || !_liveActive || _liveTarget is null) return;
        _liveInFlight = true;
        try
        {
            while (_liveActive && _liveTarget is { } t)
            {
                var bytes = await _engine.LiveFrameAsync(new
                {
                    yaw = t.Yaw, pitch = t.Pitch, roll = t.Roll, hfov = t.H, vfov = t.V,
                });
                if (bytes is null)
                {
                    SetStatus("움직임 미리보기 준비 중… 손을 떼면 그 값으로 그립니다");
                    await Task.Delay(300);
                }
                else if (_liveActive)
                {
                    PreviewImage.Source = await ToBitmapAsync(bytes);
                }
                if (_liveTarget is { } now && now.Equals(t)) break;
            }
        }
        catch { }
        finally { _liveInFlight = false; }
    }

    // ---------------------------------------------------------------- 속성 조작

    /// <summary>미리보기 그림 자체는 그대로인 설정들 — 굳이 다시 그리게 하지 않는다.</summary>
    private static readonly HashSet<string> ExportOnly = new()
        { "out_width", "scale_percent", "format", "quality", "gpano", "interpolation" };

    private async Task ApplySettingAsync(string key, object value)
    {
        if (_suppress) return;
        try
        {
            await _engine.PatchAsync("/api/settings", new Dictionary<string, object> { [key] = value });
            await RefreshAsync();
            if (!ExportOnly.Contains(key)) { _pending = true; UpdateStatus(); }
        }
        catch (Exception ex) { await ReportAsync(ex.Message); }
    }

    private void Projection_Changed(object s, SelectionChangedEventArgs e)
    {
        if (_suppress || ProjectionCombo.SelectedIndex < 0) return;
        _ = ApplySettingAsync("projection", ProjKeys[ProjectionCombo.SelectedIndex]);
    }
    private void Seam_Changed(object s, SelectionChangedEventArgs e)
    {
        if (_suppress || SeamCombo.SelectedIndex < 0) return;
        _ = ApplySettingAsync("seam", SeamKeys[SeamCombo.SelectedIndex]);
    }
    private void Blend_Changed(object s, SelectionChangedEventArgs e)
    {
        if (_suppress || BlendCombo.SelectedIndex < 0) return;
        _ = ApplySettingAsync("blender", BlendKeys[BlendCombo.SelectedIndex]);
    }
    private void Exposure_Changed(object s, SelectionChangedEventArgs e)
    {
        if (_suppress || ExposureCombo.SelectedIndex < 0) return;
        _ = ApplySettingAsync("exposure", ExposureCombo.SelectedIndex == 0 ? "auto" : "none");
    }
    private void Wb_Click(object s, RoutedEventArgs e) => _ = ApplySettingAsync("per_channel", WbCheck.IsChecked == true);
    private void Vig_Click(object s, RoutedEventArgs e) => _ = ApplySettingAsync("vignetting", VigCheck.IsChecked == true);
    private void Nadir_Click(object s, RoutedEventArgs e) => _ = ApplySettingAsync("patch_nadir", NadirCheck.IsChecked == true);
    private void FillGaps_Click(object s, RoutedEventArgs e) => _ = ApplySettingAsync("fill_gaps", FillGapsCheck.IsChecked == true);
    private void Quality_Changed(object s, RangeBaseValueChangedEventArgs e)
        => _ = ApplySettingAsync("quality", (int)QualitySlider.Value);
    private void Format_Changed(object s, SelectionChangedEventArgs e)
    {
        if (_suppress || FormatCombo.SelectedIndex < 0) return;
        _ = ApplySettingAsync("format", FormatCombo.SelectedIndex switch { 1 => "tif", 2 => "png", _ => "jpg" });
    }

    private void Planet_Click(object s, RoutedEventArgs e) => _ = PlanetAsync(-90);
    private void Tunnel_Click(object s, RoutedEventArgs e) => _ = PlanetAsync(90);
    private async Task PlanetAsync(double pitch)
    {
        try
        {
            await _engine.PatchAsync("/api/settings", new { projection = "stereographic" });
            await RefreshAsync();
            ApplyOrientation(0, pitch, 0);
        }
        catch (Exception ex) { await ReportAsync(ex.Message); }
    }

    // 방향
    private void Orient_SliderChanged(object s, RangeBaseValueChangedEventArgs e)
    {
        if (_suppress) return;
        _suppress = true;
        YawBox.Value = Math.Round(YawSlider.Value, 1);
        PitchBox.Value = Math.Round(PitchSlider.Value, 1);
        RollBox.Value = Math.Round(RollSlider.Value, 1);
        _suppress = false;
        ShowLive((YawSlider.Value, PitchSlider.Value, RollSlider.Value, HfovBox.Value, VfovBox.Value));
    }
    private void Orient_BoxChanged(object s, NumberBoxValueChangedEventArgs e)
    {
        if (_suppress || double.IsNaN(e.NewValue)) return;
        _suppress = true;
        YawSlider.Value = YawBox.Value; PitchSlider.Value = PitchBox.Value; RollSlider.Value = RollBox.Value;
        _suppress = false;
        EndLive();
        ApplyOrientation(YawBox.Value, PitchBox.Value, RollBox.Value);
    }
    private void ApplyOrient_Click(object s, RoutedEventArgs e)
    {
        EndLive();
        ApplyOrientation(YawBox.Value, PitchBox.Value, RollBox.Value);
    }
    private void ResetOrient_Click(object s, RoutedEventArgs e)
    {
        EndLive();
        ApplyOrientation(0, 0, 0);
    }

    // 화각
    private void Fov_SliderChanged(object s, RangeBaseValueChangedEventArgs e)
    {
        if (_suppress) return;
        _suppress = true;
        HfovBox.Value = Math.Round(HfovSlider.Value, 1);
        VfovBox.Value = Math.Round(VfovSlider.Value, 1);
        _suppress = false;
        var o = CurrentOrientation();
        ShowLive((o.Yaw, o.Pitch, o.Roll, HfovSlider.Value, VfovSlider.Value));
    }
    private void Fov_BoxChanged(object s, NumberBoxValueChangedEventArgs e)
    {
        if (_suppress || double.IsNaN(e.NewValue)) return;
        _suppress = true;
        HfovSlider.Value = HfovBox.Value; VfovSlider.Value = VfovBox.Value;
        _suppress = false;
        _ = ApplyFovAsync(HfovBox.Value, VfovBox.Value);
    }
    private async Task ApplyFovAsync(double h, double v)
    {
        EndLive();
        try
        {
            await _engine.PatchAsync("/api/settings", new { hfov = h, vfov = v });
            await RefreshAsync();
            var o = CurrentOrientation();
            ApplyOrientation(o.Yaw, o.Pitch, o.Roll);
        }
        catch (Exception ex) { await ReportAsync(ex.Message); }
    }
    private void FitFov_Click(object s, RoutedEventArgs e) => _ = ApplyFovAsync(0, 0);

    // 출력 크기
    private void Width_Changed(object s, NumberBoxValueChangedEventArgs e)
    {
        if (_suppress || double.IsNaN(e.NewValue) || e.NewValue < 64) return;
        _ = ApplySettingAsync("out_width", (int)e.NewValue);
    }
    private void Height_Changed(object s, NumberBoxValueChangedEventArgs e)
    {
        if (_suppress || double.IsNaN(e.NewValue) || e.NewValue < 64) return;
        if (_layout?["full_size"] is not JsonArray fs || fs.Count < 2) return;
        double aspect = fs[0]!.GetValue<double>() / Math.Max(1, fs[1]!.GetValue<double>());
        _ = ApplySettingAsync("out_width", (int)Math.Round(e.NewValue * aspect));
    }
    private void NativeSize_Click(object s, RoutedEventArgs e) => _ = ApplySettingAsync("out_width", 0);

    // ---------------------------------------------------------------- 확대와 내보내기

    private void ZoomFit_Click(object s, RoutedEventArgs e) => ZoomFit();
    private void ZoomIn_Click(object s, RoutedEventArgs e)
        => PreviewScroll.ChangeView(null, null, PreviewScroll.ZoomFactor * 1.3f);
    private void ZoomOut_Click(object s, RoutedEventArgs e)
        => PreviewScroll.ChangeView(null, null, PreviewScroll.ZoomFactor / 1.3f);

    private void ZoomFit()
    {
        if (PreviewImage.Source is not BitmapImage b || b.PixelWidth == 0) return;
        double w = PreviewScroll.ViewportWidth, h = PreviewScroll.ViewportHeight;
        if (w < 2 || h < 2) return;
        float f = (float)Math.Min(Math.Min(w / b.PixelWidth, h / b.PixelHeight), 1.0);
        PreviewScroll.ChangeView(null, null, Math.Max(f, PreviewScroll.MinZoomFactor));
    }

    private async void Export_Click(object sender, RoutedEventArgs e)
    {
        if (!Optimized) { await ReportAsync("먼저 자동 정렬을 해주세요"); return; }
        string fmt = SettingStr("format", "jpg");
        var picker = new FileSavePicker { SuggestedFileName = "panorama" };
        picker.FileTypeChoices.Add(fmt.ToUpperInvariant(), new List<string> { "." + fmt });
        WinRT.Interop.InitializeWithWindow.Initialize(picker, Hwnd);
        var file = await picker.PickSaveFileAsync();
        if (file is null) return;

        try
        {
            var r = await RunJobAsync("내보내는 중", "/api/render", new { path = file.Path }, cancellable: true);
            if (r?["path"]?.GetValue<string>() is { } p)
                await _engine.PostAsync("/api/reveal", new { path = p });
        }
        catch (Engine.JobCancelledException) { SetStatus("내보내기를 취소했습니다"); }
        catch (Exception ex) { await ReportAsync(ex.Message); }
    }
}
