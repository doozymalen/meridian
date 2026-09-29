"""로컬 웹 서버 — 엔진과 브라우저 UI 를 잇는다.

수 GB 짜리 RAW 를 브라우저로 업로드시킬 수는 없으므로, 파일 선택은 macOS
네이티브 다이얼로그(osascript)를 서버가 직접 띄워 경로만 받아온다. 원본은
항상 있던 자리에서 읽고 프로젝트에는 경로만 담긴다.

오래 걸리는 작업(정렬·렌더)은 작업 스레드로 돌리고 진행률은 폴링으로 준다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import features, live, optimize as opt, photometric, render, sysmem
from .camera import ImageParams, Lens, pixels_to_rays, rays_to_pixels, rotation_matrix, lens_inverse_lut
from .features import ControlPoint
from .images import SUPPORTED_EXT, build_proxy, build_thumb, cache_key, imwrite
from .project import Project, RenderSettings
from .warp import clamp_fov, compute_layout

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

ROOT = Path(__file__).resolve().parent.parent


def _data_root() -> Path:
    """캐시와 프로젝트를 둘 곳.

    윈도우에서는 프로그램 폴더가 Program Files 일 수 있어 쓰기가 막힌다.
    사용자별 %LOCALAPPDATA%\\Meridian 에 둔다. 맥은 앱 번들 안에 두던 그대로다.
    MERIDIAN_DATA 로 어디든 바꿀 수 있다.
    """
    env = os.environ.get("MERIDIAN_DATA")
    if env:
        return Path(env)
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Meridian"
    return ROOT


DATA = _data_root()
CACHE = DATA / "cache"
PROJECTS = DATA / "projects"
CACHE.mkdir(parents=True, exist_ok=True)
PROJECTS.mkdir(parents=True, exist_ok=True)
render.sweep_scratch(CACHE)

app = FastAPI(title="Meridian")

STATE: dict = {"project": Project(), "path": None}
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


def P() -> Project:
    return STATE["project"]


# ---------------------------------------------------------------- 작업 스레드

class Cancelled(Exception):
    """사용자가 작업을 중단시켰다. 실패가 아니므로 따로 다룬다."""


def run_job(fn, label: str, cancellable: bool = False) -> str:
    """오래 걸리는 작업을 스레드로 돌리고 job id 를 준다.

    중단은 progress 를 통해 전한다. 오래 걸리는 작업은 어차피 진행률을 계속
    알리고 있으므로, 그 자리에서 예외를 던지면 어디서 멈추든 안전하게 풀린다.
    스레드를 밖에서 죽이는 방법은 파이썬에 없고, 있어도 반쯤 쓰다 만 파일을
    남기게 된다.
    """
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"stage": "start", "frac": 0.0, "message": label,
                 "done": False, "error": None, "result": None,
                 "cancellable": cancellable, "cancelled": False,
                 "started": time.time()}

    def progress(stage: str, frac: float, message: str):
        j = JOBS.get(jid)
        if not j:
            return
        if j.get("cancel"):
            raise Cancelled()
        j.update(stage=stage, frac=float(np.clip(frac, 0, 1)), message=message)

    def worker():
        try:
            res = fn(progress)
            JOBS[jid].update(done=True, frac=1.0, result=res, stage="done",
                             message="완료", elapsed=time.time() - JOBS[jid]["started"])
        except Cancelled:
            JOBS[jid].update(done=True, cancelled=True, stage="cancelled",
                             message="취소했습니다")
        except Exception as e:
            traceback.print_exc()
            JOBS[jid].update(done=True, error=f"{type(e).__name__}: {e}", stage="error",
                             message="실패")

    sysmem.start_heavy_thread(worker)
    return jid


@app.get("/api/job/{jid}")
def job_status(jid: str):
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, "그런 작업이 없습니다")
    return {k: v for k, v in j.items() if k not in ("started", "cancel")}


@app.post("/api/job/{jid}/cancel")
def job_cancel(jid: str):
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, "그런 작업이 없습니다")
    if j["done"]:
        return {"ok": False, "reason": "이미 끝났습니다"}
    j["cancel"] = True
    j["message"] = "중단하는 중…"
    return {"ok": True}


# ---------------------------------------------------------------- 프로젝트

def project_payload() -> dict:
    p = P()
    d = p.to_dict()
    d["cp_stats"] = p.cp_stats()
    d["lens_users"] = {str(lid): [i for i, q in p.params.items() if q.lens_id == lid]
                       for lid in p.lenses}
    d["path"] = STATE["path"]
    return d


@app.get("/api/project")
def get_project():
    return project_payload()


class NewProject(BaseModel):
    name: str = "제목 없는 파노라마"


@app.post("/api/project/new")
def new_project(req: NewProject):
    STATE["project"] = Project(name=req.name)
    STATE["path"] = None
    return project_payload()


class SavePath(BaseModel):
    path: str | None = None


@app.post("/api/project/save")
def save_project(req: SavePath):
    path = req.path or STATE["path"]
    if not path:
        path = str(PROJECTS / f"{P().name or 'panorama'}.meridian")
    P().save(Path(path))
    STATE["path"] = str(path)
    return {"path": STATE["path"]}


@app.post("/api/project/open")
def open_project(req: SavePath):
    if not req.path:
        picked = pick_files(kind="project")
        if not picked:
            return {"cancelled": True}
        req.path = picked[0]
    STATE["project"] = Project.load(Path(req.path))
    STATE["path"] = req.path
    P().refresh_exif()
    return project_payload()


@app.post("/api/images/refresh-exif")
def refresh_exif():
    """EXIF 를 못 읽은 채 들어간 사진을 다시 읽는다."""
    return {"fixed": P().refresh_exif(), "project": project_payload()}


# ---------------------------------------------------------------- 이미지

def pick_files(kind: str = "image") -> list[str]:
    """파일 선택창을 띄워 경로만 받아온다.

    수 GB 짜리 RAW 를 브라우저로 올리게 할 수는 없으므로, 창은 서버가 직접
    띄우고 원본은 있던 자리에서 읽는다. macOS 는 osascript 가 가장 자연스럽고,
    그 밖의 플랫폼에서는 표준 라이브러리의 tkinter 를 쓴다.
    """
    if IS_MAC:
        return _pick_files_mac(kind)
    return _pick_files_tk(kind)


def _pick_files_mac(kind: str) -> list[str]:
    if kind == "project":
        script = ('choose file with prompt "프로젝트 열기" of type {"meridian"}')
    elif kind == "folder":
        script = 'choose folder with prompt "사진이 든 폴더 선택"'
    else:
        script = ('choose file with prompt "사진 선택" with multiple selections allowed')
    try:
        r = subprocess.run(["osascript", "-e", f'set t to ({script})',
                            "-e", 'set o to ""',
                            "-e", 'repeat with x in (t as list)',
                            "-e", 'set o to o & POSIX path of x & linefeed',
                            "-e", 'end repeat', "-e", "return o"],
                           capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return []
    if r.returncode != 0:
        return []                     # 사용자가 취소
    return [ln for ln in r.stdout.strip().splitlines() if ln]


def _pick_files_tk(kind: str) -> list[str]:
    """tkinter 파일 선택창. 윈도우와 리눅스에서 쓴다."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return []

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        if kind == "project":
            picked = filedialog.askopenfilename(
                title="프로젝트 열기", filetypes=[("Meridian 프로젝트", "*.meridian")])
            return [picked] if picked else []
        if kind == "folder":
            picked = filedialog.askdirectory(title="사진이 든 폴더 선택")
            return [picked] if picked else []
        types = [("사진", " ".join(f"*{e}" for e in sorted(SUPPORTED_EXT))), ("모든 파일", "*.*")]
        picked = filedialog.askopenfilenames(title="사진 선택", filetypes=types)
        return list(picked)
    finally:
        root.destroy()


