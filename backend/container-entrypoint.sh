#!/usr/bin/env bash
set -euo pipefail

: "${DS_BACKEND_HOST:?DS_BACKEND_HOST is required}"
: "${DS_BACKEND_PORT:?DS_BACKEND_PORT is required}"
: "${DS_BACKEND_RELOAD:?DS_BACKEND_RELOAD is required}"

reload=()
case "$DS_BACKEND_RELOAD" in
  true) reload=(--reload) ;;
  false) ;;
  *) echo "ERROR: DS_BACKEND_RELOAD must be true or false" >&2; exit 2 ;;
esac

exec python -m uvicorn --app-dir /app/backend app.main:app \
  --host "$DS_BACKEND_HOST" --port "$DS_BACKEND_PORT" "${reload[@]}"
