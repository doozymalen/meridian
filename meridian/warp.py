"""파노라마 캔버스 배치와 워핑.

캔버스는 두 층으로 나눠 다룬다.

  full_w x full_h : hfov 전체에 해당하는 '가상' 캔버스. 투영 수식의 기준.
  x0, y0, w, h    : 그중 실제로 내보낼 영역.

이렇게 두면 기가픽셀 출력에서 캔버스를 통째로 메모리에 올리지 않고도
타일별로 정확한 좌표를 계산할 수 있다. pano_grid_to_rays 의 roi 가 그 통로다.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import cv2
import numpy as np

from .camera import (ImageParams, Lens, lens_inverse_lut, pano_grid_to_rays,
                     pixels_to_rays, rays_to_pano, rays_to_pixels, rotation_matrix)


@dataclass
class PanoLayout:
    projection: str = "equirect"
    full_w: int = 4000
    full_h: int = 2000
    hfov: float = 360.0               # full_w 에 대응하는 가로 화각(도)
    center_yaw: float = 0.0           # 라디안
    center_pitch: float = 0.0
    center_roll: float = 0.0          # 수평선을 기울이는 회전
    x0: int = 0
    y0: int = 0
    w: int = 4000
    h: int = 2000

    @property
    def center(self) -> tuple[float, float, float]:
        return (self.center_yaw, self.center_pitch, self.center_roll)

    @property
    def roi(self) -> tuple[int, int, int, int]:
        return (self.x0, self.y0, self.w, self.h)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def wraps(self) -> bool:
        """좌우 끝이 같은 지점으로 이어지는 캔버스인지."""
        return (self.hfov >= 358.0
                and self.projection in ("equirect", "cylindrical", "mercator")
                and self.x0 <= 0 and self.x0 + self.w >= self.full_w)

    def wrap_pad(self) -> int:
        """감싸기 처리를 위해 좌우로 덧댈 폭.

        이 만큼 넓게 렌더해 심 찾기와 블렌딩이 경계를 건너 이어지게 한 뒤,
        가운데만 잘라 낸다. 멀티밴드가 번지는 폭보다 넉넉해야 한다.
        """
        if not self.wraps:
            return 0
        # 멀티밴드는 대역 수만큼(2^n) 번지므로 그보다 넓어야 경계 효과가 없다.
        bands = max(1, int(np.clip(np.log2(max(self.w, self.h)) - 4, 1, 7)))
        return int(max(2 ** bands * 1.6, round(self.w * 0.10)))

    def scaled(self, factor: float) -> "PanoLayout":
        """미리보기용으로 전체를 비례 축소한다."""
        f = max(1e-6, factor)
        fw = max(4, round(self.full_w * f))
        wrapped = self.wraps
        return PanoLayout(
            projection=self.projection,
            full_w=fw, full_h=max(4, round(self.full_h * f)),
            hfov=self.hfov, center_yaw=self.center_yaw, center_pitch=self.center_pitch,
            center_roll=self.center_roll,
            # 감기는 캔버스는 축소한 뒤에도 폭이 정확히 full_w 와 같아야 한다
            x0=0 if wrapped else round(self.x0 * f),
            y0=round(self.y0 * f),
            w=fw if wrapped else max(1, round(self.w * f)),
            h=max(1, round(self.h * f)),
        )


def _sample_border(w: int, h: int, n: int = 24) -> np.ndarray:
    """이미지 테두리 + 내부 격자를 샘플링한다. 캔버스 범위 산정용."""
    t = np.linspace(0, 1, n)
    top = np.stack([t * (w - 1), np.zeros(n)], 1)
    bot = np.stack([t * (w - 1), np.full(n, h - 1.0)], 1)
    lef = np.stack([np.zeros(n), t * (h - 1)], 1)
    rig = np.stack([np.full(n, w - 1.0), t * (h - 1)], 1)
    gx, gy = np.meshgrid(np.linspace(0, w - 1, 5), np.linspace(0, h - 1, 5))
    inner = np.stack([gx.ravel(), gy.ravel()], 1)
    return np.vstack([top, bot, lef, rig, inner])


def image_rays(img_id: int, params: ImageParams, lens: Lens,
               size: tuple[int, int], n: int = 24) -> np.ndarray:
    w, h = size
    R = rotation_matrix(params.yaw, params.pitch, params.roll)
    return pixels_to_rays(_sample_border(w, h, n), lens, w, h, R)


def auto_center(images: dict[int, ImageParams], lenses: dict[int, Lens],
                sizes: dict[int, tuple[int, int]]) -> tuple[float, float, float]:
    """모든 이미지가 고르게 담기도록 파노라마 중심 방향을 고른다."""
    dirs = []
    for i, p in images.items():
        R = rotation_matrix(p.yaw, p.pitch, p.roll)
        dirs.append(R @ np.array([0.0, 0.0, 1.0]))
    if not dirs:
        return 0.0, 0.0, 0.0
    m = np.mean(dirs, axis=0)
    n = float(np.linalg.norm(m))
    # 사방을 고르게 덮었으면 평균 벡터가 거의 0 이 되고, 그 방향은 잡음일
    # 뿐이다. 그걸 중심으로 삼으면 파노라마가 엉뚱하게 기운 채로 잘린다.
    # 이럴 땐 수평 정면을 기준으로 두는 편이 언제나 자연스럽다.
    if n < 0.35:
        return 0.0, 0.0, 0.0
    m = m / n
    return (float(np.arctan2(m[0], m[2])),
            float(np.arcsin(np.clip(m[1], -1, 1))), 0.0)


def compute_layout(images: dict[int, ImageParams], lenses: dict[int, Lens],
                   sizes: dict[int, tuple[int, int]], projection: str = "equirect",
                   center: tuple[float, ...] | None = None,
                   max_dim: int | None = None,
                   scale_percent: float = 100.0,
                   out_width: int = 0,
                   fov: tuple[float, float] | None = None) -> PanoLayout:
    """카메라 배치로부터 출력 캔버스를 정한다.

    fov(가로, 세로 화각 — 도)를 주면 사진이 어디를 덮든 그 화각으로 틀을
    고정한다. 주지 않으면 사진이 덮는 영역에 맞춰 자른다. 고정 틀은 방향을
    돌려도 크기가 변하지 않는다 — 출력 화각을 프로젝트 값으로 못박는 셈이다.

    기본 해상도는 '원본 화소 밀도 유지' — 파노라마 중심에서 원본과 1:1 이
    되도록 잡는다. 원본 화소를 버리지도, 없는 화소를 지어내지도 않는 크기다.
    """
    active = {i: p for i, p in images.items() if i in sizes}
    if not active:
        return PanoLayout()
    if center is None:
        center = auto_center(active, lenses, sizes)

    # 1) 픽셀 밀도: 각 이미지의 초점거리(픽셀/라디안) 중앙값
    focals = []
    for i, p in active.items():
        w, h = sizes[i]
        focals.append(lenses[p.lens_id].focal_px(w, h))
    f_med = float(np.median(focals))

    if fov and fov[0] > 0 and fov[1] > 0:
        lay = _fixed_layout(projection, fov[0], fov[1], f_med, center)
        return _finish_layout(lay, f_med, max_dim, scale_percent, out_width)

    # 2) 커버 범위를 중심 기준 경위도로 측정
    lons, lats, all_rays = [], [], []
    for i, p in active.items():
        v = image_rays(i, p, lenses[p.lens_id], sizes[i])
        all_rays.append(v)
        vc = v @ rotation_matrix(center[0], center[1],
                                 center[2] if len(center) > 2 else 0.0)
        lons.append(np.arctan2(vc[:, 0], vc[:, 2]))
        lats.append(np.arcsin(np.clip(vc[:, 1], -1, 1)))
    lon = np.concatenate(lons)
    lat = np.concatenate(lats)

    lon_span = _angular_span(lon)
    lat_lo, lat_hi = float(lat.min()), float(lat.max())

    # 3) 투영별 전체 캔버스와 유효 화각
    # 투영마다 실제로 담을 수 있는 화각이 다르다. 직선 투영은 90도만 넘어도
    # 가장자리가 감당 못 하게 늘어나고, 스테레오는 360도에서 발산한다.
    span_deg = np.degrees(lon_span) * 1.02
    limit = {"rectilinear": 140.0, "stereographic": 320.0}.get(projection, 360.0)
    hfov = float(max(min(span_deg, limit), 1.0))

    full_w = _full_width(projection, hfov, f_med)
    full_h = _full_height(projection, full_w, hfov, f_med)

    # 4) 실제 커버 영역을 픽셀로 환산해 crop
    rays = np.vstack(all_rays)
    pts = rays_to_pano(projection, rays, full_w, full_h, hfov, center)
    pts = pts[np.isfinite(pts).all(axis=1)]
    if len(pts) == 0:
        return PanoLayout(projection, full_w, full_h, hfov, center[0], center[1],
                          center[2] if len(center) > 2 else 0.0, 0, 0, full_w, full_h)

    full_wrap = (np.degrees(lon_span) >= 358.0
                 and projection in ("equirect", "cylindrical", "mercator"))
    if full_wrap:
        x0, x1 = 0.0, float(full_w)          # 전방위는 폭을 꽉 채운다
    elif np.degrees(lon_span) >= 358.0:
        x0, x1 = 0.0, float(full_w)
    else:
        x0, x1 = float(pts[:, 0].min()), float(pts[:, 0].max())
    y0, y1 = float(pts[:, 1].min()), float(pts[:, 1].max())

    vlim = _vertical_limit(projection, f_med)
    if vlim is not None:
        y0 = max(y0, full_h / 2.0 - vlim)
        y1 = min(y1, full_h / 2.0 + vlim)
        if y1 - y0 < 2:                      # 전부 잘려 나가는 일은 없게
            y0, y1 = full_h / 2.0 - vlim, full_h / 2.0 + vlim

    # 직선·스테레오 투영은 가로로도 발산한다. 광축과 거의 직각인 광선은
    # z 가 0 에 가까워 x 좌표가 수십만 픽셀까지 밀려나므로 같은 한계를 건다.
    # 나머지 투영은 가로가 360도로 닫혀 있어 손댈 필요가 없다.
    if projection in ("rectilinear", "stereographic") and vlim is not None:
        x0 = max(x0, full_w / 2.0 - vlim)
        x1 = min(x1, full_w / 2.0 + vlim)
        if x1 - x0 < 2:
            x0, x1 = full_w / 2.0 - vlim, full_w / 2.0 + vlim

    pad = 1
    y0i = int(np.floor(y0)) - pad
    h = int(np.ceil(y1 - y0)) + 2 * pad
    if full_wrap:
        # 360도로 감기는 캔버스는 폭이 정확히 full_w 여야 왼쪽 끝과 오른쪽 끝이
        # 같은 지점이 된다. 여백을 한 화소라도 붙이면 두 끝이 어긋나 이어지지 않는다.
        x0i, w = 0, int(full_w)
    else:
        x0i = int(np.floor(x0)) - pad
        w = int(np.ceil(x1 - x0)) + 2 * pad
    x0, y0 = x0i, y0i

    lay = PanoLayout(projection, full_w, full_h, hfov, center[0], center[1],
                     center[2] if len(center) > 2 else 0.0, x0, y0, w, h)

    return _finish_layout(lay, f_med, max_dim, scale_percent, out_width)



# ---------------------------------------------------------------- 화각

# 투영마다 담을 수 있는 한계. 직선·원통·메르카토르는 90도(세로 180도)에서
# 좌표가 무한대로 가므로 그 앞에서 끊는다.
FOV_MAX = {
    "equirect": (360.0, 180.0), "cylindrical": (360.0, 170.0),
    "mercator": (360.0, 170.0), "rectilinear": (170.0, 170.0),
    "stereographic": (358.0, 358.0), "fisheye": (360.0, 360.0),
}
# '맞춤' 이 고르는 상한. 한계까지 채우면 가장자리가 수십 배로 늘어나 볼 게
# 없으므로, 자동으로 고를 때는 한 발 물러선다. 손으로는 한계까지 넣을 수 있다.
_FIT_MAX = {
    "cylindrical": (360.0, 150.0), "mercator": (360.0, 160.0),
    "rectilinear": (140.0, 140.0), "stereographic": (320.0, 320.0),
}


def clamp_fov(projection: str, hfov: float, vfov: float) -> tuple[float, float]:
    hmax, vmax = FOV_MAX.get(projection, (360.0, 180.0))
    return float(np.clip(hfov, 1.0, hmax)), float(np.clip(vfov, 1.0, vmax))


def _fixed_layout(projection: str, hfov: float, vfov: float, f: float,
                  center: tuple[float, ...]) -> PanoLayout:
    """화각으로 틀을 정한다. 가운데 화소 밀도는 f(화소/라디안) 그대로다.

    가로는 pano_grid_to_rays 가 hfov 와 캔버스 폭으로 초점을 되짚으므로,
    그 식을 거꾸로 풀어 폭을 정한다. 세로는 투영 식에 반각을 넣어 높이를 잰다.
    """
    hfov, vfov = clamp_fov(projection, hfov, vfov)
    H, V = np.radians(hfov), np.radians(vfov)
    if projection == "rectilinear":
        half_w, half_h = f * np.tan(H / 2), f * np.tan(V / 2)
    elif projection == "stereographic":
        half_w, half_h = 2 * f * np.tan(H / 4), 2 * f * np.tan(V / 4)
    elif projection == "cylindrical":
        half_w, half_h = f * H / 2, f * np.tan(V / 2)
    elif projection == "mercator":
        half_w, half_h = f * H / 2, f * np.log(np.tan(V / 4 + np.pi / 4))
    else:                                   # equirect, fisheye
        half_w, half_h = f * H / 2, f * V / 2
    w = max(2, int(round(2 * half_w)))
    h = max(2, int(round(2 * half_h)))
    c = tuple(center) + (0.0, 0.0, 0.0)
    return PanoLayout(projection, w, h, hfov, float(c[0]), float(c[1]), float(c[2]),
                      0, 0, w, h)


def fit_fov(images: dict[int, ImageParams], lenses: dict[int, Lens],
            sizes: dict[int, tuple[int, int]], projection: str,
            center: tuple[float, ...]) -> tuple[float, float]:
    """사진이 전부 들어가는 가장 작은 화각. 틀은 중심 대칭이다.

    한쪽으로만 치우친 파노라마면 반대쪽에 빈 곳이 생긴다. 중심을 옮기면
    중심을 옮기면 방향까지 바뀌므로, 화각만 키워 대칭으로 담는다.
    """
    active = {i: p for i, p in images.items() if i in sizes}
    if not active:
        return FOV_MAX.get(projection, (360.0, 180.0))
    c = tuple(center) + (0.0, 0.0, 0.0)
    R = rotation_matrix(c[0], c[1], c[2])
    v = np.vstack([image_rays(i, p, lenses[p.lens_id], sizes[i])
                   for i, p in active.items()]) @ R
    v /= np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)
    x, y, z = v[:, 0], v[:, 1], v[:, 2]

    if projection in ("equirect", "cylindrical", "mercator"):
        lon = np.arctan2(x, z)
        lat = np.arcsin(np.clip(y, -1, 1))
        H = 360.0 if np.degrees(_angular_span(lon)) >= 358.0 \
            else 2 * np.degrees(np.abs(lon).max())
        V = 2 * np.degrees(np.abs(lat).max())
    elif projection == "rectilinear":
        front = z > 1e-3
        if not front.all():
            H = V = 180.0                   # 뒤쪽까지 덮으면 한계로
        else:
            H = 2 * np.degrees(np.arctan(np.abs(x / z).max()))
            V = 2 * np.degrees(np.arctan(np.abs(y / z).max()))
    else:                                   # stereographic, fisheye — 반지름 투영
        rxy = np.hypot(x, y)
        theta = np.arctan2(rxy, z)
        with np.errstate(invalid="ignore", divide="ignore"):
            ux = np.where(rxy > 1e-12, x / rxy, 0.0)
            uy = np.where(rxy > 1e-12, y / rxy, 0.0)
        if projection == "stereographic":
            r = 2 * np.tan(np.clip(theta, 0, np.radians(179)) / 2)
            H = 4 * np.degrees(np.arctan(np.abs(ux * r).max() / 2))
            V = 4 * np.degrees(np.arctan(np.abs(uy * r).max() / 2))
        else:
            H = 2 * np.degrees(np.abs(ux * theta).max())
            V = 2 * np.degrees(np.abs(uy * theta).max())

    hcap, vcap = _FIT_MAX.get(projection, FOV_MAX.get(projection, (360.0, 180.0)))
    H, V = min(float(H), hcap), min(float(V), vcap)
    return clamp_fov(projection, round(H, 1), round(V, 1))


def _finish_layout(lay: PanoLayout, f_med: float, max_dim: int | None,
                   scale_percent: float, out_width: int) -> PanoLayout:
    # 4-b) 발산하는 투영은 화소 예산으로 한 번 눌러 준다.
    # '원본 화소 밀도 유지' 는 중심에서만 뜻이 있다. 스테레오 투영은 중심에서
    # 멀어질수록 같은 하늘을 몇 배로 늘려 그리므로, 그 기준을 곧이곧대로 쓰면
    # 리틀플래닛 한 장이 8기가픽셀까지 간다 — 늘어난 만큼이 전부 보간이라
    # 새로 담기는 정보는 없는데 내보내기만 몇 시간이 된다. 같은 화각을 담은
    # 정방형도법과 화소 수를 맞추면, 중심부는 원본 밀도를 지키면서 주변부의
    # 헛된 확대만 걷힌다.
    if lay.projection in ("stereographic", "rectilinear") and lay.w > 0 and lay.h > 0:
        ref_w = _full_width("equirect", lay.hfov, f_med)
        ref_h = _full_height("equirect", ref_w, lay.hfov, f_med)
        budget = float(ref_w) * float(ref_h)
        area = float(lay.w) * float(lay.h)
        if budget > 0 and area > budget:
            lay = lay.scaled(float(np.sqrt(budget / area)))

    # 5) 사용자가 요청한 크기 적용. out_width 가 있으면 그 가로폭에 정확히
    #    맞추고, 없으면 퍼센트를 쓴다. 화소 수에 상한은 두지 않는다.
    if out_width and lay.w > 0:
        factor = out_width / float(lay.w)
    else:
        factor = max(0.001, scale_percent / 100.0)
    if max_dim:
        factor = min(factor, max_dim / max(lay.w, lay.h))
    if abs(factor - 1.0) > 1e-9:
        lay = lay.scaled(factor)
    return lay


def _angular_span(lon: np.ndarray) -> float:
    """원형으로 감긴 경도 값들이 차지하는 최소 호의 길이(라디안)."""
    a = np.sort(np.mod(lon, 2 * np.pi))
    if len(a) < 2:
        return 0.0
    gaps = np.diff(np.concatenate([a, a[:1] + 2 * np.pi]))
    return float(2 * np.pi - gaps.max())


def _vertical_limit(projection: str, f: float) -> float | None:
    """파노라마 중심에서 위아래로 허용할 최대 픽셀 거리.

    원통·메르카토르·직선·스테레오 투영은 극 쪽으로 갈수록 좌표가 무한히
    늘어난다. 전방위로 찍은 사진을 그대로 담으려 하면 세로가 수만 픽셀로
    폭발하면서 정작 볼 만한 가운데 부분은 납작해진다. 실제로 쓸모 있는
    범위에서 끊는다. equirect 와 어안은 원래 유한하므로 제한하지 않는다.
    """
    if projection == "cylindrical":
        return f * float(np.tan(np.radians(75.0)))
    if projection == "mercator":
        return f * float(np.log(np.tan(np.radians(80.0) / 2 + np.pi / 4)))
    if projection == "rectilinear":
        return f * float(np.tan(np.radians(70.0)))
    if projection == "stereographic":
        return 2.0 * f * float(np.tan(np.radians(160.0) / 2))
    return None


def _full_width(projection: str, hfov: float, f: float) -> int:
    half = np.radians(hfov) / 2.0
    if projection == "rectilinear":
        return int(round(2 * f * np.tan(min(half, np.radians(70.0)))))
    if projection == "stereographic":
        return int(round(4 * f * np.tan(min(half, np.radians(179.0)) / 2.0)))
    return int(round(2 * f * half))       # equirect / cylindrical / mercator / fisheye


def _full_height(projection: str, full_w: int, hfov: float, f: float) -> int:
    """세로는 '이론상 최대'로 잡는다. 실제 출력은 crop 이 정한다."""
    if projection == "equirect":
        return max(2, int(round(full_w * (np.pi / np.radians(hfov)))))
    if projection == "fisheye":
        return full_w
    return max(2, int(round(full_w * 4)))  # cylindrical/mercator/rectilinear 는 세로가 열려 있다


# ---------------------------------------------------------------- 워핑

WARP_BAND_PIXELS = 262144    # 워핑할 때 광선을 한 번에 계산할 화소 수 (띠 하나)


def warp_image(img: np.ndarray, params: ImageParams, lens: Lens,
               layout: PanoLayout, src_size: tuple[int, int] | None = None,
               roi: tuple[int, int, int, int] | None = None,
               interp: int = cv2.INTER_LINEAR,
               ) -> tuple[np.ndarray, np.ndarray, tuple[int, int]]:
    """이미지 한 장을 파노라마 좌표계로 옮긴다.

    src_size 는 렌즈 파라미터가 기준으로 삼는 원본 크기다. img 가 프록시면
    좌표를 그에 맞춰 축소해 준다. 반환은 (워핑 결과, 마스크, (좌, 상)).
    """
    sh, sw = img.shape[:2]
    ow, oh = src_size if src_size else (sw, sh)
    rx0, ry0, rw, rh = roi if roi else layout.roi

    R = rotation_matrix(params.yaw, params.pitch, params.roll)
    lut = lens_inverse_lut(lens.a, lens.b, lens.c)
    sx, sy = sw / ow, sh / oh          # 원본 -> 실제 픽셀 배열 배율

    # 광선 계산은 float64 배열 여러 개를 화소마다 만든다. 구 전체(2400x1200)를 한 번에
    # 하면 한 장에 수백 MB 라, 여러 장을 동시에 워핑하면 메모리가 작은 PC 에서 엔진이
    # 꺼졌다. 가로 띠로 나눠 계산하고 결과(float32)만 모은다. 화소별 계산이라 결과는 같다.
    mx = np.empty((rh, rw), np.float32)
    my = np.empty((rh, rw), np.float32)
    inside = np.empty((rh, rw), bool)
    band = max(1, WARP_BAND_PIXELS // max(1, rw))
    for y in range(0, rh, band):
        bh = min(band, rh - y)
        rays, ok = pano_grid_to_rays(layout.projection, layout.full_w, layout.full_h,
                                     layout.hfov, layout.center, (rx0, ry0 + y, rw, bh))
        px, valid = rays_to_pixels(rays, lens, ow, oh, R, lut)
        bx = (px[..., 0] * sx).astype(np.float32)
        by = (px[..., 1] * sy).astype(np.float32)
        mx[y:y + bh], my[y:y + bh] = bx, by
        inside[y:y + bh] = (ok & valid & (bx >= -0.5) & (by >= -0.5)
                            & (bx <= sw - 0.5) & (by <= sh - 0.5))
        del rays, ok, px, valid, bx, by
    if not inside.any():
        return (np.zeros((0, 0, 3), img.dtype), np.zeros((0, 0), np.uint8), (rx0, ry0))

    # 실제로 덮이는 부분만 잘라 메모리를 아낀다
    ys, xs = np.where(inside)
    ty0, ty1 = int(ys.min()), int(ys.max()) + 1
    tx0, tx1 = int(xs.min()), int(xs.max()) + 1
    mx_c = mx[ty0:ty1, tx0:tx1].copy()
    my_c = my[ty0:ty1, tx0:tx1].copy()
    mask = (inside[ty0:ty1, tx0:tx1].astype(np.uint8)) * 255
    mx_c[mask == 0] = -1
    my_c[mask == 0] = -1

    out = cv2.remap(img, mx_c, my_c, interp, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    out[mask == 0] = 0
    return out, mask, (rx0 + tx0, ry0 + ty0)


def warped_bounds(params: ImageParams, lens: Lens, layout: PanoLayout,
                  src_size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """워핑하지 않고 이미지가 캔버스에서 차지할 사각형만 구한다.

    타일 렌더링에서 '이 타일에 어떤 이미지가 걸치는가'를 판단할 때 쓴다.
    """
    w, h = src_size
    R = rotation_matrix(params.yaw, params.pitch, params.roll)
    rays = pixels_to_rays(_sample_border(w, h, 48), lens, w, h, R)
    pts = rays_to_pano(layout.projection, rays, layout.full_w, layout.full_h,
                       layout.hfov, layout.center)
    pts = pts[np.isfinite(pts).all(axis=1)]
    if len(pts) == 0:
        return None
    x0 = int(np.floor(pts[:, 0].min())) - 2
    y0 = int(np.floor(pts[:, 1].min())) - 2
    x1 = int(np.ceil(pts[:, 0].max())) + 2
    y1 = int(np.ceil(pts[:, 1].max())) + 2
    return (x0, y0, x1 - x0, y1 - y0)
