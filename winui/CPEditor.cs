using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Microsoft.UI;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using Microsoft.UI.Xaml.Shapes;
using Windows.Foundation;
using Color = Windows.UI.Color;

namespace Meridian;

/// <summary>제어점 편집기에 넘기는 사진 한 장.</summary>
public sealed record EditorImage(int Id, string Name, int Width, int Height);

/// <summary>
/// 제어점 편집 화면. 맥의 CPEditor/CPPairView 를 따른다.
///
/// 두 사진을 나란히 놓고, 왼쪽에서 한 점을 찍으면 오른쪽에서 짝을 찾아 준다.
/// 자동 정렬이 실패한 자리를 사람이 직접 이어 주는 곳이라, 확대경과 잔차 색을
/// 함께 보여 주는 것이 중요하다. 좌표는 언제나 '원본 픽셀' 로 주고받는다.
/// </summary>
public sealed class CPEditor : UserControl
{
    public enum Side { Left, Right }

    private sealed class Marker
    {
        public int Index;          // 프로젝트 전체에서의 제어점 번호
        public int Number;         // 이 쌍 안에서의 순번 (화면에 보이는 숫자)
        public Point Point;        // 원본 픽셀 좌표
        public double Error;
        public bool Enabled;
    }

    /// <summary>한쪽 칸.</summary>
    private sealed class Pane
    {
        public readonly Canvas Surface = new();
        public readonly Image Image = new() { Stretch = Stretch.Fill };
        public readonly Canvas Overlay = new() { IsHitTestVisible = false };
        public readonly TextBlock Caption = new() { FontSize = 11, Foreground = new SolidColorBrush(Colors.White) };
        public readonly Border CaptionBox = new()
        {
            Background = new SolidColorBrush(ColorHelper.FromArgb(140, 0, 0, 0)),
            CornerRadius = new CornerRadius(5), Padding = new Thickness(7, 3, 7, 3),
        };
        public readonly TextBlock Placeholder = new() { Text = "사진을 고르세요", Opacity = 0.45 };
        public readonly StackPanel Strip = new() { Orientation = Orientation.Horizontal, Spacing = 5, Padding = new Thickness(6) };
        public readonly ScrollViewer StripScroll = new()
        {
            HorizontalScrollMode = ScrollMode.Enabled, VerticalScrollMode = ScrollMode.Disabled,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
            VerticalScrollBarVisibility = ScrollBarVisibility.Disabled,
        };
        public BitmapImage? Bitmap;
        public Size Src = new(1, 1);
        public List<Marker> Markers = new();
        public double Zoom;
        public Point Offset;
        public bool UserAdjusted;

        public double ProxyScale => Bitmap is { PixelWidth: > 0 } b && Src.Width > 0 ? b.PixelWidth / Src.Width : 1;

        public Point ToView(Point src)
        {
            double k = ProxyScale;
            return new Point(Offset.X + src.X * k * Zoom, Offset.Y + src.Y * k * Zoom);
        }

        public Point ToSource(Point view)
        {
            double k = ProxyScale;
            if (Zoom <= 0 || k <= 0) return default;
            return new Point((view.X - Offset.X) / Zoom / k, (view.Y - Offset.Y) / Zoom / k);
        }
    }

    public Engine? Engine { get; set; }
    /// <summary>제어점이 바뀌어 프로젝트를 다시 읽어야 할 때.</summary>
    public event Action? Changed;

    private readonly Pane _left = new(), _right = new();
    private readonly ComboBox _kind = new() { Width = 100 };
    private readonly TextBlock _hint = new() { Opacity = 0.7, TextTrimming = TextTrimming.CharacterEllipsis, VerticalAlignment = VerticalAlignment.Center };
    private readonly TextBlock _summary = new()
    {
        Opacity = 0.6, FontSize = 11, FontFamily = new FontFamily("Consolas"),
        VerticalAlignment = VerticalAlignment.Center, TextTrimming = TextTrimming.CharacterEllipsis,
    };

    // 확대경 — 한 화소를 다투는 작업이라 꼭 필요하다
    private const double LoupeSize = 132, LoupeMag = 6;
    private readonly Canvas _loupeLayer = new() { IsHitTestVisible = false };
    private readonly Border _loupe = new()
    {
        Width = LoupeSize, Height = LoupeSize, CornerRadius = new CornerRadius(8),
        BorderThickness = new Thickness(1), Background = new SolidColorBrush(Colors.Black),
        Visibility = Visibility.Collapsed,
    };
    private readonly Canvas _loupeCanvas = new() { Width = LoupeSize, Height = LoupeSize };
    private readonly Image _loupeImage = new() { Stretch = Stretch.Fill };

