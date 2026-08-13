.PHONY: run test

run:
	uv run --project backend python backend/src/ai_customer_assistant/main.py

test:
	uv run --project backend pytest backend/tests

uvicorn: ## Start the FastAPI local development server
	POSTGRES_HOST=$(POSTGRES_HOST) \
	POSTGRES_PORT=$(POSTGRES_PORT) \
	PYTHONPATH=$(PYTHONPATH) \
	uv run --env-file backend/.env --project backend uvicorn main:app --reload --port 8002 --app-dir backend/src/ai_customer_assistant


# Default environment variables. Use plain "=" (not "?=") so an ambient
# POSTGRES_HOST/POSTGRES_PORT exported in the shell (e.g. from `source
# backend/.env`, which contains docker-internal values) can't leak into
# host-side targets. Override explicitly on the command line if needed:
#   make ingest POSTGRES_HOST=postgres POSTGRES_PORT=5432
POSTGRES_HOST = localhost
POSTGRES_PORT = 5433
DEFAULT_URL ?= https://alpiniststudios.com/app-prototype-a-complete-guide/
DEFAULT_USER_ID ?= 00000000-0000-0000-0000-000000000000
PYTHONPATH := backend:backend/src:backend/src/ai_customer_assistant

# Browser opener: xdg-open on Linux, `open` on macOS.
OPEN ?= $(shell command -v xdg-open >/dev/null 2>&1 && echo xdg-open || echo open)

.PHONY: help up down status worker ingest verify logs clean frontend chat graph

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

ingest: ## Crawl and ingest a URL (prompts for URL and whether to crawl the whole site)
	@if [ -z "$(URL)" ]; then \
		read -p "Enter URL to ingest: " TARGET_URL; \
	else \
		TARGET_URL="$(URL)"; \
	fi; \
	if [ -z "$(SITE)" ]; then \
		read -p "Crawl entire site? [Y/N]: " SITE_CHOICE; \
	else \
		SITE_CHOICE="$(SITE)"; \
	fi; \
	case "$$SITE_CHOICE" in \
		y|Y|yes|YES) SITE_FLAG="--site" ;; \
		*) SITE_FLAG="" ;; \
	esac; \
	POSTGRES_HOST=$(POSTGRES_HOST) \
	POSTGRES_PORT=$(POSTGRES_PORT) \
	PYTHONPATH=$(PYTHONPATH) \
	uv run --env-file backend/.env --project backend python -m scripts.crawl_and_ingest \
		"$$TARGET_URL" \
		--uploaded-by "$(or $(USER_ID),$(DEFAULT_USER_ID))" \
		$$SITE_FLAG

verify: ## Check database records for knowledge sources
	docker exec -it -e PAGER=cat ai-customer-assistant-postgres psql -U ai_assistant -d ai_customer_assistant -c \
		"SELECT source_id, source_name, source_type, updated_at FROM knowledge_source ORDER BY updated_at DESC;"

psql :
	docker exec -it ai-customer-assistant-postgres psql -U ai_assistant -d ai_customer_assistant

graph :
	$(OPEN) "http://127.0.0.1:8002/#/graph"

frontend: ## Open the AI Customer Assistant web app (chat) in your browser
	$(OPEN) "http://127.0.0.1:8002/#/chat"

chat: ## Alias for frontend — open the chat portal
	$(OPEN) "http://127.0.0.1:8002/#/chat"

backend:
	cd backend/src/ai_customer_assistant && uvicorn main:app --reload --port 8002

trunc:
	docker exec -i ai-customer-assistant-postgres psql -U ai_assistant -d ai_customer_assistant -c "TRUNCATE TABLE value, attribute, relation, entity, knowledge_source_entity_map, embedding_chunk, knowledge_injection_job, knowledge_source_version, knowledge_source, knowledge_category, app_user RESTART IDENTITY CASCADE;"

make user:
	docker exec -i ai-customer-assistant-postgres psql -U ai_assistant -d ai_customer_assistant -c "INSERT INTO app_user (id, email, is_service_account) VALUES ('00000000-0000-0000-0000-000000000000','admin@admin.com', True);"