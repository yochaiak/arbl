SHELL := /bin/bash
.PHONY: configure setup infra migrate dev api worker web verifier test lint typecheck seed-demo integration-test build down

configure:
	@python3 scripts/configure.py

setup: configure
	uv sync --locked
	PLAYWRIGHT_SKIP_BROWSER_GC=1 uv run playwright install chromium
	pnpm --dir apps/web install --frozen-lockfile
	cd apps/verifier && npm ci

infra: configure
	docker compose up -d --wait

migrate:
	uv run alembic upgrade head

dev: infra migrate
	@bash scripts/dev.sh

api:
	uv run uvicorn abrl.api:app --host 127.0.0.1 --port 8400 --no-proxy-headers

worker:
	uv run dramatiq abrl.runtime --processes 1 --threads 1

web:
	pnpm --dir apps/web dev

verifier:
	node --env-file-if-exists=.env apps/verifier/server.js

test:
	uv run python -m pytest -m 'not integration'
	cd apps/verifier && npm test

lint:
	uv run ruff check abrl tests migrations scripts
	pnpm --dir apps/web lint

typecheck:
	pnpm --dir apps/web exec tsc --noEmit

seed-demo:
	uv run python -m abrl.starter

integration-test:
	uv run python -m pytest -m integration

build:
	pnpm --dir apps/web build

down:
	docker compose down
