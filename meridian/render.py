"""렌더링 파이프라인 — 미리보기와 최종 출력.

미리보기는 프록시로 통째 처리하고, 최종은 타일 스트리밍으로 간다.
기가픽셀 캔버스를 메모리에 올릴 수 없기 때문인데, 그렇다고 타일마다 따로
노출 보정과 심 찾기를 하면 타일 경계가 드러난다. 그래서 순서를 이렇게 둔다.

  1. 프록시 해상도에서 전역으로 게인과 이음선을 구한다.
  2. 그 결과를 타일에 그대로 적용해 원본 해상도로 렌더한다.

이음선 마스크는 프록시 좌표에 있으므로 타일마다 확대해 쓴다. 이음선은 몇
픽셀 흔들려도 블렌딩이 덮으니 이 근사가 실제로 티나지 않는다.
"""

from __future__ import annotations

import gc
import itertools
import math
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .blend import (Patch, apply_field, apply_gains, blend, distance_partition,
                    find_seams, gains_to_ev, match_low_frequency, solve_gains)
from .camera import ImageParams, Lens
from .images import build_proxy, imwrite, read_image
from . import photometric, sysmem
from .project import Project, RenderSettings
from . import xmp
from .warp import (PanoLayout, auto_center, compute_layout, fit_fov, warp_image,
                   warped_bounds)

INTERP = {"nearest": cv2.INTER_NEAREST, "linear": cv2.INTER_LINEAR,
          "cubic": cv2.INTER_CUBIC, "lanczos": cv2.INTER_LANCZOS4}

TILE = 1536               # 타일 한 변
TILE_MARGIN = 192         # 멀티밴드가 번지는 폭보다 넉넉히
RENDER_CACHE_BUDGET_MB = 5000     # 원본 디코딩 결과를 붙들어 둘 상한


def _env_int(name: str) -> int:
    try:
        v = int(os.environ.get(name, ""))
        return v if v > 0 else 0
    except ValueError:
        return 0


def _cache_budget_mb() -> int:
    # 원본 디코딩 캐시는 많을수록 빠르지만, 고정 5GB 는 메모리 4GB 인 PC 에서 엔진을
    # 통째로 꺼지게 한다. 남은 메모리에 맞춰 줄인다.
    return _env_int("MERIDIAN_CACHE_MB") or sysmem.budget_mb(RENDER_CACHE_BUDGET_MB)


def _worker_count() -> int:
    """타일을 동시에 몇 개 그릴지.

    코어 수만큼 띄우면 오히려 느려진다. 워핑 한 번이 32MP 원본을 무작위
    순서로 훑어 메모리 대역폭을 먼저 다 쓰기 때문이다. 8코어에서 재 보면
    워커 4개가 가장 빠르고 8개는 1.5배 도로 느려진다.
    """
    n = _env_int("MERIDIAN_WORKERS")
    if n:
        return n
    cpu = os.cpu_count() or 4
    # 현대 Apple Silicon 칩은 메모리 대역폭이 매우 커서 코어 전체를 써도 잘 버틴다.
    return max(1, cpu - 1)


def _decode_workers() -> int:
    """디코딩은 대역폭보다 CPU 에 묶여 있어 더 많이 띄워도 이득이다."""
    return max(1, min(8, os.cpu_count() or 4))


def _mb_per_image(project: Project) -> float:
    """원본 한 장을 BGR 로 펼쳤을 때의 크기(MB)."""
    sizes = [(im.width, im.height) for im in project.images.values() if im.enabled]
    if not sizes:
        return 100.0
    w, h = max(sizes, key=lambda t: t[0] * t[1])
    return max(1.0, w * h * 3 / 1e6)


@dataclass
class RenderReport:
    width: int
    height: int
    images_used: int
    seconds: float
    path: str | None = None
    exposure_ev: list[float] | None = None
    tiles: int = 0


def ensure_fov(project: Project, center: tuple[float, ...] | None = None) -> tuple[float, float]:
    """화각이 아직 없으면 지금 방향에서 사진이 다 들어가게 맞춰 저장한다."""
    s = project.settings
    if s.hfov > 0 and s.vfov > 0:
        return s.hfov, s.vfov
    if center is None:
        center = (project.layout.center if project.layout
                  else auto_center(project.active_params(), project.lenses, project.sizes()))
    s.hfov, s.vfov = fit_fov(project.active_params(), project.lenses, project.sizes(),
                             s.projection, center)
    return s.hfov, s.vfov


def _layout_for(project: Project, max_dim: int | None = None,
                center: tuple[float, ...] | None = None,
                fov: tuple[float, float] | None = None) -> PanoLayout:
    s = project.settings
    if center is None and project.layout:
        center = project.layout.center
    if center is None:
        center = auto_center(project.active_params(), project.lenses, project.sizes())
    return compute_layout(
        project.active_params(), project.lenses, project.sizes(),
        projection=s.projection,
        center=center,
        max_dim=max_dim or (s.max_dim or None),
        scale_percent=s.scale_percent if max_dim is None else 100.0,
        out_width=s.out_width if max_dim is None else 0,
        fov=fov or ensure_fov(project, center),
    )


