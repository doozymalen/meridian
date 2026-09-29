"""프로젝트 상태 — 이미지 목록, 카메라, 렌즈, 제어점, 출력 설정.

모든 상태는 JSON 한 덩어리로 저장한다. 좌표는 원본 픽셀 기준이므로
프록시 해상도를 바꿔도 프로젝트는 그대로 유효하다.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np

from .camera import ImageParams, Lens
from .photometric import Vignetting
from .features import ControlPoint
from .images import SourceImage, register
from .warp import PanoLayout


@dataclass
class RenderSettings:
    projection: str = "equirect"
    # 출력 틀의 화각(도). 0 이면 아직 정하지 않은 것 — 처음 그릴 때 사진이
    # 다 들어가게 맞춰 채운다. 한번 정해지면 방향을 돌려도 틀이 그대로다.
    hfov: float = 0.0
    vfov: float = 0.0
    blender: str = "multiband"        # multiband | feather | none
    num_bands: int = 0                # 0 이면 캔버스 크기에서 자동
    seam: str = "dp_color_grad"       # dp_color_grad | graphcut | voronoi | none
    # 보정은 모두 꺼진 상태로 시작한다. 원본이 그대로 보이는 편이 기준이 되고,
    # 필요한 보정만 눈으로 확인하며 켜는 쪽이 사진을 다루는 순서에 맞는다.
    exposure: str = "none"            # auto | none
    per_channel: bool = False         # 화이트밸런스까지 맞출지
    vignetting: bool = False          # 렌즈 주변부 어두움 보정
    low_freq: float = 0.0             # 이미지 내부 밝기 기울기 맞춤 (0이면 끔)
    scale_percent: float = 100.0
    out_width: int = 0                # 0 보다 크면 이 가로폭에 정확히 맞춘다
    gpano: bool = True                # 파노라마 뷰어용 GPano/XMP 태그를 심는다
    patch_nadir: bool = False         # 천정/삼각대 자국 자동 복구
    # 사진이 없는 곳을 주변 색으로 메울지. 기본은 끔 — 안 찍힌 곳은 보이는 편이
    # 낫다. 실제로 찍힌 내용이 아니라 주변 색을 늘린 것이라, 켜면 어디를 못 찍었는지
    # 알 수 없게 된다. 켜든 끄든 미리보기와 내보내기는 똑같이 따른다.
    fill_gaps: bool = False
    max_dim: int = 0                  # 0 이면 제한 없음
    format: str = "jpg"               # jpg | tif | png
    quality: int = 94
    interpolation: str = "cubic"      # nearest | linear | cubic | lanczos

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Project:
    name: str = "제목 없는 파노라마"
    images: dict[int, SourceImage] = field(default_factory=dict)
    params: dict[int, ImageParams] = field(default_factory=dict)
    lenses: dict[int, Lens] = field(default_factory=dict)
    control_points: list[ControlPoint] = field(default_factory=list)
    layout: PanoLayout | None = None
    settings: RenderSettings = field(default_factory=RenderSettings)
    anchor: int | None = None
    exposure_ev: dict[int, float] = field(default_factory=dict)
    vignetting: dict[int, Vignetting] = field(default_factory=dict)   # lens_id -> 계수
    photo_exposure: dict[int, float] = field(default_factory=dict)    # image_id -> 배율
    photo_flare: dict[int, float] = field(default_factory=dict)        # image_id -> 미광
    optimized: bool = False
    last_rms: float = 0.0
    created: float = field(default_factory=time.time)
    modified: float = field(default_factory=time.time)

    # -- 이미지 -----------------------------------------------------
    def next_id(self) -> int:
        return max(self.images, default=-1) + 1

    def add_image(self, path: str | Path, cache_dir: Path) -> SourceImage:
        """원본을 등록하고 화각이 비슷한 기존 렌즈에 묶는다."""
        img = register(path, self.next_id(), cache_dir)
        self.images[img.id] = img
        lens_id = self._match_lens(img)
        self.params[img.id] = ImageParams(lens_id=lens_id)
        if self.anchor is None:
            self.anchor = img.id
        self.modified = time.time()
        return img

    def _match_lens(self, img: SourceImage) -> int:
        """같은 렌즈로 찍은 사진은 파라미터를 공유해야 왜곡 추정이 안정적이다."""
        key_model = (img.exif.get("lens"), round(img.exif.get("focal_length") or 0, 1))
        for lid, lens in self.lenses.items():
            users = [i for i, p in self.params.items() if p.lens_id == lid]
            if not users:
                continue
            other = self.images[users[0]]
            other_key = (other.exif.get("lens"), round(other.exif.get("focal_length") or 0, 1))
            same_shape = (other.width, other.height) == (img.width, img.height)
            if same_shape and (key_model == other_key or abs(lens.fov - img.fov_hint) < 0.5):
                return lid
        lid = max(self.lenses, default=-1) + 1
        self.lenses[lid] = Lens(fov=img.fov_hint, projection="rectilinear")
        return lid

    def remove_image(self, image_id: int) -> None:
        self.images.pop(image_id, None)
        self.params.pop(image_id, None)
        self.exposure_ev.pop(image_id, None)
        self.control_points = [c for c in self.control_points
                               if c.img_a != image_id and c.img_b != image_id]
        if self.anchor == image_id:
            self.anchor = min(self.images) if self.images else None
        self.modified = time.time()

    # -- 조회 -------------------------------------------------------
    @property
    def active_ids(self) -> list[int]:
        return sorted(i for i, im in self.images.items() if im.enabled)

    def sizes(self) -> dict[int, tuple[int, int]]:
        return {i: (im.width, im.height) for i, im in self.images.items()}

    def active_params(self) -> dict[int, ImageParams]:
        return {i: self.params[i] for i in self.active_ids if i in self.params}

    def refresh_exif(self) -> int:
        """EXIF 를 못 읽은 채 등록된 사진을 다시 읽는다. 고친 장수를 돌려준다.

        CR3 처럼 예전에 읽지 못하던 형식으로 추가한 사진은 빈 EXIF 를 들고 있다.
        읽는 쪽을 고쳐도 프로젝트에 이미 들어간 값은 그대로라, 열 때마다 비어 있는
        것만 다시 읽는다. 정렬 전이면 렌즈 화각도 새 추정으로 바꾸고, 정렬 뒤라면
        번들 조정이 이미 푼 화각이 더 정확하므로 건드리지 않는다.
        """
        import statistics
        from .images import _exif_cache, estimate_hfov, read_exif
        fixed: list[int] = []
        for img in self.images.values():
            if img.exif.get("make") or img.exif.get("focal_length"):
                continue
            if not Path(img.path).exists():
                continue
            _exif_cache.pop(str(Path(img.path)), None)
            ex = read_exif(img.path)
            if not (ex.get("make") or ex.get("focal_length")):
                continue
            img.exif = {k: v for k, v in ex.items() if v is not None}
            img.fov_hint, img.fov_source = estimate_hfov(img.path, img.width, img.height)
            fixed.append(img.id)
        if fixed and not self.optimized:
            for lid, lens in self.lenses.items():
                hints = [self.images[i].fov_hint for i in fixed
                         if i in self.params and self.params[i].lens_id == lid]
                if hints:
                    lens.fov = float(statistics.median(hints))
        return len(fixed)

    def photometric_for(self, image_id: int) -> tuple[Vignetting, float, float]:
        """이 사진에 적용할 (비네팅 계수, 노출 배율, 미광). 꺼져 있으면 무보정."""
        if not self.settings.vignetting:
            return Vignetting(), 1.0, 0.0
        lens_id = self.params[image_id].lens_id if image_id in self.params else 0
        return (self.vignetting.get(lens_id, Vignetting()),
                self.photo_exposure.get(image_id, 1.0),
                self.photo_flare.get(image_id, 0.0))

    def cp_stats(self) -> dict:
        """제어점 현황과 연결이 끊긴 이미지를 알려준다."""
        linked: dict[int, set[int]] = {i: set() for i in self.active_ids}
        n_enabled = 0
        for c in self.control_points:
            if not c.enabled:
                continue
            n_enabled += 1
            if c.img_a in linked and c.img_b in linked and c.img_a != c.img_b:
                linked[c.img_a].add(c.img_b)
                linked[c.img_b].add(c.img_a)
        # 연결 요소 구하기
        seen: set[int] = set()
        groups: list[list[int]] = []
        for i in self.active_ids:
            if i in seen:
                continue
            stack, comp = [i], []
            seen.add(i)
            while stack:
                cur = stack.pop()
                comp.append(cur)
                for nb in linked.get(cur, ()):
                    if nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
            groups.append(sorted(comp))
        return {
            "total": len(self.control_points),
            "enabled": n_enabled,
            "groups": sorted(groups, key=len, reverse=True),
            "orphans": sorted(i for i in self.active_ids if not linked.get(i)),
        }

    # -- 직렬화 -----------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "images": {str(i): im.to_dict() for i, im in self.images.items()},
            "params": {str(i): asdict(p) for i, p in self.params.items()},
            "lenses": {str(i): asdict(l) for i, l in self.lenses.items()},
            "control_points": [c.to_dict() for c in self.control_points],
            "layout": self.layout.to_dict() if self.layout else None,
            "settings": self.settings.to_dict(),
            "anchor": self.anchor,
            "exposure_ev": {str(k): v for k, v in self.exposure_ev.items()},
            "vignetting": {str(k): asdict(v) for k, v in self.vignetting.items()},
            "photo_exposure": {str(k): v for k, v in self.photo_exposure.items()},
            "photo_flare": {str(k): v for k, v in self.photo_flare.items()},
            "optimized": self.optimized,
            "last_rms": self.last_rms,
            "created": self.created,
            "modified": self.modified,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        p = cls(name=d.get("name", "제목 없는 파노라마"))
        for k, v in (d.get("images") or {}).items():
            v = dict(v)
            v.pop("name", None)
            v.pop("is_raw", None)
            p.images[int(k)] = SourceImage(**v)
        for k, v in (d.get("params") or {}).items():
            v = dict(v)
            if "white_balance" in v and v["white_balance"] is not None:
                v["white_balance"] = tuple(v["white_balance"])
            p.params[int(k)] = ImageParams(**v)
        lens_fields = set(Lens().__dict__)
        for k, v in (d.get("lenses") or {}).items():
            p.lenses[int(k)] = Lens(**{a: b for a, b in v.items() if a in lens_fields})
        p.control_points = [ControlPoint(**c) for c in (d.get("control_points") or [])]
        if d.get("layout"):
            p.layout = PanoLayout(**d["layout"])
        if d.get("settings"):
            known = {f for f in RenderSettings().__dict__}
            p.settings = RenderSettings(**{k: v for k, v in d["settings"].items() if k in known})
        from .blend import SAFE_SEAM_FINDERS
        if p.settings.seam not in SAFE_SEAM_FINDERS:
            p.settings.seam = "dp_color_grad"
        p.anchor = d.get("anchor")
        p.exposure_ev = {int(k): float(v) for k, v in (d.get("exposure_ev") or {}).items()}
        p.vignetting = {int(k): Vignetting(**v) for k, v in (d.get("vignetting") or {}).items()}
        p.photo_exposure = {int(k): float(v) for k, v in (d.get("photo_exposure") or {}).items()}
        p.photo_flare = {int(k): float(v) for k, v in (d.get("photo_flare") or {}).items()}
        p.optimized = bool(d.get("optimized", False))
        p.last_rms = float(d.get("last_rms", 0.0))
        p.created = d.get("created", time.time())
        p.modified = d.get("modified", time.time())
        return p

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.modified = time.time()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)             # 저장 중 죽어도 기존 파일이 남도록

    @classmethod
    def load(cls, path: Path) -> "Project":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
