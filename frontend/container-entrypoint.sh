#!/usr/bin/env bash
set -euo pipefail

: "${DS_FRONTEND_HOST:?DS_FRONTEND_HOST is required}"
: "${DS_FRONTEND_PORT:?DS_FRONTEND_PORT is required}"

case "${DS_CONTAINER_MODE:-production}" in
  development)
    exec npm run dev --prefix /app/frontend -- \
      --host "$DS_FRONTEND_HOST" --port "$DS_FRONTEND_PORT" --strictPort
    ;;
  production)
    exec node /app/frontend/built-frontend-server.mjs \
      --host "$DS_FRONTEND_HOST" --port "$DS_FRONTEND_PORT"
    ;;
  *)
    echo "ERROR: DS_CONTAINER_MODE must be development or production" >&2
    exit 2
    ;;
esac