    private List<EditorImage> _images = new();
    private readonly Dictionary<int, BitmapImage?> _thumbs = new();
    private List<JsonNode> _points = new();
    private int? _pairA, _pairB;
    private Point? _pendingLeft;
    private int? _selected;
    private bool _busy, _aligned;
    private int _loadGen;

    // 끌기
    private enum DragKind { None, Pan, Move }
    private DragKind _drag;
    private Side _dragSide;
    private Point _dragMouse, _dragOffset;
    private Marker? _dragMarker;
    private bool _dragMoved;

    private static readonly Color[] Palette =
    {
        ColorHelper.FromArgb(255, 255, 204, 0), ColorHelper.FromArgb(255, 0, 122, 255), ColorHelper.FromArgb(255, 255, 45, 85),
        ColorHelper.FromArgb(255, 142, 142, 147), ColorHelper.FromArgb(255, 52, 199, 89), ColorHelper.FromArgb(255, 0, 199, 190),
        ColorHelper.FromArgb(255, 255, 149, 0), ColorHelper.FromArgb(255, 255, 59, 48), ColorHelper.FromArgb(255, 88, 86, 214),
        ColorHelper.FromArgb(255, 48, 176, 199), ColorHelper.FromArgb(255, 175, 82, 222), ColorHelper.FromArgb(255, 162, 132, 94),
    };

    /// <summary>짝을 눈으로 잇기 위한 색. 같은 번호는 양쪽에서 같은 색으로 찍힌다.</summary>
    private static Color MarkerColor(int number) => Palette[Math.Max(0, number - 1) % Palette.Length];

    private static Brush Accent =>
        Application.Current.Resources.TryGetValue("AccentFillColorDefaultBrush", out var b) && b is Brush br
            ? br : new SolidColorBrush(ColorHelper.FromArgb(255, 0, 120, 212));

    private static Brush Themed(string key, Color fallback) =>
        Application.Current.Resources.TryGetValue(key, out var b) && b is Brush br ? br : new SolidColorBrush(fallback);

