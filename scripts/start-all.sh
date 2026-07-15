#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/process.sh"
source "$SCRIPT_DIR/lib/readiness.sh"

process_manager_init

"$SCRIPT_DIR/setup-backend.sh"

process_start_child "Ollama" "$SCRIPT_DIR/start-ollama.sh"
OLLAMA_PID="$(process_last_started_pid)"
wait_for_http "http://localhost:11434/api/tags" "Ollama" "$OLLAMA_PID"

"$SCRIPT_DIR/start-voicevox.sh"

process_start_child "Backend" "$SCRIPT_DIR/start-backend.sh"
BACKEND_PID="$(process_last_started_pid)"
wait_for_http "http://localhost:8000" "Backend" "$BACKEND_PID"

process_start_child "Frontend" "$SCRIPT_DIR/start-frontend.sh"

process_wait_all