class AddImages(BaseModel):
    paths: list[str] | None = None
    browse: str | None = None         # "files" | "folder"


@app.post("/api/images/add")
def add_images(req: AddImages):
    paths: list[str] = list(req.paths or [])
    if req.browse == "files":
        paths = pick_files("image")
    elif req.browse == "folder":
        folders = pick_files("folder")
        for f in folders:
            paths.extend(sorted(str(x) for x in Path(f).iterdir()
                                if x.suffix.lower() in SUPPORTED_EXT))
    paths = [p for p in paths if Path(p).suffix.lower() in SUPPORTED_EXT and Path(p).exists()]
    if not paths:
        return {"added": [], "cancelled": True}

    def work(progress):
        added = []
        with LOCK:
            known = {im.path for im in P().images.values()}
            for k, path in enumerate(paths):
                if path in known:
                    continue
                progress("add", (k + 1) / len(paths), f"불러오는 중 {k + 1}/{len(paths)} — {Path(path).name}")
                try:
                    added.append(P().add_image(path, CACHE).to_dict())
                except Exception as e:
                    progress("add", (k + 1) / len(paths), f"건너뜀: {Path(path).name} ({e})")
        return {"added": added}

    return {"job": run_job(work, "사진 불러오는 중")}


@app.post("/api/images/upload")
async def upload_images(files: list[UploadFile] = File(...)):
    """브라우저 드래그&드롭 경로. 받은 파일을 캐시에 두고 등록한다."""
    drop = CACHE / "dropped"
    drop.mkdir(exist_ok=True)
    saved: list[str] = []
    for f in files:
        if Path(f.filename or "").suffix.lower() not in SUPPORTED_EXT:
            continue
        dst = drop / Path(f.filename).name
        dst.write_bytes(await f.read())
        saved.append(str(dst))
    if not saved:
        return {"added": []}
    added = []
    with LOCK:
        known = {im.path for im in P().images.values()}
        for path in saved:
            if path not in known:
                added.append(P().add_image(path, CACHE).to_dict())
    return {"added": added}


