"""특징점 검출·매칭 — 자동 제어점 생성.

오매칭을 줄이려고 세 가지를 쓴다.

  1. 자기-모호 특징점 사전 제거
     같은 이미지 안에서 디스크립터가 너무 비슷한 점들을 매칭 전에 미리 버린다.
     나뭇잎·벽돌 등 반복 패턴이 만드는 오매칭의 근본 원인을 차단한다.

  2. 양방향 일관성 검증 (상호 최근접)
     A→B 최근접과 B→A 최근접이 서로를 가리킬 때만 유효 매칭으로 받아들인다.
     한쪽만 닮은 허위 매칭을 걸러내 RANSAC 의 부담을 줄인다.

  3. 제어점 서브픽셀 정밀 조정
     RANSAC 을 통과한 제어점에 대해 원본 프록시의 작은 패치를 잘라 NCC
     (정규화 교차 상관)로 ±수 픽셀 범위를 탐색해 가장 잘 맞는 위치를 찾는다.
     번들 조정의 정밀도가 올라가 이음새가 줄어든다.

이미지가 수백 장인 기가픽셀 작업에서 모든 쌍을 매칭하면 O(n^2) 로 터진다.
그래서 Brown & Lowe 의 "Recognising Panoramas" 방식을 쓴다.

  1. 모든 이미지의 디스크립터를 하나의 FLANN 인덱스에 넣는다.
  2. 각 디스크립터의 최근접 이웃 k 개를 찾아 어떤 이미지 쌍이 겹치는지 투표한다.
  3. 표를 많이 받은 쌍만 RANSAC 기하 검증을 돌린다.

이러면 매칭 비용이 쌍 수가 아니라 전체 특징점 수에 비례한다.
"""

from __future__ import annotations

import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path

import cv2
import numpy as np


@dataclass
class ControlPoint:
    """제어점 한 쌍. 좌표는 항상 '원본 픽셀' 기준으로 저장한다."""

    img_a: int
    img_b: int
    xa: float
    ya: float
    xb: float
    yb: float
    kind: str = "auto"          # auto | manual | vertical | horizontal
    weight: float = 1.0
    enabled: bool = True
    error: float = 0.0          # 최근 번들 조정의 잔차(픽셀)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FeatureSet:
    image_id: int
    keypoints: np.ndarray       # (N,2) 프록시 좌표
    descriptors: np.ndarray     # (N,128) float32
    scale: float                # 프록시 -> 원본 배율
    size: tuple[int, int]       # 프록시 (w, h)


