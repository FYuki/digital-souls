#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/profile.sh"

profile_use_resolved_report_or_resolve "dev"
profile_require_managed_dependency ollama

echo "Starting Ollama service..."
exec ollama serve