@app.delete("/api/images/{image_id}")
def delete_image(image_id: int):
    with LOCK:
        P().remove_image(image_id)
    return project_payload()


class ImageFlag(BaseModel):
    enabled: bool | None = None
    exposure_ev: float | None = None
    anchor: bool | None = None


@app.patch("/api/images/{image_id}")
def patch_image(image_id: int, req: ImageFlag):
    p = P()
    if image_id not in p.images:
        raise HTTPException(404, "없는 이미지")
    if req.enabled is not None:
        p.images[image_id].enabled = req.enabled
    if req.exposure_ev is not None:
        p.exposure_ev[image_id] = req.exposure_ev
    if req.anchor:
        p.anchor = image_id
    return project_payload()


@app.get("/api/thumb/{image_id}")
def get_thumb(image_id: int):
    p = P()
    if image_id not in p.images:
        raise HTTPException(404, "없는 이미지")
    return FileResponse(build_thumb(p.images[image_id].path, CACHE), media_type="image/jpeg")


@app.get("/api/proxy/{image_id}")
def get_proxy(image_id: int):
    p = P()
    if image_id not in p.images:
        raise HTTPException(404, "없는 이미지")
    _, path = build_proxy(p.images[image_id].path, CACHE)
    return FileResponse(path, media_type="image/jpeg")


# ---------------------------------------------------------------- 정렬

class AlignReq(BaseModel):
    detect: bool = True               # 제어점을 새로 찾을지
    mode: str = opt.MODE_FULL
    straighten: bool = True
    max_features: int = 10000


