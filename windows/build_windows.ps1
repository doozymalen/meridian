# Meridian 윈도우 빌드 — 반드시 '윈도우에서' 실행해야 한다.
#
# 파이썬 실행 파일은 교차 빌드가 되지 않는다. macOS 에서 .exe 를 만들 수 없고,
# 그 반대도 마찬가지다. 이 스크립트는 윈도우 PC 에 소스를 옮긴 뒤 돌리는 것이다.
#
#   PowerShell 에서:  .\windows\build_windows.ps1
#   결과:             dist\Meridian\Meridian.exe

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "1/4  파이썬 확인"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "파이썬 3.11 이상이 필요합니다: https://www.python.org/downloads/" }
$ver = & python -c "import sys;print('%d.%d' % sys.version_info[:2])"
Write-Host "     python $ver"

Write-Host "2/4  가상환경과 의존성 (몇 분 걸립니다)"
if (-not (Test-Path ".venv-win")) { & python -m venv .venv-win }
$pip = ".\.venv-win\Scripts\pip.exe"
& $pip install --upgrade pip --quiet
& $pip install --quiet `
    numpy scipy "opencv-contrib-python-headless>=4.10" rawpy pillow `
    fastapi "uvicorn[standard]" python-multipart exifread pyinstaller

Write-Host "3/4  실행 파일 빌드"
$sep = ";"
& .\.venv-win\Scripts\pyinstaller.exe --noconfirm --clean `
    --name Meridian `
    --windowed `
    --icon "icon\meridian.ico" `
    --add-data "meridian$($sep)meridian" `
    --add-data "web$($sep)web" `
    --hidden-import "uvicorn.logging" `
    --hidden-import "uvicorn.loops.auto" `
    --hidden-import "uvicorn.protocols.http.auto" `
    --hidden-import "uvicorn.protocols.websockets.auto" `
    --hidden-import "uvicorn.lifespan.on" `
    --collect-submodules "meridian" `
    "windows\launcher.py"

Write-Host "4/4  마무리"
$exe = "dist\Meridian\Meridian.exe"
if (Test-Path $exe) {
    $size = [math]::Round((Get-ChildItem -Recurse "dist\Meridian" | Measure-Object Length -Sum).Sum / 1MB)
    Write-Host ""
    Write-Host "완료: $exe  (약 ${size}MB)"
    Write-Host "폴더째 옮기면 파이썬이 없는 PC 에서도 그대로 실행됩니다."
} else {
    throw "빌드에 실패했습니다. 위 로그를 확인해 주세요."
}
