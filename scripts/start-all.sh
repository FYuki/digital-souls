#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHILD_PIDS=()
_CLEANED_UP=0

cleanup() {
  if [ "$_CLEANED_UP" -eq 1 ]; then
    return
  fi

  _CLEANED_UP=1
  set +e

  local pid
  for pid in "${CHILD_PIDS[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null
    fi
  done

  if [ "${#CHILD_PIDS[@]}" -gt 0 ]; then
    wait "${CHILD_PIDS[@]}" 2>/dev/null
  fi
}

handle_signal() {
  local exit_code="$1"
  cleanup
  exit "$exit_code"
}

trap cleanup EXIT
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM

_wait_for_http() {
  local url="$1"
  local label="$2"
  local max_attempts=30
  local attempt=0
  echo "Waiting for $label to be ready at $url..."
  until curl -sf "$url" > /dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge "$max_attempts" ]; then
      echo "ERROR: $label did not become ready within $max_attempts seconds" >&2
      exit 1
    fi
    sleep 1
  done
  echo "$label is ready."
}

echo "==> Starting Ollama..."
"$SCRIPT_DIR/start-ollama.sh" &
OLLAMA_PID=$!
CHILD_PIDS+=("$OLLAMA_PID")
_wait_for_http "http://localhost:11434/api/tags" "Ollama"

echo "==> Starting Backend..."
"$SCRIPT_DIR/start-backend.sh" &
BE_PID=$!
CHILD_PIDS+=("$BE_PID")
_wait_for_http "http://localhost:8000" "Backend"

echo "==> Starting Frontend..."
"$SCRIPT_DIR/start-frontend.sh" &
FE_PID=$!
CHILD_PIDS+=("$FE_PID")

wait "$OLLAMA_PID" "$BE_PID" "$FE_PID"
