.PHONY: run test

run:
	uv run --project backend python backend/src/ai-customer-assistant/main.py

test:
	uv run --project backend pytest backend/tests



# Default environment variables
POSTGRES_HOST ?= localhost
POSTGRES_PORT ?= 5433
DEFAULT_URL ?= https://alpiniststudios.com/app-prototype-a-complete-guide/
DEFAULT_USER_ID ?= 00000000-0000-0000-0000-000000000000
PYTHONPATH := backend:backend/src:backend/src/ai_customer_assistant

.PHONY: help up down status worker ingest verify logs clean

help: ## Show this help message
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?##/ {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

up: ## 1. Bring up full docker stack and check status
	docker compose up -d 
	docker compose ps

down: ## Stop docker containers
	docker compose down

status: ## Check docker container health status
	docker compose ps

worker: ## 2. Start the ingestion worker process
	@set -a; [ -f backend/.env ] && . backend/.env; set +a; \
	POSTGRES_HOST=$(POSTGRES_HOST) \
	POSTGRES_PORT=$(POSTGRES_PORT) \
	PYTHONPATH=$(PYTHONPATH) \
	uv run --project backend python backend/scripts/run_worker.py

ingest: ## Crawl and ingest a URL (e.g. make ingest URL="https://...")
	@if [ -z "$(URL)" ]; then \
		read -p "Enter URL to ingest: " TARGET_URL; \
	else \
		TARGET_URL="$(URL)"; \
	fi; \
	POSTGRES_HOST=$(POSTGRES_HOST) \
	POSTGRES_PORT=$(POSTGRES_PORT) \
	PYTHONPATH=$(PYTHONPATH) \
	uv run --env-file backend/.env --project backend python -m scripts.crawl_and_ingest \
		"$$TARGET_URL" \
		--uploaded-by "$(or $(USER_ID),$(DEFAULT_USER_ID))"

verify: ## Check database records for knowledge sources
	docker exec -it -e PAGER=cat ai-customer-assistant-postgres psql -U ai_assistant -d ai_customer_assistant -c \
		"SELECT source_id, source_name, source_type, updated_at FROM knowledge_source ORDER BY updated_at DESC;"
