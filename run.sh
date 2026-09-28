#!/bin/bash
# Meridian 실행 — 로컬 서버를 띄우고 브라우저를 연다.
cd "$(dirname "$0")" || exit 1
PORT="${MERIDIAN_PORT:-8756}"
if [ ! -d .venv ]; then
  echo "의존성을 설치합니다…"
  uv sync --python 3.11 || { echo "uv 가 필요합니다: https://docs.astral.sh/uv/"; exit 1; }
fi
( sleep 1.2; open "http://127.0.0.1:$PORT/" ) &
exec .venv/bin/python -m uvicorn meridian.server:app --host 127.0.0.1 --port "$PORT" "$@"
