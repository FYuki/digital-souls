#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]}"
SCRIPT_DIR="${SCRIPT_PATH%/*}"
if [ "$SCRIPT_DIR" = "$SCRIPT_PATH" ]; then
  SCRIPT_DIR="."
fi
case "$SCRIPT_DIR" in
  /*) ;;
  *) SCRIPT_DIR="$PWD/$SCRIPT_DIR" ;;
esac
source "$SCRIPT_DIR/lib/readiness.sh"

BACKEND_DIR="$SCRIPT_DIR/../backend"
VOICEVOX_CONTAINER_NAME="voicevox_engine"
DEFAULT_VOICEVOX_BASE_URL="http://localhost:50021"
VOICEVOX_SETUP_COMMAND="docker run -d --name voicevox_engine -p 50021:50021 voicevox/voicevox_engine:cpu-latest"

if [ -f "$BACKEND_DIR/.env" ]; then
  set -a
  source "$BACKEND_DIR/.env"
  set +a
fi

_resolve_voicevox_base_url() {
  if [ "${VOICEVOX_BASE_URL+x}" = "x" ] && [ -n "$VOICEVOX_BASE_URL" ]; then
    printf '%s\n' "${VOICEVOX_BASE_URL%/}"
    return
  fi

  printf '%s\n' "$DEFAULT_VOICEVOX_BASE_URL"
}

_is_local_default_voicevox() {
  local base_url="$1"

  case "$base_url" in
    http://localhost:50021|http://127.0.0.1:50021) return 0 ;;
    *) return 1 ;;
  esac
}

_print_voicevox_setup_message() {
  echo "ERROR: VOICEVOX container \"$VOICEVOX_CONTAINER_NAME\" does not exist." >&2
  echo "Create it before running this script:" >&2
  echo "  $VOICEVOX_SETUP_COMMAND" >&2
}

_require_docker() {
  if ! command -v docker > /dev/null 2>&1; then
    echo "ERROR: docker is required to start VOICEVOX." >&2
    echo "Install Docker and create the VOICEVOX container before running this script:" >&2
    echo "  $VOICEVOX_SETUP_COMMAND" >&2
    exit 1
  fi
}

_ensure_voicevox_container_exists() {
  local inspect_output

  if inspect_output="$(docker container inspect "$VOICEVOX_CONTAINER_NAME" 2>&1 > /dev/null)"; then
    return
  fi

  if printf '%s\n' "$inspect_output" | grep -E "No such (object|container)" > /dev/null; then
    _print_voicevox_setup_message
    exit 1
  fi

  echo "ERROR: failed to inspect VOICEVOX container \"$VOICEVOX_CONTAINER_NAME\"." >&2
  echo "$inspect_output" >&2
  exit 1
}

VOICEVOX_BASE_URL_RESOLVED="$(_resolve_voicevox_base_url)"
VOICEVOX_HEALTH_URL="$VOICEVOX_BASE_URL_RESOLVED/version"

if _is_local_default_voicevox "$VOICEVOX_BASE_URL_RESOLVED"; then
  _require_docker
  _ensure_voicevox_container_exists

  echo "==> Starting VOICEVOX..."
  docker start "$VOICEVOX_CONTAINER_NAME" > /dev/null
  echo "VOICEVOX container start requested."
else
  echo "==> Using configured VOICEVOX at $VOICEVOX_BASE_URL_RESOLVED"
fi

wait_for_http "$VOICEVOX_HEALTH_URL" "VOICEVOX"
