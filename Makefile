.PHONY: dev test lint format migrate bot worker check-ebay
dev:
	docker compose up -d --wait postgres redis
	uv sync
	uv run alembic upgrade head
	uv run uvicorn pricehunter.api.app:create_app --factory --reload
test:
	uv run pytest
lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
format:
	uv run ruff check --fix .
	uv run ruff format .
migrate:
	uv run alembic upgrade head
bot:
	uv run python -m pricehunter.apps.bot
worker:
	uv run arq pricehunter.jobs.worker.WorkerSettings
check-ebay:
	uv run python -m pricehunter.apps.check_ebay --country BE
