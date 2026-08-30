#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/load-environment.sh"
dogfood_load_environment
dogfood_require_identity
compose_file="$DOGFOOD_CLONE_DIR/infra/dogfood/livekit/compose.yaml"
action=${2-${1-up}}
case "$action" in
  up) exec docker compose -f "$compose_file" up -d ;;
  down) exec docker compose -f "$compose_file" down ;;
  *) echo "ERROR: run-livekit.sh requires compose up or compose down" >&2; exit 2 ;;
esac
