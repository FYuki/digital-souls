#!/usr/bin/env bash
# The Python process stays on the host, so an explicit embedding profile can reach loopback.
set -euo pipefail
bash "$(dirname "$0")/with-test-postgres.sh" uv run --no-sync python "$(dirname "$0")/evaluate-semantic-retrieval.py" "$@"
