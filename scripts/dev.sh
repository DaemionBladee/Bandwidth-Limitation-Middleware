#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/bandwidth-uv-cache}"
if [[ ! -f .env ]]; then
  printf '%s\n' 'Create .env from .env.example and run the bootstrap steps in README.md first.' >&2
  exit 1
fi
uv sync --frozen
docker compose up -d --wait db
uv run --frozen alembic upgrade head
.venv/bin/uvicorn bandwidth.api:app --host 127.0.0.1 --port "${PORT:-8000}" &
api_pid=$!
.venv/bin/python -m bandwidth.worker &
worker_pid=$!
cleanup() {
  kill "$api_pid" "$worker_pid" 2>/dev/null || true
  wait "$api_pid" "$worker_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' INT TERM
wait -n "$api_pid" "$worker_pid"