    public CPEditor()
    {
        IsTabStop = true;
        var root = new Grid();
        root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(78) });
        root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(44) });
        root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(8) });
        root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

        foreach (var (pane, side, col) in new[] { (_left, Side.Left, 0), (_right, Side.Right, 2) })
        {
            pane.StripScroll.Content = pane.Strip;
            Grid.SetRow(pane.StripScroll, 0);
            Grid.SetColumn(pane.StripScroll, col);
            root.Children.Add(pane.StripScroll);

            var host = BuildPane(pane, side);
            Grid.SetRow(host, 1);
            Grid.SetColumn(host, col);
            root.Children.Add(host);
        }

        var divider = new Rectangle { Width = 1, Fill = Themed("CardStrokeColorDefaultBrush", Colors.Gray) };
        Grid.SetColumn(divider, 1);
        Grid.SetRowSpan(divider, 2);
        root.Children.Add(divider);

        _loupeCanvas.Children.Add(_loupeImage);
        _loupe.Child = _loupeCanvas;
        _loupe.BorderBrush = Accent;
        _loupeLayer.Children.Add(_loupe);
        Grid.SetRow(_loupeLayer, 1);
        Grid.SetColumnSpan(_loupeLayer, 3);
        root.Children.Add(_loupeLayer);

        var bar = BuildToolStrip();
        Grid.SetRow(bar, 2);
        Grid.SetColumnSpan(bar, 3);
        root.Children.Add(bar);

        Content = root;
        KeyDown += OnKeyDown;
        RefreshHint();
    }

    private FrameworkElement BuildPane(Pane p, Side side)
    {
        p.Surface.Background = Themed("SolidBackgroundFillColorBaseBrush", ColorHelper.FromArgb(255, 32, 32, 32));
        p.Surface.Children.Add(p.Image);
        p.Surface.Children.Add(p.Overlay);
        p.CaptionBox.Child = p.Caption;
        Canvas.SetLeft(p.CaptionBox, 10);
        Canvas.SetTop(p.CaptionBox, 8);
        p.CaptionBox.Visibility = Visibility.Collapsed;
        p.Surface.Children.Add(p.CaptionBox);
        p.Surface.Children.Add(p.Placeholder);

        p.Surface.SizeChanged += (_, e) =>
        {
            p.Surface.Clip = new RectangleGeometry { Rect = new Rect(0, 0, e.NewSize.Width, e.NewSize.Height) };
            Canvas.SetLeft(p.Placeholder, e.NewSize.Width / 2 - 45);
            Canvas.SetTop(p.Placeholder, e.NewSize.Height / 2 - 10);
            if (!p.UserAdjusted) Fit(p);
            Redraw(p);
        };
        p.Surface.PointerPressed += (s, e) => OnPressed(side, e);
        p.Surface.PointerMoved += (s, e) => OnMoved(side, e);
        p.Surface.PointerReleased += (s, e) => OnReleased(side, e);
        p.Surface.PointerExited += (s, e) => { if (_drag == DragKind.None) _loupe.Visibility = Visibility.Collapsed; };
        p.Surface.PointerWheelChanged += (s, e) => OnWheel(side, e);
        return p.Surface;
    }

    private FrameworkElement BuildToolStrip()
    {
        var swap = new Button { Content = "⇄ 좌우 바꾸기" };
        swap.Click += (_, _) => SwapPair();

        _kind.ItemsSource = new[] { "보통", "수직선", "수평선" };
        _kind.SelectedIndex = 0;
        ToolTipService.SetToolTip(_kind, "수직선·수평선은 같은 사진 안에서 두 점을 찍습니다");
        _kind.SelectionChanged += (_, _) => { _pendingLeft = null; RedrawAll(); RefreshHint(); };

        var prune = new Button { Content = "나쁜 점 정리" };
        ToolTipService.SetToolTip(prune, "잔차가 큰 제어점을 꺼서 최적화 품질을 올립니다");
        prune.Click += async (_, _) => await PruneAsync();

        var del = new Button { Content = "지우기" };
        ToolTipService.SetToolTip(del, "고른 제어점을 지웁니다 (Delete)");
        del.Click += async (_, _) => await DeleteSelectedAsync();

        var grid = new Grid
        {
            Padding = new Thickness(12, 0, 12, 0), ColumnSpacing = 10,
            BorderBrush = Themed("CardStrokeColorDefaultBrush", Colors.Gray), BorderThickness = new Thickness(0, 1, 0, 0),
        };
        for (int i = 0; i < 4; i++) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        int c = 0;
        foreach (FrameworkElement el in new FrameworkElement[] { swap, _kind, prune, del, _hint, _summary })
        {
            el.VerticalAlignment = VerticalAlignment.Center;
            Grid.SetColumn(el, c++);
            grid.Children.Add(el);
        }
        return grid;
    }

    private string Kind => _kind.SelectedIndex switch { 1 => "vertical", 2 => "horizontal", _ => "manual" };

    // ---------------------------------------------------------------- 상태

    /// <summary>
    /// 사진 목록이 바뀌거나 화면에 나타날 때 부른다. 켜진 사진만 받는다.
    /// 아직 정렬하지 않았으면 그 사실을 알려 준다 — 제어점이 비어 있는 게
    /// 고장이 아니라 순서의 문제라는 걸 알아야 한다.
    /// </summary>
    public async Task UpdateAsync(IReadOnlyList<EditorImage> images, bool aligned)
    {
        _aligned = aligned;
        bool changed = !_images.Select(i => i.Id).SequenceEqual(images.Select(i => i.Id));
        _images = images.ToList();

        bool pairChanged = false;
        if (_pairA is null || !_images.Any(i => i.Id == _pairA))
        {
            _pairA = _images.FirstOrDefault()?.Id;
            pairChanged = true;
        }
        if (_pairB is null || _pairB == _pairA || !_images.Any(i => i.Id == _pairB))
        {
            _pairB = _images.FirstOrDefault(i => i.Id != _pairA)?.Id;
            pairChanged = true;
        }

        await SyncStripsAsync(changed);
        if (changed || pairChanged || _left.Bitmap is null) await LoadPairAsync();
        else await ReloadPointsAsync();
    }

    private async Task SyncStripsAsync(bool rebuild)
    {
        if (Engine is null) return;
        foreach (var (pane, side) in new[] { (_left, Side.Left), (_right, Side.Right) })
        {
            if (rebuild || pane.Strip.Children.Count != _images.Count)
            {
                pane.Strip.Children.Clear();
                foreach (var im in _images)
                {
                    var cell = new Grid { Width = 60, Height = 60 };
                    var img = new Image { Stretch = Stretch.UniformToFill, Source = await ThumbAsync(im.Id) };
                    cell.Children.Add(img);
                    cell.Children.Add(new Border
                    {
                        Background = new SolidColorBrush(ColorHelper.FromArgb(160, 0, 0, 0)),
                        CornerRadius = new CornerRadius(3), Padding = new Thickness(4, 0, 4, 1),
                        Margin = new Thickness(3), HorizontalAlignment = HorizontalAlignment.Left,
                        VerticalAlignment = VerticalAlignment.Top,
                        Child = new TextBlock { Text = $"{im.Id + 1}", FontSize = 10, Foreground = new SolidColorBrush(Colors.White) },
                    });
                    var btn = new Button { Padding = new Thickness(0), Content = cell, Tag = im.Id, CornerRadius = new CornerRadius(5) };
                    ToolTipService.SetToolTip(btn, $"{im.Id + 1}. {im.Name}");
                    var s = side;
                    btn.Click += async (o, _) => await PickAsync(s, (int)((Button)o).Tag);
                    pane.Strip.Children.Add(btn);
                }
            }
            int? pick = side == Side.Left ? _pairA : _pairB;
            foreach (var child in pane.Strip.Children.OfType<Button>())
            {
                bool sel = (int)child.Tag == pick;
                child.BorderThickness = new Thickness(sel ? 3 : 1);
                child.BorderBrush = sel ? Accent : Themed("CardStrokeColorDefaultBrush", Colors.Gray);
                if (sel) child.StartBringIntoView();
            }
        }
    }

    private async Task<BitmapImage?> ThumbAsync(int id)
    {
        if (_thumbs.TryGetValue(id, out var c)) return c;
        BitmapImage? bmp = null;
        try { bmp = await MainWindow.ToBitmapAsync(await Engine!.BytesAsync($"/api/thumb/{id}")); } catch { }
        _thumbs[id] = bmp;
        return bmp;
    }

    private async Task PickAsync(Side side, int id)
    {
        if (side == Side.Left)
        {
            if (id == _pairB) _pairB = _pairA;       // 같은 장을 두 번 고르지 않게
            _pairA = id;
        }
        else
        {
            if (id == _pairA) _pairA = _pairB;
            _pairB = id;
        }
        await SyncStripsAsync(false);
        await LoadPairAsync();
    }

    private async void SwapPair()
    {
        (_pairA, _pairB) = (_pairB, _pairA);
        await SyncStripsAsync(false);
        await LoadPairAsync();
    }

    private EditorImage? ImageWith(int? id) => _images.FirstOrDefault(i => i.Id == id);

    private async Task LoadPairAsync()
    {
        _pendingLeft = null;
        _selected = null;
        var ia = ImageWith(_pairA);
        var ib = ImageWith(_pairB);
        if (Engine is null || ia is null || ib is null || ia.Id == ib.Id)
        {
            SetPane(_left, null, ia);
            SetPane(_right, null, ib);
            _points.Clear();
            ApplyMarkers();
            return;
        }
        int gen = ++_loadGen;
        var ba = await ProxyAsync(ia.Id);
        var bb = await ProxyAsync(ib.Id);
        if (gen != _loadGen) return;               // 그사이 다른 쌍을 골랐다
        SetPane(_left, ba, ia);
        SetPane(_right, bb, ib);
        await ReloadPointsAsync();
    }

    /// <summary>프록시 사진. 이따금 빈 값이 오므로 한 번은 다시 시도한다.</summary>
    private async Task<BitmapImage?> ProxyAsync(int id)
    {
        for (int i = 0; i < 2; i++)
        {
            try { return await MainWindow.ToBitmapAsync(await Engine!.BytesAsync($"/api/proxy/{id}")); }
            catch { await Task.Delay(150); }
        }
        return null;
    }

    private void SetPane(Pane p, BitmapImage? bmp, EditorImage? im)
    {
        p.Bitmap = bmp;
        p.Image.Source = bmp;
        p.Src = im is { Width: > 0, Height: > 0 } ? new Size(im.Width, im.Height) : new Size(1, 1);
        p.Caption.Text = im is null ? "" : $"{im.Id + 1}. {im.Name}";
        p.CaptionBox.Visibility = im is null ? Visibility.Collapsed : Visibility.Visible;
        p.Placeholder.Visibility = bmp is null ? Visibility.Visible : Visibility.Collapsed;
        p.Markers.Clear();
        p.UserAdjusted = false;
        p.Zoom = 0;
        Fit(p);
        Redraw(p);
    }

    private async Task ReloadPointsAsync()
    {
        if (Engine is null || _pairA is not int a || _pairB is not int b) return;
        try
        {
            var r = await Engine.GetAsync($"/api/control-points?pair={a},{b}");
            _points = (r?["points"] as JsonArray)?.Where(n => n is not null).Select(n => n!).ToList() ?? new();
            if (_selected is int sel && !_points.Any(n => Int(n, "index") == sel)) _selected = null;
            ApplyMarkers();
        }
        catch (Exception ex) { _hint.Text = ex.Message; }
    }

    private static int Int(JsonNode n, string k) => n[k]?.GetValue<int>() ?? 0;
    private static double Num(JsonNode n, string k) => n[k]?.GetValue<double>() ?? 0;

    private void ApplyMarkers()
    {
        int a = _pairA ?? -1;
        // 화면에 보이는 번호는 이 쌍 안에서의 순번이다. 양쪽이 같은 번호·같은
        // 색을 쓰므로 어느 점과 어느 점이 짝인지 눈으로 바로 이어진다.
        _left.Markers = _points.Select((c, n) => new Marker
        {
            Index = Int(c, "index"), Number = n + 1, Error = Num(c, "error"),
            Enabled = c["enabled"]?.GetValue<bool>() != false,
            Point = Int(c, "img_a") == a ? new Point(Num(c, "xa"), Num(c, "ya")) : new Point(Num(c, "xb"), Num(c, "yb")),
        }).ToList();
        _right.Markers = _points.Select((c, n) => new Marker
        {
            Index = Int(c, "index"), Number = n + 1, Error = Num(c, "error"),
            Enabled = c["enabled"]?.GetValue<bool>() != false,
            Point = Int(c, "img_a") == a ? new Point(Num(c, "xb"), Num(c, "yb")) : new Point(Num(c, "xa"), Num(c, "ya")),
        }).ToList();
        RedrawAll();
        RefreshHint();

        var errs = _points.Where(c => c["enabled"]?.GetValue<bool>() != false && Num(c, "error") > 0)
                          .Select(c => Num(c, "error")).ToList();
        _summary.Text = errs.Count == 0
            ? $"이 쌍의 제어점 {_points.Count}개"
            : $"이 쌍의 제어점 {_points.Count}개 · RMS {Math.Sqrt(errs.Average(e => e * e)):0.00}px · 최대 {errs.Max():0.00}px";
    }

    private void RefreshHint()
    {
        if (_images.Count < 2) _hint.Text = "사진이 두 장 이상 있어야 제어점을 찍을 수 있습니다";
        else if (!_aligned) _hint.Text = "아직 정렬하지 않았습니다 — 자동 정렬(Ctrl+R)을 먼저 누르세요";
        else if (_pendingLeft is not null)
            _hint.Text = Kind == "manual" ? "오른쪽 사진에서 짝이 될 지점을 클릭하세요" : "같은 사진에서 두 번째 점을 찍으세요";
        else if (_points.Count == 0) _hint.Text = "이 쌍에는 제어점이 없습니다. 왼쪽 사진을 클릭해 직접 찍을 수 있습니다 (Alt+클릭: 짝도 직접)";
        else _hint.Text = "";
    }

    // ---------------------------------------------------------------- 확대와 그리기

    private static void Fit(Pane p)
    {
        if (p.Bitmap is not { PixelWidth: > 0 } b) return;
        double w = p.Surface.ActualWidth, h = p.Surface.ActualHeight;
        if (w < 8 || h < 8) return;
        const double pad = 16;
        p.Zoom = Math.Max(0.02, Math.Min((w - pad) / b.PixelWidth, (h - pad) / b.PixelHeight));
        p.Offset = new Point((w - b.PixelWidth * p.Zoom) / 2, (h - b.PixelHeight * p.Zoom) / 2);
    }

    /// <summary>어떤 지점을 그 칸 한가운데로 가져온다.</summary>
    private void CenterOn(Pane p, Point src, double? zoom = null)
    {
        if (zoom is double z) p.Zoom = z;
        p.UserAdjusted = true;
        double k = p.ProxyScale;
        p.Offset = new Point(p.Surface.ActualWidth / 2 - src.X * k * p.Zoom,
                             p.Surface.ActualHeight / 2 - src.Y * k * p.Zoom);
        Redraw(p);
    }

    private void RedrawAll() { Redraw(_left); Redraw(_right); }

    private void Redraw(Pane p)
    {
        if (p.Bitmap is { PixelWidth: > 0 } b)
        {
            p.Image.Width = b.PixelWidth * p.Zoom;
            p.Image.Height = b.PixelHeight * p.Zoom;
            Canvas.SetLeft(p.Image, p.Offset.X);
            Canvas.SetTop(p.Image, p.Offset.Y);
        }
        p.Overlay.Children.Clear();
        if (p.Bitmap is null) return;
        foreach (var m in p.Markers) p.Overlay.Children.Add(MarkerVisual(p, m));
        if (_pendingLeft is Point pend && p == _left)
        {
            var v = p.ToView(pend);
            var ring = new Ellipse
            {
                Width = 16, Height = 16, Stroke = Accent, StrokeThickness = 2,
                StrokeDashArray = new DoubleCollection { 2, 1.5 },
            };
            Canvas.SetLeft(ring, v.X - 8);
            Canvas.SetTop(ring, v.Y - 8);
            p.Overlay.Children.Add(ring);
        }
    }

    private UIElement MarkerVisual(Pane p, Marker m)
    {
        var v = p.ToView(m.Point);
        var group = new Canvas();
        var white = new SolidColorBrush(ColorHelper.FromArgb(230, 255, 255, 255));
        group.Children.Add(new Line { X1 = v.X - 5, Y1 = v.Y, X2 = v.X + 5, Y2 = v.Y, Stroke = white, StrokeThickness = 1 });
        group.Children.Add(new Line { X1 = v.X, Y1 = v.Y - 5, X2 = v.X, Y2 = v.Y + 5, Stroke = white, StrokeThickness = 1 });

        var tint = m.Enabled ? MarkerColor(m.Number) : ColorHelper.FromArgb(255, 120, 120, 120);
        double lum = (0.299 * tint.R + 0.587 * tint.G + 0.114 * tint.B) / 255;
        bool sel = m.Index == _selected;
        var badge = new Border
        {
            Background = new SolidColorBrush(tint),
            CornerRadius = new CornerRadius(3),
            Padding = new Thickness(4, 0, 4, 1),
            // 고른 점은 강조색, 잔차가 큰 점은 빨간 테두리
            BorderBrush = sel ? Accent : new SolidColorBrush(ColorHelper.FromArgb(255, 255, 59, 48)),
            BorderThickness = new Thickness(sel ? 2 : m.Error > 12 ? 1.5 : 0),
            Child = new TextBlock
            {
                Text = m.Number.ToString(), FontSize = 10, FontWeight = Microsoft.UI.Text.FontWeights.Bold,
                Foreground = new SolidColorBrush(lum > 0.62 ? Colors.Black : Colors.White),
            },
        };
        Canvas.SetLeft(badge, v.X + 4);
        Canvas.SetTop(badge, v.Y + 4);
        group.Children.Add(badge);
        return group;
    }

    private void ShowLoupe(Side side, Point view)
    {
        var p = side == Side.Left ? _left : _right;
        if (p.Bitmap is not { PixelWidth: > 0 } b || p.Zoom >= 6)
        {
            _loupe.Visibility = Visibility.Collapsed;
            return;
        }
        // 확대경은 두 칸을 덮는 층 위에 있다. 오른쪽 칸이면 그만큼 민다.
        double paneX = side == Side.Left ? 0 : _right.Surface.ActualWidth + 8;
        double totalW = _left.Surface.ActualWidth + 8 + _right.Surface.ActualWidth;
        double x = Math.Clamp(paneX + view.X + 22, 8, Math.Max(8, totalW - LoupeSize - 8));
        double y = Math.Clamp(view.Y - LoupeSize - 22, 8, Math.Max(8, p.Surface.ActualHeight - LoupeSize - 8));
        Canvas.SetLeft(_loupe, x);
        Canvas.SetTop(_loupe, y);

        var src = p.ToSource(view);
        double k = p.ProxyScale;
        if (_loupeImage.Source != p.Bitmap) _loupeImage.Source = p.Bitmap;
        _loupeImage.Width = b.PixelWidth * LoupeMag;
        _loupeImage.Height = b.PixelHeight * LoupeMag;
        Canvas.SetLeft(_loupeImage, LoupeSize / 2 - src.X * k * LoupeMag);
        Canvas.SetTop(_loupeImage, LoupeSize / 2 - src.Y * k * LoupeMag);
        _loupeCanvas.Clip = new RectangleGeometry { Rect = new Rect(0, 0, LoupeSize, LoupeSize) };
        if (_loupeCanvas.Children.Count == 1)
        {
            // 가운데 십자 — 가운데 한 화소는 비워 둬서 가리지 않는다
            foreach (var (x1, y1, x2, y2) in new[] { (66.0, 54.0, 66.0, 62.0), (66.0, 70.0, 66.0, 78.0), (54.0, 66.0, 62.0, 66.0), (70.0, 66.0, 78.0, 66.0) })
                _loupeCanvas.Children.Add(new Line { X1 = x1, Y1 = y1, X2 = x2, Y2 = y2, Stroke = Accent, StrokeThickness = 1 });
        }
        _loupe.Visibility = Visibility.Visible;
    }

    // ---------------------------------------------------------------- 입력

    private Pane PaneOf(Side s) => s == Side.Left ? _left : _right;

    private Marker? HitMarker(Pane p, Point view)
    {
        for (int i = p.Markers.Count - 1; i >= 0; i--)
        {
            var m = p.Markers[i];
            var v = p.ToView(m.Point);
            if (Math.Sqrt((v.X - view.X) * (v.X - view.X) + (v.Y - view.Y) * (v.Y - view.Y)) < 11) return m;
            if (view.X >= v.X + 2 && view.X <= v.X + 28 && view.Y >= v.Y + 2 && view.Y <= v.Y + 20) return m;
        }
        return null;
    }

    private void OnPressed(Side side, PointerRoutedEventArgs e)
    {
        var p = PaneOf(side);
        var pt = e.GetCurrentPoint(p.Surface);
        if (!pt.Properties.IsLeftButtonPressed || p.Bitmap is null) return;
        Focus(FocusState.Pointer);
        p.Surface.CapturePointer(e.Pointer);
        _dragSide = side;
        _dragMouse = pt.Position;
        _dragMoved = false;
        if (HitMarker(p, pt.Position) is { } hit)
        {
            _selected = hit.Index;
            _drag = DragKind.Move;
            _dragMarker = hit;
            RedrawAll();
        }
        else
        {
            _drag = DragKind.Pan;
            _dragOffset = p.Offset;
        }
        e.Handled = true;
    }

    private void OnMoved(Side side, PointerRoutedEventArgs e)
    {
        var p = PaneOf(side);
        var pos = e.GetCurrentPoint(p.Surface).Position;
        if (_drag == DragKind.Pan && side == _dragSide)
        {
            if (Math.Abs(pos.X - _dragMouse.X) + Math.Abs(pos.Y - _dragMouse.Y) > 3) _dragMoved = true;
            p.Offset = new Point(_dragOffset.X + pos.X - _dragMouse.X, _dragOffset.Y + pos.Y - _dragMouse.Y);
            p.UserAdjusted = true;
            Redraw(p);
        }
        else if (_drag == DragKind.Move && side == _dragSide && _dragMarker is { } m)
        {
            _dragMoved = true;
            m.Point = p.ToSource(pos);
            Redraw(p);
        }
        ShowLoupe(side, pos);
    }

    private async void OnReleased(Side side, PointerRoutedEventArgs e)
    {
        var p = PaneOf(side);
        var pos = e.GetCurrentPoint(p.Surface).Position;
        p.Surface.ReleasePointerCapture(e.Pointer);
        var kind = _drag;
        var marker = _dragMarker;
        _drag = DragKind.None;
        _dragMarker = null;
        if (side != _dragSide) return;

        if (kind == DragKind.Pan && !_dragMoved)
        {
            var src = p.ToSource(pos);
            if (src.X >= 0 && src.Y >= 0 && src.X < p.Src.Width && src.Y < p.Src.Height)
            {
                bool alt = e.KeyModifiers.HasFlag(Windows.System.VirtualKeyModifiers.Menu);
                if (side == Side.Left) await LeftClickedAsync(src, alt);
                else await RightClickedAsync(src);
            }
        }
        else if (kind == DragKind.Move && _dragMoved && marker is not null)
        {
            await MovePointAsync(marker.Index, side, marker.Point);
        }
    }

    private void OnWheel(Side side, PointerRoutedEventArgs e)
    {
        var p = PaneOf(side);
        if (p.Bitmap is null) return;
        var pt = e.GetCurrentPoint(p.Surface);
        int delta = pt.Properties.MouseWheelDelta;
        double next = Math.Clamp(p.Zoom * Math.Exp(delta / 120.0 * 0.18), 0.05, 20);
        double k = next / Math.Max(p.Zoom, 0.0001);
        var local = pt.Position;
        // 커서 아래 지점이 그대로 머물도록 확대한다
        p.Offset = new Point(local.X - (local.X - p.Offset.X) * k, local.Y - (local.Y - p.Offset.Y) * k);
        p.Zoom = next;
        p.UserAdjusted = true;
        Redraw(p);
        ShowLoupe(side, local);
        e.Handled = true;
    }

    private async void OnKeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key is Windows.System.VirtualKey.Delete or Windows.System.VirtualKey.Back)
        {
            e.Handled = true;
            await DeleteSelectedAsync();
        }
        else if (e.Key == Windows.System.VirtualKey.Escape && _pendingLeft is not null)
        {
            _pendingLeft = null;
            RedrawAll();
            RefreshHint();
        }
    }

    // ---------------------------------------------------------------- 점 찍기

    private async Task LeftClickedAsync(Point pt, bool alt)
    {
        if (_busy || _pairA is not int a || _pairB is not int b) return;
        if (Kind != "manual")
        {
            // 수직·수평선은 같은 사진 안의 두 점을 잇는다
            if (_pendingLeft is Point first)
            {
                _pendingLeft = null;
                await AddPointAsync(a, a, first, pt, Kind);
            }
            else
            {
                _pendingLeft = pt;
                RedrawAll();
                RefreshHint();
            }
            return;
        }
        if (alt)
        {
            _pendingLeft = pt;
            RedrawAll();
            RefreshHint();
            return;
        }
        await SuggestMatchAsync(a, b, pt);
    }

    private async Task RightClickedAsync(Point pt)
    {
        if (_busy) return;
        if (_pendingLeft is not Point first || Kind != "manual" || _pairA is not int a || _pairB is not int b)
        {
            _hint.Text = "왼쪽 사진을 먼저 클릭하세요";
            return;
        }
        _pendingLeft = null;
        await AddPointAsync(a, b, first, pt, "manual");
    }

    /// <summary>현재 카메라 파라미터로 짝을 예측하고 상호상관으로 다듬어 준다.</summary>
    private async Task SuggestMatchAsync(int a, int b, Point pt)
    {
        _busy = true;
        _hint.Text = "짝을 찾는 중…";
        try
        {
            var r = await Engine!.PostAsync("/api/control-points/suggest",
                                            new { img_a = a, img_b = b, xa = pt.X, ya = pt.Y });
            if (r is null) { _hint.Text = "짝을 찾지 못했습니다"; return; }
            double score = r["score"]?.GetValue<double>() ?? 0;
            var at = new Point(r["xb"]?.GetValue<double>() ?? 0, r["yb"]?.GetValue<double>() ?? 0);
            if (score < 0.45)
            {
                // 자신 없으면 사람에게 맡긴다. 예측 위치로 화면만 옮겨 준다.
                _pendingLeft = pt;
                CenterOn(_right, at, 1.2);
                RedrawAll();
                _hint.Text = $"자신이 없습니다 (일치도 {score:0.00}). 오른쪽에서 직접 찍어 주세요";
                return;
            }
            await AddPointAsync(a, b, pt, at, "manual", $"추가됨 (일치도 {score:0.00})");
        }
        catch (Exception ex) { _hint.Text = ex.Message; }
        finally { _busy = false; }
    }

    private async Task AddPointAsync(int a, int b, Point p1, Point p2, string kind, string note = "추가됨")
    {
        try
        {
            await Engine!.PostAsync("/api/control-points", new
            {
                img_a = a, img_b = b, xa = p1.X, ya = p1.Y, xb = p2.X, yb = p2.Y, kind,
            });
            await ReloadPointsAsync();
            _hint.Text = note;
            Changed?.Invoke();
        }
        catch (Exception ex) { _hint.Text = ex.Message; }
    }

    private async Task MovePointAsync(int index, Side side, Point pt)
    {
        // 서버에 저장된 짝의 방향(img_a 가 어느 쪽인지)에 맞춰 보낸다
        var cp = _points.FirstOrDefault(n => Int(n, "index") == index);
        bool leftIsA = cp is null || Int(cp, "img_a") == _pairA;
        bool writeA = (side == Side.Left) == leftIsA;
        var body = writeA
            ? new Dictionary<string, double> { ["xa"] = pt.X, ["ya"] = pt.Y }
            : new Dictionary<string, double> { ["xb"] = pt.X, ["yb"] = pt.Y };
        try
        {
            await Engine!.PatchAsync($"/api/control-points/{index}", body);
            await ReloadPointsAsync();
            Changed?.Invoke();
        }
        catch (Exception ex) { _hint.Text = ex.Message; }
    }

    private async Task DeleteSelectedAsync()
    {
        if (Engine is null || _selected is not int idx) return;
        try
        {
            await Engine.DeleteAsync($"/api/control-points/{idx}");
            _selected = null;
            await ReloadPointsAsync();
            _hint.Text = "제어점을 지웠습니다";
            Changed?.Invoke();
        }
        catch (Exception ex) { _hint.Text = ex.Message; }
    }

    private async Task PruneAsync()
    {
        if (Engine is null) return;
        try
        {
            var r = await Engine.PostAsync("/api/control-points/prune", new { threshold = 0 });
            int n = r?["disabled"]?.GetValue<int>() ?? 0;
            double thr = r?["threshold"]?.GetValue<double>() ?? 0;
            await ReloadPointsAsync();
            _hint.Text = n > 0 ? $"잔차 {thr:0.0}px 초과 {n}개를 껐습니다" : "정리할 점이 없습니다";
            if (n > 0) Changed?.Invoke();
        }
        catch (Exception ex) { _hint.Text = ex.Message; }
    }
}
