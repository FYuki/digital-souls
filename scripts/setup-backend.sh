#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/../backend"
VENV_DIR="$BACKEND_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR" || {
    status=$?
    echo "ERROR: Backend setup failed during virtual environment creation." >&2
    exit "$status"
  }
fi

"$VENV_DIR/bin/pip" install -r "$BACKEND_DIR/requirements.txt" || {
  status=$?
  echo "ERROR: Backend setup failed during dependency installation." >&2
  exit "$status"
}
