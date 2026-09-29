"""끌어서 방향을 돌리는 동안 보여 줄 빠른 미리보기.

정식 미리보기는 사진 29장을 전부 워핑하고 이음선을 찾아 합성하므로 십수 초가
걸린다. 마우스를 끄는 동안 그걸 매번 돌릴 수는 없다.

그런데 방향을 바꾸는 일은 구(球)를 돌리는 것뿐이라, 합성 결과 자체는 달라지지
않는다. 그래서 전방위를 담은 정방형도법 '원판' 을 한 번만 합성해 두고, 끄는
동안에는 그 원판을 새 방향·새 투영으로 다시 펼치기만 한다. 재투영은 화소마다
좌표를 한 번 옮기는 일이라 수십 밀리초면 끝난다.

원판은 정렬·렌즈·보정 설정이 바뀌면 낡는다. 투영 방식이나 출력 크기처럼
합성에 영향이 없는 설정은 원판을 그대로 쓴다.
"""

from __future__ import annotations

import hashlib
import json
import threading
import traceback
from dataclasses import asdict, is_dataclass

import cv2
import numpy as np

from . import render, sysmem
from .camera import pano_grid_to_rays, rays_to_pano
from .warp import PanoLayout, clamp_fov, compute_layout

MASTER_DIM = 2400      # 원판의 긴 변
LIVE_DIM = 900         # 끄는 동안 보여 줄 한 장의 긴 변
_WRAP_PAD = 4          # 감기는 원판의 좌우에 덧대는 폭

# 합성 결과를 바꾸지 않는 설정 — 이것만 바뀌었으면 원판을 다시 만들 필요가 없다
_LAYOUT_ONLY = {"projection", "out_width", "scale_percent", "max_dim",
                "format", "quality", "gpano", "interpolation"}

_lock = threading.Lock()
_master: dict = {"key": None, "img": None, "layout": None}
_building: dict = {"key": None}


def _plain(o):
    if is_dataclass(o):
        return asdict(o)
    if isinstance(o, dict):
        return {str(k): _plain(v) for k, v in o.items()}
    return o


def master_key(p) -> str:
    """합성 결과를 좌우하는 것들만 모아 지문을 낸다."""
    data = {
        "ids": sorted(p.active_ids),
        "paths": {str(i): str(im.path) for i, im in p.images.items()},
        "params": _plain(p.params),
        "lenses": _plain(p.lenses),
        "settings": {k: v for k, v in asdict(p.settings).items() if k not in _LAYOUT_ONLY},
        "ev": _plain(p.exposure_ev),
        "vig": _plain(p.vignetting),
        "pe": _plain(p.photo_exposure),
        "pf": _plain(p.photo_flare),
    }
    blob = json.dumps(data, sort_keys=True, default=str).encode()
    return hashlib.sha1(blob).hexdigest()


def _build(p, cache_dir) -> tuple[np.ndarray, PanoLayout]:
    # 원판은 자르지 않고 구 전체(360x180)로 만든다. 사진이 덮는 범위에 맞춰
    # 자르면, 그 범위를 사진 테두리의 표본점으로만 재기 때문에 표본 사이로 삐져나온
    # 천정·천저 끝이 잘려 나간다. 끄는 중에만 하늘에 검은 점이 보였던 이유다.
    lay = compute_layout(p.active_params(), p.lenses, p.sizes(), "equirect",
                         center=(0.0, 0.0, 0.0), max_dim=MASTER_DIM, fov=(360.0, 180.0))
    img, lay, _ = render.render_preview(p, cache_dir, max_dim=MASTER_DIM, layout=lay)
    if lay.wraps:
        # 좌우 끝에 반대편 열을 몇 줄 덧대 둔다. 경계 바로 옆 화소를 보간할 때
        # 바깥(검정)을 섞으면 이음매 자리에 가는 검은 세로줄이 생긴다.
        img = np.hstack([img[:, -_WRAP_PAD:], img, img[:, :_WRAP_PAD]])
    return img, lay


def prepare(p, cache_dir) -> dict:
    """원판이 최신이면 바로 알리고, 아니면 뒤에서 만들기 시작한다."""
    key = master_key(p)
    with _lock:
        if _master["key"] == key and _master["img"] is not None:
            return {"ready": True}
        if _building["key"] == key:
            return {"ready": False, "building": True}
        _building["key"] = key

    def work():
        try:
            img, lay = _build(p, cache_dir)
            with _lock:
                _master.update(key=key, img=img, layout=lay)
        except Exception:
            traceback.print_exc()
        finally:
            with _lock:
                if _building["key"] == key:
                    _building["key"] = None

    sysmem.start_heavy_thread(work)
    return {"ready": False, "building": True}


def deg_per_px(lay: PanoLayout) -> float:
    """캔버스 가운데에서 화소 하나가 몇 도인지. 끈 거리를 각도로 바꾸는 데 쓴다."""
    if lay.projection == "rectilinear":
        half = min(np.radians(lay.hfov) / 2.0, np.radians(70.0))
        f = lay.full_w / (2.0 * np.tan(half))
        return float(np.degrees(1.0 / max(f, 1e-6)))
    return float(lay.hfov / max(1, lay.full_w))


def frame(p, yaw: float, pitch: float, roll: float,
          max_dim: int = LIVE_DIM,
          fov: tuple[float, float] | None = None) -> np.ndarray | None:
    """원판을 주어진 방향(도)과 현재 투영으로 펼친다. 원판이 없거나 낡았으면 None."""
    with _lock:
        img, ml, key = _master["img"], _master["layout"], _master["key"]
    if img is None or key != master_key(p):
        return None

    center = (float(np.radians(yaw)), float(np.radians(pitch)), float(np.radians(roll)))
    if not (fov and fov[0] > 0 and fov[1] > 0):
        fov = render.ensure_fov(p, center)
    # 정식 미리보기와 같은 틀을 쓴다 — 끄는 동안 크기가 출렁이지 않는다
    out = compute_layout(p.active_params(), p.lenses, p.sizes(), p.settings.projection,
                         center=center, max_dim=max_dim,
                         fov=clamp_fov(p.settings.projection, fov[0], fov[1]))
    if out.w < 2 or out.h < 2:
        return None

    rays, ok = pano_grid_to_rays(out.projection, out.full_w, out.full_h, out.hfov,
                                 out.center, out.roi)
    pts = rays_to_pano("equirect", rays.reshape(-1, 3), ml.full_w, ml.full_h,
                       ml.hfov, ml.center)
    mx = pts[:, 0] - ml.x0
    my = pts[:, 1] - ml.y0
    if ml.wraps:
        # 원판은 좌우가 이어진 캔버스다. 경계 너머는 반대편에서 가져오고,
        # 덧댄 폭만큼 밀어 준다.
        mx = np.mod(mx, ml.full_w) + _WRAP_PAD
    mx = mx.reshape(out.h, out.w).astype(np.float32)
    my = my.reshape(out.h, out.w).astype(np.float32)
    bad = ~ok | ~np.isfinite(mx) | ~np.isfinite(my)
    mx[bad] = -1
    my[bad] = -1
    return cv2.remap(img, mx, my, cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_CONSTANT, borderValue=0)
