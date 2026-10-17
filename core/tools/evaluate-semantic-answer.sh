#!/usr/bin/env bash
set -euo pipefail
bash "$(dirname "$0")/with-test-postgres.sh" node "$(dirname "$0")/evaluate-semantic-answer.mjs" "$@"
