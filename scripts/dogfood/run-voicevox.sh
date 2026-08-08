#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/load-environment.sh"
dogfood_load_environment
dogfood_require_identity
mapfile -t endpoints < <("$SCRIPT_DIR/resolve-inference-endpoints.py")
for endpoint in "${endpoints[@]}"; do
  case "$endpoint" in
    VOICEVOX_HOST=*) export VOICEVOX_HOST=${endpoint#*=} ;;
    VOICEVOX_PORT=*) export VOICEVOX_PORT=${endpoint#*=} ;;
  esac
done
: "${VOICEVOX_HOST:?VOICEVOX hostを解決できません}"
: "${VOICEVOX_PORT:?VOICEVOX portを解決できません}"
compose_file="$DOGFOOD_CLONE_DIR/infra/dogfood/voicevox/compose.yaml"
action=${2-${1-up}}
case "$action" in
  up) exec docker compose -f "$compose_file" up -d ;;
  down) exec docker compose -f "$compose_file" down ;;
  *) echo "ERROR: run-voicevox.sh requires compose up or compose down" >&2; exit 2 ;;
esac
