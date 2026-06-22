#!/usr/bin/env bash
set -euo pipefail

if curl -sf "http://localhost:11434/api/tags" > /dev/null 2>&1; then
  echo "WARNING: Ollama already responding at http://localhost:11434 — assuming it's already running." >&2
  exec sleep infinity
fi

echo "Starting Ollama service..."
ollama serve
