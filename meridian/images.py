"""이미지 입출력 — RAW 디코딩, EXIF 해석, 프록시 캐시.

원본은 한 장이 1억 화소를 넘을 수 있으므로 화면 표시와 특징점 검출은 모두
프록시(긴 변 기준 축소본)로 한다. 최종 렌더링만 원본을 스트리밍으로 읽는다.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

RAW_EXT = {".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng", ".raf",
           ".orf", ".rw2", ".pef", ".srw", ".erf", ".kdc", ".dcr", ".mos", ".iiq", ".3fr"}
LDR_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".jp2", ".hdr", ".exr"}
SUPPORTED_EXT = RAW_EXT | LDR_EXT

PROXY_MAX = 1600          # 특징점 검출·미리보기용 긴 변
THUMB_MAX = 360           # 필름스트립용


def is_raw(path: Path | str) -> bool:
    return Path(path).suffix.lower() in RAW_EXT


# ---------------------------------------------------------------- 파일 입출력
#
# cv2.imread / cv2.imwrite 는 윈도우에서 경로에 비ASCII 문자(한글 사용자 이름,
# 폴더 이름)가 있으면 오류 없이 None / False 를 돌려준다. 그런 경로일 때만
# 바이트를 파이썬이 읽고 쓰고 OpenCV 에는 메모리 버퍼를 넘긴다. 나머지는 그대로
# 두는데, 버퍼를 거치면 기가픽셀 TIFF 가 통째로 메모리에 한 번 더 올라가기 때문이다.

def _needs_buffer(path: Path | str) -> bool:
    if sys.platform != "win32":
        return False
    return not str(path).isascii()


def imread(path: Path | str, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None:
    if not _needs_buffer(path):
        return cv2.imread(str(path), flags)
    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if buf.size == 0:
        return None
    return cv2.imdecode(buf, flags)


def imwrite(path: Path | str, img: np.ndarray, params: list[int] | None = None) -> bool:
    params = params or []
    if not _needs_buffer(path):
        return cv2.imwrite(str(path), img, params)
    ok, buf = cv2.imencode(Path(path).suffix or ".jpg", img, params)
    if not ok:
        return False
    try:
        buf.tofile(str(path))
    except OSError:
        return False
    return True


def _open_raw(path: Path):
    """rawpy(LibRaw) 도 윈도우에서는 비ASCII 경로를 못 연다. 그때는 파일 객체로 넘긴다."""
    import rawpy
    if not _needs_buffer(path):
        return rawpy.imread(str(path))
    with open(path, "rb") as fh:
        return rawpy.imread(fh)


# ---------------------------------------------------------------- 원본 읽기

def read_image(path: Path | str, half_size: bool = False) -> np.ndarray:
    """경로 -> BGR uint8 배열. RAW 는 rawpy 로 현상한다.

    half_size 는 RAW 에서만 의미가 있고, 디모자이크를 건너뛰어 4배 빨라진다.
    """
    path = Path(path)
    if is_raw(path):
        import rawpy
        with _open_raw(path) as raw:
            rgb = raw.postprocess(
                use_camera_wb=True,
                no_auto_bright=True,          # 장마다 밝기가 튀면 스티칭이 어긋난다
                output_bps=8,
                half_size=half_size,
                output_color=rawpy.ColorSpace.sRGB,
                # user_flip 은 건드리지 않는다 — 기본값이 카메라가 기록한
                # 회전(sizes.flip)을 그대로 적용하고, read_size 가 이와 맞춘다.
            )
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

    img = imread(path, cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    if img is None:                           # PNG 16bit, EXR 등 예외 경로
        img = imread(path, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise OSError(f"이미지를 열 수 없습니다: {path}")
        if img.dtype != np.uint8:
            img = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        elif img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return _apply_exif_rotation(img, path)


def _apply_exif_rotation(img: np.ndarray, path: Path) -> np.ndarray:
    """IMREAD_IGNORE_ORIENTATION 으로 읽었으므로 방향을 직접 적용한다."""
    orient = read_exif(path).get("orientation", 1)
    if orient == 3:
        return cv2.rotate(img, cv2.ROTATE_180)
    if orient == 6:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if orient == 8:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img


def read_size(path: Path | str) -> tuple[int, int]:
    """전체 디코딩 없이 (width, height) 를 얻는다."""
    path = Path(path)
    if is_raw(path):
        with _open_raw(path) as raw:
            # iwidth/iheight 는 회전 '전' 크기다. postprocess 는 sizes.flip 을
            # 적용해 내보내므로 여기서도 같은 회전을 반영해야 한다.
            # (flip 5 = 90도 반시계, 6 = 90도 시계 — 둘 다 가로세로가 바뀐다)
            w, h = int(raw.sizes.iwidth), int(raw.sizes.iheight)
            if raw.sizes.flip in (5, 6):
                w, h = h, w
            # postprocess 가 이미 회전을 넣으므로 EXIF 방향은 보지 않는다.
            return w, h

    from PIL import Image
    with Image.open(path) as im:
        w, h = im.size
    if read_exif(path).get("orientation", 1) in (5, 6, 7, 8):
        w, h = h, w
    return int(w), int(h)


# ---------------------------------------------------------------- EXIF

_exif_cache: dict[str, dict] = {}


# 캐논 CR3 의 메타데이터 상자 식별자
_CANON_UUID = bytes.fromhex("85c0b687820f11e08111f4ce462b6a48")


def _bmff_boxes(fh, start: int, end: int):
    """ISO BMFF(MP4 와 같은 상자 구조)의 상자를 차례로 돌려준다: (종류, 본문 시작, 끝)."""
    import struct
    pos = start
    while pos + 8 <= end:
        fh.seek(pos)
        size, typ = struct.unpack(">I4s", fh.read(8))
        head = 8
        if size == 1:
            size = struct.unpack(">Q", fh.read(8))[0]
            head = 16
        elif size == 0:
            size = end - pos
        if size < head:
            return
        yield typ, pos + head, pos + size
        pos += size


def _cr3_tags(path: Path) -> dict:
    """캐논 CR3 에서 EXIF 를 꺼낸다.

    CR3 는 CR2 와 달리 TIFF 가 아니라 동영상 파일과 같은 상자 구조라서 exifread 가
    읽지 못한다 — 제조사도 렌즈도 초점거리도 전부 빈 채로 돌아와, 렌즈가 '이름 없는
    렌즈' 로 뜨고 화각도 기본값으로 들어갔다. 실제 EXIF 는 moov 안 캐논 상자에
    TIFF 조각으로 들어 있다. CMT1 은 기본 정보(제조사·기종·방향), CMT2 는 촬영 정보
    (초점거리·렌즈·센서 해상도). 이 둘을 꺼내 exifread 에 그대로 넘긴다.

    CMT2 는 Exif 하위 목록이 맨 앞에 오는 TIFF 라 exifread 가 항목 이름을 'Image …'
    로 붙인다. 다른 형식과 같은 이름('EXIF …')으로 맞춰 준다.
    """
    import io
    import exifread
    blocks: dict[str, bytes] = {}
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        for typ, a, b in _bmff_boxes(fh, 0, size):
            if typ != b"moov":
                continue
            for t2, a2, b2 in _bmff_boxes(fh, a, b):
                fh.seek(a2)
                if t2 != b"uuid" or fh.read(16) != _CANON_UUID:
                    continue
                for t3, a3, b3 in _bmff_boxes(fh, a2 + 16, b2):
                    if t3 in (b"CMT1", b"CMT2"):
                        fh.seek(a3)
                        blocks[t3.decode()] = fh.read(b3 - a3)
            break
    tags: dict = {}
    if "CMT1" in blocks:
        tags.update(exifread.process_file(io.BytesIO(blocks["CMT1"]), details=False))
    if "CMT2" in blocks:
        exif = exifread.process_file(io.BytesIO(blocks["CMT2"]), details=False)
        for k, v in exif.items():
            tags["EXIF " + k[6:] if k.startswith("Image ") else k] = v
    return tags


def read_exif(path: Path | str) -> dict:
    """스티칭에 필요한 EXIF 항목만 추린다."""
    path = Path(path)
    key = str(path)
    if key in _exif_cache:
        return _exif_cache[key]

    out: dict = {}
    try:
        import exifread
        tags = _cr3_tags(path) if path.suffix.lower() == ".cr3" else {}
        if not tags:
            with open(path, "rb") as fh:
                tags = exifread.process_file(fh, details=False)

        def num(tag):
            v = tags.get(tag)
            if v is None:
                return None
            try:
                r = v.values[0]
                return float(r.num) / float(r.den) if hasattr(r, "den") else float(r)
            except Exception:
                return None

        def txt(tag):
            v = tags.get(tag)
            return str(v).strip() if v is not None else None

        out["focal_length"] = num("EXIF FocalLength")
        out["focal_35"] = num("EXIF FocalLengthIn35mmFilm")
        out["fnumber"] = num("EXIF FNumber")
        out["iso"] = num("EXIF ISOSpeedRatings")
        out["exposure_time"] = num("EXIF ExposureTime")
        out["make"] = txt("Image Make")
        out["model"] = txt("Image Model")
        out["lens"] = txt("EXIF LensModel") or txt("MakerNote LensType")
        out["datetime"] = txt("EXIF DateTimeOriginal") or txt("Image DateTime")
        o = tags.get("Image Orientation")
        if o is not None:
            out["orientation"] = int(o.values[0]) if o.values else 1
        # 센서 크기 역산용
        out["fp_x_res"] = num("EXIF FocalPlaneXResolution")
        out["fp_unit"] = num("EXIF FocalPlaneResolutionUnit")
        out["exif_w"] = num("EXIF ExifImageWidth")
    except Exception:
        pass

    out.setdefault("orientation", 1)
    _exif_cache[key] = out
    return out


def estimate_hfov(path: Path | str, width: int, height: int) -> tuple[float, str]:
    """EXIF 로 가로 화각(도)을 추정한다. (값, 근거) 를 돌려준다.

    35mm 환산 초점거리가 최우선이고, 없으면 센서 해상도 태그로 역산한다.
    둘 다 없으면 보수적으로 50도를 쓴다 — 번들 조정이 결국 맞춰준다.
    """
    ex = read_exif(path)
    portrait = height > width

    f35 = ex.get("focal_35")
    if f35 and f35 > 1:
        # 35mm 환산은 36x24 프레임 기준. 세로 사진이면 짧은 변이 가로가 된다.
        frame = 24.0 if portrait else 36.0
        return float(math.degrees(2 * math.atan(frame / (2 * f35)))), f"EXIF 35mm 환산 {f35:.0f}mm"

    f = ex.get("focal_length")
    fp_res, fp_unit, exif_w = ex.get("fp_x_res"), ex.get("fp_unit"), ex.get("exif_w")
    if f and fp_res and exif_w and fp_res > 0:
        unit_mm = {2: 25.4, 3: 10.0, 4: 1.0, 5: 0.001}.get(int(fp_unit or 2), 25.4)
        sensor_w = exif_w / fp_res * unit_mm
        if 1.0 < sensor_w < 100.0:
            if portrait:                 # 센서 긴 변 -> 짧은 변(=사진의 가로)
                sensor_w *= min(width, height) / max(width, height)
            return float(math.degrees(2 * math.atan(sensor_w / (2 * f)))), \
                f"EXIF 초점거리 {f:.0f}mm + 센서 {sensor_w:.1f}mm"

    if f and f > 1:
        # 크롭바디(1.5x)를 가정한 마지막 수단
        frame = (24.0 if portrait else 36.0) / 1.5
        return float(math.degrees(2 * math.atan(frame / (2 * f)))), f"초점거리 {f:.0f}mm, 크롭 1.5x 가정"

    return 50.0, "EXIF 없음 — 기본값"


# ---------------------------------------------------------------- 프록시 캐시

def cache_key(path: Path | str, tag: str) -> str:
    p = Path(path)
    try:
        st = p.stat()
        sig = f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}|{tag}"
    except OSError:
        sig = f"{p}|{tag}"
    return hashlib.sha1(sig.encode()).hexdigest()[:20]


def build_proxy(path: Path | str, cache_dir: Path, max_dim: int = PROXY_MAX) -> tuple[np.ndarray, Path]:
    """축소본을 만들고 캐시에 저장한다. 이미 있으면 그대로 읽는다."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    dst = cache_dir / f"{cache_key(path, f'proxy{max_dim}')}.jpg"
    if dst.exists():
        img = imread(dst)
        if img is not None:
            return img, dst

    # RAW 는 half_size 로 읽어 디코딩 시간을 아낀다
    src = read_image(path, half_size=is_raw(path))
    h, w = src.shape[:2]
    scale = min(1.0, max_dim / max(w, h))
    if scale < 1.0:
        interp = cv2.INTER_AREA
        src = cv2.resize(src, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=interp)
    imwrite(dst, src, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return src, dst


def build_thumb(path: Path | str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    dst = cache_dir / f"{cache_key(path, 'thumb')}.jpg"
    if not dst.exists():
        proxy, _ = build_proxy(path, cache_dir)
        h, w = proxy.shape[:2]
        s = min(1.0, THUMB_MAX / max(w, h))
        t = cv2.resize(proxy, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)
        imwrite(dst, t, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return dst


@dataclass
class SourceImage:
    """프로젝트에 등록된 원본 한 장."""

    id: int
    path: str
    width: int = 0
    height: int = 0
    proxy_width: int = 0
    proxy_height: int = 0
    exif: dict = field(default_factory=dict)
    fov_hint: float = 50.0
    fov_source: str = ""
    enabled: bool = True

    @property
    def proxy_scale(self) -> float:
        """프록시 좌표 -> 원본 좌표 배율."""
        return self.width / self.proxy_width if self.proxy_width else 1.0

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()}
        d["name"] = Path(self.path).name
        d["is_raw"] = is_raw(self.path)
        return d


def register(path: Path | str, image_id: int, cache_dir: Path) -> SourceImage:
    """원본을 프로젝트에 등록하며 프록시·EXIF·초기 화각을 준비한다."""
    path = Path(path)
    proxy, _ = build_proxy(path, cache_dir)
    build_thumb(path, cache_dir)
    w, h = read_size(path)
    fov, src = estimate_hfov(path, w, h)
    ex = read_exif(path)
    return SourceImage(
        id=image_id, path=str(path), width=w, height=h,
        proxy_width=proxy.shape[1], proxy_height=proxy.shape[0],
        exif={k: v for k, v in ex.items() if v is not None},
        fov_hint=fov, fov_source=src,
    )
