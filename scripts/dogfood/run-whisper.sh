#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/load-environment.sh"
dogfood_load_environment_settings --with-images
dogfood_read_revision
dogfood_require_identity
compose_file="$DOGFOOD_CLONE_DIR/infra/whisper/compose.yaml"
action=${2-${1-up}}
case "$action" in
  up)
    docker compose -f "$compose_file" pull whisper
    exec docker compose -f "$compose_file" up --detach --no-build \
      --wait --wait-timeout 600 whisper
    ;;
  down) exec docker compose -f "$compose_file" down ;;
  *) echo "ERROR: run-whisper.sh requires compose up or compose down" >&2; exit 2 ;;
esac
