#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$SCRIPT_DIR/../frontend"

if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  echo "node_modules not found, running npm install..."
  npm install --prefix "$FRONTEND_DIR"
fi

npm run dev --prefix "$FRONTEND_DIR"
