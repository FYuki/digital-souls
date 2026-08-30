#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
exec "$REPOSITORY_ROOT/backend/.venv/bin/python" \
  "$REPOSITORY_ROOT/environments/environment_cli.py" wait-readiness \
  --profile dogfood \
  --service ollama \
  --service voicevox \
  --service livekit \
  --max-attempts 30 \
  --interval-seconds 1 \
  --request-timeout-seconds 1
