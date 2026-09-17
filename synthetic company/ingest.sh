#!/usr/bin/env bash
#
# Upload the Aldermoor Technologies corpus into a running AI Customer
# Assistant, and wait for each job to finish.
#
#   ./ingest.sh you@example.com                # prompts for the password
#   APP_PORT=8001 ./ingest.sh you@example.com  # non-default port
#
# The account must have the `member` role or higher — every /ingest/*
# endpoint is guarded by require_member.
#
# Two things this script does deliberately:
#
#   * it sets the MIME type on every upload. curl guesses from the file
#     extension and gets Markdown wrong (application/octet-stream), which
#     the API rejects with HTTP 415 "Expected PDF, DOCX or Markdown".
#
#   * it uploads one file at a time and polls the job. Ingestion is
#     asynchronous — HTTP 202 means queued, not indexed — and the worker
#     runs one job at a time, so firing all fifteen at once just builds a
#     queue you cannot see the end of.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${APP_PORT:-8000}"
HOST="${APP_HOST:-127.0.0.1}"
BASE="http://${HOST}:${PORT}"

EMAIL="${1:-}"
if [ -z "$EMAIL" ]; then
  echo "usage: $0 <email>   (the account must be a member or admin)" >&2
  exit 64
fi

read -r -s -p "Password for ${EMAIL}: " PASSWORD
echo

echo "Signing in to ${BASE} ..."
TOKEN="$(curl -sS -X POST "${BASE}/auth/login" \
  -H 'Content-Type: application/json' \
  -d "$(printf '{"email":%s,"password":%s,"transport":"bearer"}' \
        "$(printf '%s' "$EMAIL" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')" \
        "$(printf '%s' "$PASSWORD" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')")" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("access_token",""))')"

if [ -z "$TOKEN" ]; then
  echo "Sign-in failed. Check the address and password." >&2
  exit 77
fi
echo "Signed in."
echo

mime_for() {
  case "$1" in
    *.pdf)  echo "application/pdf" ;;
    *.docx) echo "application/vnd.openxmlformats-officedocument.wordprocessingml.document" ;;
    *.md)   echo "text/markdown" ;;
    *)      echo "" ;;
  esac
}

wait_for_job() {
  local job_id="$1" name="$2"
  for _ in $(seq 1 90); do
    local status
    status="$(curl -sS -H "Authorization: Bearer ${TOKEN}" \
      "${BASE}/ingest/jobs/${job_id}" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' 2>/dev/null || echo "")"
    case "$status" in
      SUCCEEDED|COMPLETED) echo "    indexed"; return 0 ;;
      FAILED|DEAD_LETTER)  echo "    FAILED (see: make logs)"; return 1 ;;
      *) sleep 4 ;;
    esac
  done
  echo "    still running after 6 minutes — check the Ingest page"
  return 0
}

total=0
failed=0
for file in "${HERE}"/pdf/*.pdf "${HERE}"/docx/*.docx "${HERE}"/markdown/*.md; do
  [ -e "$file" ] || continue
  name="$(basename "$file")"
  mime="$(mime_for "$file")"
  total=$((total + 1))
  printf '%2d. %-45s ' "$total" "$name"

  response="$(curl -sS -X POST "${BASE}/ingest/upload" \
    -H "Authorization: Bearer ${TOKEN}" \
    -F "file=@${file};type=${mime}")"

  job_id="$(printf '%s' "$response" \
    | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); raise SystemExit
print(d.get("job_id") or "")' 2>/dev/null || echo "")"

  if [ -z "$job_id" ]; then
    echo "skipped"
    echo "    ${response}"
    continue
  fi
  echo "queued"
  wait_for_job "$job_id" "$name" || failed=$((failed + 1))
done

echo
echo "${total} documents submitted, ${failed} failed."
echo "Ask the assistant something like:"
echo "  \"How much does Relay cost per technician?\""
echo "  \"What are your support hours?\""
echo "  \"Where is our data stored?\""
