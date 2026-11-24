SHELL := /bin/bash
COMPOSE = docker compose
ifeq ($(origin COMPOSE_FILE),undefined)
ifneq ($(wildcard .abrl/compose-network.yml),)
COMPOSE += -f compose.yml -f .abrl/compose-network.yml
endif
endif
.PHONY: configure setup infra migrate dev dev-fixtures api worker scheduler sessions sessions-vendor web verifier test lint typecheck integration-test migration-test profile-test build down

configure:
	@python3 scripts/configure.py

setup: configure
	uv sync --locked
	PLAYWRIGHT_SKIP_BROWSER_GC=1 uv run playwright install chromium
	pnpm --dir apps/web install --frozen-lockfile
	cd apps/verifier && npm ci

infra: configure
	$(COMPOSE) up -d --wait

migrate:
	uv run alembic upgrade head

dev: infra migrate
	@bash scripts/dev.sh

dev-fixtures: infra migrate
	@bash scripts/dev.sh --test-fixtures

api:
	uv run uvicorn abrl.api:app --host 127.0.0.1 --port 8400 --no-proxy-headers --no-access-log

worker:
	uv run dramatiq abrl.runtime --processes 1 --threads 1

scheduler:
	uv run python -m abrl.scheduler_service

sessions-vendor:
	uv run python -m abrl.session_runner --pool vendor

sessions:
	uv run python -m abrl.session_runner

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

integration-test:
	uv run python -m pytest -m integration

migration-test:
	PYTHONPATH=. uv run python scripts/check-migrations.py

profile-test:
	PYTHONPATH=. uv run python scripts/check-profile-roundtrip.py

build:
	pnpm --dir apps/web build

down:
	$(COMPOSE) down
