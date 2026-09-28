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


def main() -> None:
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
    uvicorn.run("meridian.server:app", host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
