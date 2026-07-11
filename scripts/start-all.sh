#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHILD_PIDS=()
LAST_CHILD_PID=""
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
  local pid="$3"
  local max_attempts=30
  local attempt=0
  echo "Waiting for $label to be ready at $url..."
  until curl -sf "$url" > /dev/null 2>&1; do
    if ! kill -0 "$pid" 2>/dev/null; then
      local child_status=0
      wait "$pid" 2>/dev/null || child_status="$?"
      if [ "$child_status" -eq 0 ]; then
        child_status=1
      fi
      echo "ERROR: $label process exited before becoming ready" >&2
      exit "$child_status"
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge "$max_attempts" ]; then
      echo "ERROR: $label did not become ready within $max_attempts seconds" >&2
      exit 1
    fi
    sleep 1
  done
  echo "$label is ready."
}

_start_child() {
  local label="$1"
  local script_path="$2"

  echo "==> Starting $label..."
  "$script_path" &
  local pid="$!"
  CHILD_PIDS+=("$pid")
  LAST_CHILD_PID="$pid"
}

_wait_for_children() {
  local status=0
  local pid

  for pid in "${CHILD_PIDS[@]}"; do
    if wait "$pid"; then
      :
    else
      status="$?"
    fi
  done

  return "$status"
}

_start_child "Ollama" "$SCRIPT_DIR/start-ollama.sh"
OLLAMA_PID="$LAST_CHILD_PID"
_wait_for_http "http://localhost:11434/api/tags" "Ollama" "$OLLAMA_PID"

"$SCRIPT_DIR/start-voicevox.sh"

_start_child "Backend" "$SCRIPT_DIR/start-backend.sh"
BACKEND_PID="$LAST_CHILD_PID"
_wait_for_http "http://localhost:8000" "Backend" "$BACKEND_PID"

_start_child "Frontend" "$SCRIPT_DIR/start-frontend.sh"

_wait_for_children
