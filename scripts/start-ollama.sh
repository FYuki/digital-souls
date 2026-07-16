#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/profile.sh"

profile_use_resolved_report_or_resolve "dev"
profile_require_managed_dependency ollama

if curl -sf "http://localhost:11434/api/tags" > /dev/null 2>&1; then
  echo "WARNING: Ollama already responding at http://localhost:11434 — assuming it's already running." >&2
  exec sleep infinity
fi

echo "Starting Ollama service..."
ollama serve
