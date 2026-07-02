#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/../backend"

if [ ! -x "$BACKEND_DIR/.venv/bin/uvicorn" ]; then
  echo "backend virtualenv is required. Run scripts/setup-backend.sh before starting the backend." >&2
  exit 1
fi

source "$BACKEND_DIR/.venv/bin/activate"

if [ -f "$BACKEND_DIR/.env" ]; then
  set -a
  source "$BACKEND_DIR/.env"
  set +a
fi

exec "$BACKEND_DIR/.venv/bin/uvicorn" --app-dir "$BACKEND_DIR" app.main:app --reload
