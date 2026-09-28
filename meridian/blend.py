"""노출 보정 · 심 찾기 · 블렌딩.

세 단계가 결과물의 '티 안 나는 정도'를 결정한다.

  노출 보정 : 장마다 다른 밝기를 겹침 영역 기준으로 맞춘다.
  심 찾기   : 겹침 안에서 이음선을 물체를 피해 지나가게 놓는다.
  블렌딩    : 남은 차이를 주파수 대역별로 섞어 이음선을 지운다.

심 찾기와 블렌딩은 OpenCV 의 검증된 구현을 쓰고, 노출 보정만 직접 푼다.
이미지별 게인을 EV 로 환산해 UI 에 보여주고 사용자가 손볼 수 있어야 해서다.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Patch:
    """캔버스 위에 놓인 워핑 결과 한 장."""

    image_id: int
    image: np.ndarray                 # BGR uint8
    mask: np.ndarray                  # 0/255 uint8
    corner: tuple[int, int]           # 캔버스 기준 (x, y)

    @property
    def rect(self) -> tuple[int, int, int, int]:
        h, w = self.mask.shape[:2]
        return (self.corner[0], self.corner[1], w, h)


# ---------------------------------------------------------------- 노출 보정

def solve_gains(patches: list[Patch], per_channel: bool = True,
                sigma_n: float = 5.0 / 255.0, sigma_g: float = 0.12,
                max_side: int = 400) -> np.ndarray:
    """겹침 영역의 밝기를 맞추는 이미지별 게인을 최소자승으로 푼다.

    Brown & Lowe 의 이득 보상식이다. 각 쌍의 겹침에서 평균 밝기가 같아지도록
    하되, 게인이 1 에서 멀어지는 것에 벌점을 줘 전체가 어두워지는 표류를 막는다.
    per_channel 이면 채널별로 풀어 화이트밸런스 차이까지 잡는다.
    """
    n = len(patches)
    ch = 3 if per_channel else 1
    gains = np.ones((n, 3), np.float64)
    if n < 2:
        return gains

    # 속도를 위해 축소본으로 통계만 낸다
    small: list[tuple[np.ndarray, np.ndarray, tuple[int, int], float]] = []
    for p in patches:
        h, w = p.mask.shape[:2]
        s = min(1.0, max_side / max(1, max(w, h)))
        if s < 1.0:
            img = cv2.resize(p.image, (max(1, round(w * s)), max(1, round(h * s))),
                             interpolation=cv2.INTER_AREA)
            msk = cv2.resize(p.mask, (max(1, round(w * s)), max(1, round(h * s))),
                             interpolation=cv2.INTER_NEAREST)
        else:
            img, msk = p.image, p.mask
        small.append((img.astype(np.float64) / 255.0, msk > 0, p.corner, s))

    sums = np.zeros((n, n, 3))        # sums[i][j] = 겹침에서 i 의 평균 밝기
    counts = np.zeros((n, n))
    for i in range(n):
        ia, ma, (ax, ay), sa = small[i]
        ha_i, wa_i = ma.shape
        ax_s, ay_s = ax * sa, ay * sa
        for j in range(i + 1, n):
            # 바운딩 박스로 빠르게 걸러 겹치지 않는 쌍은 건너뛴다
            ib, mb, (bx, by), sb = small[j]
            hb_j, wb_j = mb.shape
            bx_s, by_s = bx * sb, by * sb
            if ax_s + wa_i <= bx_s or bx_s + wb_j <= ax_s:
                continue
            if ay_s + ha_i <= by_s or by_s + hb_j <= ay_s:
                continue
            r = _overlap(small[i], small[j])
            if r is None:
                continue
            (ai, aj, npx) = r
            if npx < 12:
                continue
            counts[i, j] = counts[j, i] = npx
            sums[i, j] = ai
            sums[j, i] = aj

    sn2, sg2 = sigma_n ** 2, sigma_g ** 2
    for c in range(ch):
        A = np.zeros((n, n))
        b = np.zeros(n)
        for i in range(n):
            for j in range(n):
                if i == j or counts[i, j] <= 0:
                    continue
                N = counts[i, j]
                Iij, Iji = sums[i, j][c], sums[j, i][c]
                A[i, i] += N * (Iij * Iij / sn2 + 1.0 / sg2)
                A[i, j] -= N * (Iij * Iji) / sn2
                b[i] += N / sg2
        if not np.any(counts):
            break
        A += np.eye(n) * 1e-9
        try:
            g = np.linalg.solve(A, b)
        except np.linalg.LinAlgError:
            g = np.ones(n)
        g = np.clip(g, 0.25, 4.0)
        if per_channel:
            gains[:, c] = g
        else:
            gains[:] = g[:, None]
            break

    # 전체가 밝아지거나 어두워지는 쪽으로 쏠리지 않게 중앙값을 1 로 맞춘다
    gains /= max(1e-6, float(np.median(gains)))
    return np.clip(gains, 0.25, 4.0)


def _overlap(a, b):
    """두 축소 패치의 겹침에서 각자의 평균 색을 구한다."""
    ia, ma, (ax, ay), sa = a
    ib, mb, (bx, by), sb = b
    ax, ay = ax * sa, ay * sa
    bx, by = bx * sb, by * sb
    ha, wa = ma.shape
    hb, wb = mb.shape

    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + wa, bx + wb), min(ay + ha, by + hb)
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    axs, ays = int(round(x0 - ax)), int(round(y0 - ay))
    bxs, bys = int(round(x0 - bx)), int(round(y0 - by))
    w, h = int(x1 - x0), int(y1 - y0)
    w = min(w, wa - axs, wb - bxs)
    h = min(h, ha - ays, hb - bys)
    if w < 1 or h < 1:
        return None

    sub_ma = ma[ays:ays + h, axs:axs + w]
    sub_mb = mb[bys:bys + h, bxs:bxs + w]
    both = sub_ma & sub_mb
    npx = int(both.sum())
    if npx == 0:
        return None
    va = ia[ays:ays + h, axs:axs + w][both].mean(axis=0)
    vb = ib[bys:bys + h, bxs:bxs + w][both].mean(axis=0)
    return va, vb, npx


def apply_gains(patches: list[Patch], gains: np.ndarray,
                extra_ev: dict[int, float] | None = None) -> None:
    """게인을 픽셀에 반영한다. extra_ev 는 사용자가 손으로 준 보정값."""
    for k, p in enumerate(patches):
        g = gains[k].astype(np.float32)
        if extra_ev and p.image_id in extra_ev:
            g = g * float(2.0 ** extra_ev[p.image_id])
        if np.allclose(g, 1.0, atol=1e-3):
            continue
        img = p.image.astype(np.float32)
        img *= g[None, None, :]
        p.image = np.clip(img, 0, 255).astype(np.uint8)


def gains_to_ev(gains: np.ndarray) -> list[float]:
    """게인을 사람이 읽는 EV 로. UI 표시용."""
    lum = gains.mean(axis=1)
    return [float(np.log2(max(1e-6, g))) for g in lum]


# ---------------------------------------------------------------- 저주파 맞춤

def match_low_frequency(patches: list[Patch], canvas: tuple[int, int, int, int],
                        strength: float = 1.0, work_max: int = 448,
                        smooth: float = 0.16, limit: float = 1.9,
                        return_fields: bool = False, wrap_width: int = 0):
    """이미지 '안에서' 변하는 밝기 차이를 서로 맞춘다.

    이미지당 상수 게인으로는 하늘처럼 화면을 가로질러 완만하게 변하는 차이를
    잡을 수 없다. 편광, 태양 방향, 비네팅 잔차가 겹치면 같은 하늘도 장마다
    기울기가 달라지고, 그 차이가 이음선을 따라 쐐기 모양 얼룩으로 드러난다.

    그래서 겹쳐 놓은 모든 패치의 평균을 기준으로 삼고, 각 패치가 그 기준과
    어긋난 비율의 '저주파 성분만' 뽑아 되돌린다. 고주파는 손대지 않으므로
    구름이나 벽돌 무늬 같은 실제 디테일은 그대로 남는다.

    wrap_width 는 360도에 해당하는 캔버스 폭이다. 보정 필드를 만드는 흐리기
    반경은 캔버스 폭의 몇 분의 일이나 되므로, 감싸기를 모르면 왼쪽 끝과
    오른쪽 끝에 서로 다른 보정이 걸려 파노라마를 빙 돌렸을 때 그 자리에
    한 줄이 남는다.
    """
    fields: dict[int, tuple[np.ndarray, tuple[int, int], float]] = {}
    if len(patches) < 2 or strength <= 0:
        return fields if return_fields else None
    x0, y0, cw, ch = canvas
    scale = min(1.0, work_max / max(1, max(cw, ch)))
    sw, sh = max(8, round(cw * scale)), max(8, round(ch * scale))
    period = int(round(wrap_width * scale)) if wrap_width else 0
    wrap = 0 < period <= sw

    def blur(a: np.ndarray, sigma: float) -> np.ndarray:
        if not wrap:
            return cv2.GaussianBlur(a, (0, 0), sigma)
        ext = np.hstack([a[:, sw - period:], a, a[:, :period]])
        return cv2.GaussianBlur(ext, (0, 0), sigma)[:, period:period + sw]

    # 1) 축소 캔버스에 전부 더해 평균(기준)을 만든다
    acc = np.zeros((sh, sw, 3), np.float32)
    cnt = np.zeros((sh, sw, 1), np.float32)
    placed: list[tuple[np.ndarray, np.ndarray, int, int]] = []
    for p in patches:
        ph, pw = p.mask.shape[:2]
        nw, nh = max(1, round(pw * scale)), max(1, round(ph * scale))
        im = cv2.resize(p.image, (nw, nh), interpolation=cv2.INTER_AREA).astype(np.float32)
        mk = cv2.resize(p.mask, (nw, nh), interpolation=cv2.INTER_AREA)
        dx = int(round((p.corner[0] - x0) * scale))
        dy = int(round((p.corner[1] - y0) * scale))
        placed.append((im, mk, dx, dy))
        sx0, sy0 = max(0, -dx), max(0, -dy)
        dx0, dy0 = max(0, dx), max(0, dy)
        w = min(nw - sx0, sw - dx0)
        h = min(nh - sy0, sh - dy0)
        if w <= 0 or h <= 0:
            continue
        wgt = (mk[sy0:sy0 + h, sx0:sx0 + w].astype(np.float32) / 255.0)[:, :, None]
        acc[dy0:dy0 + h, dx0:dx0 + w] += im[sy0:sy0 + h, sx0:sx0 + w] * wgt
        cnt[dy0:dy0 + h, dx0:dx0 + w] += wgt
    ref = acc / np.maximum(cnt, 1e-3)

    # 2) 패치마다 기준과의 비율을 구하고 저주파만 남긴다.
    #    비율과 흐리기는 캔버스 전체에서 처리해야 감싸기를 반영할 수 있다.
    sigma = max(2.0, smooth * max(sw, sh))
    for p, (im, mk, dx, dy) in zip(patches, placed):
        nh, nw = im.shape[:2]
        sx0, sy0 = max(0, -dx), max(0, -dy)
        dx0, dy0 = max(0, dx), max(0, dy)
        w = min(nw - sx0, sw - dx0)
        h = min(nh - sy0, sh - dy0)
        if w <= 4 or h <= 4:
            continue

        own_f = np.zeros((sh, sw, 3), np.float32)
        vm_f = np.zeros((sh, sw, 1), np.float32)
        own_f[dy0:dy0 + h, dx0:dx0 + w] = im[sy0:sy0 + h, sx0:sx0 + w]
        valid = ((mk[sy0:sy0 + h, sx0:sx0 + w] > 200)
                 & (cnt[dy0:dy0 + h, dx0:dx0 + w, 0] > 1.2))
        if valid.sum() < 32:
            continue
        vm_f[dy0:dy0 + h, dx0:dx0 + w, 0] = valid.astype(np.float32)

        ratio = np.where(own_f > 1.0, ref / np.maximum(own_f, 1.0), 1.0).astype(np.float32)
        # 자르기는 반드시 마스크를 곱하기 '전'에 한다. 순서를 바꾸면 무효 영역의
        # 0 이 하한값으로 잘려 들어가 흐리기를 오염시키고, 보정이 거꾸로 걸린다.
        ratio = np.clip(ratio, 1.0 / limit, limit)
        bn = blur(ratio * vm_f, sigma)
        # 가중치는 채널이 같으므로 한 장만 흐리면 된다 (세 번 하면 그만큼 느리다)
        bd = blur(vm_f[:, :, 0], sigma)[:, :, None]
        field_f = np.where(bd > 1e-3, bn / np.maximum(bd, 1e-3), 1.0)
        field_f = 1.0 + (field_f - 1.0) * float(strength)
        field_f = np.clip(field_f, 1.0 / limit, limit)

        # 패치 좌표로 되가져온다
        full = np.ones((nh, nw, 3), np.float32)
        full[sy0:sy0 + h, sx0:sx0 + w] = field_f[dy0:dy0 + h, dx0:dx0 + w]
        if return_fields:
            # 최종 렌더는 타일 단위라 같은 필드를 나중에 다시 써야 한다.
            fields[p.image_id] = (full, (dx, dy), scale)
        up = cv2.resize(full, (p.mask.shape[1], p.mask.shape[0]), interpolation=cv2.INTER_LINEAR)
        out = p.image.astype(np.float32) * up
        p.image = np.clip(out, 0, 255).astype(np.uint8)

    return fields if return_fields else None


def apply_field(img: np.ndarray, corner: tuple[int, int],
                field: tuple[np.ndarray, tuple[int, int], float],
                canvas_origin: tuple[int, int]) -> np.ndarray:
    """미리 구해 둔 저주파 보정 필드를 이 조각에 입힌다 (타일 렌더용)."""
    arr, (fx, fy), scale = field
    h, w = img.shape[:2]
    # 이 조각이 필드 좌표계에서 차지하는 위치
    gx = (corner[0] - canvas_origin[0]) * scale - fx
    gy = (corner[1] - canvas_origin[1]) * scale - fy
    M = np.array([[1.0 / scale, 0.0, -gx / scale],
                  [0.0, 1.0 / scale, -gy / scale]], np.float32)
    up = cv2.warpAffine(arr, M, (w, h), flags=cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_REPLICATE)
    return np.clip(img.astype(np.float32) * up, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- 심 찾기

def distance_partition(patches: list[Patch], canvas: tuple[int, int, int, int],
                       work_scale: float = 0.35, wrap_width: int = 0) -> None:
    """각 화소를 '자기 안쪽 깊숙이 있는' 사진에게 준다.

    천정이나 바닥처럼 모든 사진의 가장자리가 한데 모이는 곳에서는, 쌍끼리
    이음선을 찾는 방식이 경계를 잘게 쪼갠다. 그러면 구름처럼 장마다 모습이
    다른 대상이 여러 조각으로 맞붙어 원호 모양 경계가 그대로 드러난다.

    그래서 먼저 거리 변환으로 크게 나눈다. 화소마다 '그 사진의 유효 영역
    안쪽으로 얼마나 깊은가'를 재고 가장 깊은 사진에게 준다. 경계가 단순해져
    한 사진이 넓은 면을 통째로 맡고, 각 사진의 가장자리(품질이 나쁜 쪽)는
    자연스럽게 밀려난다.

    wrap_width 는 360도에 해당하는 캔버스 폭이다. 이 값을 주면 좌우 끝을
    이어진 것으로 보고 깊이를 잰다. 이걸 빼먹으면 경계를 걸친 사진이 양쪽에
    두 조각으로 나뉘어, 실제로는 한복판인 곳이 '가장자리'로 계산된다.
    그 결과 왼쪽 끝과 오른쪽 끝에서 서로 다른 사진이 이겨 파노라마를
    빙 돌렸을 때 이음매가 한 줄 나타난다.
    """
    if len(patches) < 2:
        return
    x0, y0, cw, ch = canvas
    scale = float(np.clip(work_scale, 0.05, 1.0))
    sw, sh = max(8, round(cw * scale)), max(8, round(ch * scale))
    period = int(round(wrap_width * scale)) if wrap_width else 0
    wrap = 0 < period <= sw

    best_d = np.zeros((sh, sw), np.float32)
    best_i = np.full((sh, sw), -1, np.int16)
    boxes: list[tuple[int, int, int, int]] = []

    for k, p in enumerate(patches):
        ph, pw = p.mask.shape[:2]
        nw, nh = max(1, round(pw * scale)), max(1, round(ph * scale))
        mk = cv2.resize(p.mask, (nw, nh), interpolation=cv2.INTER_NEAREST)
        dx = int(round((p.corner[0] - x0) * scale))
        dy = int(round((p.corner[1] - y0) * scale))
        boxes.append((dx, dy, nw, nh))

        # 캔버스 전체에 놓고 깊이를 잰다. 패치 조각마다 따로 재면 감싸기를
        # 반영할 수 없고, 조각 경계가 가짜 '가장자리'가 된다.
        full = np.zeros((sh, sw), np.uint8)
        sx0, sy0 = max(0, -dx), max(0, -dy)
        dx0, dy0 = max(0, dx), max(0, dy)
        w = min(nw - sx0, sw - dx0)
        h = min(nh - sy0, sh - dy0)
        if w <= 0 or h <= 0:
            continue
        full[dy0:dy0 + h, dx0:dx0 + w] = mk[sy0:sy0 + h, sx0:sx0 + w]

        if wrap:
            # 좌우로 한 바퀴만큼 이어 붙여 재고 가운데만 되가져온다
            ext = np.hstack([full[:, sw - period:], full, full[:, :period]])
            d = cv2.distanceTransform((ext > 0).astype(np.uint8), cv2.DIST_L2, 3)
            dist = d[:, period:period + sw]
        else:
            dist = cv2.distanceTransform((full > 0).astype(np.uint8), cv2.DIST_L2, 3)

        win = dist > best_d
        best_d[win] = dist[win]
        best_i[win] = k

    for k, p in enumerate(patches):
        ph, pw = p.mask.shape[:2]
        dx, dy, nw, nh = boxes[k]
        own_full = (best_i == k).astype(np.uint8) * 255
        # 캔버스 좌표에서 패치 좌표로 되가져온다
        own = np.zeros((nh, nw), np.uint8)
        sx0, sy0 = max(0, -dx), max(0, -dy)
        dx0, dy0 = max(0, dx), max(0, dy)
        w = min(nw - sx0, sw - dx0)
        h = min(nh - sy0, sh - dy0)
        if w > 0 and h > 0:
            own[sy0:sy0 + h, sx0:sx0 + w] = own_full[dy0:dy0 + h, dx0:dx0 + w]
        up = cv2.resize(own, (pw, ph), interpolation=cv2.INTER_LINEAR)
        grow = max(2, int(round(1.0 / scale)) + 1)
        kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (grow * 2 + 1, grow * 2 + 1))
        up = cv2.dilate((up > 110).astype(np.uint8), kern)
        p.mask = ((up > 0) & (p.mask > 0)).astype(np.uint8) * 255


def find_seams(patches: list[Patch], method: str = "dp_color_grad",
               work_scale: float = 0.25) -> None:
    """겹침 안에서 이음선을 정하고 patches 의 마스크를 그에 맞게 깎는다.

    전체 해상도로 돌리면 감당이 안 되므로 축소본에서 찾고 되돌린다.
    이음선은 몇 픽셀 어긋나도 블렌딩이 덮으므로 이 정도로 충분하다.
    """
    if len(patches) < 2 or method == "none":
        return
    if method not in SAFE_SEAM_FINDERS:
        method = "dp_color_grad"      # 옛 프로젝트에 남은 값이 들어와도 안전하게
    scale = float(np.clip(work_scale, 0.05, 1.0))

    small_imgs, small_masks, corners, shapes = [], [], [], []
    for p in patches:
        h, w = p.mask.shape[:2]
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        small_imgs.append(cv2.resize(p.image, (nw, nh), interpolation=cv2.INTER_AREA
                                     ).astype(np.float32))
        small_masks.append(cv2.resize(p.mask, (nw, nh), interpolation=cv2.INTER_NEAREST))
        corners.append((int(round(p.corner[0] * scale)), int(round(p.corner[1] * scale))))
        shapes.append((w, h))

    finder = _make_finder(method)
    if finder is None:
        return
    try:
        seamed = finder.find(small_imgs, corners, small_masks)
    except cv2.error:
        return
    if seamed is None:
        seamed = small_masks
    seamed = [np.asarray(m.get() if hasattr(m, "get") else m) for m in seamed]

    grow = max(2, int(round(1.0 / scale)) + 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (grow * 2 + 1, grow * 2 + 1))
    for p, sm, (w, h) in zip(patches, seamed, shapes):
        up = cv2.resize(sm, (w, h), interpolation=cv2.INTER_LINEAR)
        m = (up > 100).astype(np.uint8)
        # 축소본에서 찾은 이음선을 되키우면 경계가 흐려져 양쪽 마스크가 동시에
        # 비는 한두 픽셀짜리 틈이 생기고, 그게 최종 결과에 검은 실선으로 남는다.
        # 서로 조금 겹치도록 부풀려서 메운다 — 블렌더는 겹침을 알아서 처리한다.
        m = cv2.dilate(m, kernel)
        # 원래 유효 영역 밖으로는 절대 새지 않게 교집합을 취한다
        p.mask = ((m > 0) & (p.mask > 0)).astype(np.uint8) * 255


# OpenCV 5 의 Python 바인딩에서 VoronoiSeamFinder 와 NoSeamFinder 는 find()
# 호출 시 널 포인터를 건드려 프로세스째 죽는다(SIGSEGV). 세그폴트는 예외로
# 잡을 수 없으므로 아예 부르지 않는 것 말고는 막을 방법이 없다.
SAFE_SEAM_FINDERS = {"distance", "dp_color_grad", "graphcut", "none"}


def _make_finder(method: str):
    try:
        if method == "graphcut":
            return cv2.detail_GraphCutSeamFinder("COST_COLOR_GRAD")
        if method == "none":
            return None               # 마스크를 그대로 둔다 (NoSeamFinder 호출 금지)
        return cv2.detail_DpSeamFinder("COLOR_GRAD")
    except cv2.error:
        return None


# ---------------------------------------------------------------- 블렌딩

def _extrapolate_edges(img: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """MultiBandBlender 의 치명적 단점인 검은 후광(Dark Halo) 번짐을 막기 위해,
    마스크 바깥쪽의 검은 허공을 가장자리 색상으로 부드럽게 채워 넣는다."""
    if mask.all():
        return img
    
    # 속도를 위해 아주 작게 줄여서 인페인팅한 뒤 다시 키운다
    scale = 0.1
    h, w = img.shape[:2]
    sh, sw = max(2, int(h * scale)), max(2, int(w * scale))
    
    small_img = cv2.resize(img, (sw, sh), interpolation=cv2.INTER_AREA)
    small_mask = cv2.resize(mask, (sw, sh), interpolation=cv2.INTER_NEAREST)
    
    inpaint_mask = (small_mask == 0).astype(np.uint8) * 255
    if not inpaint_mask.any():
        return img
        
    small_img[small_mask == 0] = 0
    inpainted = cv2.inpaint(small_img, inpaint_mask, 3, cv2.INPAINT_TELEA)
    inpainted_full = cv2.resize(inpainted, (w, h), interpolation=cv2.INTER_LINEAR)
    
    out = img.copy()
    out[mask == 0] = inpainted_full[mask == 0]
    return out


def blend(patches: list[Patch], canvas: tuple[int, int, int, int],
          mode: str = "multiband", num_bands: int = 5,
          feather_sharpness: float = 0.02) -> tuple[np.ndarray, np.ndarray]:
    """패치들을 하나의 캔버스로 합친다. (BGR uint8, 마스크) 를 돌려준다."""
    x0, y0, cw, ch = canvas
    usable = [p for p in patches if p.mask.size and p.mask.any()]
    if not usable:
        return np.zeros((ch, cw, 3), np.uint8), np.zeros((ch, cw), np.uint8)

    if mode == "none" or len(usable) == 1:
        out = np.zeros((ch, cw, 3), np.uint8)
        acc = np.zeros((ch, cw), np.uint8)
        for p in usable:
            _paste(out, acc, p, x0, y0)
        return out, acc

    if mode == "feather":
        blender = cv2.detail_FeatherBlender()
        blender.setSharpness(feather_sharpness)
    else:
        blender = cv2.detail_MultiBandBlender()
        # 대역 수가 너무 많으면 느리고, 적으면 이음선이 비친다
        auto = int(np.clip(np.log2(max(cw, ch)) - 4, 1, 7))
        blender.setNumBands(num_bands if num_bands > 0 else auto)

    blender.prepare((x0, y0, cw, ch))
    for p in usable:
        feed_img = _extrapolate_edges(p.image, p.mask) if mode == "multiband" else p.image
        blender.feed(feed_img.astype(np.int16), p.mask, p.corner)
        
    result, result_mask = blender.blend(None, None)
    result = np.clip(np.asarray(result), 0, 255).astype(np.uint8)
    return result, np.asarray(result_mask)


def _paste(dst: np.ndarray, acc: np.ndarray, p: Patch, x0: int, y0: int) -> None:
    ph, pw = p.mask.shape[:2]
    dx, dy = p.corner[0] - x0, p.corner[1] - y0
    sx0, sy0 = max(0, -dx), max(0, -dy)
    dx0, dy0 = max(0, dx), max(0, dy)
    w = min(pw - sx0, dst.shape[1] - dx0)
    h = min(ph - sy0, dst.shape[0] - dy0)
    if w <= 0 or h <= 0:
        return
    m = p.mask[sy0:sy0 + h, sx0:sx0 + w] > 0
    dst[dy0:dy0 + h, dx0:dx0 + w][m] = p.image[sy0:sy0 + h, sx0:sx0 + w][m]
    acc[dy0:dy0 + h, dx0:dx0 + w][m] = 255
