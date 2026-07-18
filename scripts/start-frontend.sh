#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$SCRIPT_DIR/../frontend"
source "$SCRIPT_DIR/lib/profile.sh"

profile_use_resolved_report_or_resolve "dev"
profile_require_managed_dependency frontend

if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  echo "ERROR: frontend dependencies are missing. Run environments/up.sh to prepare them." >&2
  exit 1
fi

exec npm run dev --prefix "$FRONTEND_DIR"