# ---------------------------------------------------------------- 미리보기

def render_preview(project: Project, cache_dir: Path, max_dim: int = 1400,
                   layout: PanoLayout | None = None,
                   show_seams: bool = False,
                   progress=None) -> tuple[np.ndarray, PanoLayout, list[float]]:
    """프록시로 빠르게 합성한다. UI 가 계속 부르는 경로라 속도가 우선이다."""
    lay = layout or _layout_for(project, max_dim=max_dim)
    ids = [i for i in project.active_ids if i in project.params]
    if not ids:
        return np.zeros((10, 10, 3), np.uint8), lay, []

    # 360도로 감기는 캔버스는 좌우로 조금 넓게 그린다. 그래야 이음선 찾기와
    # 블렌딩이 경계를 건너 이어져, 왼쪽 끝과 오른쪽 끝이 실제로 맞물린다.
    pad = lay.wrap_pad()
    canvas = (lay.x0 - pad, lay.y0, lay.w + 2 * pad, lay.h)

    patches: list[Patch] = []
    
    def _warp_one(k: int, i: int) -> Patch | None:
        if progress:
            progress("warp", (k + 1) / len(ids), f"워핑 {k + 1}/{len(ids)}")
        src = project.images[i]
        proxy, _ = build_proxy(src.path, cache_dir)
        vig, expo, flare = project.photometric_for(i)
        proxy = photometric.apply(proxy, vig, (src.width, src.height), expo, flare)
        img, mask, corner = warp_image(
            proxy, project.params[i], project.lenses[project.params[i].lens_id],
            lay, src_size=(src.width, src.height), roi=canvas, interp=cv2.INTER_LINEAR)
        if mask.size and mask.any():
            return Patch(i, img, mask, corner)
        return None

    import os
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=sysmem.workers(80)) as pool:
        futures = [pool.submit(_warp_one, k, i) for k, i in enumerate(ids)]
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                patches.append(res)
                
    # 순서 보장 (나중에 문제 생기지 않도록 원래 순서대로 정렬)
    patches.sort(key=lambda p: p.image_id)

    if not patches:
        return np.zeros((lay.h, lay.w, 3), np.uint8), lay, []

    evs: list[float] = []
    if project.settings.exposure == "auto":
        if progress:
            progress("exposure", 0.5, "노출 맞추는 중…")
        gains = solve_gains(patches, per_channel=project.settings.per_channel)
        evs = gains_to_ev(gains)
        apply_gains(patches, gains, project.exposure_ev)
    elif project.exposure_ev:
        apply_gains(patches, np.ones((len(patches), 3)), project.exposure_ev)

    if project.settings.low_freq > 0:
        if progress:
            progress("match", 0.65, "밝기 기울기 맞추는 중…")
        match_low_frequency(patches, canvas, strength=project.settings.low_freq,
                            wrap_width=lay.full_w if pad else 0)

    if project.settings.seam != "none":
        if progress:
            progress("seam", 0.7, "이음선 찾는 중…")
        # 먼저 거리 변환으로 크게 나눠 각 사진이 넓은 면을 통째로 맡게 하고,
        # 그 다음에만 선택된 방법으로 경계를 다듬는다. 순서를 바꾸면 천정처럼
        # 여러 사진이 모이는 곳에서 경계가 잘게 쪼개진다.
        distance_partition(patches, canvas, wrap_width=lay.full_w if pad else 0)
        if project.settings.seam in ("dp_color_grad", "graphcut"):
            find_seams(patches, project.settings.seam, work_scale=0.5)

    outlines = [p.mask.copy() for p in patches] if show_seams else None
    if progress:
        progress("blend", 0.9, "합성 중…")
    out, out_mask = blend(patches, canvas,
                   mode=project.settings.blender, num_bands=project.settings.num_bands)

    if outlines:
        out = _draw_seams(out, patches, outlines, lay, x_shift=pad)
    out_mask = np.asarray(out_mask)
    if out_mask.shape[:2] != out.shape[:2]:    # 혹시 크기가 어긋나면 채우지 않는다
        out_mask = np.full(out.shape[:2], 255, np.uint8)
    if pad:
        out = out[:, pad:pad + lay.w]          # 덧댄 부분을 잘라 낸다
        out_mask = out_mask[:, pad:pad + lay.w]
    out = np.ascontiguousarray(out)
    if project.settings.fill_gaps:
        extend_uncovered(out, out_mask)
    else:
        # 채우지 않기로 했으면 미리보기도 덮인 곳만 남긴다. 멀티밴드 합성은 사진
        # 가장자리 너머로 색을 번지게 하는데, 최종 출력은 덮인 곳만 옮겨 적으므로
        # 그 번짐을 그냥 두면 미리보기에만 빈 곳이 메워져 보인다.
        out[out_mask == 0] = 0
        
    if project.settings.patch_nadir:
        fill_holes(out, lay, is_preview=True)

    return out, lay, evs