def detect(image: np.ndarray, image_id: int, scale: float,
           max_features: int = 5000, mask: np.ndarray | None = None) -> FeatureSet:
    """프록시 한 장에서 SIFT 특징점을 뽑고 공간적으로 고르게 분산시킨다.

    대비가 강한 곳(예: 죽녹원의 굵은 대나무 줄기)에만 특징점이 몰리면,
    사진의 가장자리나 하늘/땅 부분은 제어점이 없어 스티칭이 어긋난다.
    이미지를 격자로 나누어 각 구역마다 골고루 특징점을 남긴다.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    # 충분히 많이 뽑은 뒤 격자 필터링으로 솎아낸다
    sift = cv2.SIFT_create(nfeatures=int(max_features * 1.5), contrastThreshold=0.012, edgeThreshold=16)
    kps, desc = sift.detectAndCompute(gray, mask)
    if desc is None or len(kps) == 0:
        return FeatureSet(image_id, np.zeros((0, 2), np.float32),
                          np.zeros((0, 128), np.float32), scale, (gray.shape[1], gray.shape[0]))

    pts = np.array([k.pt for k in kps], dtype=np.float32)
    desc = np.asarray(desc, dtype=np.float32)
    
    # 응답(response) 값이 강한 순서대로 정렬
    responses = np.array([k.response for k in kps], dtype=np.float32)
    order = np.argsort(-responses)
    pts = pts[order]
    desc = desc[order]

    # --- 공간적 고른 분산 (Grid-based distribution) ---
    # 이미지를 8x8 격자로 나누고 각 칸마다 할당량을 두어 특징점이 한곳에 몰리는 것을 막는다.
    grid_size = 8
    h, w = gray.shape
    cell_w, cell_h = w / grid_size, h / grid_size
    per_cell = max(1, max_features // (grid_size * grid_size))
    
    counts = defaultdict(int)
    keep = []
    for i, (x, y) in enumerate(pts):
        gx, gy = int(x // cell_w), int(y // cell_h)
        if counts[(gx, gy)] < per_cell:
            counts[(gx, gy)] += 1
            keep.append(i)
        if len(keep) >= max_features:
            break
            
    # 할당량을 못 채운 칸이 있으면, 남은 특징점 중에서 강한 순서대로 추가로 채운다
    if len(keep) < max_features:
        keep_set = set(keep)
        for i in range(len(pts)):
            if len(keep) >= max_features:
                break
            if i not in keep_set:
                keep.append(i)
                keep_set.add(i)

    keep = np.array(keep, dtype=int)
    pts = pts[keep]
    desc = desc[keep]

    # RootSIFT: L1 정규화 후 제곱근. 유클리드 거리가 곧 헬링거 거리가 되어 매칭이 는다.
    desc /= np.maximum(desc.sum(axis=1, keepdims=True), 1e-7)
    desc = np.sqrt(desc)

    # 반복 패턴이 만드는 오매칭을 뿌리에서 자른다 (_remove_self_ambiguous 참고)
    if len(desc) > 10:
        pts, desc = _remove_self_ambiguous(pts, desc)

    return FeatureSet(image_id, pts, desc, scale, (gray.shape[1], gray.shape[0]))


def _remove_self_ambiguous(pts: np.ndarray, desc: np.ndarray,
                           ratio_thresh: float = 0.95,
                           min_dist: float = 30.0) -> tuple[np.ndarray, np.ndarray]:
    """같은 이미지 안에서 너무 비슷한 디스크립터를 가진 점을 제거한다.

    나뭇잎, 창문, 벽돌 등 반복 패턴의 특징점은 이미지 내부에 자기와
    거의 똑같은 디스크립터가 여러 개 존재한다. 이런 점은 다른 이미지의
    비슷한 반복 패턴과도 쉽게 매칭되어 대량 오매칭의 원인이 된다.

    1-NN (자기 자신 제외) 거리 / 2-NN 거리 비율이 ratio_thresh 이상이면서
    물리적 거리가 min_dist 이상이면 모호한 점으로 판단해 제거한다.
    (물리적으로 가까운 점은 텍스처 경계의 정상적 중복이다.)
    """
    n = len(desc)
    if n < 3:
        return pts, desc

    # 이미지 내부 KNN (자기 자신 포함 3개)
    index = cv2.flann_Index(desc, dict(algorithm=1, trees=4))
    k = min(3, n)
    idx, dist = index.knnSearch(desc, k, params=dict(checks=32))
    dist = np.sqrt(np.maximum(dist, 0.0))

    keep = np.ones(n, dtype=bool)
    for q in range(n):
        # idx[q,0]은 자기 자신이므로 idx[q,1]이 실제 1-NN
        if k < 3:
            continue
        d1 = dist[q, 1]  # 가장 가까운 다른 점
        d2 = dist[q, 2]  # 두 번째로 가까운 점

        if d2 < 1e-9:
            # 2-NN도 거의 같은 디스크립터 → 확실히 반복 패턴
            keep[q] = False
            continue

        ratio = d1 / d2
        # 비율이 높으면 1-NN과 2-NN이 비슷하게 가깝다는 뜻
        # → 이 특징점과 닮은 점이 여럿 있다 → 모호하다
        if ratio > ratio_thresh:
            # 물리적으로 가까운 건 같은 물체의 경계이므로 괜찮다
            nn_idx = idx[q, 1]
            spatial_dist = float(np.linalg.norm(pts[q] - pts[nn_idx]))
            if spatial_dist > min_dist:
                keep[q] = False

    return pts[keep], desc[keep]


# ---------------------------------------------------------------- 후보 쌍 찾기

def candidate_pairs(sets: list[FeatureSet], neighbors: int = 12,
                    min_votes: int = 8,
                    max_pairs_per_image: int = 10) -> dict[tuple[int, int], list[tuple[int, int]]]:
    """전역 인덱스로 겹칠 법한 쌍과 그 잠정 매칭을 찾는다.

    상호 최근접만 받아들인다. A의 특징점 a 가 B의 b 를 최근접으로 가리키고, B의 b 도 A의 a 를
    최근접으로 가리킬 때만 유효 매칭으로 인정한다.

    반환: {(a, b): [(a 쪽 특징점 인덱스, b 쪽 인덱스), ...]}  (a < b)
    """
    usable = [s for s in sets if len(s.descriptors) >= 8]
    if len(usable) < 2:
        return {}

    all_desc = np.vstack([s.descriptors for s in usable])
    owner = np.concatenate([np.full(len(s.descriptors), i, np.int32) for i, s in enumerate(usable)])
    local = np.concatenate([np.arange(len(s.descriptors), dtype=np.int32) for s in usable])

    n = len(all_desc)
    
    # 겹치는 이미지들(bamboo 같이 반복 패턴)을 찾기 위해 검색 개수(k)를 넉넉하게 잡는다.
    # 원래 neighbors * 3 + 1 이었으나 너무 작아서 반복 패턴에서 끊어지므로 150까지 허용
    k = min(150, n)
    index = cv2.flann_Index(all_desc, dict(algorithm=1, trees=4))   # KD-tree
    idx, dist = index.knnSearch(all_desc, k, params=dict(checks=64))
    dist = np.sqrt(np.maximum(dist, 0.0))

    # 1차: 각 특징점의 이미지별 최근접 후보들 수집
    # best_for[q] = {상대이미지인덱스: [(거리, 전역인덱스, 로컬인덱스), ...]}
    from collections import defaultdict
    best_for: list[dict[int, list[tuple[float, int, int]]]] = [defaultdict(list) for _ in range(n)]
    
    for q in range(n):
        qi = owner[q]
        for slot in range(k):
            t = idx[q, slot]
            if t < 0 or t == q:
                continue
            ti = owner[t]
            if ti == qi:
                continue
            d = dist[q, slot]
            best_for[q][ti].append((d, t, local[t]))
            
    # 2차: 이미지 쌍별(Pairwise) 비율 검증(Lowe's ratio) 및 양방향 일관성(Mutual Best Match)
    votes: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for q in range(n):
        qi = owner[q]
        for ti, matches in best_for[q].items():
            if not matches:
                continue
            # 가장 가까운 매칭 2개를 비교해 모호함을 거른다 (Pairwise Lowe's Ratio)
            # 파이썬 sort()는 짧은 리스트에서 매우 빠르다.
            matches.sort(key=lambda x: x[0])
            d1, t_global, b_li = matches[0]
            
            if len(matches) > 1:
                d2 = matches[1][0]
                if d1 > d2 * 0.8:
                    continue  # 이 상대 이미지와는 확실한 매칭이 아님
                    
            # 양방향 일관성: 상대방(t_global)도 나(q)를 가장 가까운 것으로 보는지 확인
            ti_matches = best_for[t_global].get(qi, [])
            if not ti_matches:
                continue
                
            ti_matches.sort(key=lambda x: x[0])
            _, rev_global, _ = ti_matches[0]
            if rev_global != q:
                continue  # 양방향 일치 실패
                
            # 통과!
            a_gi, b_gi = usable[qi].image_id, usable[ti].image_id
            a_li = local[q]
            if a_gi < b_gi:
                votes[(a_gi, b_gi)].append((int(a_li), int(b_li)))
            else:
                votes[(b_gi, a_gi)].append((int(b_li), int(a_li)))

    # 이미지당 표가 많은 쌍만 남긴다
    keep: dict[tuple[int, int], list[tuple[int, int]]] = {}
    per_image: dict[int, list[tuple[int, tuple[int, int]]]] = defaultdict(list)
    for pair, m in votes.items():
        if len(m) >= min_votes:
            per_image[pair[0]].append((len(m), pair))
            per_image[pair[1]].append((len(m), pair))
    allowed: set[tuple[int, int]] = set()
    for _, lst in per_image.items():
        lst.sort(reverse=True)
        for _, pair in lst[:max_pairs_per_image]:
            allowed.add(pair)
    for pair in allowed:
        keep[pair] = list(dict.fromkeys(votes[pair]))
    return keep


# ---------------------------------------------------------------- 기하 검증

def verify_pair(fa: FeatureSet, fb: FeatureSet, matches: list[tuple[int, int]],
                reproj_thresh: float = 3.0, min_inliers: int = 8,
                max_points: int = 40) -> list[ControlPoint]:
    """RANSAC 호모그래피로 매칭을 검증하고 제어점으로 만든다.

    순수 회전으로 찍은 파노라마라면 두 장 사이 관계는 정확히 호모그래피다.
    그래서 여기서 걸러지는 매칭은 시차나 오매칭으로 보면 된다.
    """
    if len(matches) < min_inliers:
        return []
    ia = np.array([m[0] for m in matches], np.int32)
    ib = np.array([m[1] for m in matches], np.int32)
    pa, pb = fa.keypoints[ia], fb.keypoints[ib]

    H, mask = cv2.findHomography(pa, pb, cv2.USAC_MAGSAC, reproj_thresh,
                                 maxIters=8000, confidence=0.9995)
    if H is None or mask is None:
        return []
    inl = mask.ravel().astype(bool)
    n_in, n_f = int(inl.sum()), len(matches)
    if n_in < max(min_inliers, 12) or (n_in < 25 and n_in < 0.1 * n_f):
        return []

    pa, pb = pa[inl], pb[inl]
    if len(pa) > max_points:
        pa, pb = _spatial_thin(pa, pb, fa.size, max_points)

    return [ControlPoint(fa.image_id, fb.image_id,
                         float(x1 * fa.scale), float(y1 * fa.scale),
                         float(x2 * fb.scale), float(y2 * fb.scale))
            for (x1, y1), (x2, y2) in zip(pa, pb)]


def _spatial_thin(pa: np.ndarray, pb: np.ndarray, size: tuple[int, int],
                  target: int) -> tuple[np.ndarray, np.ndarray]:
    """제어점이 한쪽에 뭉치면 번들 조정이 그 구역만 맞춘다. 격자로 고르게 남긴다."""
    w, h = size
    g = max(1, int(np.ceil(np.sqrt(target))))
    cell = defaultdict(list)
    for i, (x, y) in enumerate(pa):
        cell[(min(g - 1, int(x / w * g)), min(g - 1, int(y / h * g)))].append(i)
    picked: list[int] = []
    per = max(1, target // max(1, len(cell)))
    for _, ids in cell.items():
        picked.extend(ids[:per])
    if len(picked) < target:
        rest = [i for i in range(len(pa)) if i not in set(picked)]
        picked.extend(rest[:target - len(picked)])
    picked = picked[:target]
    return pa[picked], pb[picked]


# ---------------------------------------------------------------- 서브픽셀 정밀 조정

def fine_tune_points(cps: list[ControlPoint],
                     proxies: dict[int, np.ndarray],
                     scales: dict[int, float],
                     patch_radius: int = 16,
                     search_radius: int = 4) -> list[ControlPoint]:
    """NCC 패치 매칭으로 제어점의 서브픽셀 정밀도를 올린다.

    RANSAC 이 통과시킨 제어점의 좌표를 원본 프록시 패치에서 NCC(정규화
    교차 상관)로 ±search_radius 범위를 탐색해 가장 잘 맞는 위치로 옮긴다.
    번들 조정의 수렴이 빨라지고 잔차가 줄어든다.
    """
    import os
    from concurrent.futures import ThreadPoolExecutor
    
    def _tune_one(cp: ControlPoint) -> ControlPoint:
        if cp.img_a not in proxies or cp.img_b not in proxies:
            return cp
            
        img_a = proxies[cp.img_a]
        img_b = proxies[cp.img_b]
        sa = scales.get(cp.img_a, 1.0)
        sb = scales.get(cp.img_b, 1.0)
        
        xa_p, ya_p = cp.xa / sa, cp.ya / sa
        xb_p, yb_p = cp.xb / sb, cp.yb / sb
        
        dx, dy, score = _ncc_refine(img_a, img_b, xa_p, ya_p, xb_p, yb_p,
                                     patch_radius, search_radius)
        if score > 0.5:
            new_cp = ControlPoint(
                cp.img_a, cp.img_b,
                float((xb_p + dx) * sb), float((yb_p + dy) * sb),
                cp.xa, cp.ya,
                cp.kind, cp.weight, cp.enabled, cp.error,
            )
            new_cp.xa = cp.xa
            new_cp.ya = cp.ya
            new_cp.xb = float((xb_p + dx) * sb)
            new_cp.yb = float((yb_p + dy) * sb)
            return new_cp
        return cp

    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        refined = list(pool.map(_tune_one, cps))
        
    return refined


def _ncc_refine(img_a: np.ndarray, img_b: np.ndarray,
                xa: float, ya: float, xb: float, yb: float,
                patch_r: int, search_r: int) -> tuple[float, float, float]:
    """img_a 의 (xa, ya) 주변 패치를 img_b 에서 NCC 탐색해 오프셋을 반환한다."""
    ha, wa = img_a.shape[:2]
    hb, wb = img_b.shape[:2]

    # 그레이스케일
    ga = cv2.cvtColor(img_a, cv2.COLOR_BGR2GRAY) if img_a.ndim == 3 else img_a
    gb = cv2.cvtColor(img_b, cv2.COLOR_BGR2GRAY) if img_b.ndim == 3 else img_b

    # A 에서 패치 추출
    ax, ay = int(round(xa)), int(round(ya))
    r = patch_r
    if ax - r < 0 or ay - r < 0 or ax + r >= wa or ay + r >= ha:
        return 0.0, 0.0, 0.0
    template = ga[ay - r:ay + r + 1, ax - r:ax + r + 1].astype(np.float32)

    # B 에서 탐색 영역 추출
    bx, by = int(round(xb)), int(round(yb))
    s = search_r
    x0 = max(0, bx - r - s)
    y0 = max(0, by - r - s)
    x1 = min(wb, bx + r + s + 1)
    y1 = min(hb, by + r + s + 1)
    if x1 - x0 < template.shape[1] or y1 - y0 < template.shape[0]:
        return 0.0, 0.0, 0.0
    search_area = gb[y0:y1, x0:x1].astype(np.float32)

    # NCC 매칭
    result = cv2.matchTemplate(search_area, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)

    # 최고점의 서브픽셀 위치 계산
    best_x = x0 + max_loc[0] + r
    best_y = y0 + max_loc[1] + r

    dx = best_x - bx
    dy = best_y - by
    return float(dx), float(dy), float(max_val)


# ---------------------------------------------------------------- 전체 파이프라인

def find_control_points(proxies: dict[int, np.ndarray], scales: dict[int, float],
                        progress=None, max_features: int = 5000) -> tuple[list[ControlPoint], dict]:
    """프록시 묶음 -> 제어점 전체. progress(단계, 진행률, 메시지) 로 상황을 알린다.

    3단계 파이프라인:
      1. SIFT 검출 + 자기-모호 특징점 제거
      2. FLANN 전역 매칭 + 양방향 일관성 검증
      3. RANSAC 기하 검증 + NCC 서브픽셀 정밀 조정
    """
    def note(stage, frac, msg):
        if progress:
            progress(stage, frac, msg)

    # 1단계: 특징점 검출 + 자기-모호 제거
    sets: list[FeatureSet] = []
    ids = sorted(proxies)
    import os
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    n_detect_workers = max(1, os.cpu_count() or 4)
    with ThreadPoolExecutor(max_workers=n_detect_workers) as pool:
        futures = {pool.submit(detect, proxies[img_id], img_id, scales.get(img_id, 1.0), max_features): img_id for img_id in ids}
        completed = 0
        for fut in as_completed(futures):
            sets.append(fut.result())
            completed += 1
            note("detect", completed / len(ids), f"특징점 검출 {completed}/{len(ids)}장")

    # 2단계: 후보 쌍 찾기 (양방향 일관성 적용)
    note("match", 0.0, "겹치는 쌍 찾는 중…")
    pairs = candidate_pairs(sets)
    by_id = {s.image_id: s for s in sets}

    # 3단계: 기하 검증 — 쌍별로 병렬 실행
    cps: list[ControlPoint] = []
    stats = {"pairs_tested": len(pairs), "pairs_kept": 0,
             "features": {s.image_id: len(s.keypoints) for s in sets}}
    n_workers = max(1, min(len(pairs), (os.cpu_count() or 4)))

    def _verify(pair_matches):
        pair, matches = pair_matches
        a, b = pair
        return pair, verify_pair(by_id[a], by_id[b], matches)

    verified_count = 0
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(_verify, (pair, matches))
                   for pair, matches in sorted(pairs.items())]
        for fut in as_completed(futures):
            pair, found = fut.result()
            if found:
                cps.extend(found)
                stats["pairs_kept"] += 1
            verified_count += 1
            note("verify", verified_count / max(1, len(pairs)),
                 f"쌍 검증 {verified_count}/{len(pairs)} — 제어점 {len(cps)}개")

    # 4단계: NCC 서브픽셀 정밀 조정
    if cps:
        note("refine", 0.0, "제어점 정밀 조정 중…")
        cps = fine_tune_points(cps, proxies, scales)
        note("refine", 1.0, f"정밀 조정 완료 — 제어점 {len(cps)}개")

    return cps, stats
