#!/usr/bin/env bash

DOGFOOD_SCRIPT_LIBRARY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOGFOOD_ROOT_DIR="$DOGFOOD_SCRIPT_LIBRARY_DIR/../.."
source "$DOGFOOD_ROOT_DIR/scripts/dogfood/load-environment.sh"
dogfood_load_environment

if [ "${DS_DATA_DIR+x}" != "x" ] || [ -z "$DS_DATA_DIR" ]; then
  echo "ERROR: dogfood requires DS_DATA_DIR in DOGFOOD_ENV_FILE" >&2
  exit 2
fi
dogfood_require_identity

export DS_PROFILE="dogfood"
export DS_ENVIRONMENT_RUN_REPORT="$DS_DATA_DIR/runtime/dogfood/environment-run.json"
export DS_PROFILE_REPORT="$DS_DATA_DIR/runtime/dogfood/resolved-profile.json"
