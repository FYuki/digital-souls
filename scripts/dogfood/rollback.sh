#!/usr/bin/env bash
set -euo pipefail

requested_target=
if [ "$#" -gt 0 ]; then
  if [ "$#" -ne 2 ] || [ "$1" != "--to" ]; then
    echo "ERROR: rollbackの引数は --to <SHA> のみです" >&2
    exit 2
  fi
  requested_target=$2
  if ! [[ "$requested_target" =~ ^[0-9a-f]{40}$ ]]; then
    echo "ERROR: --toには完全な小文字SHAを指定してください" >&2
    exit 2
  fi
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/load-environment.sh"
source "$SCRIPT_DIR/deployment-lib.sh"
dogfood_load_environment
dogfood_require_identity
dogfood_require_root
dogfood_validate_deployment_storage

current_manifest="$DOGFOOD_STATE_DIR/deployments/current.json"
if [ ! -f "$current_manifest" ]; then
  echo "ERROR: current deployment manifestがありません" >&2
  exit 2
fi
if [ -z "$requested_target" ]; then
  target=$(dogfood_manifest_field "$current_manifest" previousCommit)
  saved_manifest=$current_manifest
else
  target=$requested_target
  saved_manifest=$(dogfood_find_saved_manifest "$target") || {
    echo "ERROR: 指定commitの保存済みmanifestがありません" >&2
    exit 2
  }
fi
dogfood_require_commit_sha "$target"
backup_id=$(dogfood_manifest_field "$saved_manifest" backupId)

dogfood_verify_origin
dogfood_require_clean_checkout
dogfood_fetch_and_resolve_commit "$target"
previous=$(git -C "$DOGFOOD_CLONE_DIR" rev-parse HEAD)
if ! dogfood_activate_revision "$target" || ! dogfood_check_readiness; then
  echo "ERROR: rollback後のreadiness確認に失敗しました: $target" >&2
  exit 1
fi
manifest=$(dogfood_manifest_metadata "$previous" "$target" "$backup_id")
dogfood_write_manifest "$manifest" "$target"
echo "rollbackが完了しました: $target"