@app.post("/api/align")
def align(req: AlignReq):
    def work(progress):
        p = P()
        ids = [i for i in p.active_ids if i in p.params]
        if len(ids) < 2:
            raise ValueError("사진이 두 장 이상 필요합니다")

        if req.detect:
            proxies, scales = {}, {}
            from concurrent.futures import ThreadPoolExecutor, as_completed
            import os
            
            def load_proxy(img_id, path):
                img, _ = build_proxy(path, CACHE)
                return img_id, img
                
            with ThreadPoolExecutor(max_workers=os.cpu_count() or 8) as ex:
                futures = {ex.submit(load_proxy, i, p.images[i].path): i for i in ids}
                completed = 0
                for fut in as_completed(futures):
                    i, img = fut.result()
                    proxies[i] = img
                    scales[i] = p.images[i].proxy_scale
                    completed += 1
                    progress("proxy", 0.15 * completed / len(ids), f"프록시 준비 {completed}/{len(ids)}")

            def sub(stage, frac, msg):
                base = {"detect": 0.15, "match": 0.45, "verify": 0.5}.get(stage, 0.5)
                span = {"detect": 0.3, "match": 0.05, "verify": 0.25}.get(stage, 0.2)
                progress(stage, base + span * frac, msg)

            cps, stats = features.find_control_points(proxies, scales, sub, req.max_features)
            manual = [c for c in p.control_points if c.kind != "auto"]
            p.control_points = manual + cps

        progress("solve", 0.8, "초기 자세 추정 중…")
        cps = p.control_points
        if not cps:
            raise ValueError("제어점을 찾지 못했습니다. 사진이 실제로 겹치는지 확인해 주세요")
        trusted = all(str(p.images[i].fov_source).startswith("EXIF") for i in ids)
        init, f_ref, groups = opt.initial_guess(
            cps, p.sizes(), {i: p.images[i].fov_hint for i in ids}, anchor=p.anchor,
            trusted_fov=trusted)
        for i, q in init.items():
            if i in p.params:
                q.lens_id = p.params[i].lens_id
        if trusted:
            for lid, lens in p.lenses.items():
                users = [i for i in ids if p.params[i].lens_id == lid]
                if users:
                    lens.fov = p.images[users[0]].fov_hint

        # '자세만' 단계는 건너뛴다. 화각을 고정한 채로 풀면 0.5도만 어긋나도
        # 자세가 그 오차를 떠안으려 하면서 반복 한도까지 헤맨다. 화각을 함께
        # 풀면 같은 데이터에서 훨씬 빨리, 더 낮은 잔차로 내려간다.
        progress("solve", 0.86, "번들 조정 — 자세 + 화각")
        r = opt.optimize(cps, init, p.lenses, p.sizes(), opt.MODE_POSITION_FOV, p.anchor)
        if req.mode in (opt.MODE_FULL, opt.MODE_EVERYTHING):
            progress("solve", 0.95, "번들 조정 — 렌즈 왜곡 포함")
            r = opt.optimize(cps, r.images, r.lenses, p.sizes(), req.mode, p.anchor)

        with LOCK:
            p.params.update(r.images)
            p.lenses.update(r.lenses)
            if req.straighten:
                p.params.update(opt.straighten(p.active_params()))
            for c, e in zip([c for c in cps if c.enabled and c.kind in ("auto", "manual")],
                            r.per_point):
                c.error = float(e)
            p.optimized = True
            p.last_rms = r.rms
            p.layout = compute_layout(p.active_params(), p.lenses, p.sizes(),
                                      p.settings.projection)
            # 다시 정렬하면 사진이 덮는 범위가 달라진다. 화각은 처음 그릴 때 새로 맞춘다.
            p.settings.hfov = p.settings.vfov = 0.0

        # 정렬이 끝나야 겹침 대응점을 알 수 있으므로 비네팅은 여기서 추정한다
        progress("photo", 0.97, "렌즈 비네팅 재는 중…")
        try:
            _estimate_photometric(p, progress)
        except Exception as e:
            progress("photo", 0.99, f"비네팅 추정 건너뜀 ({e})")
        return {"rms": r.rms, "max_error": r.max_error, "control_points": len(cps),
                "groups": [len(g) for g in p.cp_stats()["groups"]],
                "fov": {str(i): l.fov for i, l in p.lenses.items()},
                "converged": r.converged}

    return {"job": run_job(work, "자동 정렬 중")}


def _estimate_photometric(p: Project, progress=None) -> dict:
    """겹침에서 비네팅 계수와 이미지별 노출을 함께 추정해 프로젝트에 넣는다."""
    ids = [i for i in p.active_ids if i in p.params]
    if len(ids) < 2:
        return {}
    proxies = {i: build_proxy(p.images[i].path, CACHE)[0] for i in ids}
    pairs = sorted({(min(c.img_a, c.img_b), max(c.img_a, c.img_b))
                    for c in p.control_points
                    if c.enabled and c.img_a != c.img_b
                    and c.img_a in proxies and c.img_b in proxies})
    samples = photometric.collect_samples(proxies, p.params, p.lenses, p.sizes(), pairs)
    vig, expo, flare, rms = photometric.solve(
        samples, ids, {i: p.params[i].lens_id for i in ids})
    with LOCK:
        p.vignetting = vig
        p.photo_exposure = expo
        p.photo_flare = flare
    corner = {}
    for lid, v in vig.items():
        corner[str(lid)] = round(float(v.falloff(np.array([1.0]))[0]), 4)
    fl = list(flare.values())
    return {"rms": rms, "corner_falloff": corner,
            "flare_max": round(float(max(fl)) if fl else 0.0, 5),
            "samples": int(len(samples.get("Ii", [])))}


@app.post("/api/photometric")
def redo_photometric():
    """비네팅·노출을 다시 잰다."""
    def work(progress):
        progress("photo", 0.3, "겹침에서 밝기 재는 중…")
        return _estimate_photometric(P(), progress)
    return {"job": run_job(work, "밝기 보정 다시 계산")}


