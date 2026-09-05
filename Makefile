.PHONY: run test uvicorn serve

# Always invoke the project venv's interpreter by absolute path rather than
# letting PATH decide. An activated conda env, or a stale VIRTUAL_ENV left
# over from another checkout, otherwise shadows the project tools and the app
# fails with ModuleNotFoundError (pgvector, langgraph, ...) because it is
# running under the wrong Python entirely.
VENV_PY := $(CURDIR)/backend/.venv/bin/python

run: ## Alias for `make uvicorn`
	$(MAKE) uvicorn

test: ## Run the backend test suite in the project venv
	@cd backend && VIRTUAL_ENV= PYTHONHOME= $(VENV_PY) -m pytest $(PYTEST_ARGS)

uvicorn: ## Start the FastAPI dev server on the host (port $(DEV_PORT))
	@set -a; [ -f backend/.env ] && . ./backend/.env; set +a; \
	POSTGRES_HOST=$(POSTGRES_HOST) \
	POSTGRES_PORT=$(POSTGRES_PORT) \
	PYTHONPATH=$(PYTHONPATH) \
	VIRTUAL_ENV= PYTHONHOME= \
	$(VENV_PY) -m uvicorn main:app --reload --port $(DEV_PORT) --app-dir backend/src/ai_customer_assistant

serve: uvicorn ## Alias for `make uvicorn`


# Default environment variables. Use plain "=" (not "?=") so an ambient
# POSTGRES_HOST/POSTGRES_PORT exported in the shell (e.g. from `source
# backend/.env`, which contains docker-internal values) can't leak into
# host-side targets. Override explicitly on the command line if needed:
#   make ingest POSTGRES_HOST=postgres POSTGRES_PORT=5432
POSTGRES_HOST = localhost
POSTGRES_PORT = 5433
# Host-side dev server port (`make uvicorn`). The docker stack publishes the
# backend on APP_PORT from the root .env instead - see DOCKER_PORT below.
DEV_PORT ?= 8002
DOCKER_PORT ?= $(shell sed -n 's/^APP_PORT=//p' .env 2>/dev/null | head -1)
DOCKER_PORT := $(if $(DOCKER_PORT),$(DOCKER_PORT),8000)
PYTEST_ARGS ?= -q
DEFAULT_URL ?= https://alpiniststudios.com/app-prototype-a-complete-guide/
DEFAULT_USER_ID ?= 00000000-0000-0000-0000-000000000000
PYTHONPATH := backend:backend/src:backend/src/ai_customer_assistant

# Browser opener: xdg-open on Linux, `open` on macOS.
OPEN ?= $(shell command -v xdg-open >/dev/null 2>&1 && echo xdg-open || echo open)

.PHONY: help up down status worker ingest verify logs clean frontend chat graph psql trunc user

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

psql: ## Open a psql shell on the running database
	docker exec -it ai-customer-assistant-postgres psql -U ai_assistant -d ai_customer_assistant

# PORT defaults to the docker-published port; pass PORT=$(DEV_PORT) when you
# are running the host-side dev server via `make uvicorn` instead.
PORT ?= $(DOCKER_PORT)

graph: ## Open the knowledge-graph explorer
	$(OPEN) "http://127.0.0.1:$(PORT)/#/graph"

frontend: ## Open the AI Customer Assistant web app (chat) in your browser
	$(OPEN) "http://127.0.0.1:$(PORT)/#/chat"

chat: ## Alias for frontend — open the chat portal
	$(OPEN) "http://127.0.0.1:$(PORT)/#/chat"

logs: ## Tail the docker stack logs
	docker compose logs -f --tail=100

clean: ## Stop containers and remove volumes (DESTROYS the database)
	@printf 'This deletes the postgres and minio volumes. Type "yes" to continue: '; \
	read ans; [ "$$ans" = "yes" ] || { echo "aborted"; exit 1; }; \
	docker compose down -v

trunc: ## Empty every knowledge-base table (keeps the schema)
	docker exec -i ai-customer-assistant-postgres psql -U ai_assistant -d ai_customer_assistant -c "TRUNCATE TABLE value, attribute, relation, entity, knowledge_source_entity_map, embedding_chunk, knowledge_injection_job, knowledge_source_version, knowledge_source, knowledge_category, app_user RESTART IDENTITY CASCADE;"

user: ## Insert the default service-account app_user row
	docker exec -i ai-customer-assistant-postgres psql -U ai_assistant -d ai_customer_assistant -c "INSERT INTO app_user (id, email, is_service_account) VALUES ('00000000-0000-0000-0000-000000000000','admin@admin.com', True);"

