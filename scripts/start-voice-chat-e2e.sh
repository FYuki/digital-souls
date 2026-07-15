#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_REPORT_PATH="${VOICE_CHAT_E2E_BACKEND_REPORT:-$SCRIPT_DIR/../frontend/test-results/voice-chat-backend.json}"
source "$SCRIPT_DIR/lib/process.sh"
source "$SCRIPT_DIR/lib/readiness.sh"

process_manager_init

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
  process_start_child "Frontend" "$SCRIPT_DIR/start-frontend.sh"
  process_wait_all
}

_start_real_services() {
  "$SCRIPT_DIR/setup-backend.sh" || return $?

  local ollama_pid
  process_start_child "Ollama" "$SCRIPT_DIR/start-ollama.sh"
  ollama_pid="$(process_last_started_pid)"
  wait_for_http "http://localhost:11434/api/tags" "Ollama" "$ollama_pid" || return $?

  "$SCRIPT_DIR/start-voicevox.sh" || return $?

  local backend_pid
  process_start_child "Backend" "$SCRIPT_DIR/start-backend.sh"
  backend_pid="$(process_last_started_pid)"
  wait_for_http "http://localhost:8000" "Backend" "$backend_pid" || return $?

  _write_backend_report "real" "backend, Ollama, and VOICEVOX startup checks passed"
  process_start_child "Frontend" "$SCRIPT_DIR/start-frontend.sh"
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
    process_wait_all
    ;;
esac
