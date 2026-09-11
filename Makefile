.DEFAULT_GOAL := help
COMPOSE := docker compose

.PHONY: help up down logs migrate revision test test-unit lint fmt typecheck check reset health seed-sources sources gate

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up: ## Build and start the whole stack, waiting for health
	$(COMPOSE) up -d --build --wait
	@echo "api:    http://localhost:8000"
	@echo "docs:   http://localhost:8000/docs"
	@echo "health: http://localhost:8000/health"

down: ## Stop the stack (keeps the database volume)
	$(COMPOSE) down

reset: ## Stop the stack and DESTROY the database volume
	$(COMPOSE) down -v

logs: ## Follow logs
	$(COMPOSE) logs -f

health: ## Check the API health endpoint
	@curl -fsS http://localhost:8000/health

migrate: ## Apply migrations inside the stack
	$(COMPOSE) run --rm migrate alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add x"
	$(COMPOSE) run --rm migrate alembic revision --autogenerate -m "$(m)"

seed-sources: ## Register the candidate sources (all disabled and unreviewed)
	$(COMPOSE) run --rm migrate python scripts/seed_sources.py

sources: ## Show each source and whether it has cleared the review gates
	$(COMPOSE) run --rm migrate python scripts/review_source_terms.py --list

gate: ## Run the Phase 1, 2 and 3 gates on their own
	$(COMPOSE) run --rm migrate pytest -v \
		tests/test_verify.py \
		tests/test_pipeline_integration.py \
		tests/test_ingestion_integration.py \
		tests/test_briefing.py \
		tests/test_review.py \
		tests/test_ui.py

test: ## Run all tests, including live-database integration tests
	$(COMPOSE) run --rm -e GRI_DATABASE_URL=postgresql+psycopg://gri:gri@db:5432/gri \
		migrate pytest --cov=gri --cov-report=term-missing

test-unit: ## Run only the tests that need no database
	pytest -m "not integration"

lint: ## Lint
	ruff check .
	ruff format --check .

fmt: ## Format and autofix
	ruff format .
	ruff check --fix .

typecheck: ## Type-check
	mypy

check: lint typecheck test-unit ## Everything that runs without Docker