def _draw_seams(canvas: np.ndarray, patches: list[Patch], masks: list[np.ndarray],
                lay: PanoLayout, x_shift: int = 0) -> np.ndarray:
    """이음선을 눈으로 확인할 수 있게 윤곽을 그린다."""
    palette = [(80, 200, 255), (120, 255, 140), (255, 160, 90), (200, 130, 255),
               (110, 220, 255), (255, 210, 120)]
    over = canvas.copy()
    for k, (p, m) in enumerate(zip(patches, masks)):
        edge = cv2.morphologyEx(m, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
        ys, xs = np.where(edge > 0)
        if len(ys) == 0:
            continue
        gx = xs + p.corner[0] - lay.x0 + x_shift
        gy = ys + p.corner[1] - lay.y0
        keep = (gx >= 0) & (gy >= 0) & (gx < canvas.shape[1]) & (gy < canvas.shape[0])
        over[gy[keep], gx[keep]] = palette[k % len(palette)]
    return cv2.addWeighted(canvas, 0.45, over, 0.55, 0)


def fill_holes(img: np.ndarray, lay: PanoLayout, radius_ratio: float = 0.05, is_preview: bool = False) -> np.ndarray:
    """천정/삼각대 자국과 가장자리 얇은 검은 줄(렌더링 결함)을 주변 픽셀로 메운다."""
    h, w = img.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    
    # 1. 천정 (삼각대) 마스크
    if lay.projection == "equirect":
        rh = max(1, int(h * radius_ratio))
        mask[-rh:, :] = 255
    else:
        from .camera import rays_to_pano
        nadir = rays_to_pano(lay.projection, np.array([[0.0, 1.0, 0.0]]), lay.full_w, lay.full_h, lay.hfov, lay.center)
        nx, ny = nadir[0]
        nx -= lay.x0
        ny -= lay.y0
        
        if 0 <= nx < w and 0 <= ny < h:
            radius = int(max(w, h) * radius_ratio)
            cv2.circle(mask, (int(nx), int(ny)), radius, 255, -1)

    # 2. 얇은 검은 줄 (경계 아티팩트) 마스크
    # 미리보기든 최종이든 RGB 가 모두 0인 영역을 찾는다.
    black = (img[:, :, 0] == 0) & (img[:, :, 1] == 0) & (img[:, :, 2] == 0)
    
    # 너무 넓은 허공까지 다 칠하면 한세월이므로, 실제 그림이 있는 곳에서
    # 조금만 뻗어나간 얇은 줄기만 칠하도록 팽창(dilate)을 쓴다.
    valid = (~black).astype(np.uint8) * 255
    kern = cv2.getStructuringElement(cv2.MORPH_RECT, (21, 21))
    valid_expanded = cv2.dilate(valid, kern)
    
    # 진짜 그림 영역 근처에 있는 검은 픽셀들만 마스크에 추가
    mask[(black) & (valid_expanded > 0)] = 255

    if not mask.any():
        return img
    
    # 거대한 캔버스 전체를 한 번에 인페인트하면 기가픽셀에서 터지므로, 구역별로 쪼개서 칠한다.
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8, cv2.CV_32S)
    for i in range(1, num_labels):
        x = stats[i, cv2.CC_STAT_LEFT]
        y = stats[i, cv2.CC_STAT_TOP]
        w_box = stats[i, cv2.CC_STAT_WIDTH]
        h_box = stats[i, cv2.CC_STAT_HEIGHT]
        
        # 팽창 크기만큼 여유를 둔다
        pad = int(max(w_box, h_box) * 0.1) + 5
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(w, x + w_box + pad), min(h, y + h_box + pad)
        
        crop = img[y0:y1, x0:x1].copy()
        crop_mask = mask[y0:y1, x0:x1]
        
        repaired = cv2.inpaint(crop, crop_mask, 5, cv2.INPAINT_TELEA)
        img[y0:y1, x0:x1] = repaired
        
    return img


