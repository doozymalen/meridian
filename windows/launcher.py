"""윈도우용 실행 진입점.

맥 쪽 화면은 AppKit 으로 만들어서 윈도우에서는 쓸 수 없다. 대신 같은 엔진 위에
웹 UI(web/)를 얹고, 브라우저를 '앱 창' 모드로 띄워 주소창 없이 보이게 한다.
서버는 로컬 주소에서만 듣고, 이 창이 닫히면 함께 끝난다.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path


def resource_root() -> Path:
    """PyInstaller 로 묶인 경우와 소스에서 바로 실행한 경우 둘 다 처리한다."""
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) if bundled else Path(__file__).resolve().parent.parent


def free_port() -> int:
    """커널에게 빈 포트를 하나 받아 그대로 쓴다."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def app_window_command(url: str) -> list[str] | None:
    """주소창 없는 앱 창으로 열 수 있는 브라우저를 찾는다."""
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Google/Chrome/Application/chrome.exe",
    ]
    for exe in candidates:
        if exe.exists():
            return [str(exe), f"--app={url}", "--window-size=1280,860"]
    return None


def open_ui(url: str) -> subprocess.Popen | None:
    """서버가 응답하기 시작하면 화면을 연다."""
    import urllib.error
    import urllib.request

    for _ in range(200):
        try:
            with urllib.request.urlopen(url + "api/project", timeout=1):
                break
        except Exception:
            time.sleep(0.15)

    cmd = app_window_command(url)
    if cmd:
        try:
            return subprocess.Popen(cmd)
        except Exception:
            pass
    webbrowser.open(url)          # 앱 창이 안 되면 평범한 탭으로라도 연다
    return None


def ensure_std_streams() -> None:
    """콘솔 없이 뜬 경우 표준 출력을 로그 파일로 돌린다.

    --windowed 로 묶은 exe 를 탐색기에서(또는 콘솔 없는 화면이) 띄우면 sys.stdout /
    sys.stderr 가 None 이다. uvicorn 은 시작하자마자 sys.stdout.isatty() 를 불러서
    거기서 죽는다. 버리지 않고 파일로 남겨 두면 문제가 생겼을 때 볼 수 있다.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    log_dir = Path(base) / "Meridian"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        stream = open(log_dir / "engine.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def main() -> None:
    # 서버는 문자열("meridian.server:app")이 아니라 객체로 넘긴다. PyInstaller 는
    # import 문을 따라가며 담을 라이브러리를 고르는데, 문자열로 넘기면 서버 모듈을
    # 보지 못해 fastapi 같은 것을 통째로 빠뜨린다 (v1.0.0 윈도우 엔진이 그랬다).
    ensure_std_streams()
    # OpenCV 같은 C 코드 안에서 죽으면 파이썬 오류 메시지가 남지 않는다. 그래도 어디서
    # 죽었는지 engine.log 에 남기게 한다.
    try:
        import faulthandler
        faulthandler.enable(file=sys.stderr, all_threads=True)
    except Exception:
        pass
    # 네이티브 화면(WinUI)이 띄울 때는 브라우저를 열지 않는다. 화면이 포트를
    # 정해 주고, 자기 프로세스 번호를 넘겨 자신이 죽으면 엔진도 같이 끝나게 한다.
    if os.environ.get("MERIDIAN_ENGINE_ONLY") == "1":
        root = resource_root()
        os.chdir(root)
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import uvicorn
        from meridian.server import app
        port = int(os.environ.get("MERIDIAN_PORT") or free_port())
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
        return

    root = resource_root()
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    import uvicorn

    port = free_port()
    url = f"http://127.0.0.1:{port}/"

    holder: dict[str, subprocess.Popen | None] = {}
    def launch():
        holder["browser"] = open_ui(url)
        # 앱 창을 닫으면 서버도 함께 끝낸다
        proc = holder.get("browser")
        if proc is not None:
            proc.wait()
            os._exit(0)

    threading.Thread(target=launch, daemon=True).start()
    from meridian.server import app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
