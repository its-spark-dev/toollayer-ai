SHELL := /bin/bash
PYTHON ?= python3
VENV := .venv
BIN := $(VENV)/bin

CONTROL_PLANE_PORT ?= 8080
DEMO_API_PORT ?= 8081
RUNTIME_PORT ?= 8082

export PYTHONPATH := packages/contracts:packages/openapi-converter:packages/policy-engine:packages/mock-llm:apps/control-plane/backend:apps/runtime:apps/demo-api

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show the available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: setup
setup: ## Create the virtualenv and install every dependency
	$(PYTHON) -m venv $(VENV) 2>/dev/null || true
	$(BIN)/python -m pip install --quiet --upgrade pip
	$(BIN)/python -m pip install --quiet -e ".[dev]"
	@echo "Environment ready. Run 'make demo' or 'make test'."

.PHONY: test
test: ## Run the whole test suite
	$(BIN)/python -m pytest tests

.PHONY: test-security
test-security: ## Run only the tests that prove a control refuses something
	$(BIN)/python -m pytest tests/security tests/contract -v

.PHONY: lint
lint: ## Check formatting and lint rules
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

.PHONY: format
format: ## Apply formatting and safe lint fixes
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

.PHONY: typecheck
typecheck: ## Run strict type checking
	$(BIN)/mypy packages apps

.PHONY: check
check: lint typecheck test ## Everything CI runs

.PHONY: run-demo-api
run-demo-api: ## Start the synthetic Support API
	$(BIN)/uvicorn demo_api.main:app --host 0.0.0.0 --port $(DEMO_API_PORT)

.PHONY: run-control-plane
run-control-plane: ## Start the Tool Control Plane
	$(BIN)/uvicorn control_plane.main:app --host 0.0.0.0 --port $(CONTROL_PLANE_PORT)

.PHONY: run-runtime
run-runtime: ## Start the LLM Orchestration Runtime
	$(BIN)/uvicorn runtime_service.main:app --host 0.0.0.0 --port $(RUNTIME_PORT)

.PHONY: demo
demo: ## Start every service locally and run the end-to-end demonstration
	@./scripts/run_demo.sh

.PHONY: demo-docker
demo-docker: ## Bring the stack up with Docker Compose and run the demonstration
	docker compose up -d --build
	@echo "waiting for services to become ready…"
	@$(BIN)/python scripts/demo.py \
		--control-plane-url http://localhost:$(CONTROL_PLANE_PORT) \
		--runtime-url http://localhost:$(RUNTIME_PORT)

.PHONY: down
down: ## Stop the Docker Compose stack
	docker compose down -v

.PHONY: clean
clean: ## Remove build artifacts and local state
	rm -rf .pytest_cache .ruff_cache .mypy_cache data
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
