"""GPano(XMP) 메타데이터 — FSPviewer 같은 파노라마 뷰어와 맞추기 위한 것.

FSPviewer 는 정방형도법(equirectangular) JPEG 을 읽는데, 360x180 을 다 채우지
못한 사진이면 '전체 구(球) 중 어디를 얼마나 덮고 있는지'를 알아야 제대로
띄운다. 그 정보를 담는 표준이 구글이 만든 GPano 스키마이고, PTGui 도 같은
태그를 쓴다. 우리 PanoLayout 은 이미 full_w/full_h(전체 구)와 x0/y0/w/h(잘라
낸 영역)를 들고 있어서 그대로 옮기면 된다.

주의할 점이 하나 있다. 우리 full_w 는 hfov 만큼만 덮는 폭이라 부분 파노라마
에서는 360도 폭이 아니다. GPano 의 FullPanoWidthPixels 는 반드시 360도짜리
폭이어야 하므로, 세로(항상 180도)를 기준으로 다시 계산해 넘긴다.
"""

from __future__ import annotations

import struct
import zlib

XMP_NS = b"http://ns.adobe.com/xap/1.0/\x00"

_TEMPLATE = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Meridian">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:GPano="http://ns.google.com/photos/1.0/panorama/"
{body}/>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""


def gpano_fields(layout, width: int, height: int) -> dict[str, str] | None:
    """PanoLayout 에서 GPano 태그를 만든다. 정방형도법이 아니면 None."""
    if layout is None or layout.projection != "equirect":
        return None
    if layout.hfov <= 0 or layout.full_w < 2 or width < 1 or height < 1:
        return None

    # 화소 밀도(화소/도)로 360x180 전체 틀의 크기를 되짚는다. 우리 캔버스는
    # 화각을 고정하면 가로가 hfov, 세로가 vfov 만 덮으므로 full_h 를 180도로
    # 볼 수 없다. 캔버스 중심이 파노라마의 경도 0·위도 0 이다.
    ppd = layout.full_w / float(layout.hfov)
    pano_w = int(round(360.0 * ppd))
    pano_h = int(round(180.0 * ppd))
    left = int(round(pano_w / 2.0 - layout.full_w / 2.0)) + int(layout.x0)
    top = int(round(pano_h / 2.0 - layout.full_h / 2.0)) + int(layout.y0)

    # 출력 파일이 layout 과 다른 크기로 저장됐다면(리샘플) 비율을 맞춘다
    if width != int(layout.w) or height != int(layout.h):
        sx = width / float(max(1, layout.w))
        sy = height / float(max(1, layout.h))
        pano_w = int(round(pano_w * sx))
        pano_h = int(round(pano_h * sy))
        left = int(round(left * sx))
        top = int(round(top * sy))

    left = max(0, min(left, max(0, pano_w - width)))
    top = max(0, min(top, max(0, pano_h - height)))

    return {
        "UsePanoramaViewer": "True",
        "ProjectionType": "equirectangular",
        "CroppedAreaImageWidthPixels": str(width),
        "CroppedAreaImageHeightPixels": str(height),
        "FullPanoWidthPixels": str(pano_w),
        "FullPanoHeightPixels": str(pano_h),
        "CroppedAreaLeftPixels": str(left),
        "CroppedAreaTopPixels": str(top),
        "InitialViewHeadingDegrees": "0",
        "InitialViewPitchDegrees": "0",
        "InitialViewRollDegrees": "0",
        "InitialHorizontalFOVDegrees": "70",
    }


def _esc(v: str) -> str:
    return (v.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def build_packet(fields: dict[str, str]) -> bytes:
    # 속성 형태로 적는다 — GPano 명세가 보여 주는 형태이고 PTGui 도 이렇게 쓴다.
    # 태그를 먼저 닫아 버리면 항목이 전부 본문 텍스트가 되어 아무도 못 읽는다.
    body = "\n".join(f'    GPano:{k}="{_esc(v)}"' for k, v in fields.items())
    return _TEMPLATE.format(body=body).encode("utf-8")


# ---------------------------------------------------------------- JPEG

def embed_jpeg(path, packet: bytes) -> bool:
    """JPEG 에 XMP 를 APP1 세그먼트로 끼워 넣는다.

    APP0/APP1(EXIF) 뒤, 나머지 세그먼트 앞에 넣는다. 이미 XMP 가 있으면
    갈아 끼운다 — 두 개가 있으면 읽는 쪽이 어느 것을 볼지 알 수 없다.
    """
    data = bytearray(open(path, "rb").read())
    if data[:2] != b"\xff\xd8":
        return False

    payload = XMP_NS + packet
    if len(payload) + 2 > 0xFFFF:
        return False                      # 65KB 넘는 XMP 는 분할 규약이 따로 있다
    segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload

    i = 2
    insert_at = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            break
        seg_len = struct.unpack(">H", bytes(data[i + 2:i + 4]))[0]
        if seg_len < 2:
            break
        end = i + 2 + seg_len
        if marker == 0xE1 and bytes(data[i + 4:i + 4 + len(XMP_NS)]) == XMP_NS:
            del data[i:end]               # 옛 XMP 를 걷어내고 그 자리에 넣는다
            insert_at = i
            break
        if 0xE0 <= marker <= 0xEF:
            insert_at = end
            i = end
            continue
        break                             # SOF/DQT 등이 나오면 여기가 끝

    data[insert_at:insert_at] = segment
    with open(path, "wb") as f:
        f.write(bytes(data))
    return True


# ---------------------------------------------------------------- PNG

def embed_png(path, packet: bytes) -> bool:
    """PNG 는 iTXt 청크에 넣는다 (키워드는 XMP 규약이 정해 둔 것)."""
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    body = (b"XML:com.adobe.xmp\x00"      # 키워드
            b"\x00\x00"                   # 압축 안 함, 압축 방식 0
            b"\x00\x00"                   # 언어 태그 없음, 번역된 키워드 없음
            + packet)
    chunk = (struct.pack(">I", len(body)) + b"iTXt" + body
             + struct.pack(">I", zlib.crc32(b"iTXt" + body) & 0xFFFFFFFF))
    # IHDR(8 + 25바이트) 바로 뒤에 넣는다
    at = 8 + 25
    with open(path, "wb") as f:
        f.write(data[:at] + chunk + data[at:])
    return True


def embed(path, layout, width: int, height: int) -> bool:
    """확장자를 보고 알아서 넣는다. 못 넣으면 조용히 False."""
    fields = gpano_fields(layout, width, height)
    if not fields:
        return False
    packet = build_packet(fields)
    ext = str(path).lower().rsplit(".", 1)[-1]
    try:
        if ext in ("jpg", "jpeg"):
            return embed_jpeg(path, packet)
        if ext == "png":
            return embed_png(path, packet)
    except Exception:
        return False
    return False
