"""남은 메모리에 맞춰 일을 나눈다.

엔진은 무거운 단계(특징점 검출, 미리보기 워핑, 최종 렌더)를 코어 수만큼 동시에
돌린다. 그런데 한 갈래가 수백 MB 를 쓰므로, 코어는 많고 메모리는 적은 PC
(예: 4GB 가상 머신)에서는 동시에 돌린 만큼 메모리가 모자라 엔진이 통째로
꺼진다. 오류를 남길 틈도 없이 꺼져서 사용자에게는 '연결 거부' 로만 보였다.

그래서 동시에 몇 갈래를 돌릴지 정할 때 코어 수와 함께 지금 남은 물리 메모리를 본다.
남은 양을 알 수 없으면 코어 수만 따른다 (예전과 같다).
"""

from __future__ import annotations

import os
import sys
import threading


def available_mb() -> int | None:
    """지금 쓸 수 있는 물리 메모리(MB). 알 수 없으면 None.

    MERIDIAN_AVAILABLE_MB 로 값을 정해 줄 수 있다 — 메모리가 작은 PC 를 흉내 내 시험할 때 쓴다.
    """
    env = os.environ.get("MERIDIAN_AVAILABLE_MB", "")
    if env.isdigit():
        return int(env)
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullAvailPhys // (1024 * 1024))
            return None
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) // 1024
            return None
        if sys.platform == "darwin":
            # 맥은 압축 메모리와 스왑이 넉넉해 '남은 양' 이 늘 작게 잡힌다.
            # 전체의 절반을 쓸 수 있다고 본다.
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            return int(total // (1024 * 1024) // 2)
    except Exception:
        return None
    return None


def workers(mb_each: float, cap: int | None = None, share: float = 0.6) -> int:
    """한 갈래가 mb_each MB 를 쓸 때 동시에 몇 갈래를 돌릴지.

    코어 수(와 cap)를 넘지 않고, 남은 메모리의 share 비율 안에 들게 한다.
    최소 1. MERIDIAN_WORKERS 가 있으면 그 값을 따른다.
    """
    env = os.environ.get("MERIDIAN_WORKERS", "")
    if env.isdigit() and int(env) > 0:
        return int(env)
    n = os.cpu_count() or 4
    if cap:
        n = min(n, cap)
    avail = available_mb()
    if avail is not None and mb_each > 0:
        n = min(n, int(avail * share // mb_each))
    return max(1, n)


def budget_mb(want: int, share: float = 0.4) -> int:
    """캐시처럼 '많을수록 빠른' 것에 줄 메모리. want 와 남은 양의 share 중 작은 쪽."""
    avail = available_mb()
    if avail is None:
        return want
    return max(256, min(want, int(avail * share)))


# 윈도우의 스레드 스택은 기본 1MB 다 (리눅스는 8MB). 구 전체 원판의 이음선 찾기처럼
# OpenCV 가 스택을 깊게 쓰는 무거운 작업은 넉넉한 스택이 있는 스레드에서 돌린다.
# 모든 스레드에 주면 안 된다 — 윈도우는 스택을 실제 메모리로 잡아 두므로, 서버가
# 띄우는 수십 개 스레드에 64MB 씩 줬더니 자원이 바닥나 엔진이 멈췄다.
HEAVY_STACK = 8 * 1024 * 1024
_stack_lock = threading.Lock()


def start_heavy_thread(target) -> threading.Thread:
    """무거운 작업용 스레드를 넉넉한 스택으로 띄운다. 다른 스레드의 스택은 그대로다."""
    with _stack_lock:
        try:
            old = threading.stack_size(HEAVY_STACK)
        except (ValueError, RuntimeError):
            old = None
        try:
            t = threading.Thread(target=target, daemon=True)
            t.start()
        finally:
            if old is not None:
                threading.stack_size(old)
    return t
