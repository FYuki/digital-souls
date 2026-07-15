#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/../backend"
VENV_BIN_DIR="$BACKEND_DIR/.venv/bin"

if [ ! -f "$VENV_BIN_DIR/activate" ]; then
  echo "ERROR: backend virtualenv is required: activate script is missing. Run scripts/setup-backend.sh before starting the backend." >&2
  exit 1
fi

if [ ! -x "$VENV_BIN_DIR/uvicorn" ]; then
  echo "ERROR: Backend uvicorn executable is missing. Run scripts/setup-backend.sh before starting the backend." >&2
  exit 1
fi

source "$VENV_BIN_DIR/activate"

if [ -f "$BACKEND_DIR/.env" ]; then
  set -a
  source "$BACKEND_DIR/.env"
  set +a
fi

exec "$VENV_BIN_DIR/uvicorn" --app-dir "$BACKEND_DIR" app.main:app --reload
