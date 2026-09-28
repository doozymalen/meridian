#!/bin/bash
# Meridian.app 빌드 — 파이썬 런타임까지 번들 안에 넣어 자립 실행되게 만든다.
set -euo pipefail
cd "$(dirname "$0")"

APP="dist/Meridian.app"
RES="$APP/Contents/Resources"

# 코드만 바뀐 경우엔 파이썬 런타임을 다시 깔 필요가 없다 (몇 초면 끝난다)
build_ui() {
  # Apple Silicon 맥은 macOS 11 이 최소 버전이라 arm64 배포 타겟의 하한이 11.0 이다.
  # 코드 자체는 10.15 에서도 컴파일되도록 맞춰 두었다(Sources/Compat.swift).
  #
  # Intel 맥까지 담은 universal 바이너리는 x86_64 용 Swift 호환성 라이브러리가
  # 있어야 하는데, Command Line Tools 에는 arm64 슬라이스만 들어 있다.
  # Xcode 가 설치된 환경에서만 MERIDIAN_UNIVERSAL=1 로 시도한다.
  local archs=(-target arm64-apple-macosx11.0)
  if [ "${MERIDIAN_UNIVERSAL:-}" = "1" ] && [ -d /Applications/Xcode.app ]; then
    echo "  네이티브 UI 빌드 (universal: arm64 + x86_64)"
    swiftc -O -swift-version 5 -target arm64-apple-macosx11.0 \
      -o /tmp/meridian_arm64 mac/Sources/*.swift
    swiftc -O -swift-version 5 -target x86_64-apple-macosx11.0 \
      -o /tmp/meridian_x86 mac/Sources/*.swift
    lipo -create /tmp/meridian_arm64 /tmp/meridian_x86 -output "$APP/Contents/MacOS/Meridian"
    rm -f /tmp/meridian_arm64 /tmp/meridian_x86
  else
    echo "  네이티브 UI 빌드 (AppKit, arm64)"
    swiftc -O -swift-version 5 "${archs[@]}" \
      -o "$APP/Contents/MacOS/Meridian" mac/Sources/*.swift
  fi
}

if [ "${1:-}" = "--code-only" ] && [ -d "$RES/venv" ]; then
  echo "코드만 갱신"
  rm -rf "$RES/meridian" "$RES/web"
  rsync -a --exclude '__pycache__' --exclude '*.pyc' meridian web "$RES/"
  [ -f icon/meridian.icns ] && cp icon/meridian.icns "$RES/meridian.icns"
  build_ui
  codesign --force --deep --sign - "$APP" 2>/dev/null || true
  echo "완료: $APP"
  exit 0
fi
UV="${UV:-$HOME/.local/bin/uv}"
[ -x "$UV" ] || UV="$(command -v uv)" || { echo "uv 가 필요합니다: https://docs.astral.sh/uv/"; exit 1; }

echo "1/6  기존 번들 정리"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$RES"

echo "2/6  파이썬 런타임과 의존성 설치 (몇 분 걸립니다)"
"$UV" venv --relocatable --python 3.11 "$RES/venv" >/dev/null
VIRTUAL_ENV="$RES/venv" "$UV" pip install --python "$RES/venv/bin/python" -q \
  numpy scipy "opencv-contrib-python-headless>=4.10" rawpy pillow \
  fastapi "uvicorn[standard]" python-multipart exifread

# uv 가 만든 venv 의 python 은 심볼릭 링크라 다른 맥에서 깨진다.
# 실제 바이너리와 표준 라이브러리를 번들 안으로 복사해 자립시킨다.
echo "3/6  파이썬 런타임 자립화"
PYREAL="$(readlink -f "$RES/venv/bin/python3.11")"
PYROOT="$(cd "$(dirname "$PYREAL")/.." && pwd)"
if [ ! -d "$RES/python" ]; then
  rsync -a --exclude '__pycache__' --exclude '*.pyc' "$PYROOT/" "$RES/python/"
fi
for f in python python3 python3.11; do
  rm -f "$RES/venv/bin/$f"
  ln -s "../../python/bin/python3.11" "$RES/venv/bin/$f"
done

echo "4/6  앱 코드 복사"
rsync -a --exclude '__pycache__' --exclude '*.pyc' meridian "$RES/"
rsync -a web "$RES/"
mkdir -p "$RES/cache" "$RES/projects"

echo "5/6  네이티브 UI 빌드와 Info.plist"
build_ui

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Meridian</string>
  <key>CFBundleDisplayName</key><string>Meridian</string>
  <key>CFBundleIdentifier</key><string>com.doozymalen.meridian</string>
  <key>CFBundleVersion</key><string>1.0.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Meridian</string>
  <key>CFBundleIconFile</key><string>meridian.icns</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSSupportsAutomaticTermination</key><false/>
  <key>NSSupportsSuddenTermination</key><false/>
  <key>NSRequiresAquaSystemAppearance</key><false/>
  <key>NSHumanReadableCopyright</key><string>파노라마 이미지 스티칭</string>
</dict>
</plist>
PLIST

echo "6/6  아이콘과 서명"
if [ -f icon/meridian.icns ]; then cp icon/meridian.icns "$RES/meridian.icns"; fi
# 서명이 없으면 Gatekeeper 가 막는다. 자체 서명(ad-hoc)으로 최소한은 맞춰 둔다.
codesign --force --deep --sign - "$APP" 2>/dev/null || echo "   (코드 서명 건너뜀)"

SIZE=$(du -sh "$APP" | cut -f1)
echo
echo "완료: $APP  ($SIZE)"
echo "실행: open $APP"
