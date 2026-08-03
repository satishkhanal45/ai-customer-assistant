FROM python:3.12-slim

# postgresql-client -> pg_isready, used by scripts/entrypoint.sh to wait for the DB
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml* uv.lock* ./
RUN uv sync --no-install-project

COPY . .
RUN uv sync

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH="/app/src/ai_customer_assistant"

RUN chmod +x scripts/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["scripts/entrypoint.sh"]