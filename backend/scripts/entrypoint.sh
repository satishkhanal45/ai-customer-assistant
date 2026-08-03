#!/usr/bin/env bash
set -euo pipefail

wait_for_postgres() {
  echo "Waiting for postgres at ${POSTGRES_HOST:-postgres}:${POSTGRES_PORT:-5432}..."
  until pg_isready -h "${POSTGRES_HOST:-postgres}" -p "${POSTGRES_PORT:-5432}" -U "${POSTGRES_USER}" > /dev/null 2>&1; do
    sleep 1
  done
  echo "Postgres is ready."
}

run_migrations() {
  echo "Applying Alembic migrations..."
  alembic upgrade head
}

start_app() {
  echo "Starting application..."
  exec uvicorn main:app --host 0.0.0.0 --port "${APP_PORT:-8000}"
}

wait_for_postgres

# If a command was explicitly passed (e.g. `docker compose run --rm backend
# alembic upgrade head`), run exactly that and stop -- don't also run
# migrations or start the app. Only fall through to the full startup
# sequence when no command was given (the normal `docker compose up` case).
if [ "$#" -gt 0 ]; then
  echo "Running: $*"
  exec "$@"
fi

run_migrations
start_app