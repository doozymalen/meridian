"""번들 조정 — 제어점으로부터 모든 카메라의 자세와 렌즈를 동시에 푼다.

제어점의 어긋남을 최소로 만드는 번들 조정. 잔차는 파노라마 구면 위에서 잰다.

    이미지 i 의 제어점 -> 월드 단위광선 v_i
    이미지 j 의 짝점   -> 월드 단위광선 v_j
    잔차 = (v_i - v_j) * f_ref

두 광선이 같은 방향을 봐야 한다는 조건이다. 이미지 평면으로 되쏘는 방식보다
이쪽이 나은 이유는 왜곡 역변환(LUT 보간)을 타지 않아 잔차가 매끄럽고,
화각이 큰 어안에서도 발산하지 않기 때문이다. f_ref 를 곱해 단위를 픽셀에
맞춰 두면 사람이 읽을 때도 편하다.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix

from .camera import ImageParams, Lens, matrix_to_ypr, pixels_to_rays, rotation_matrix

# 무엇까지 함께 풀지 고르는 단계. 뒤로 갈수록 미지수가 늘어난다.
MODE_POSITION = "position"            # yaw/pitch/roll 만
MODE_POSITION_FOV = "position_fov"    # + 화각
MODE_FULL = "full"                    # + 방사 왜곡
MODE_EVERYTHING = "everything"        # + 주점 이동


@dataclass
class OptimizeResult:
    images: dict[int, ImageParams]
    lenses: dict[int, Lens]
    rms: float                        # 픽셀 단위 RMS 잔차
    max_error: float
    per_point: np.ndarray             # 제어점별 잔차(픽셀)
    iterations: int
    message: str
    converged: bool


# ---------------------------------------------------------------- 초기값

def focal_from_homography(H: np.ndarray) -> tuple[float | None, float | None]:
    """순수 회전 호모그래피에서 두 이미지의 초점거리를 추정한다.

    Szeliski 의 표준 해법. H 는 주점이 원점인 좌표계여야 한다.
    """
    h = H.ravel() / H[2, 2]
    out: list[float | None] = []

    d1, d2 = h[6] * h[7], (h[7] - h[6]) * (h[7] + h[6])
    v1 = -(h[0] * h[1] + h[3] * h[4]) / d1 if abs(d1) > 1e-12 else None
    v2 = (h[0] ** 2 + h[3] ** 2 - h[1] ** 2 - h[4] ** 2) / d2 if abs(d2) > 1e-12 else None
    out.append(_pick_focal(v1, v2, d1, d2))

    d1, d2 = h[0] * h[3] + h[1] * h[4], h[0] ** 2 + h[1] ** 2 - h[3] ** 2 - h[4] ** 2
    v1 = -h[2] * h[5] / d1 if abs(d1) > 1e-12 else None
    v2 = (h[5] ** 2 - h[2] ** 2) / d2 if abs(d2) > 1e-12 else None
    out.append(_pick_focal(v1, v2, d1, d2))
    return out[1], out[0]             # (이미지 A 의 f, 이미지 B 의 f)


def _pick_focal(v1, v2, d1, d2) -> float | None:
    cands = [(v, d) for v, d in ((v1, d1), (v2, d2)) if v is not None and v > 0]
    if not cands:
        return None
    if len(cands) == 2:
        # 분모가 큰 쪽이 수치적으로 안정적이다
        v = max(cands, key=lambda t: abs(t[1]))[0]
        return float(np.sqrt(v))
    return float(np.sqrt(cands[0][0]))


def initial_guess(cps, sizes: dict[int, tuple[int, int]],
                  fov_hints: dict[int, float], anchor: int | None = None,
                  trusted_fov: bool = False,
                  ) -> tuple[dict[int, ImageParams], float, list[list[int]]]:
    """제어점만으로 초기 자세를 잡는다.

    쌍별 호모그래피 -> 초점거리 추정 -> 상대 회전 분해 -> 최대 신장 트리로 전파.
    연결이 끊긴 묶음이 있으면 각각 따로 세우고 그룹 목록을 함께 돌려준다.
    """
    by_pair: dict[tuple[int, int], list] = defaultdict(list)
    for cp in cps:
        if cp.enabled and cp.kind in ("auto", "manual"):
            by_pair[(cp.img_a, cp.img_b)].append(cp)

    # 1) 쌍별 호모그래피와 초점거리 후보
    homographies: dict[tuple[int, int], np.ndarray] = {}
    focal_votes: dict[int, list[float]] = defaultdict(list)
    for (a, b), pts in by_pair.items():
        if len(pts) < 4:
            continue
        wa, ha = sizes[a]
        wb, hb = sizes[b]
        pa = np.array([[p.xa - wa / 2, p.ya - ha / 2] for p in pts], np.float64)
        pb = np.array([[p.xb - wb / 2, p.yb - hb / 2] for p in pts], np.float64)
        H, _ = _fit_homography(pa, pb)
        if H is None:
            continue
        homographies[(a, b)] = H
        fa, fb = focal_from_homography(H)
        for img, f, (w, h) in ((a, fa, (wa, ha)), (b, fb, (wb, hb))):
            # 터무니없는 값은 버린다 (화각 3도 ~ 200도 범위)
            if f and 0.28 * w < f < 20.0 * w:
                focal_votes[img].append(f)

    # 2) 초점거리 합의 — EXIF 힌트가 있으면 함께 고려한다
    all_f: list[float] = []
    for img, votes in focal_votes.items():
        all_f.append(float(np.median(votes)))

    hint_f = None
    if fov_hints:
        hs = []
        for img, fov in fov_hints.items():
            if img in sizes:
                w0 = sizes[img][0]
                hs.append(w0 / 2 / np.tan(np.radians(np.clip(fov, 5, 170)) / 2))
        if hs:
            hint_f = float(np.median(hs))

    # EXIF 가 믿을 만하면 그쪽을 쓴다. 호모그래피 추정은 회전각이 작거나
    # 렌즈 왜곡이 있으면 쉽게 10% 넘게 빗나가는데, 초기값이 어긋나면
    # 번들 조정이 훨씬 오래 헤맨다.
    if trusted_fov and hint_f:
        f_ref = hint_f
    elif all_f:
        f_ref = float(np.median(all_f))
    elif hint_f:
        f_ref = hint_f
    else:
        w0, h0 = next(iter(sizes.values()))
        f_ref = w0 / 2 / np.tan(np.radians(50.0) / 2)

    # 3) 최대 신장 트리로 회전 전파
    node_ids = sorted(sizes)
    adj: dict[int, list[tuple[int, int, np.ndarray, bool]]] = defaultdict(list)
    for (a, b), H in homographies.items():
        n = len(by_pair[(a, b)])
        adj[a].append((n, b, H, True))
        adj[b].append((n, a, H, False))

    params = {i: ImageParams() for i in node_ids}
    seen: set[int] = set()
    groups: list[list[int]] = []
    order = sorted(node_ids, key=lambda i: -sum(e[0] for e in adj[i]))
    if anchor is not None and anchor in node_ids:
        order = [anchor] + [i for i in order if i != anchor]

    for root in order:
        if root in seen:
            continue
        seen.add(root)
        group = [root]
        rot = {root: np.eye(3)}
        frontier = [(-sum(e[0] for e in adj[root]), root)]
        while frontier:
            frontier.sort()
            _, cur = frontier.pop(0)
            for n, nxt, H, forward in sorted(adj[cur], key=lambda e: -e[0]):
                if nxt in seen:
                    continue
                R_rel = _rotation_from_homography(H if forward else np.linalg.inv(H),
                                                  f_ref, f_ref)
                if R_rel is None:
                    continue
                rot[nxt] = rot[cur] @ R_rel
                seen.add(nxt)
                group.append(nxt)
                frontier.append((-n, nxt))
        for img, R in rot.items():
            y, p, r = matrix_to_ypr(R)
            params[img] = ImageParams(yaw=y, pitch=p, roll=r)
        groups.append(sorted(group))

    return params, f_ref, groups


def _fit_homography(pa: np.ndarray, pb: np.ndarray):
    import cv2
    if len(pa) < 4:
        return None, None
    H, mask = cv2.findHomography(pa, pb, cv2.RANSAC, 3.0, maxIters=4000, confidence=0.999)
    return (H.astype(np.float64) if H is not None else None), mask


def _rotation_from_homography(H: np.ndarray, fa: float, fb: float) -> np.ndarray | None:
    """H (A -> B) 와 초점거리로부터 상대 회전 R (B -> A 방향 카메라 자세)."""
    Ka = np.diag([fa, fa, 1.0])
    Kb = np.diag([fb, fb, 1.0])
    M = np.linalg.inv(Kb) @ H @ Ka
    # 가장 가까운 회전 행렬로 사영 (직교 프로크루스테스)
    u, s, vt = np.linalg.svd(M)
    if s[2] < 1e-9:
        return None
    R = u @ vt
    if np.linalg.det(R) < 0:
        u[:, 2] *= -1
        R = u @ vt
    return R.T                        # A 기준에서 본 B 의 자세


# ---------------------------------------------------------------- 최적화

class _Problem:
    """파라미터 벡터 <-> 카메라 객체 변환과 잔차 계산을 담는다."""

    def __init__(self, cps, images: dict[int, ImageParams], lenses: dict[int, Lens],
                 sizes: dict[int, tuple[int, int]], mode: str, anchor: int,
                 f_ref: float):
        self.sizes = sizes
        self.mode = mode
        self.anchor = anchor
        self.f_ref = f_ref
        self.img_ids = sorted(images)
        self.lens_ids = sorted(lenses)
        self.images = {i: ImageParams(**vars(p)) for i, p in images.items()}
        self.lenses = {i: Lens(**vars(l)) for i, l in lenses.items()}

        self.pair_cps = [c for c in cps if c.enabled and c.kind in ("auto", "manual")]
        self.line_cps = [c for c in cps if c.enabled and c.kind in ("vertical", "horizontal")]

        # 잔차 함수는 야코비안 한 번에 파라미터 수만큼 호출된다. 제어점 배열을
        # 매번 다시 만들면 그 파이썬 루프가 전체 시간을 잡아먹으므로 여기서
        # 이미지별로 한 번만 정리해 둔다.
        self._side_a = self._group_points(True)
        self._side_b = self._group_points(False)
        self._weights = np.array([c.weight for c in self.pair_cps], np.float64)[:, None] \
            if self.pair_cps else np.zeros((0, 1))

        self.n_lens_params = {MODE_POSITION: 0, MODE_POSITION_FOV: 1,
                              MODE_FULL: 4, MODE_EVERYTHING: 6}[mode]
        self.img_slots = {i: k for k, i in enumerate(self.img_ids)}
        self.lens_slots = {i: k for k, i in enumerate(self.lens_ids)}

    def _group_points(self, first: bool) -> list[tuple[int, np.ndarray, np.ndarray]]:
        """(이미지 id, 해당 제어점 인덱스, 픽셀 좌표) 목록으로 미리 묶는다."""
        buckets: dict[int, list[int]] = defaultdict(list)
        for n, c in enumerate(self.pair_cps):
            buckets[c.img_a if first else c.img_b].append(n)
        out = []
        for img, idxs in buckets.items():
            arr = np.array(idxs, np.int64)
            pts = np.array([[self.pair_cps[n].xa, self.pair_cps[n].ya] if first
                            else [self.pair_cps[n].xb, self.pair_cps[n].yb]
                            for n in idxs], np.float64)
            out.append((img, arr, pts))
        return out

    # -- 벡터 변환 ------------------------------------------------
    def pack(self) -> np.ndarray:
        x = []
        for i in self.img_ids:
            p = self.images[i]
            x.extend([p.yaw, p.pitch, p.roll])
        for li in self.lens_ids:
            l = self.lenses[li]
            if self.n_lens_params >= 1:
                x.append(np.radians(l.fov))
            if self.n_lens_params >= 4:
                x.extend([l.a, l.b, l.c])
            if self.n_lens_params >= 6:
                x.extend([l.cx / 1000.0, l.cy / 1000.0])
        return np.array(x, np.float64)

    def unpack(self, x: np.ndarray) -> None:
        k = 0
        for i in self.img_ids:
            p = self.images[i]
            if i == self.anchor:
                p.yaw = p.pitch = p.roll = 0.0     # 게이지 고정
            else:
                p.yaw, p.pitch, p.roll = x[k], x[k + 1], x[k + 2]
            k += 3
        for li in self.lens_ids:
            l = self.lenses[li]
            if self.n_lens_params >= 1:
                l.fov = float(np.clip(np.degrees(x[k]), 3.0, 359.0)); k += 1
            if self.n_lens_params >= 4:
                l.a, l.b, l.c = float(x[k]), float(x[k + 1]), float(x[k + 2]); k += 3
            if self.n_lens_params >= 6:
                l.cx, l.cy = float(x[k]) * 1000.0, float(x[k + 1]) * 1000.0; k += 2

    # -- 잔차 ------------------------------------------------------
    def rays_for(self, img_id: int, pts: np.ndarray) -> np.ndarray:
        p = self.images[img_id]
        lens = self.lenses[p.lens_id]
        w, h = self.sizes[img_id]
        return pixels_to_rays(pts, lens, w, h, rotation_matrix(p.yaw, p.pitch, p.roll))

    def residuals(self, x: np.ndarray) -> np.ndarray:
        self.unpack(x)
        out: list[np.ndarray] = []

        if self.pair_cps:
            va = np.empty((len(self.pair_cps), 3))
            vb = np.empty((len(self.pair_cps), 3))
            for img, idxs, pts in self._side_a:
                va[idxs] = self.rays_for(img, pts)
            for img, idxs, pts in self._side_b:
                vb[idxs] = self.rays_for(img, pts)
            out.append(((va - vb) * self.f_ref * self._weights).ravel())

        for c in self.line_cps:
            # 같은 이미지 안의 두 점이 수직(또는 수평)선 위에 있어야 한다는 조건
            v = self.rays_for(c.img_a, np.array([[c.xa, c.ya], [c.xb, c.yb]]))
            lon = np.arctan2(v[:, 0], v[:, 2])
            lat = np.arcsin(np.clip(v[:, 1], -1, 1))
            if c.kind == "vertical":
                d = np.arctan2(np.sin(lon[0] - lon[1]), np.cos(lon[0] - lon[1]))
            else:
                d = lat[0] - lat[1]
            out.append(np.array([d * self.f_ref * c.weight]))

        if not out:
            return np.zeros(1)
        return np.concatenate(out)

    def sparsity(self, n_res: int, n_par: int) -> lil_matrix:
        """야코비안 희소 구조. 이게 없으면 이미지 100장에서 수치미분이 못 끝난다."""
        S = lil_matrix((n_res, n_par), dtype=int)
        n_img = len(self.img_ids)
        lens_base = n_img * 3

        def lens_cols(lens_id: int) -> list[int]:
            if self.n_lens_params == 0:
                return []
            s = lens_base + self.lens_slots[lens_id] * self.n_lens_params
            return list(range(s, s + self.n_lens_params))

        row = 0
        for c in self.pair_cps:
            ca = self.img_slots[c.img_a] * 3
            cb = self.img_slots[c.img_b] * 3
            cols = [ca, ca + 1, ca + 2, cb, cb + 1, cb + 2]
            cols += lens_cols(self.images[c.img_a].lens_id)
            cols += lens_cols(self.images[c.img_b].lens_id)
            for r in range(row, row + 3):
                for col in set(cols):
                    S[r, col] = 1
            row += 3
        for c in self.line_cps:
            ca = self.img_slots[c.img_a] * 3
            cols = [ca, ca + 1, ca + 2] + lens_cols(self.images[c.img_a].lens_id)
            for col in set(cols):
                S[row, col] = 1
            row += 1
        return S


def optimize(cps, images: dict[int, ImageParams], lenses: dict[int, Lens],
             sizes: dict[int, tuple[int, int]], mode: str = MODE_POSITION_FOV,
             anchor: int | None = None, max_iter: int = 60,
             robust: bool = True) -> OptimizeResult:
    """번들 조정 본체."""
    if anchor is None:
        anchor = sorted(images)[0]
    lens0 = lenses[images[anchor].lens_id]
    w0, h0 = sizes[anchor]
    f_ref = lens0.focal_px(w0, h0)

    prob = _Problem(cps, images, lenses, sizes, mode, anchor, f_ref)
    x0 = prob.pack()
    n_res = len(prob.residuals(x0))
    S = prob.sparsity(n_res, len(x0))

    res = least_squares(
        prob.residuals, x0,
        jac_sparsity=S,
        method="trf",
        loss="soft_l1" if robust else "linear",
        f_scale=8.0,                   # 8px 를 넘어가면 아웃라이어로 눌러준다
        x_scale="jac",
        max_nfev=max_iter * (len(x0) + 1),
        ftol=1e-5, xtol=1e-5, gtol=1e-5,
    )
    prob.unpack(res.x)

    per_point = point_errors(prob)
    rms = float(np.sqrt(np.mean(per_point ** 2))) if per_point.size else 0.0
    return OptimizeResult(
        images=prob.images, lenses=prob.lenses, rms=rms,
        max_error=float(per_point.max()) if per_point.size else 0.0,
        per_point=per_point, iterations=int(res.nfev),
        message=str(res.message), converged=bool(res.success),
    )


def point_errors(prob: _Problem) -> np.ndarray:
    """제어점별 잔차 크기(픽셀). UI 에서 나쁜 점을 골라낼 때 쓴다."""
    errs: list[float] = []
    for c in prob.pair_cps:
        va = prob.rays_for(c.img_a, np.array([[c.xa, c.ya]]))[0]
        vb = prob.rays_for(c.img_b, np.array([[c.xb, c.yb]]))[0]
        errs.append(float(np.linalg.norm(va - vb) * prob.f_ref))
    for c in prob.line_cps:
        v = prob.rays_for(c.img_a, np.array([[c.xa, c.ya], [c.xb, c.yb]]))
        lon = np.arctan2(v[:, 0], v[:, 2])
        lat = np.arcsin(np.clip(v[:, 1], -1, 1))
        d = (np.arctan2(np.sin(lon[0] - lon[1]), np.cos(lon[0] - lon[1]))
             if c.kind == "vertical" else lat[0] - lat[1])
        errs.append(float(abs(d) * prob.f_ref))
    return np.array(errs)


def straighten(images: dict[int, ImageParams]) -> dict[int, ImageParams]:
    """수평 맞추기 — 카메라 위쪽 벡터들의 주성분으로 파노라마를 세운다.

    지평선을 수평으로 되돌린다. 삼각대가 수평이 아니었을 때
    지평선이 물결치는 것을 잡아준다.
    """
    rights, downs = [], []
    for p in images.values():
        R = rotation_matrix(p.yaw, p.pitch, p.roll)
        rights.append(R[:, 0])        # 카메라의 '오른쪽' 방향 (월드)
        downs.append(R[:, 1])         # 카메라의 '아래' 방향 (월드)
    if len(rights) < 2:
        return images

    # 카메라를 롤 없이 돌려 찍었다면 '오른쪽' 벡터는 모두 수평면 안에 있다.
    # 따라서 그 벡터들이 이루는 평면의 법선이 곧 수직축이다. SVD 의 최소
    # 특이벡터가 그 법선이다 — 최대 쪽을 쓰면 수직축이 아니라 벡터들이 가장
    # 넓게 퍼진 '수평' 방향이 잡혀 파노라마가 통째로 눕는다.
    M = np.array(rights)
    _, _, vt = np.linalg.svd(M)
    axis = vt[2] / np.linalg.norm(vt[2])

    # 법선의 부호는 평균 '아래' 방향으로 맞춘다
    if float(np.dot(axis, np.mean(downs, axis=0))) < 0:
        axis = -axis
    target = np.array([0.0, 1.0, 0.0])
    v = np.cross(axis, target)
    s, c = np.linalg.norm(v), float(np.dot(axis, target))
    if s < 1e-9:
        return images
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    R_fix = np.eye(3) + vx + vx @ vx * ((1 - c) / s ** 2)

    out = {}
    for i, p in images.items():
        y, pi, r = matrix_to_ypr(R_fix @ rotation_matrix(p.yaw, p.pitch, p.roll))
        q = ImageParams(**vars(p))
        q.yaw, q.pitch, q.roll = y, pi, r
        out[i] = q
    return out