class OptimizeReq(BaseModel):
    mode: str = opt.MODE_POSITION_FOV
    straighten: bool = False


@app.post("/api/optimize")
def reoptimize(req: OptimizeReq):
    def work(progress):
        p = P()
        progress("solve", 0.3, "번들 조정 중…")
        r = opt.optimize(p.control_points, p.active_params(), p.lenses, p.sizes(),
                         req.mode, p.anchor)
        with LOCK:
            p.params.update(r.images)
            p.lenses.update(r.lenses)
            if req.straighten:
                p.params.update(opt.straighten(p.active_params()))
            for c, e in zip([c for c in p.control_points
                             if c.enabled and c.kind in ("auto", "manual")], r.per_point):
                c.error = float(e)
            p.optimized = True
            p.last_rms = r.rms
        return {"rms": r.rms, "max_error": r.max_error, "converged": r.converged,
                "fov": {str(i): l.fov for i, l in p.lenses.items()}}
    return {"job": run_job(work, "최적화 중")}


@app.post("/api/straighten")
def do_straighten():
    with LOCK:
        P().params.update(opt.straighten(P().active_params()))
        P().layout = None
    return project_payload()


# ---------------------------------------------------------------- 제어점

@app.get("/api/control-points")
def list_cps(pair: str | None = None):
    cps = P().control_points
    out = []
    for n, c in enumerate(cps):
        if pair:
            a, b = (int(x) for x in pair.split(","))
            if {c.img_a, c.img_b} != {a, b}:
                continue
        d = c.to_dict()
        d["index"] = n
        out.append(d)
    return {"points": out}


class CPCreate(BaseModel):
    img_a: int
    img_b: int
    xa: float
    ya: float
    xb: float
    yb: float
    kind: str = "manual"


@app.post("/api/control-points")
def add_cp(req: CPCreate):
    with LOCK:
        P().control_points.append(ControlPoint(req.img_a, req.img_b, req.xa, req.ya,
                                               req.xb, req.yb, kind=req.kind))
    return {"count": len(P().control_points)}


class CPPatch(BaseModel):
    enabled: bool | None = None
    weight: float | None = None
    xa: float | None = None
    ya: float | None = None
    xb: float | None = None
    yb: float | None = None


@app.patch("/api/control-points/{index}")
def patch_cp(index: int, req: CPPatch):
    cps = P().control_points
    if not 0 <= index < len(cps):
        raise HTTPException(404, "없는 제어점")
    if req.enabled is not None:
        cps[index].enabled = req.enabled
    if req.weight is not None:
        cps[index].weight = req.weight
    if req.xa is not None:
        cps[index].xa = req.xa
    if req.ya is not None:
        cps[index].ya = req.ya
    if req.xb is not None:
        cps[index].xb = req.xb
    if req.yb is not None:
        cps[index].yb = req.yb
    
    # Swift's ControlPoint model requires 'index'
    d = cps[index].to_dict()
    d["index"] = index
    return d


@app.delete("/api/control-points/{index}")
def delete_cp(index: int):
    with LOCK:
        cps = P().control_points
        if not 0 <= index < len(cps):
            raise HTTPException(404, "없는 제어점")
        cps.pop(index)
    return {"count": len(P().control_points)}


class CPPrune(BaseModel):
    threshold: float = 0.0            # 0 이면 RMS 의 3배를 자동 기준으로


@app.post("/api/control-points/prune")
def prune_cps(req: CPPrune):
    """잔차가 큰 제어점을 꺼서 최적화 품질을 끌어올린다."""
    p = P()
    errs = [c.error for c in p.control_points if c.enabled and c.error > 0]
    if not errs:
        return {"disabled": 0}
    thr = req.threshold or max(3.0, float(np.sqrt(np.mean(np.square(errs)))) * 3.0)
    n = 0
    with LOCK:
        for c in p.control_points:
            if c.enabled and c.error > thr:
                c.enabled = False
                n += 1
    return {"disabled": n, "threshold": thr}


class CPSuggest(BaseModel):
    img_a: int
    img_b: int
    xa: float
    ya: float
    search: int = 220                 # 탐색 반경(원본 픽셀)
    patch: int = 56                   # 비교할 패치 크기