def extend_uncovered(canvas, covered, feather: float = 1.5) -> None:
    """사진이 덮지 않은 곳을 가장 가까운 덮인 화소 색으로 채운다 (제자리 수정).

    미리보기와 내보내기가 다르게 보이던 원인이 여기였다. 미리보기는 합성할 때
    멀티밴드가 마스크 바깥으로 색을 번지게 해 빈 곳이 하늘색으로 메워져 보였고,
    내보내기는 타일마다 '덮인 곳' 만 캔버스에 옮겨 적어 빈 곳이 검게 남았다.
    번짐에 기대지 말고 두 경로가 같은 규칙으로 직접 채우게 한다.

    기가픽셀 캔버스를 통째로 메모리에 올릴 수 없으므로, 축소한 판에서 가장 가까운
    색을 구한 뒤 가로줄 단위로 되돌린다. 빈 곳은 하늘처럼 매끄러운 면이라 이
    근사가 눈에 띄지 않는다. 덮인 곳은 한 화소도 건드리지 않는다.
    """
    h, w = covered.shape[:2]
    step = max(1, int(np.ceil(max(w, h) / 2000.0)))
    small_cov = np.asarray(covered[::step, ::step])
    if small_cov.all() or not small_cov.any():
        return                                  # 다 덮였거나 아무것도 없다
    small_img = np.asarray(canvas[::step, ::step])

    holes = (small_cov == 0).astype(np.uint8)
    _, labels = cv2.distanceTransformWithLabels(
        holes, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    ys, xs = np.where(holes == 0)               # 덮인 화소들 (라벨 순서와 같다)
    filled = small_img[ys[labels - 1], xs[labels - 1]]
    if feather > 0:
        filled = cv2.GaussianBlur(filled, (0, 0), feather)

    band = max(1, 4_000_000 // max(1, w))       # 한 번에 다룰 줄 수
    for y0 in range(0, h, band):
        y1 = min(h, y0 + band)
        sy0, sy1 = y0 // step, min(small_cov.shape[0], -(-y1 // step) + 1)
        if sy1 <= sy0:
            continue
        up = cv2.resize(filled[sy0:sy1], (w, (sy1 - sy0) * step),
                        interpolation=cv2.INTER_LINEAR)
        up = up[y0 - sy0 * step:y0 - sy0 * step + (y1 - y0)]
        if up.shape[0] != y1 - y0:
            continue
        gap = np.asarray(covered[y0:y1]) == 0
        if gap.any():
            block = np.asarray(canvas[y0:y1])
            block[gap] = up[gap]
            canvas[y0:y1] = block


# ---------------------------------------------------------------- 최종 출력

SCRATCH_GLOBS = ("canvas_*.dat", "canvas_*.mask")


def _remove_scratch(path: Path) -> bool:
    """렌더용 임시 캔버스 파일을 지운다.

    윈도우는 메모리 맵이 살아 있는 파일을 지우지 못한다(PermissionError).
    취소로 빠져나올 때는 아직 풀리지 않은 참조가 잠깐 남아 있을 수 있어,
    가비지 수집을 돌리고 몇 번 다시 해 본다. 그래도 안 되면 다음에 엔진이
    켜질 때 sweep_scratch 가 치운다.
    """
    for attempt in range(5):
        try:
            path.unlink(missing_ok=True)
            return True
        except PermissionError:
            gc.collect()
            time.sleep(0.1 * (attempt + 1))
    return False


def sweep_scratch(cache_dir: Path) -> None:
    """지난 실행이 남긴 임시 캔버스를 치운다 (수십 GB 가 될 수 있다).

    다른 엔진이 지금 쓰고 있는 파일이면 윈도우는 지우기를 거절하고, POSIX 는
    매핑이 풀릴 때까지 내용을 살려 두므로 어느 쪽이든 안전하다.
    """
    for pattern in SCRATCH_GLOBS:
        for f in Path(cache_dir).glob(pattern):
            try:
                f.unlink()
            except OSError:
                pass


def render_final(project: Project, out_path: Path, cache_dir: Path,
                 progress=None) -> RenderReport:
    """원본 해상도로 렌더해 파일로 쓴다.

    타일끼리는 서로를 건드리지 않으므로 스레드로 나눠 돌린다. 무거운 일은
    전부 OpenCV/numpy 안(GIL 밖)에서 벌어져 스레드로도 실제로 코어가 붙는다.
    다만 워커를 코어 수만큼 띄우면 오히려 느려진다 — 워핑은 32MP 원본을
    무작위로 훑어 메모리 대역폭에서 먼저 막히기 때문이다. 측정상 8코어에서
    워커 4개가 가장 빨랐고, 그때는 OpenCV 자체 스레드를 꺼야 한다.
    """
    import time
    t0 = time.time()
    s = project.settings
    lay = _layout_for(project)
    ids = [i for i in project.active_ids if i in project.params]
    if not ids:
        raise ValueError("활성화된 이미지가 없습니다")

    interp = INTERP.get(s.interpolation, cv2.INTER_CUBIC)
    # 워커마다 원본 몇 장과 타일 작업 공간을 쥔다. 남은 메모리 안에 드는 만큼만 띄운다.
    workers = min(_worker_count(), sysmem.workers(_mb_per_image(project) * 2 + 200))

    # 원본을 얼마나 미리 줄일지는 사진마다 따로 정한다 (아래 _source_scale).
    src_scales: dict[int, float] = {}

    def tick(stage: str, frac: float, message: str) -> None:
        """progress 가 취소 예외를 던지는 통로이기도 하다."""
        if progress:
            progress(stage, frac, message)

    # 1) 프록시에서 전역 게인과 이음선 마스크를 먼저 구한다
    tick("plan", 0.02, "노출과 이음선 계산 중…")
    guide_scale = min(1.0, 1800.0 / max(lay.w, lay.h))
    guide_lay = lay.scaled(guide_scale)
    guides: dict[int, tuple[np.ndarray, tuple[int, int]]] = {}

    def guide_patch(i: int) -> Patch | None:
        src = project.images[i]
        proxy, _ = build_proxy(src.path, cache_dir)
        vig, expo, flare = project.photometric_for(i)
        proxy = photometric.apply(proxy, vig, (src.width, src.height), expo, flare)
        img, mask, corner = warp_image(
            proxy, project.params[i], project.lenses[project.params[i].lens_id],
            guide_lay, src_size=(src.width, src.height), interp=cv2.INTER_LINEAR)
        return Patch(i, img, mask, corner) if (mask.size and mask.any()) else None

    with ThreadPoolExecutor(max_workers=_decode_workers()) as ex:
        gpatches = [p for p in ex.map(guide_patch, ids) if p is not None]

    gains = np.ones((len(gpatches), 3))
    if s.exposure == "auto" and len(gpatches) > 1:
        gains = solve_gains(gpatches, per_channel=s.per_channel)
    evs = gains_to_ev(gains)
    gain_by_id = {p.image_id: gains[k] for k, p in enumerate(gpatches)}
    apply_gains(gpatches, gains, project.exposure_ev)

    # 저주파 보정 필드도 여기서 한 번만 구해 타일마다 재사용한다.
    # 타일별로 따로 구하면 타일 경계에서 보정이 어긋나 새 이음매가 생긴다.
    fields: dict = {}
    if s.low_freq > 0 and len(gpatches) > 1:
        tick("plan", 0.05, "밝기 기울기 맞추는 중…")
        fields = match_low_frequency(gpatches, (guide_lay.x0, guide_lay.y0,
                                                guide_lay.w, guide_lay.h),
                                     strength=s.low_freq, return_fields=True,
                                     wrap_width=guide_lay.full_w if guide_lay.wraps else 0) or {}

    if s.seam != "none" and len(gpatches) > 1:
        distance_partition(gpatches, (guide_lay.x0, guide_lay.y0, guide_lay.w, guide_lay.h),
                           wrap_width=guide_lay.full_w if guide_lay.wraps else 0)
        if s.seam in ("dp_color_grad", "graphcut"):
            find_seams(gpatches, s.seam, work_scale=1.0)
    for p in gpatches:
        guides[p.image_id] = (p.mask, p.corner)
    del gpatches

    # 2) 캔버스를 디스크에 잡는다 — 기가픽셀도 메모리를 넘기지 않는다
    canvas_path = Path(cache_dir) / f"canvas_{abs(hash(str(out_path)))}.dat"
    canvas = np.memmap(canvas_path, dtype=np.uint8, mode="w+", shape=(lay.h, lay.w, 3))
    covered = np.memmap(canvas_path.with_suffix(".mask"), dtype=np.uint8,
                        mode="w+", shape=(lay.h, lay.w))

    # 3) 이미지별 캔버스 점유 사각형을 미리 구해 타일마다 후보를 좁힌다
    cell = int(np.clip(-(-max(lay.w, lay.h) // 1500), 8, 64))
    grids: dict[int, np.ndarray] = {}
    rects: dict[int, tuple[int, int, int, int]] = {}
    for i in ids:
        src = project.images[i]
        g = _coverage_grid(project.params[i], project.lenses[project.params[i].lens_id],
                           lay, (src.width, src.height), cell)
        if g is None:
            continue
        grids[i] = g
        r = _grid_box(g, lay, cell, (lay.x0, lay.y0, lay.w, lay.h))
        if r:
            rects[i] = r
            src_scales[i] = _source_scale(project.params[i],
                                          project.lenses[project.params[i].lens_id],
                                          lay, (src.width, src.height), r)

    tiles: list[tuple[int, int]] = []
    tiles_x = math.ceil(lay.w / TILE)
    tiles_y = math.ceil(lay.h / TILE)
    for ty in range(tiles_y):
        for tx in range(tiles_x):
            tiles.append((tx, ty))
    total_tiles = len(tiles)

    # 이웃한 타일은 대체로 같은 원본들을 쓴다. 한 장만 붙들고 있으면 타일이
    # 바뀔 때마다 디코딩을 처음부터 다시 해서 전체 시간이 몇 배가 된다.
    cache: "OrderedDict[int, np.ndarray]" = OrderedDict()
    cache_limit = max(workers + 1,
                      int(_cache_budget_mb() // max(1, _mb_per_image(project))))
    cache_lock = threading.Lock()
    decode_locks: dict[int, threading.Lock] = {}
    counter = itertools.count(1)
    stop = threading.Event()

    def full_image(i: int) -> np.ndarray:
        """원본을 디코딩해 들고 있는다. 같은 장을 두 스레드가 겹쳐 읽지 않는다."""
        with cache_lock:
            hit = cache.get(i)
            if hit is not None:
                cache.move_to_end(i)
                return hit
            lock = decode_locks.setdefault(i, threading.Lock())
        with lock:
            with cache_lock:
                hit = cache.get(i)
                if hit is not None:
                    cache.move_to_end(i)
                    return hit
            src = project.images[i]
            vig, expo, flare = project.photometric_for(i)
            arr = read_image(src.path)
            sc = src_scales.get(i, 1.0)
            if sc < 0.9:
                arr = cv2.resize(arr, (max(16, round(arr.shape[1] * sc)),
                                       max(16, round(arr.shape[0] * sc))),
                                 interpolation=cv2.INTER_AREA)
            # 비네팅은 여기서 한 번만 걷어내고 타일마다 재사용한다. warp_image 는
            # src_size 로 원본 크기를 받으므로 줄여 놔도 좌표는 그대로 맞는다.
            arr = photometric.apply(arr, vig, (src.width, src.height), expo, flare)
            with cache_lock:
                cache[i] = arr
                cache.move_to_end(i)
                # 밀려난 배열도 쓰고 있는 스레드가 참조를 쥐고 있으니 안전하다
                while len(cache) > cache_limit:
                    cache.popitem(last=False)
            return arr

    def do_tile(pos: tuple[int, int]) -> None:
        if stop.is_set():                 # 취소됐으면 남은 타일은 그냥 흘려보낸다
            return
        tx, ty = pos
        tx0 = lay.x0 + tx * TILE
        ty0 = lay.y0 + ty * TILE
        tw = min(TILE, lay.x0 + lay.w - tx0)
        th = min(TILE, lay.y0 + lay.h - ty0)
        if tw <= 0 or th <= 0:
            return
        # 블렌딩이 번지는 만큼 넓게 렌더하고 가운데만 취한다
        ex0, ey0 = tx0 - TILE_MARGIN, ty0 - TILE_MARGIN
        ew, eh = tw + 2 * TILE_MARGIN, th + 2 * TILE_MARGIN

        boxes = {i: _grid_box(grids[i], lay, cell, (ex0, ey0, ew, eh))
                 for i in ids if i in grids}
        members = [i for i in ids if boxes.get(i) is not None]
        n = next(counter)
        tick("render", n / total_tiles,
             f"타일 {n}/{total_tiles} — 이미지 {len(members)}장")
        if not members:
            return

        patches: list[Patch] = []
        for i in members:
            src = project.images[i]
            # 이 사진이 실제로 닿는 곳만 워핑한다. 타일 전체를 넘기면 닿지도
            # 않는 화소까지 역투영을 계산하느라 몇 배를 더 쓴다.
            sub = boxes[i]
            if sub is None:
                continue
            img, mask, corner = warp_image(
                full_image(i), project.params[i],
                project.lenses[project.params[i].lens_id],
                lay, src_size=(src.width, src.height), roi=sub, interp=interp)
            if not (mask.size and mask.any()):
                continue
            mask = _apply_guide_mask(mask, corner, guides.get(i), guide_scale)
            if not mask.any():
                continue
            p = Patch(i, img, mask, corner)
            if i in gain_by_id or project.exposure_ev:
                apply_gains([p], np.array([gain_by_id.get(i, np.ones(3))]),
                            project.exposure_ev)
            if i in fields:
                p.image = apply_field(p.image, p.corner, fields[i],
                                      (guide_lay.x0, guide_lay.y0))
            patches.append(p)

        if not patches:
            return
        tile_img, tile_mask = blend(patches, (ex0, ey0, ew, eh),
                                    mode=s.blender, num_bands=s.num_bands)
        # 가운데 영역만 캔버스에 쓴다 — 타일끼리 겹치지 않아 잠글 필요가 없다
        cx, cy = tx0 - ex0, ty0 - ey0
        crop = tile_img[cy:cy + th, cx:cx + tw]
        cmask = tile_mask[cy:cy + th, cx:cx + tw]
        dy, dx = ty0 - lay.y0, tx0 - lay.x0
        sel = cmask > 0
        canvas[dy:dy + th, dx:dx + tw][sel] = crop[sel]
        covered[dy:dy + th, dx:dx + tw][sel] = 255

    prev_cv_threads = cv2.getNumThreads()
    try:
      try:
        if workers > 1:
            # 타일마다 스레드를 쓰는 동안 OpenCV 가 또 코어를 나눠 가지면
            # 서로 밀어내기만 한다. 바깥 병렬이 더 크게 이긴다.
            cv2.setNumThreads(1)
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(do_tile, t) for t in tiles]
                try:
                    for fut in futures:
                        fut.result()      # 예외(취소 포함)를 여기서 받는다
                except BaseException:
                    # 취소라면 남은 수백 개 타일을 기다려 줄 이유가 없다
                    stop.set()
                    for f in futures:
                        f.cancel()
                    raise
        else:
            for t in tiles:
                do_tile(t)
      finally:
        cv2.setNumThreads(prev_cv_threads)
        cache.clear()

        canvas.flush()
        covered.flush()

        # 미리보기와 같은 규칙으로 빈 곳을 채운다 (위 extend_uncovered 설명 참고)
        if s.fill_gaps:
            tick("save", 0.95, "빈 곳 채우는 중…")
            extend_uncovered(canvas, covered)
            canvas.flush()

        if project.settings.patch_nadir:
            tick("save", 0.96, "천정 삼각대 지우는 중…")
            fill_holes(canvas, lay, is_preview=False)
            canvas.flush()

        tick("save", 0.97, "파일로 쓰는 중…")
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _write_image(np.asarray(canvas), np.asarray(covered), out_path, s, lay)
    finally:
        # 기가픽셀 캔버스는 디스크에 수십 GB 다. 취소로 빠져나가도 지워야 한다.
        del canvas, covered
        _remove_scratch(canvas_path)
        _remove_scratch(canvas_path.with_suffix(".mask"))

    return RenderReport(width=lay.w, height=lay.h, images_used=len(ids),
                        seconds=time.time() - t0, path=str(out_path),
                        exposure_ev=evs, tiles=total_tiles)


def _coverage_grid(params: ImageParams, lens: Lens, lay: PanoLayout,
                   src_size: tuple[int, int], cell: int) -> np.ndarray | None:
    """이 사진이 캔버스의 어느 칸을 덮는지 성긴 격자로 잰다.

    예전에는 사진이 차지하는 범위를 테두리 표본의 '사각형' 하나로 잡았다. 그런데
    리틀 플래닛(스테레오)처럼 사진이 고리 모양으로 퍼지는 투영에서는 실제 덮는 모양이
    사각형과 전혀 다르다. 그 사각형으로 타일의 후보를 고르면 덮고 있는 사진을
    빠뜨려, 내보내기에만 검은 구멍이 남았다 (미리보기는 캔버스를 통째로 그려서
    멀쩡했다). 모양을 그대로 재면 이런 어긋남이 없다.
    """
    from .camera import (lens_inverse_lut, pano_grid_to_rays, rays_to_pixels,
                         rotation_matrix)
    cw = max(1, -(-lay.w // cell))
    ch = max(1, -(-lay.h // cell))
    ls = lay.scaled(1.0 / cell)
    roi = (ls.x0, ls.y0, cw, ch)
    rays, ok = pano_grid_to_rays(ls.projection, ls.full_w, ls.full_h, ls.hfov,
                                 ls.center, roi)
    ow, oh = src_size
    R = rotation_matrix(params.yaw, params.pitch, params.roll)
    px, valid = rays_to_pixels(rays, lens, ow, oh, R,
                               lens_inverse_lut(lens.a, lens.b, lens.c))
    inside = (ok & valid & (px[..., 0] >= 0) & (px[..., 1] >= 0)
              & (px[..., 0] < ow) & (px[..., 1] < oh))
    if not inside.any():
        return None
    # 격자 한 칸은 실제 화소 cell 개다. 한 칸 번져 두어 경계에서 놓치지 않게 한다.
    return cv2.dilate(inside.astype(np.uint8),
                      cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))) > 0


def _grid_box(grid: np.ndarray, lay: PanoLayout, cell: int,
              roi: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    """roi 안에서 이 사진이 실제로 덮는 부분만 추려 돌려준다. 하나도 없으면 None."""
    x0, y0, w, h = roi
    cx0 = max(0, (x0 - lay.x0) // cell)
    cy0 = max(0, (y0 - lay.y0) // cell)
    cx1 = min(grid.shape[1], -(-(x0 + w - lay.x0) // cell))
    cy1 = min(grid.shape[0], -(-(y0 + h - lay.y0) // cell))
    if cx1 <= cx0 or cy1 <= cy0:
        return None
    sub = grid[cy0:cy1, cx0:cx1]
    if not sub.any():
        return None
    ys, xs = np.where(sub)
    bx0 = lay.x0 + (cx0 + int(xs.min())) * cell
    by0 = lay.y0 + (cy0 + int(ys.min())) * cell
    bx1 = lay.x0 + (cx0 + int(xs.max()) + 1) * cell
    by1 = lay.y0 + (cy0 + int(ys.max()) + 1) * cell
    return _intersect((x0, y0, w, h), (bx0, by0, bx1 - bx0, by1 - by0))


def _source_scale(params: ImageParams, lens: Lens, lay: PanoLayout,
                  src_size: tuple[int, int],
                  rect: tuple[int, int, int, int]) -> float:
    """이 사진의 원본을 워핑 전에 얼마까지 줄여도 되는지.

    출력이 원본보다 작으면 미리 줄여 둬야 한다. 3200만 화소를 작은 캔버스로
    바로 워핑하면 화소 하나만 집어 오는 점 샘플링이 되어 계단과 잡음이 남고,
    프록시를 쓰는 미리보기와 다르게 보인다.

    다만 줄이는 정도를 캔버스 전체 평균으로 정하면 안 된다. 스테레오(리틀
    플래닛)는 가장자리로 갈수록 몇 배씩 확대되는 투영이라, 평균에 맞추면 바깥쪽
    하늘과 건물에 쓸 화소가 모자라 흐려진다. 그래서 이 사진이 덮는 영역을
    거칠게 훑어 '출력 1화소가 원본 몇 화소에 해당하는지' 를 재고, 그 값이 가장
    작은 곳 — 가장 크게 확대되는 곳 — 에 맞춘다. 거기서 모자라지 않으면
    나머지는 전부 넉넉하다.
    """
    from .camera import (lens_inverse_lut, pano_grid_to_rays, rays_to_pixels,
                         rotation_matrix)
    x0, y0, w, h = rect
    if w < 4 or h < 4:
        return 1.0
    s = min(1.0, 96.0 / max(w, h))
    ls = lay.scaled(s)
    roi = (int(round(x0 * s)), int(round(y0 * s)),
           max(3, int(round(w * s))), max(3, int(round(h * s))))
    R = rotation_matrix(params.yaw, params.pitch, params.roll)
    rays, ok = pano_grid_to_rays(ls.projection, ls.full_w, ls.full_h, ls.hfov,
                                 ls.center, roi)
    ow, oh = src_size
    px, valid = rays_to_pixels(rays, lens, ow, oh, R, lens_inverse_lut(lens.a, lens.b, lens.c))
    good = (ok & valid & (px[..., 0] >= 0) & (px[..., 1] >= 0)
            & (px[..., 0] < ow) & (px[..., 1] < oh))

    # 이웃한 격자점 사이의 원본 거리. 격자 한 칸은 출력 1/s 화소다.
    dx = np.linalg.norm(px[:, 1:] - px[:, :-1], axis=-1) * s
    dy = np.linalg.norm(px[1:, :] - px[:-1, :], axis=-1) * s
    gx = good[:, 1:] & good[:, :-1]
    gy = good[1:, :] & good[:-1, :]
    steps = np.concatenate([dx[gx], dy[gy]])
    steps = steps[np.isfinite(steps) & (steps > 0)]
    if steps.size < 8:
        return 1.0
    # 가장 확대되는 곳을 기준으로 삼되, 가장자리 격자 몇 개의 튀는 값은 뺀다
    footprint = float(np.percentile(steps, 2))
    # 출력 1화소에 원본이 1.4화소 이상 남도록 한다 — 보간이 흐려지지 않는 선
    return float(np.clip(1.4 / footprint, 0.02, 1.0))


def _intersect(a: tuple[int, int, int, int],
               b: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1 - x0, y1 - y0


def _rect_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)


def _apply_guide_mask(mask: np.ndarray, corner: tuple[int, int],
                      guide: tuple[np.ndarray, tuple[int, int]] | None,
                      guide_scale: float) -> np.ndarray:
    """프록시에서 구한 이음선 마스크를 이 타일 조각에 입힌다.

    배율이 1 이라고 건너뛰면 안 된다. 이 함수가 하는 일은 '확대' 가 아니라
    '이 조각이 이음선 안쪽인지 가려내기' 이고, 좌표를 맞추는 일은 배율과
    무관하게 필요하다. 건너뛰면 이음선이 통째로 사라져 겹친 사진들이 전부
    섞이고, 하늘에 렌즈 모양 호가 줄줄이 남는다 — 1800px 이하 출력에서만
    나타나서 눈에 덜 띄었을 뿐이다.
    """
    if guide is None:
        return mask
    gmask, gcorner = guide
    h, w = mask.shape[:2]
    # 이 조각이 가이드 좌표계에서 차지하는 영역
    gx = corner[0] * guide_scale - gcorner[0]
    gy = corner[1] * guide_scale - gcorner[1]
    gw, gh = w * guide_scale, h * guide_scale
    M = np.array([[1.0 / guide_scale, 0.0, -gx / guide_scale],
                  [0.0, 1.0 / guide_scale, -gy / guide_scale]], np.float32)
    up = cv2.warpAffine(gmask, M, (w, h), flags=cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    return ((up > 110) & (mask > 0)).astype(np.uint8) * 255


def _write_image(canvas: np.ndarray, covered: np.ndarray, path: Path,
                 s: RenderSettings, layout: PanoLayout | None = None) -> None:
    ext = path.suffix.lower().lstrip(".") or s.format
    if ext in ("tif", "tiff"):
        params = [cv2.IMWRITE_TIFF_COMPRESSION, 5]         # LZW
        ok = imwrite(path, canvas, params)
    elif ext == "png":
        # 덮이지 않은 곳은 투명하게 — 크롭 전 상태를 보존한다
        bgra = np.dstack([canvas, covered])
        ok = imwrite(path, bgra, [cv2.IMWRITE_PNG_COMPRESSION, 6])
    else:
        ok = imwrite(path, canvas,
                     [cv2.IMWRITE_JPEG_QUALITY, int(np.clip(s.quality, 1, 100))])
    if not ok:
        raise OSError(f"저장하지 못했습니다: {path}")

    # 파노라마 뷰어(FSPviewer 등)가 구(球) 위 어디를 덮은 사진인지 알 수 있게
    # GPano 태그를 심는다. 실패해도 사진 자체는 멀쩡하니 막지 않는다.
    if s.gpano:
        xmp.embed(path, layout, canvas.shape[1], canvas.shape[0])
