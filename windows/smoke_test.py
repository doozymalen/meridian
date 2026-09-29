"""엔진을 실제로 한 바퀴 돌려 본다 — 사진 추가 → 자동 정렬 → 미리보기 → 내보내기.

빌드가 '성공' 하고 창이 떠도, 사용자가 누르는 순간 엔진이 죽는 일이 있었다.
윈도우 러너에서 번들된 엔진(engine\\Meridian.exe)을 띄워 사람이 하는 순서대로
요청을 보내고, 어디서든 엔진이 죽거나 오류를 내면 실패로 끝낸다.

    python windows/smoke_test.py publish\\engine\\Meridian.exe

시험 사진은 여기서 만든다: 도형을 흩뿌린 360도 장면에서 35도씩 돌려 본 네 장.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import cv2
import numpy as np


def make_photos(folder: Path) -> list[str]:
    rng = np.random.default_rng(1)
    H, W = 2000, 4000
    eq = (rng.random((H // 40, W // 40, 3)) * 255).astype(np.uint8).repeat(40, 0).repeat(40, 1)
    for _ in range(3000):
        c = tuple(int(x) for x in rng.integers(0, 255, 3))
        cv2.circle(eq, (int(rng.integers(0, W)), int(rng.integers(0, H))),
                   int(rng.integers(4, 30)), c, -1)
    w, h, fov = 1500, 1000, np.radians(60)
    f = (w / 2) / np.tan(fov / 2)
    xs, ys = np.meshgrid(np.arange(w) - w / 2, np.arange(h) - h / 2)
    paths = []
    for k, yaw in enumerate([0, 35, 70, 105]):
        d = np.stack([xs, ys, np.full_like(xs, f)], -1)
        d /= np.linalg.norm(d, axis=-1, keepdims=True)
        a = np.radians(yaw)
        R = np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])
        d = d @ R.T
        lon, lat = np.arctan2(d[..., 0], d[..., 2]), np.arcsin(d[..., 1])
        mx = ((lon / np.pi + 1) / 2 * W).astype(np.float32) % W
        my = ((lat / (np.pi / 2) + 1) / 2 * H).astype(np.float32)
        p = folder / f"사진{k}.jpg"            # 한글 경로도 함께 시험한다
        ok, buf = cv2.imencode(".jpg", cv2.remap(eq, mx, my, cv2.INTER_LINEAR))
        buf.tofile(str(p))
        paths.append(str(p))
    return paths


class Engine:
    def __init__(self, exe: str, data: Path):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        env = dict(os.environ, MERIDIAN_ENGINE_ONLY="1", MERIDIAN_PORT=str(self.port),
                   MERIDIAN_DATA=str(data))
        # 사용자 PC 처럼 콘솔 없이 띄운다 (표준 출력이 없는 상황까지 재현)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen([exe], env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=flags)
        self.base = f"http://127.0.0.1:{self.port}"
        for _ in range(200):
            self.alive("시작")
            try:
                self.get("/api/project")
                return
            except OSError:
                time.sleep(0.2)
        raise SystemExit("엔진이 40초 안에 답하지 않습니다")

    def alive(self, where: str) -> None:
        if self.proc.poll() is not None:
            raise SystemExit(f"엔진이 '{where}' 중에 죽었습니다 (종료 코드 {self.proc.returncode})")

    def _send(self, path: str, body=None, raw=False):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            out = r.read()
        return out if raw else json.loads(out)

    def get(self, path):
        return self._send(path)

    def post(self, path, body=None, raw=False):
        return self._send(path, body or {}, raw)

    def run(self, label: str, path: str, body) -> dict:
        """작업을 띄우고 끝날 때까지 진행 메시지를 찍는다."""
        print(f"== {label}", flush=True)
        jid = self.post(path, body)["job"]
        t0, last = time.time(), None
        while True:
            self.alive(label)
            s = self.get(f"/api/job/{jid}")
            msg = s.get("message")
            if msg != last:
                print(f"   {time.time() - t0:6.1f}s  {s['frac']:.2f}  {msg}", flush=True)
                last = msg
            if s["done"]:
                if s.get("error"):
                    raise SystemExit(f"'{label}' 오류: {s['error']}")
                return s.get("result") or {}
            if time.time() - t0 > 600:
                raise SystemExit(f"'{label}' 이 10분 넘게 끝나지 않습니다")
            time.sleep(0.25)


def main() -> None:
    # 윈도우 러너의 콘솔은 cp1252 라 한글을 찍다 죽는다
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    exe = sys.argv[1]
    work = Path(tempfile.mkdtemp(prefix="meridian_시험_"))
    photos = make_photos(work)
    eng = Engine(exe, work / "data")
    try:
        r = eng.run("사진 추가", "/api/images/add", {"paths": photos})
        if len(r.get("added", [])) != len(photos):
            raise SystemExit(f"사진이 다 들어가지 않았습니다: {r}")

        r = eng.run("자동 정렬", "/api/align", {"detect": True, "mode": "full", "straighten": True})
        print(f"   RMS {r['rms']:.2f}px, 제어점 {r['control_points']}, 조각 {r['groups']}")
        if r["groups"] != [len(photos)]:
            raise SystemExit("정렬이 한 덩어리로 이어지지 않았습니다")

        eng.run("미리보기", "/api/preview", {"max_dim": 1700})
        eng.post("/api/live/prepare")
        for _ in range(120):                   # 끌기용 원판이 준비될 때까지
            eng.alive("끌기 준비")
            try:
                n = len(eng.post("/api/live/frame", {"yaw": 10, "pitch": 0, "roll": 0,
                                                     "hfov": 0, "vfov": 0, "max_dim": 900}, raw=True))
                print(f"== 끌기 미리보기 {n} 바이트")
                break
            except urllib.error.HTTPError as e:
                if e.code != 409:              # 409 = 아직 준비 중
                    raise
                time.sleep(0.5)
        else:
            raise SystemExit("끌기 미리보기가 1분 안에 준비되지 않았습니다")
        eng.run("방향 바꾼 미리보기", "/api/preview",
                {"max_dim": 1700, "center_yaw": 20, "center_pitch": 0, "center_roll": 0})

        req = urllib.request.Request(eng.base + "/api/settings", method="PATCH",
                                     data=json.dumps({"out_width": 2000}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=60).read()
        out = work / "결과.jpg"
        eng.run("내보내기", "/api/render", {"path": str(out)})
        if not out.exists() or out.stat().st_size < 10_000:
            raise SystemExit("내보낸 파일이 없거나 너무 작습니다")
        left = list((work / "data" / "cache").glob("canvas_*"))
        if left:
            raise SystemExit(f"내보내기 임시 파일이 남았습니다: {left}")
        eng.alive("마무리")
        print("== 모두 통과")
    finally:
        eng.proc.kill()


if __name__ == "__main__":
    main()
