#!/usr/bin/env bash
set -euo pipefail
bash "$(dirname "$0")/with-test-postgres.sh" uv run --no-sync pytest -m postgres -q
