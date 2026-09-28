"""비네팅과 노출을 함께 푸는 광학적 보정.

이음매가 눈에 띄는 가장 큰 원인은 정렬 오차가 아니라 밝기 차이다. 렌즈는
주변부로 갈수록 어두워지는데(비네팅), 이걸 두면 이미지마다 가장자리가 어두운
채로 붙는다. 하늘처럼 매끄러운 면에서는 그 경계가 곧바로 호(弧) 모양 얼룩으로
드러난다. 이미지당 상수 게인만 맞추는 노출 보정으로는 이걸 잡을 수 없다.
공간적으로 변하는 성분이기 때문이다.

그래서 겹치는 두 장에서 '같은 장면 지점'을 모아 이렇게 푼다.

    관측값 = 실제밝기 × V(r) × 노출 + 미광,   V(r) = 1 + a·r² + b·r⁴ + c·r⁶

곱셈만으로는 부족하다. 렌즈 안에서 산란된 빛(미광, veiling flare)은 장면과
무관하게 '더해지는' 성분이라, 하늘처럼 밝고 균일한 면에서 장마다 다른 양이
얹히면 배율을 아무리 맞춰도 겹침이 어긋난 채로 남는다. 이음선을 따라 생기는
쐐기 모양 얼룩이 대개 이것이다. 그래서 이미지마다 미광 항을 함께 푼다.

같은 지점이면 실제밝기가 같아야 하므로, 두 관측값에서 미광을 빼고 배율로
나눈 값의 로그 차이를 0 으로 만드는 파라미터를 최소자승으로 구한다. r 은
이미지 중심에서의 정규화 반경이고, 비네팅 계수는 같은 렌즈끼리 공유한다.

비네팅 곡선에는 제약을 건다. 중심에서 멀어질수록 어두워져야 하고(단조 감소),
가장자리에서 터무니없이 내려가서도 안 된다. 표본이 한쪽에 몰리면 다항식이
출렁이며 그럴듯한 잔차를 내는데, 그렇게 얻은 곡선은 실제 렌즈와 무관하다.

JPEG 값은 sRGB 감마가 걸려 있어 그대로 쓰면 곱셈 모델이 성립하지 않는다.
반드시 선형 광량으로 되돌린 뒤 계산한다.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import cv2
import numpy as np
from scipy.optimize import least_squares

from .camera import Lens, pixels_to_rays, rays_to_pixels, rotation_matrix, lens_inverse_lut


@dataclass
class Vignetting:
    """렌즈 하나의 비네팅 계수. 모두 0 이면 보정하지 않는 것과 같다."""

    a: float = 0.0
    b: float = 0.0
    c: float = 0.0

    def monotonic_violation(self, r2: np.ndarray) -> np.ndarray:
        """반경이 커지는데 밝아지는 정도. 물리적으로 있을 수 없으므로 벌점 대상."""
        v = self.falloff(r2)
        return np.maximum(np.diff(v), 0.0)

    @property
    def active(self) -> bool:
        return abs(self.a) + abs(self.b) + abs(self.c) > 1e-6

    def falloff(self, r2: np.ndarray) -> np.ndarray:
        """정규화 반경의 제곱을 받아 밝기 배율 V(r) 을 돌려준다."""
        return 1.0 + r2 * (self.a + r2 * (self.b + r2 * self.c))


# ---------------------------------------------------------------- sRGB <-> 선형

def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055)


# ---------------------------------------------------------------- 대응점 수집

def collect_samples(proxies: dict[int, np.ndarray], params, lenses,
                    sizes: dict[int, tuple[int, int]], pairs: list[tuple[int, int]],
                    per_pair: int = 500) -> dict:
    """겹치는 쌍에서 같은 장면 지점의 밝기와 반경을 모은다."""
    obs_i, obs_j, r2_i, r2_j, idx_i, idx_j = [], [], [], [], [], []

    # 한 장이 여러 쌍에 등장하므로 흐리게 만드는 일은 앞에서 한 번씩만 한다.
    # (객체 id 를 캐시 키로 쓰면 회수된 뒤 id 가 재사용되어 엉뚱한 이미지를
    #  돌려줄 수 있어, 그냥 여기서 미리 만들어 둔다)
    blurred = {i: _smoothed(img) for i, img in proxies.items()}

    for a, b in pairs:
        if a not in proxies or b not in proxies:
            continue
        pa, pb = params[a], params[b]
        la, lb = lenses[pa.lens_id], lenses[pb.lens_id]
        wa, ha = sizes[a]
        wb, hb = sizes[b]
        ia, ib = blurred[a], blurred[b]

        # a 의 이미지 안에 격자를 깔고 b 로 옮겨본다
        n = int(np.sqrt(per_pair)) + 1
        gx, gy = np.meshgrid(np.linspace(wa * 0.02, wa * 0.98, n),
                             np.linspace(ha * 0.02, ha * 0.98, n))
        pts = np.stack([gx.ravel(), gy.ravel()], 1)

        rays = pixels_to_rays(pts, la, wa, ha, rotation_matrix(pa.yaw, pa.pitch, pa.roll))
        px, ok = rays_to_pixels(rays, lb, wb, hb,
                                rotation_matrix(pb.yaw, pb.pitch, pb.roll),
                                lens_inverse_lut(lb.a, lb.b, lb.c))
        inside = ok & (px[:, 0] >= 0) & (px[:, 1] >= 0) & (px[:, 0] < wb - 1) & (px[:, 1] < hb - 1)
        if inside.sum() < 12:
            continue

        va = _sample(ia, pts[inside], (wa, ha))
        vb = _sample(ib, px[inside], (wb, hb))
        # 포화되거나 너무 어두운 화소는 곱셈 모델이 무너지므로 버린다
        good = (va > 0.04) & (va < 0.92) & (vb > 0.04) & (vb < 0.92)
        if good.sum() < 12:
            continue

        norm_a = min(wa, ha) / 2.0
        norm_b = min(wb, hb) / 2.0
        ra = np.hypot(pts[inside][good, 0] - wa / 2, pts[inside][good, 1] - ha / 2) / norm_a
        rb = np.hypot(px[inside][good, 0] - wb / 2, px[inside][good, 1] - hb / 2) / norm_b

        obs_i.append(va[good]); obs_j.append(vb[good])
        r2_i.append(ra ** 2);   r2_j.append(rb ** 2)
        idx_i.append(np.full(good.sum(), a)); idx_j.append(np.full(good.sum(), b))

    if not obs_i:
        return {}
    return {
        "Ii": np.concatenate(obs_i), "Ij": np.concatenate(obs_j),
        "r2i": np.concatenate(r2_i), "r2j": np.concatenate(r2_j),
        "ai": np.concatenate(idx_i), "bj": np.concatenate(idx_j),
    }


def _smoothed(img: np.ndarray) -> np.ndarray:
    """샘플링 전에 흐릿하게 만든다.

    비네팅은 화면 전체에 걸친 저주파 현상인데, 원본 그대로 점 하나를 읽으면
    창틀이나 나뭇잎 같은 고주파가 그대로 들어온다. 정렬이 1 픽셀만 어긋나도
    두 장의 값이 크게 달라져 추정이 망가진다. 흐리게 만들면 그 민감도가
    사라지고 우리가 재려는 성분만 남는다.
    """
    k = max(3, (min(img.shape[:2]) // 120) * 2 + 1)
    return cv2.GaussianBlur(img, (k, k), 0)


def _sample(img: np.ndarray, pts: np.ndarray, src_size: tuple[int, int]) -> np.ndarray:
    """원본 좌표로 프록시에서 휘도를 읽어 선형 광량으로 돌려준다.

    좌표가 정수 격자에 걸리는 일은 거의 없으므로 이중선형으로 읽는다.
    """
    h, w = img.shape[:2]
    sx, sy = w / src_size[0], h / src_size[1]
    xs = np.clip(pts[:, 0] * sx, 0, w - 1.001).astype(np.float32)
    ys = np.clip(pts[:, 1] * sy, 0, h - 1.001).astype(np.float32)
    bgr = cv2.remap(img, xs.reshape(1, -1), ys.reshape(1, -1),
                    cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    bgr = bgr.reshape(-1, 3).astype(np.float64) / 255.0
    lum = 0.0722 * bgr[:, 0] + 0.7152 * bgr[:, 1] + 0.2126 * bgr[:, 2]
    return srgb_to_linear(lum)


# ---------------------------------------------------------------- 최소자승

def solve(samples: dict, image_ids: list[int], lens_of: dict[int, int],
          ) -> tuple[dict[int, Vignetting], dict[int, float], dict[int, float], float]:
    """비네팅(렌즈별), 노출과 미광(이미지별)을 함께 푼다.

    돌려주는 값은 (렌즈별 Vignetting, 이미지별 노출 배율, 이미지별 미광, RMS).
    미광은 선형 광량 단위이고 0 이상이다.
    """
    if not samples:
        return {}, {i: 1.0 for i in image_ids}, {i: 0.0 for i in image_ids}, 0.0

    lens_ids = sorted(set(lens_of.values()))
    lmap = {l: k for k, l in enumerate(lens_ids)}
    imap = {i: k for k, i in enumerate(image_ids)}

    Ii, Ij = samples["Ii"], samples["Ij"]
    r2i, r2j = samples["r2i"], samples["r2j"]
    li = np.array([lmap[lens_of[i]] for i in samples["ai"]])
    lj = np.array([lmap[lens_of[i]] for i in samples["bj"]])
    ei = np.array([imap[i] for i in samples["ai"]])
    ej = np.array([imap[i] for i in samples["bj"]])

    n_lens, n_img = len(lens_ids), len(image_ids)
    n_exp = max(0, n_img - 1)
    # 곡선 제약을 검사할 반경 격자 (0 ~ 1.4 정규화 반경)
    r_grid2 = np.linspace(0.0, 1.4, 24) ** 2

    def unpack(x):
        vig = x[:n_lens * 3].reshape(n_lens, 3)
        expo = np.concatenate([[0.0], x[n_lens * 3:n_lens * 3 + n_exp]])
        # 미광은 softplus 로 받아 항상 0 이상이 되게 한다
        raw = x[n_lens * 3 + n_exp:]
        flare = np.log1p(np.exp(np.clip(raw, -30, 30))) * 0.02
        return vig, expo, flare

    def falloff(vig, lidx, r2):
        a, b, c = vig[lidx, 0], vig[lidx, 1], vig[lidx, 2]
        return 1.0 + r2 * (a + r2 * (b + r2 * c))

    def residual(x):
        vig, expo, flare = unpack(x)
        vi = falloff(vig, li, r2i)
        vj = falloff(vig, lj, r2j)
        bad = (vi < 0.05) | (vj < 0.05)
        # 미광을 뺀 뒤 배율로 나누면 장면 밝기가 남는다
        si = (Ii - flare[ei]) / np.maximum(vi, 0.05) / np.exp(expo[ei])
        sj = (Ij - flare[ej]) / np.maximum(vj, 0.05) / np.exp(expo[ej])
        res = np.log(np.maximum(si, 1e-5)) - np.log(np.maximum(sj, 1e-5))
        res = np.where(bad | (si <= 1e-5) | (sj <= 1e-5), res + 10.0, res)

        # --- 비네팅 곡선에 거는 제약 (d'Angelo 의 복사 정렬에서 쓰는 벌점들) ---
        pen = []
        for k in range(n_lens):
            v = 1.0 + r_grid2 * (vig[k, 0] + r_grid2 * (vig[k, 1] + r_grid2 * vig[k, 2]))
            # 바깥으로 갈수록 밝아지면 안 된다
            pen.append(np.maximum(np.diff(v), 0.0) * 40.0)
            # 가장자리가 지나치게 어두워지는 해도 막는다
            pen.append(np.maximum(0.35 - v[-1], 0.0) * np.array([20.0]))
            # 곡선이 출렁이지 않도록 2차 차분에 약한 벌점
            pen.append(np.diff(v, n=2) * 6.0)
        reg = [np.concatenate(pen)] if pen else []
        reg.append(expo[1:] * 0.05)
        reg.append(flare * 2.0)          # 설명이 되는 만큼만 미광을 쓰도록
        return np.concatenate([res] + reg)

    x0 = np.concatenate([np.zeros(n_lens * 3), np.zeros(n_exp), np.full(n_img, -4.0)])
    out = least_squares(residual, x0, loss="soft_l1", f_scale=0.10,
                        max_nfev=120 * (len(x0) + 1), ftol=1e-10, xtol=1e-10)
    vig, expo, flare = unpack(out.x)

    r = residual(out.x)[:len(Ii)]
    rms = float(np.sqrt(np.mean(r ** 2)))

    vignetting = {l: Vignetting(*vig[lmap[l]]) for l in lens_ids}
    exposure = {i: float(np.exp(expo[imap[i]])) for i in image_ids}
    flares = {i: float(flare[imap[i]]) for i in image_ids}
    med = float(np.median(list(exposure.values()))) or 1.0
    exposure = {i: v / med for i, v in exposure.items()}
    return vignetting, exposure, flares, rms


# ---------------------------------------------------------------- 적용

_radius_cache: dict[tuple, np.ndarray] = {}


def _radius2_map(shape: tuple[int, int], src_size: tuple[int, int]) -> np.ndarray:
    """이미지 배열 크기에 맞는 정규화 반경 제곱 맵. 같은 크기는 재사용한다."""
    key = (shape, src_size)
    m = _radius_cache.get(key)
    if m is not None:
        return m
    h, w = shape
    # 원본 기준 정규화(min(W,H)/2)를 그대로 쓰되 배열 크기에 맞춰 환산한다
    ys = (np.arange(h, dtype=np.float32) + 0.5) / h - 0.5
    xs = (np.arange(w, dtype=np.float32) + 0.5) / w - 0.5
    sw, sh = src_size
    norm = min(sw, sh) / 2.0
    gx, gy = np.meshgrid(xs * sw, ys * sh)
    m = ((gx ** 2 + gy ** 2) / (norm ** 2)).astype(np.float32)
    if len(_radius_cache) > 24:
        _radius_cache.clear()
    _radius_cache[key] = m
    return m


def _tone_lut(exposure: float, flare: float = 0.0) -> np.ndarray:
    """미광 제거와 노출 배율을 8비트 값에 정확히 적용하는 조견표.

    둘 다 화소 위치와 무관한 상수 연산이라 256칸 표 하나로 정확히 끝난다.
    미광은 선형 광량에서 빼야 하므로 감마 근사로는 처리할 수 없다.
    """
    v = np.arange(256, dtype=np.float64) / 255.0
    lin = np.maximum(srgb_to_linear(v) - float(flare), 0.0)
    out = linear_to_srgb(lin * float(exposure)) * 255.0
    return np.clip(out, 0, 255).astype(np.uint8)


def apply(img: np.ndarray, vig: Vignetting, src_size: tuple[int, int],
          exposure: float = 1.0, flare: float = 0.0) -> np.ndarray:
    """이미지에서 비네팅을 걷어내고 노출을 맞춘다.

    보정은 원래 선형 광량 공간에서 곱해야 맞다. 다만 1억 화소짜리 원본을
    sRGB <-> 선형으로 왕복시키면 거듭제곱 연산만으로 장당 몇 초가 들고,
    최종 렌더는 타일마다 원본을 다시 훑으므로 그 비용이 그대로 누적된다.
    그래서 두 성분을 나눠 다룬다.

    노출은 이미지 전체에 같은 배율이므로 256칸짜리 조견표로 정확하게 끝낸다.
    비네팅은 화소마다 배율이 달라 표를 쓸 수 없어, 감마 지수만 옮겨 한 번의
    곱셈으로 근사한다(선형에서 g 를 곱하는 것은 감마 공간에서 g^(1/2.2) 를
    곱하는 것과 같다). 이 근사의 오차는 비네팅이 실제로 다루는 배율 범위
    (대체로 1.0~1.2배)에서 8비트 값으로 1 남짓이다 — 노출까지 이 경로로
    넘기면 1.6배에서 2.8 까지 벌어지므로 그렇게 하지 않는다.

    비네팅을 '재는' 쪽(collect_samples)은 표본이 적어 비용이 없으므로
    거기서는 정확한 선형화를 그대로 쓴다.
    """
    if not vig.active and abs(exposure - 1.0) < 1e-3 and flare <= 1e-5:
        return img

    out = img
    if abs(exposure - 1.0) >= 1e-3 or flare > 1e-5:
        out = cv2.LUT(out, _tone_lut(exposure, flare))
    if vig.active:
        r2 = _radius2_map(img.shape[:2], src_size)
        gain = 1.0 / np.maximum(vig.falloff(r2), 0.08)
        gamma_gain = np.power(gain, 1.0 / 2.2).astype(np.float32)
        f = out.astype(np.float32)
        f *= gamma_gain[:, :, None]
        out = np.clip(f, 0, 255, out=f).astype(np.uint8)
    return out
