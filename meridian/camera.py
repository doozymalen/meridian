"""카메라·렌즈 모델과 투영 수학.

좌표 규약
---------
월드 좌표계: X = 오른쪽, Y = 아래, Z = 앞(광축 기본 방향).
카메라 회전 R 은 "카메라 좌표 -> 월드 좌표" 변환이다. 따라서 월드 광선을
카메라로 되돌릴 때는 R.T 를 곱한다.

렌즈 왜곡은 PanoTools 규약(a, b, c)을 따르되 방향을 다음으로 고정한다.

    r_ideal = (a*r^3 + b*r^2 + c*r + d) * r_measured,   d = 1 - a - b - c

즉 "실제 촬영된 픽셀 -> 왜곡 없는 이상 좌표" 가 순방향이다. 번들 조정은
제어점(실제 픽셀)을 광선으로 보내는 일만 반복하므로 이 방향이면 다항식을
그대로 쓸 수 있어 빠르고 미분도 안정적이다. 렌더링은 반대 방향이 필요한데
이미지당 1차원 역 LUT 를 한 번만 만들어 해결한다(lens_inverse_lut).

반경 정규화 기준은 PanoTools 와 동일하게 min(w, h)/2 이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal

import numpy as np

LensProjection = Literal["rectilinear", "fisheye", "equirect", "stereographic", "orthographic"]
PanoProjection = Literal["equirect", "cylindrical", "rectilinear", "mercator", "stereographic", "fisheye"]


# ---------------------------------------------------------------- 회전

def rotation_matrix(yaw: float, pitch: float, roll: float) -> np.ndarray:
    """yaw/pitch/roll(라디안) -> 3x3 회전 행렬 (카메라 -> 월드).

    적용 순서는 R = Ry(yaw) @ Rx(pitch) @ Rz(roll) 로 PanoTools 와 맞춘다.
    """
    cy, sy = np.cos(yaw), np.sin(yaw)
    cp, sp = np.cos(pitch), np.sin(pitch)
    cr, sr = np.cos(roll), np.sin(roll)

    ry = np.array([[cy, 0.0, sy], [0.0, 1.0, 0.0], [-sy, 0.0, cy]])
    rx = np.array([[1.0, 0.0, 0.0], [0.0, cp, -sp], [0.0, sp, cp]])
    rz = np.array([[cr, -sr, 0.0], [sr, cr, 0.0], [0.0, 0.0, 1.0]])
    return ry @ rx @ rz


def matrix_to_ypr(r: np.ndarray) -> tuple[float, float, float]:
    """회전 행렬 -> (yaw, pitch, roll). rotation_matrix 의 역연산."""
    # R = Ry(yaw) @ Rx(pitch) @ Rz(roll) 을 전개하면
    #   R[1] = [cos(p)sin(r), cos(p)cos(r), -sin(p)]
    #   R[0,2] = sin(y)cos(p),  R[2,2] = cos(y)cos(p)
    pitch = np.arcsin(np.clip(-r[1, 2], -1.0, 1.0))
    if abs(np.cos(pitch)) < 1e-8:      # 짐벌락: yaw 와 roll 이 겹치므로 roll 을 0 으로 흡수
        yaw = np.arctan2(-r[2, 0], r[0, 0])
        return float(yaw), float(pitch), 0.0
    yaw = np.arctan2(r[0, 2], r[2, 2])
    roll = np.arctan2(r[1, 0], r[1, 1])
    return float(yaw), float(pitch), float(roll)


# ---------------------------------------------------------------- 렌즈

@dataclass
class Lens:
    """렌즈 파라미터. 여러 이미지가 하나의 Lens 를 공유할 수 있다."""

    fov: float = 50.0                                   # 가로 화각(도)
    projection: LensProjection = "rectilinear"
    a: float = 0.0                                      # 방사 왜곡 3차
    b: float = 0.0                                      # 2차
    c: float = 0.0                                      # 1차
    cx: float = 0.0                                     # 주점 오프셋 d (픽셀)
    cy: float = 0.0                                     # 주점 오프셋 e (픽셀)
    shear_x: float = 0.0
    shear_y: float = 0.0

    def focal_px(self, width: int, height: int) -> float:
        """가로 화각에서 초점거리(픽셀)를 얻는다."""
        half = np.radians(np.clip(self.fov, 1e-3, 359.0)) / 2.0
        if self.projection == "rectilinear":
            half = min(half, np.radians(89.0))
            return float(width / 2.0 / np.tan(half))
        if self.projection == "fisheye":                # 등거리 r = f*theta
            return float(width / 2.0 / half)
        if self.projection == "stereographic":
            return float(width / 4.0 / np.tan(half / 2.0))
        if self.projection == "orthographic":
            return float(width / 2.0 / np.sin(min(half, np.radians(89.9))))
        return float(width / 2.0 / half)                # equirect

    def to_dict(self) -> dict:
        return asdict(self)


def _poly_d(a: float, b: float, c: float) -> float:
    return 1.0 - a - b - c


def distort_forward(r_norm: np.ndarray, a: float, b: float, c: float) -> np.ndarray:
    """실측 반경 -> 이상 반경 배율. r_norm 은 min(w,h)/2 로 정규화된 값."""
    d = _poly_d(a, b, c)
    return ((a * r_norm + b) * r_norm + c) * r_norm + d


def lens_inverse_lut(a: float, b: float, c: float, r_max: float = 2.5,
                     samples: int = 4096) -> tuple[np.ndarray, np.ndarray]:
    """이상 반경 -> 실측 반경 역변환용 단조 LUT 를 만든다.

    순방향 다항식을 촘촘히 샘플링한 뒤 단조 구간만 남긴다. 왜곡이 심해
    함수가 접히는 구간은 유효 범위 밖이므로 잘라내는 게 맞다.
    """
    r_m = np.linspace(0.0, r_max, samples)
    r_i = r_m * distort_forward(r_m, a, b, c)
    keep = np.ones(samples, dtype=bool)
    peak = np.argmax(np.maximum.accumulate(r_i) > r_i)   # 첫 하강 지점
    if peak > 0:
        keep[peak:] = False
    return r_i[keep], r_m[keep]


# ---------------------------------------------------------------- 이미지 <-> 광선

@dataclass
class ImageParams:
    """개별 이미지의 자세. 렌즈는 lens_id 로 공유 참조한다."""

    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    lens_id: int = 0
    exposure: float = 0.0                 # EV 보정값
    white_balance: tuple[float, float, float] = (1.0, 1.0, 1.0)

    def rotation(self) -> np.ndarray:
        return rotation_matrix(self.yaw, self.pitch, self.roll)


def pixels_to_rays(pts: np.ndarray, lens: Lens, width: int, height: int,
                   rot: np.ndarray | None = None) -> np.ndarray:
    """이미지 픽셀 (N,2) -> 단위 광선 (N,3).

    rot 을 주면 월드 좌표까지, 없으면 카메라 좌표까지만 변환한다.
    """
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    norm = min(width, height) / 2.0
    f = lens.focal_px(width, height)

    x = pts[:, 0] - width / 2.0 + 0.5 - lens.cx
    y = pts[:, 1] - height / 2.0 + 0.5 - lens.cy
    x = x + lens.shear_x * y
    y = y + lens.shear_y * x

    r_m = np.hypot(x, y) / norm
    scale = distort_forward(r_m, lens.a, lens.b, lens.c)
    x, y = x * scale, y * scale                        # 이상 좌표(픽셀)

    r = np.hypot(x, y)
    with np.errstate(invalid="ignore", divide="ignore"):
        ux = np.where(r > 1e-12, x / r, 0.0)
        uy = np.where(r > 1e-12, y / r, 0.0)

    if lens.projection == "rectilinear":
        rays = np.stack([x, y, np.full_like(x, f)], axis=1)
    elif lens.projection == "fisheye":
        theta = r / f
        rays = np.stack([ux * np.sin(theta), uy * np.sin(theta), np.cos(theta)], axis=1)
    elif lens.projection == "stereographic":
        theta = 2.0 * np.arctan2(r, 2.0 * f)
        rays = np.stack([ux * np.sin(theta), uy * np.sin(theta), np.cos(theta)], axis=1)
    elif lens.projection == "orthographic":
        s = np.clip(r / f, 0.0, 1.0)
        rays = np.stack([ux * s, uy * s, np.sqrt(np.maximum(1.0 - s * s, 0.0))], axis=1)
    else:                                              # equirect 소스
        lon, lat = x / f, y / f
        rays = np.stack([np.cos(lat) * np.sin(lon), np.sin(lat),
                         np.cos(lat) * np.cos(lon)], axis=1)

    rays /= np.maximum(np.linalg.norm(rays, axis=1, keepdims=True), 1e-12)
    if rot is not None:
        rays = rays @ rot.T                            # (R @ v.T).T
    return rays


def rays_to_pixels(rays: np.ndarray, lens: Lens, width: int, height: int,
                   rot: np.ndarray | None = None,
                   lut: tuple[np.ndarray, np.ndarray] | None = None) -> tuple[np.ndarray, np.ndarray]:
    """단위 광선 (...,3) -> 이미지 픽셀 좌표와 유효 마스크.

    pixels_to_rays 의 역연산. 왜곡 역변환에는 LUT 를 쓴다.
    """
    rays = np.asarray(rays, dtype=np.float64)
    shape = rays.shape[:-1]
    v = rays.reshape(-1, 3)
    if rot is not None:
        v = v @ rot                                    # 월드 -> 카메라 (R.T @ v)
    v = v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)

    norm = min(width, height) / 2.0
    f = lens.focal_px(width, height)
    xs, ys, zs = v[:, 0], v[:, 1], v[:, 2]
    rxy = np.hypot(xs, ys)
    with np.errstate(invalid="ignore", divide="ignore"):
        ux = np.where(rxy > 1e-12, xs / rxy, 0.0)
        uy = np.where(rxy > 1e-12, ys / rxy, 0.0)

    valid = np.ones(v.shape[0], dtype=bool)
    if lens.projection == "rectilinear":
        valid = zs > 1e-6
        zsafe = np.where(valid, zs, 1.0)
        x, y = f * xs / zsafe, f * ys / zsafe
    elif lens.projection == "fisheye":
        theta = np.arctan2(rxy, zs)
        r = f * theta
        x, y = ux * r, uy * r
    elif lens.projection == "stereographic":
        theta = np.arctan2(rxy, zs)
        r = 2.0 * f * np.tan(np.clip(theta, 0.0, np.radians(179.0)) / 2.0)
        x, y = ux * r, uy * r
    elif lens.projection == "orthographic":
        valid = zs > 0.0
        r = f * rxy
        x, y = ux * r, uy * r
    else:
        lon = np.arctan2(xs, zs)
        lat = np.arcsin(np.clip(ys, -1.0, 1.0))
        x, y = f * lon, f * lat

    # 이상 좌표 -> 실측 좌표 (왜곡 역변환)
    if abs(lens.a) + abs(lens.b) + abs(lens.c) > 1e-12:
        if lut is None:
            lut = lens_inverse_lut(lens.a, lens.b, lens.c)
        r_i_tab, r_m_tab = lut
        r_i = np.hypot(x, y) / norm
        inside = r_i <= r_i_tab[-1]
        r_m = np.interp(r_i, r_i_tab, r_m_tab)
        with np.errstate(invalid="ignore", divide="ignore"):
            k = np.where(r_i > 1e-12, r_m / np.maximum(r_i, 1e-12), 1.0)
        x, y = x * k, y * k
        valid &= inside

    y = y - lens.shear_y * x
    x = x - lens.shear_x * y
    px = x + width / 2.0 - 0.5 + lens.cx
    py = y + height / 2.0 - 0.5 + lens.cy

    out = np.stack([px, py], axis=1).reshape(*shape, 2)
    return out, valid.reshape(shape)


# ---------------------------------------------------------------- 파노라마 투영

def pano_grid_to_rays(proj: PanoProjection, pano_w: int, pano_h: int,
                      hfov: float, center: tuple[float, ...] = (0.0, 0.0),
                      roi: tuple[int, int, int, int] | None = None) -> tuple[np.ndarray, np.ndarray]:
    """파노라마 캔버스 격자 -> 월드 광선.

    roi 는 (x0, y0, w, h). 기가픽셀 출력에서 타일 단위로 부르기 위한 것이다.
    반환은 (H, W, 3) 광선과 (H, W) 유효 마스크.
    """
    x0, y0, w, h = roi if roi else (0, 0, pano_w, pano_h)
    xs = (np.arange(x0, x0 + w, dtype=np.float64) - pano_w / 2.0 + 0.5)
    ys = (np.arange(y0, y0 + h, dtype=np.float64) - pano_h / 2.0 + 0.5)
    gx, gy = np.meshgrid(xs, ys)

    half = np.radians(np.clip(hfov, 1e-3, 360.0)) / 2.0
    valid = np.ones(gx.shape, dtype=bool)

    if proj == "equirect":
        s = (pano_w / 2.0) / half
        lon, lat = gx / s, gy / s
        valid = np.abs(lat) <= np.pi / 2.0 + 1e-9
        lat = np.clip(lat, -np.pi / 2.0, np.pi / 2.0)
    elif proj == "cylindrical":
        s = (pano_w / 2.0) / half
        lon = gx / s
        lat = np.arctan(gy / s)
    elif proj == "mercator":
        s = (pano_w / 2.0) / half
        lon = gx / s
        lat = 2.0 * np.arctan(np.exp(gy / s)) - np.pi / 2.0
    elif proj == "rectilinear":
        f = (pano_w / 2.0) / np.tan(min(half, np.radians(89.0)))
        z = np.full_like(gx, f)
        v = np.stack([gx, gy, z], axis=-1)
        v /= np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-12)
        return _apply_center(v, center), valid
    elif proj == "stereographic":
        f = (pano_w / 4.0) / np.tan(min(half, np.radians(179.0)) / 2.0)
        r = np.hypot(gx, gy)
        theta = 2.0 * np.arctan2(r, 2.0 * f)
        with np.errstate(invalid="ignore", divide="ignore"):
            ux = np.where(r > 1e-12, gx / r, 0.0)
            uy = np.where(r > 1e-12, gy / r, 0.0)
        v = np.stack([ux * np.sin(theta), uy * np.sin(theta), np.cos(theta)], axis=-1)
        return _apply_center(v, center), valid
    else:                                              # fisheye 등거리
        f = (pano_w / 2.0) / half
        r = np.hypot(gx, gy)
        theta = r / f
        valid = theta <= np.pi
        with np.errstate(invalid="ignore", divide="ignore"):
            ux = np.where(r > 1e-12, gx / r, 0.0)
            uy = np.where(r > 1e-12, gy / r, 0.0)
        v = np.stack([ux * np.sin(theta), uy * np.sin(theta), np.cos(theta)], axis=-1)
        return _apply_center(v, center), valid

    v = np.stack([np.cos(lat) * np.sin(lon), np.sin(lat),
                  np.cos(lat) * np.cos(lon)], axis=-1)
    return _apply_center(v, center), valid


def _apply_center(v: np.ndarray, center: tuple[float, ...]) -> np.ndarray:
    """파노라마 방향(yaw, pitch, roll 라디안)을 광선에 반영한다.

    roll 은 수평선을 기울이는 회전이다. 삼각대가 틀어졌거나 일부러 기울여
    보고 싶을 때 쓴다.
    """
    cyaw, cpitch, croll = _center3(center)
    if abs(cyaw) < 1e-12 and abs(cpitch) < 1e-12 and abs(croll) < 1e-12:
        return v
    r = rotation_matrix(cyaw, cpitch, croll)
    return v @ r.T


def _center3(center: tuple[float, ...]) -> tuple[float, float, float]:
    """(yaw, pitch) 만 준 옛 호출도 받아 준다."""
    vals = tuple(center) + (0.0, 0.0, 0.0)
    return float(vals[0]), float(vals[1]), float(vals[2])


def rays_to_pano(proj: PanoProjection, rays: np.ndarray, pano_w: int, pano_h: int,
                 hfov: float, center: tuple[float, ...] = (0.0, 0.0)) -> np.ndarray:
    """월드 광선 -> 파노라마 픽셀 좌표. 캔버스 크기 산정에 쓴다."""
    v = np.asarray(rays, dtype=np.float64).reshape(-1, 3)
    cyaw, cpitch, croll = _center3(center)
    if abs(cyaw) > 1e-12 or abs(cpitch) > 1e-12 or abs(croll) > 1e-12:
        v = v @ rotation_matrix(cyaw, cpitch, croll)
    v /= np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)

    half = np.radians(np.clip(hfov, 1e-3, 360.0)) / 2.0
    xs, ys, zs = v[:, 0], v[:, 1], v[:, 2]

    if proj in ("equirect", "cylindrical", "mercator"):
        s = (pano_w / 2.0) / half
        lon = np.arctan2(xs, zs)
        lat = np.arcsin(np.clip(ys, -1.0, 1.0))
        px = lon * s
        if proj == "equirect":
            py = lat * s
        elif proj == "cylindrical":
            py = np.tan(np.clip(lat, -1.5, 1.5)) * s
        else:
            py = np.log(np.tan(np.clip(lat, -1.5, 1.5) / 2.0 + np.pi / 4.0)) * s
    elif proj == "rectilinear":
        f = (pano_w / 2.0) / np.tan(min(half, np.radians(89.0)))
        zsafe = np.where(zs > 1e-6, zs, np.nan)
        px, py = f * xs / zsafe, f * ys / zsafe
    elif proj == "stereographic":
        f = (pano_w / 4.0) / np.tan(min(half, np.radians(179.0)) / 2.0)
        rxy = np.hypot(xs, ys)
        theta = np.arctan2(rxy, zs)
        r = 2.0 * f * np.tan(np.clip(theta, 0, np.radians(179.0)) / 2.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            px = np.where(rxy > 1e-12, xs / rxy, 0.0) * r
            py = np.where(rxy > 1e-12, ys / rxy, 0.0) * r
    else:
        f = (pano_w / 2.0) / half
        rxy = np.hypot(xs, ys)
        theta = np.arctan2(rxy, zs)
        r = f * theta
        with np.errstate(invalid="ignore", divide="ignore"):
            px = np.where(rxy > 1e-12, xs / rxy, 0.0) * r
            py = np.where(rxy > 1e-12, ys / rxy, 0.0) * r

    return np.stack([px + pano_w / 2.0 - 0.5, py + pano_h / 2.0 - 0.5], axis=1)