@app.post("/api/control-points/suggest")
def suggest_cp(req: CPSuggest):
    """한쪽 점을 찍으면 반대쪽 짝을 찾아준다.

    현재 카메라 파라미터로 대략 위치를 예측하고, 그 주변에서 정규화 상호상관
    으로 다듬는다. 아직 정렬 전이면 예측이 없으므로 이미지 전역에서 찾는다.
    """
    p = P()
    if req.img_a not in p.images or req.img_b not in p.images:
        raise HTTPException(404, "없는 이미지")
    sa, sb = p.images[req.img_a], p.images[req.img_b]
    pa, pb = p.params.get(req.img_a), p.params.get(req.img_b)
    proxy_a, _ = build_proxy(sa.path, CACHE)
    proxy_b, _ = build_proxy(sb.path, CACHE)

    guess = None
    if p.optimized and pa and pb:
        la, lb = p.lenses[pa.lens_id], p.lenses[pb.lens_id]
        v = pixels_to_rays(np.array([[req.xa, req.ya]]), la, sa.width, sa.height,
                           rotation_matrix(pa.yaw, pa.pitch, pa.roll))
        px, ok = rays_to_pixels(v, lb, sb.width, sb.height,
                                rotation_matrix(pb.yaw, pb.pitch, pb.roll),
                                lens_inverse_lut(lb.a, lb.b, lb.c))
        if bool(np.all(ok)):
            guess = (float(px[0, 0]), float(px[0, 1]))

    ka, kb = 1.0 / sa.proxy_scale, 1.0 / sb.proxy_scale     # 원본 -> 프록시
    half = max(8, int(req.patch * ka / 2))
    cx, cy = int(round(req.xa * ka)), int(round(req.ya * ka))
    ha, wa = proxy_a.shape[:2]
    if not (half <= cx < wa - half and half <= cy < ha - half):
        raise HTTPException(400, "가장자리라 견본을 뜰 수 없습니다")
    tmpl = proxy_a[cy - half:cy + half, cx - half:cx + half]

    hb, wb = proxy_b.shape[:2]
    if guess:
        gx, gy = guess[0] * kb, guess[1] * kb
        rad = max(24, int(req.search * kb))
    else:
        gx, gy, rad = wb / 2, hb / 2, max(wb, hb)
    x0 = int(np.clip(gx - rad, 0, wb - 1)); x1 = int(np.clip(gx + rad, 1, wb))
    y0 = int(np.clip(gy - rad, 0, hb - 1)); y1 = int(np.clip(gy + rad, 1, hb))
    region = proxy_b[y0:y1, x0:x1]
    if region.shape[0] < tmpl.shape[0] or region.shape[1] < tmpl.shape[1]:
        raise HTTPException(400, "탐색 범위가 견본보다 작습니다")

    res = cv2.matchTemplate(region, tmpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    bx = (x0 + loc[0] + half) / kb
    by = (y0 + loc[1] + half) / kb
    return {"xb": float(bx), "yb": float(by), "score": float(score),
            "guessed": guess is not None,
            "guess": {"xb": guess[0], "yb": guess[1]} if guess else None}


# ---------------------------------------------------------------- 카메라·렌즈 수동 조정

class ParamPatch(BaseModel):
    yaw: float | None = None
    pitch: float | None = None
    roll: float | None = None


@app.patch("/api/params/{image_id}")
def patch_params(image_id: int, req: ParamPatch):
    p = P()
    if image_id not in p.params:
        raise HTTPException(404, "없는 이미지")
    q = p.params[image_id]
    for k in ("yaw", "pitch", "roll"):
        v = getattr(req, k)
        if v is not None:
            setattr(q, k, float(np.radians(v)))
    return {"image_id": image_id, "params": asdict(q)}


class LensPatch(BaseModel):
    fov: float | None = None
    projection: str | None = None
    a: float | None = None
    b: float | None = None
    c: float | None = None


@app.patch("/api/lenses/{lens_id}")
def patch_lens(lens_id: int, req: LensPatch):
    p = P()
    if lens_id not in p.lenses:
        raise HTTPException(404, "없는 렌즈")
    l = p.lenses[lens_id]
    for k in ("fov", "projection", "a", "b", "c"):
        v = getattr(req, k)
        if v is not None:
            setattr(l, k, v)
    return asdict(l)


# ---------------------------------------------------------------- 설정·렌더

@app.patch("/api/settings")
def patch_settings(req: dict):
    s = P().settings
    changed_layout = False
    for k, v in req.items():
        if hasattr(s, k):
            # 배치를 버리는 건 투영 방식이 바뀔 때뿐이다. 배치에는 사용자가 정한
            # 파노라마 방향(좌우·상하·기울기)이 들어 있고, 크기는 렌더할 때마다
            # 그 위에서 새로 계산된다. 예전엔 출력 크기만 바꿔도 배치를 지워서,
            # 리틀 플래닛(상하 -90°)을 1024 로 내보내면 방향이 사라진 채
            # 엉뚱한 그림이 나왔다.
            if k == "projection" and getattr(s, k) != v:
                changed_layout = True
            setattr(s, k, v)
    if changed_layout:
        P().layout = None
        # 화각의 뜻이 투영마다 다르다 (직선 투영에 360도는 없다). 새로 맞춘다.
        if "hfov" not in req and "vfov" not in req:
            s.hfov = s.vfov = 0.0
    if "hfov" in req or "vfov" in req:
        if s.hfov > 0 and s.vfov > 0:
            s.hfov, s.vfov = clamp_fov(s.projection, s.hfov, s.vfov)
    return s.to_dict()


@app.get("/api/layout")
def layout_info():
    """설정만 바꿔 놓고 아직 안 그렸을 때 '나올 크기' 를 바로 알려 준다.

    compute_layout 은 이미지 화소를 건드리지 않아 순식간에 끝난다. 크기를
    보여 주자고 렌더를 다시 돌릴 이유가 없다.
    """
    p = P()
    if not p.active_ids or not p.params:
        return {"full_size": [0, 0], "native_size": [0, 0], "megapixels": 0.0}
    full = render._layout_for(p)
    native = compute_layout(p.active_params(), p.lenses, p.sizes(),
                            p.settings.projection, center=full.center,
                            fov=(p.settings.hfov, p.settings.vfov))
    return {"full_size": [full.w, full.h], "native_size": [native.w, native.h],
            "megapixels": round(full.w * full.h / 1e6, 1)}


class PreviewReq(BaseModel):
    max_dim: int = 1400
    show_seams: bool = False
    center_yaw: float | None = None   # 도 단위
    center_pitch: float | None = None
    center_roll: float | None = None


@app.post("/api/preview")
def preview(req: PreviewReq):
    def work(progress):
        p = P()
        if (req.center_yaw is not None or req.center_pitch is not None
                or req.center_roll is not None):
            base = p.layout or compute_layout(p.active_params(), p.lenses, p.sizes(),
                                              p.settings.projection)
            cy = np.radians(req.center_yaw) if req.center_yaw is not None else base.center_yaw
            cp = np.radians(req.center_pitch) if req.center_pitch is not None else base.center_pitch
            cr = np.radians(req.center_roll) if req.center_roll is not None else base.center_roll
            p.layout = compute_layout(p.active_params(), p.lenses, p.sizes(),
                                      p.settings.projection, center=(cy, cp, cr))
        render.ensure_fov(p)
        img, lay, evs = render.render_preview(p, CACHE, req.max_dim,
                                              show_seams=req.show_seams, progress=progress)
        name = f"preview_{uuid.uuid4().hex[:10]}.jpg"
        imwrite(CACHE / name, img, [cv2.IMWRITE_JPEG_QUALITY, 88])
        full = render._layout_for(p, center=lay.center)
        native = compute_layout(p.active_params(), p.lenses, p.sizes(),
                                p.settings.projection, center=lay.center,
                                fov=(p.settings.hfov, p.settings.vfov))
        return {"url": f"/api/cache/{name}", "width": img.shape[1], "height": img.shape[0],
                "layout": lay.to_dict(), "full_size": [full.w, full.h],
                "native_size": [native.w, native.h],
                "megapixels": round(full.w * full.h / 1e6, 1),
                "deg_per_px": live.deg_per_px(lay),
                "exposure_ev": evs}
    return {"job": run_job(work, "미리보기 만드는 중")}


@app.post("/api/live/prepare")
def live_prepare():
    """끌기를 시작할 때 부른다. 원판이 없으면 뒤에서 만든다."""
    p = P()
    if not p.optimized:
        return {"ready": False}
    return live.prepare(p, CACHE)


class LiveReq(BaseModel):
    yaw: float = 0.0          # 도 단위
    pitch: float = 0.0
    roll: float = 0.0
    hfov: float = 0.0         # 0 이면 프로젝트 설정 그대로
    vfov: float = 0.0
    max_dim: int = live.LIVE_DIM


@app.post("/api/live/frame")
def live_frame(req: LiveReq):
    """끄는 동안의 한 장. 작업(job)으로 돌리지 않고 바로 그림을 돌려준다."""
    img = live.frame(P(), req.yaw, req.pitch, req.roll, req.max_dim,
                     fov=(req.hfov, req.vfov))
    if img is None:
        raise HTTPException(409, "원판을 준비하는 중입니다")
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return Response(content=buf.tobytes(), media_type="image/jpeg")


@app.get("/api/cache/{name}")
def get_cached(name: str):
    path = CACHE / Path(name).name
    if not path.exists():
        raise HTTPException(404, "없는 파일")
    return FileResponse(path, media_type="image/jpeg")


class RenderReq(BaseModel):
    path: str | None = None


@app.post("/api/render")
def do_render(req: RenderReq):
    p = P()
    out = req.path
    if not out:
        base = (Path(STATE["path"]).stem if STATE["path"] else p.name) or "panorama"
        out = str(Path.home() / "Desktop" / f"{base}.{p.settings.format}")

    def work(progress):
        rep = render.render_final(p, Path(out), CACHE, progress)
        return asdict(rep)
    return {"job": run_job(work, "최종 렌더링 중", cancellable=True), "path": out}


@app.post("/api/reveal")
def reveal(req: SavePath):
    """파일 탐색기에서 결과물을 보여 준다."""
    if not (req.path and Path(req.path).exists()):
        return {"ok": False}
    try:
        if IS_MAC:
            subprocess.run(["open", "-R", req.path])
        elif IS_WIN:
            # explorer 는 선택 인자를 한 덩어리로 받아야 한다
            subprocess.run(["explorer", f"/select,{os.path.normpath(req.path)}"])
        else:
            subprocess.run(["xdg-open", str(Path(req.path).parent)])
    except Exception:
        return {"ok": False}
    return {"ok": True}


# ---------------------------------------------------------------- 정적 파일

WEB = ROOT / "web"
if WEB.exists():
    app.mount("/", StaticFiles(directory=str(WEB), html=True), name="web")


@app.on_event("startup")
def _watch_parent():
    """앱이 사라지면 엔진도 따라 끝낸다.

    네이티브 앱은 이 서버를 자식으로 띄우는데, 앱이 강제 종료되거나 죽으면
    종료 절차가 돌지 않아 서버만 고아로 남는다. 그런 게 쌓이면 포트와 메모리를
    계속 붙들고 있으므로, 부모가 살아 있는지 직접 지켜보다가 정리한다.
    """
    raw = os.environ.get("MERIDIAN_PARENT_PID")
    if not raw or not raw.isdigit():
        return
    parent = int(raw)

    def alive() -> bool:
        """부모가 아직 살아 있는지만 확인한다.

        윈도우에서 os.kill(pid, 0) 을 쓰면 안 된다. 그쪽 구현은 신호를 보내는
        대신 TerminateProcess 를 불러서, 확인하려던 부모를 정말로 죽여 버린다.
        """
        if IS_WIN:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, parent)
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            k32.CloseHandle(handle)
            return bool(ok) and code.value == STILL_ACTIVE
        try:
            os.kill(parent, 0)              # POSIX: 신호 0 은 존재 확인만 한다
            return True
        except OSError:
            return False

    def watch():
        while True:
            time.sleep(2.0)
            if not alive():
                os._exit(0)

    threading.Thread(target=watch, daemon=True).start()


@app.on_event("startup")
def _autoload():
    """MERIDIAN_PROJECT 가 가리키는 프로젝트가 있으면 열어 둔다 (개발·테스트용)."""
    import os
    path = os.environ.get("MERIDIAN_PROJECT")
    if path and Path(path).exists():
        try:
            STATE["project"] = Project.load(Path(path))
            STATE["path"] = path
            n = STATE["project"].refresh_exif()
            print(f"[meridian] 프로젝트 자동 로드: {path} (EXIF 다시 읽음 {n}장)")
        except Exception as e:
            print(f"[meridian] 자동 로드 실패: {e}")
