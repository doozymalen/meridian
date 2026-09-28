#!/usr/bin/env python3
"""Meridian 앱 아이콘 생성기.

형태는 둘뿐이다 — 구(球) 하나와 그것을 세로로 가르는 자오선 하나.
이름이 가리키는 것을 그대로 그린 셈이고, 16px 로 줄여도 '동그라미와 세로선'
으로 남아 살아남는다. 요소를 더 넣지 않은 이유가 그것이다.

자오선은 뒤로 돌아가는 쪽(오른쪽 호)을 푸른 회색으로 그려 구가 입체로 읽히게
했다. 호박색을 그냥 흐리게 하면 남색 바탕과 섞여 탁한 갈색이 된다 — 직접
그려 보고 알았다. 작은 크기에서는 이 뒤쪽 호가 먼저 사라져, 결국 '동그라미와
세로선' 만 남는다. 그게 16px 에서 실제로 보이는 모습이다.

좌표는 전부 1024 기준으로 적고, 실제로는 4배로 그린 뒤 줄여 계단을 없앤다.
"""

from PIL import Image, ImageDraw, ImageFilter
from pathlib import Path

S = 1024          # 최종 한 변
SS = 4            # 그릴 때 배율
W = S * SS

HERE = Path(__file__).resolve().parent

# 황혼 하늘에서 가져온 색. 짙은 남색 바탕에 지평선 빛 한 줄.
GRAD_TOP = (36, 68, 132)
GRAD_BOTTOM = (11, 22, 46)
SPHERE = (246, 244, 239)
MERIDIAN = (242, 170, 58)
BEHIND = (150, 172, 205)   # 뒤로 도는 호

PLATE = 824       # 애플 격자에서 아이콘 판의 한 변 (1024 중)
RADIUS_N = 5.0    # 초타원 지수 — 애플의 '이어지는 모서리'에 가깝다
R_SPHERE = 288    # 구 반지름
R_MINOR = 78      # 자오선 타원의 가로 반지름
STROKE = 30


def superellipse(cx, cy, half, n, steps=1440):
    """|x/a|^n + |y/a|^n = 1. 모서리가 원호로 뚝 끊기지 않고 이어진다."""
    import math
    pts = []
    for i in range(steps):
        t = 2 * math.pi * i / steps
        ct, st = math.cos(t), math.sin(t)
        x = half * (abs(ct) ** (2 / n)) * (1 if ct >= 0 else -1)
        y = half * (abs(st) ** (2 / n)) * (1 if st >= 0 else -1)
        pts.append((cx + x, cy + y))
    return pts


def vertical_gradient(size, top, bottom):
    g = Image.new("RGB", (1, size), 0)
    px = g.load()
    for y in range(size):
        t = y / max(1, size - 1)
        px[0, y] = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return g.resize((size, size), Image.NEAREST)


def build() -> Image.Image:
    u = SS                                   # 1024 좌표 -> 그리는 좌표
    cx = cy = W / 2

    # 1) 바탕 판 — 초타원 마스크에 세로 그러데이션
    mask = Image.new("L", (W, W), 0)
    ImageDraw.Draw(mask).polygon(
        superellipse(cx, cy, PLATE / 2 * u, RADIUS_N), fill=255)
    plate = vertical_gradient(W, GRAD_TOP, GRAD_BOTTOM).convert("RGBA")
    plate.putalpha(mask)

    # 2) 판 위쪽에 아주 옅은 빛 — 평평해 보이지 않을 만큼만
    gloss = Image.new("L", (W, W), 0)
    ImageDraw.Draw(gloss).ellipse(
        [cx - PLATE * 0.62 * u, cy - PLATE * 1.02 * u,
         cx + PLATE * 0.62 * u, cy - PLATE * 0.12 * u], fill=46)
    gloss = gloss.filter(ImageFilter.GaussianBlur(70 * u / 4))
    plate.alpha_composite(Image.merge(
        "RGBA", (Image.new("L", (W, W), 255),) * 3 + (Image.composite(
            gloss, Image.new("L", (W, W), 0), mask),)))

    art = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(art)
    sw = round(STROKE * u)

    # 3) 구
    r = R_SPHERE * u
    d.ellipse([cx - r, cy - r, cx + r, cy + r],
              outline=SPHERE + (255,), width=sw)

    # 4) 자오선. 뒤로 도는 오른쪽 호를 먼저 흐리게, 앞쪽 왼쪽 호를 그 위에.
    rm = R_MINOR * u
    box = [cx - rm, cy - r, cx + rm, cy + r]
    d.arc(box, -90, 90, fill=BEHIND + (120,), width=sw)
    d.arc(box, 90, 270, fill=MERIDIAN + (255,), width=sw)

    out = Image.alpha_composite(plate, art)
    return out.resize((S, S), Image.LANCZOS)


def write_icns(img: Image.Image, name: str) -> Path:
    iconset = HERE / f"{name}.iconset"
    for old in iconset.glob("*"):
        old.unlink()
    iconset.mkdir(exist_ok=True)
    for px in (16, 32, 64, 128, 256, 512, 1024):
        base = px if px <= 512 else 512
        tag = f"icon_{base}x{base}" + ("@2x" if px > base or px == 1024 else "")
        if px in (32, 64):                    # 32 는 16@2x 로도 쓰인다
            img.resize((px, px), Image.LANCZOS).save(
                iconset / f"icon_{px // 2}x{px // 2}@2x.png")
        img.resize((px, px), Image.LANCZOS).save(iconset / f"icon_{px}x{px}.png")
    # iconutil 이 요구하는 정확한 이름들만 남긴다
    keep = {}
    for base in (16, 32, 128, 256, 512):
        keep[f"icon_{base}x{base}.png"] = base
        keep[f"icon_{base}x{base}@2x.png"] = base * 2
    for f in list(iconset.glob("*.png")):
        if f.name not in keep:
            f.unlink()
    for fname, px in keep.items():
        img.resize((px, px), Image.LANCZOS).save(iconset / fname)

    icns = HERE / f"{name}.icns"
    import subprocess
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)],
                   check=True)
    return icns


def write_ico(img: Image.Image, name: str) -> Path:
    ico = HERE / f"{name}.ico"
    sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
    img.save(ico, format="ICO", sizes=sizes)
    return ico


if __name__ == "__main__":
    icon = build()
    icon.save(HERE / "meridian_1024.png")
    print("icns:", write_icns(icon, "meridian"))
    print("ico :", write_ico(icon, "meridian"))
    # 눈으로 볼 크기 비교판
    sheet = Image.new("RGBA", (760, 300), (250, 250, 252, 255))
    x = 20
    for px in (256, 128, 64, 32, 16):
        sheet.alpha_composite(icon.resize((px, px), Image.LANCZOS), (x, 20))
        x += px + 24
    sheet.save(HERE / "meridian_sizes.png")
