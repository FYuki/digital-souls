#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_REPORT_PATH="${VOICE_CHAT_E2E_BACKEND_REPORT:-$SCRIPT_DIR/../frontend/test-results/voice-chat-backend.json}"
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

_start_child() {
  local label="$1"
  local script_path="$2"

  echo "==> Starting $label..."
  "$script_path" &
  local pid="$!"
  CHILD_PIDS+=("$pid")
  LAST_CHILD_PID="$pid"
}

_wait_for_http() {
  local url="$1"
  local label="$2"
  local pid="$3"
  local max_attempts="${VOICE_CHAT_E2E_HTTP_MAX_ATTEMPTS:-30}"
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
      return "$child_status"
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge "$max_attempts" ]; then
      echo "ERROR: $label did not become ready within $max_attempts seconds" >&2
      return 1
    fi
    sleep 1
  done
  echo "$label is ready."
}

_wait_for_external_http() {
  local url="$1"
  local label="$2"
  local max_attempts="${VOICE_CHAT_E2E_HTTP_MAX_ATTEMPTS:-30}"
  local attempt=0

  echo "Waiting for $label to be ready at $url..."
  until curl -sf "$url" > /dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge "$max_attempts" ]; then
      echo "ERROR: $label did not become ready within $max_attempts seconds" >&2
      return 1
    fi
    sleep 1
  done
  echo "$label is ready."
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

_validate_backend_mode() {
  local mode="$1"
  case "$mode" in
    mock | real) ;;
    *)
      echo "ERROR: VOICE_CHAT_E2E_BACKEND must be \"mock\" or \"real\"; received \"$mode\"" >&2
      exit 1
      ;;
  esac
}

_write_backend_report() {
  local mode="$1"
  shift
  mkdir -p "$(dirname "$BACKEND_REPORT_PATH")"
  python3 - "$BACKEND_REPORT_PATH" "$mode" "$@" <<'PY'
import json
import sys

path = sys.argv[1]
mode = sys.argv[2]
reasons = sys.argv[3:]
with open(path, "w", encoding="utf-8") as report:
    json.dump({"mode": mode, "reasons": reasons}, report, ensure_ascii=False, indent=2)
    report.write("\n")
PY
}

_start_frontend_only() {
  _start_child "Frontend" "$SCRIPT_DIR/start-frontend.sh"
  _wait_for_children
}

_start_voicevox() {
  echo "==> Starting VOICEVOX..."
  docker start voicevox_engine > /dev/null
  echo "VOICEVOX container start requested."
  _wait_for_external_http "http://localhost:50021/version" "VOICEVOX"
}

_start_real_services() {
  _start_child "Ollama" "$SCRIPT_DIR/start-ollama.sh"
  local ollama_pid="$LAST_CHILD_PID"
  _wait_for_http "http://localhost:11434/api/tags" "Ollama" "$ollama_pid" || return $?

  _start_voicevox || return $?

  "$SCRIPT_DIR/setup-backend.sh" || return $?

  _start_child "Backend" "$SCRIPT_DIR/start-backend.sh"
  local backend_pid="$LAST_CHILD_PID"
  _wait_for_http "http://localhost:8000" "Backend" "$backend_pid" || return $?

  _write_backend_report "real" "backend, Ollama, and VOICEVOX startup checks passed"
  _start_child "Frontend" "$SCRIPT_DIR/start-frontend.sh"
}

backend_mode="${VOICE_CHAT_E2E_BACKEND:-real}"
_validate_backend_mode "$backend_mode"
rm -f "$BACKEND_REPORT_PATH"

case "$backend_mode" in
  mock)
    _write_backend_report "mock" "VOICE_CHAT_E2E_BACKEND=mock requested"
    _start_frontend_only
    ;;
  real)
    _start_real_services
    _wait_for_children
    ;;
esac
