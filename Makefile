SHELL := /bin/bash

# The first interpreter on PATH is frequently an older system Python. Searching for a
# supported one gives a clear message at setup time instead of an opaque pip resolution error.
# Override with `make setup PYTHON=/path/to/python3.12`.
PYTHON ?= $(shell for candidate in python3.13 python3.12 python3.11 python3; do \
	if command -v $$candidate >/dev/null 2>&1 && \
	   $$candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then \
		echo $$candidate; break; \
	fi; \
done)

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
	@if [ -z "$(PYTHON)" ]; then \
		echo "No Python 3.11 or newer was found on PATH."; \
		echo "Install one, or point at it explicitly:"; \
		echo "    make setup PYTHON=/path/to/python3.12"; \
		exit 1; \
	fi
	@echo "Using $$($(PYTHON) --version) from $$(command -v $(PYTHON))"
	$(PYTHON) -m venv $(VENV)
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

.PHONY: check-docs
check-docs: ## Verify the documentation still describes the code
	$(BIN)/python scripts/check_docs.py

.PHONY: check
check: lint typecheck test check-docs ## Everything CI runs

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
		--runtime-url http://localhost:$(RUNTIME_PORT) \
		--demo-api-url http://demo-api:8081

.PHONY: capture
capture: ## Regenerate the README screenshots from the running application
	@./scripts/run_capture.sh

.PHONY: capture-video
capture-video: ## Regenerate the screenshots and record the console walkthrough
	@./scripts/run_capture.sh --animate

.PHONY: down
down: ## Stop the Docker Compose stack
	docker compose down -v

.PHONY: clean
clean: ## Remove build artifacts and local state
	rm -rf .pytest_cache .ruff_cache .mypy_cache data
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
