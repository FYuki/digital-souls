#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/process.sh"
source "$SCRIPT_DIR/lib/readiness.sh"
source "$SCRIPT_DIR/lib/profile.sh"

process_manager_init
profile_resolve "integration-voice"
profile_require_managed_dependency frontend
profile_start_stack
process_wait_all
