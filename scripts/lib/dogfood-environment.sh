#!/usr/bin/env bash

if [ "${DS_DATA_DIR+x}" != "x" ] || [ -z "$DS_DATA_DIR" ]; then
  echo "ERROR: dogfood requires an explicit DS_DATA_DIR" >&2
  exit 2
fi

DOGFOOD_SCRIPT_LIBRARY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOGFOOD_ROOT_DIR="$DOGFOOD_SCRIPT_LIBRARY_DIR/../.."
export DS_PROFILE="dogfood"
export DS_ENVIRONMENT_ID="dogfood"
export DS_ENVIRONMENT_RUN_REPORT="$DS_DATA_DIR/runtime/dogfood/environment-run.json"
export DS_PROFILE_REPORT="$DS_DATA_DIR/runtime/dogfood/resolved-profile.json"
