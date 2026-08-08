#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/load-environment.sh"
dogfood_load_environment
dogfood_require_identity
export OLLAMA_HOST
mapfile -t endpoints < <("$SCRIPT_DIR/resolve-inference-endpoints.py")
for endpoint in "${endpoints[@]}"; do
  if [[ "$endpoint" == OLLAMA_HOST=* ]]; then OLLAMA_HOST=${endpoint#*=}; fi
  if [[ "$endpoint" == OLLAMA_PORT=* ]]; then OLLAMA_PORT=${endpoint#*=}; fi
done
: "${OLLAMA_HOST:?Ollama hostを解決できません}"
: "${OLLAMA_PORT:?Ollama portを解決できません}"
export OLLAMA_HOST="$OLLAMA_HOST:$OLLAMA_PORT"
export OLLAMA_MODELS="$DS_DATA_DIR/ollama/models"
exec ollama serve
