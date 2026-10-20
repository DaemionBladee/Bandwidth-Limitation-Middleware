export UV_CACHE_DIR ?= /tmp/bandwidth-uv-cache
.PHONY: install db migrate bootstrap demo api worker dev test check
install:
	uv sync --frozen
db:
	docker compose up -d --wait db
migrate:
	uv run --frozen alembic upgrade head
bootstrap:
	uv run --frozen python -m bandwidth.cli bootstrap
demo:
	uv run --frozen python -m bandwidth.cli demo
api:
	uv run --frozen uvicorn bandwidth.api:app --host 127.0.0.1 --port 8000
worker:
	uv run --frozen python -m bandwidth.worker
dev:
	bash scripts/dev.sh
test:
	uv run --frozen pytest -q
check:
	uv run --frozen ruff check bandwidth tests migrations
	uv run --frozen ruff format --check bandwidth tests migrations
	uv run --frozen alembic check
	node --check bandwidth/static/app.js
