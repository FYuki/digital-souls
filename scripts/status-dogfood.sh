#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/dogfood-environment.sh"
exec python3 "$DOGFOOD_ROOT_DIR/environments/environment_cli.py" status \
  --run-report "$DS_ENVIRONMENT_RUN_REPORT"
