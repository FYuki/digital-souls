#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/../backend"
VENV_BIN_DIR="$BACKEND_DIR/.venv/bin"
source "$SCRIPT_DIR/lib/profile.sh"

if [ "$#" -ne 4 ] && [ "$#" -ne 5 ]; then
  echo "ERROR: usage: start-backend.sh --host HOST --port PORT [--reload]" >&2
  exit 2
fi
if [ "$1" != "--host" ] || [ "$3" != "--port" ]; then
  echo "ERROR: host and port must be explicit" >&2
  exit 2
fi
BACKEND_HOST="$2"
BACKEND_PORT="$4"
RELOAD_ARGUMENT=()
if [ "$#" -eq 5 ]; then
  if [ "$5" != "--reload" ]; then
    echo "ERROR: unknown backend argument: $5" >&2
    exit 2
  fi
  RELOAD_ARGUMENT=("--reload")
fi

if [ -f "$BACKEND_DIR/.env" ]; then
  set -a
  source "$BACKEND_DIR/.env"
  set +a
fi

profile_use_resolved_report_or_resolve "dev"
profile_require_managed_dependency backend
RESOLVED_PROFILE_REPORT="$DS_PROFILE_REPORT"
RESOLVED_BACKEND_HOST="$(profile_get dependencies.backend.host)"
RESOLVED_BACKEND_PORT="$(profile_get dependencies.backend.port)"
RESOLVED_BACKEND_RELOAD="$(profile_get dependencies.backend.reload)"
if [ "$BACKEND_HOST" != "$RESOLVED_BACKEND_HOST" ] || [ "$BACKEND_PORT" != "$RESOLVED_BACKEND_PORT" ]; then
  echo "ERROR: Backend host and port must match the resolved Profile" >&2
  exit 2
fi
if { [ "$RESOLVED_BACKEND_RELOAD" = "true" ] && [ "${#RELOAD_ARGUMENT[@]}" -ne 1 ]; } || \
   { [ "$RESOLVED_BACKEND_RELOAD" = "false" ] && [ "${#RELOAD_ARGUMENT[@]}" -ne 0 ]; }; then
  echo "ERROR: Backend reload argument must match the resolved Profile" >&2
  exit 2
fi

if [ ! -f "$VENV_BIN_DIR/activate" ]; then
  echo "ERROR: backend virtualenv is required: activate script is missing. Run scripts/setup-backend.sh before starting the backend." >&2
  exit 1
fi

if [ ! -x "$VENV_BIN_DIR/uvicorn" ]; then
  echo "ERROR: Backend uvicorn executable is missing. Run scripts/setup-backend.sh before starting the backend." >&2
  exit 1
fi

source "$VENV_BIN_DIR/activate"

export DS_PROFILE_REPORT="$RESOLVED_PROFILE_REPORT"
profile_export_derived_environment

exec "$VENV_BIN_DIR/uvicorn" --app-dir "$BACKEND_DIR" app.main:app \
  --host "$BACKEND_HOST" --port "$BACKEND_PORT" "${RELOAD_ARGUMENT[@]}"
