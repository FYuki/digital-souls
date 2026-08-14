#!/usr/bin/env bash
set -euo pipefail

target=
auto_rollback=true
while [ "$#" -gt 0 ]; do
  case "$1" in
    --commit)
      [ "$#" -ge 2 ] || { echo "ERROR: --commitにはSHAが必要です" >&2; exit 2; }
      target=$2
      shift 2
      ;;
    --no-auto-rollback)
      auto_rollback=false
      shift
      ;;
    *)
      echo "ERROR: 未知のdeploy引数です" >&2
      exit 2
      ;;
  esac
done
if [ -z "$target" ]; then
  echo "ERROR: --commitは必須です" >&2
  exit 2
fi
if ! [[ "$target" =~ ^[0-9a-f]{40}$ ]]; then
  echo "ERROR: --commitには完全な小文字SHAを指定してください" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/load-environment.sh"
source "$SCRIPT_DIR/deployment-lib.sh"

dogfood_load_environment
dogfood_require_identity
dogfood_require_root
dogfood_validate_deployment_storage
dogfood_verify_origin
dogfood_require_clean_checkout
dogfood_fetch_and_resolve_commit "$target"

previous=$(git -C "$DOGFOOD_CLONE_DIR" rev-parse HEAD)
backup_directory=$(dogfood_backup)
manifest=$(dogfood_manifest_metadata "$previous" "$target" "$backup_directory")
dogfood_write_manifest "$manifest" "$target"

if ! dogfood_activate_revision "$target"; then
  echo "ERROR: deploy処理がreadiness確認前に失敗しました: $target" >&2
  dogfood_report_current_deployment_state
  exit 1
fi
if dogfood_check_readiness; then
  echo "deployが完了しました: $target"
  exit 0
fi

if [ "$auto_rollback" = false ]; then
  echo "ERROR: readiness確認に失敗しました。自動rollbackは抑止されています: $target" >&2
  exit 1
fi
echo "ERROR: readiness確認に失敗したためrollbackします: $target -> $previous" >&2
if dogfood_activate_revision "$previous" && dogfood_check_readiness; then
  rollback_manifest=$(dogfood_manifest_metadata "$target" "$previous" "$backup_directory")
  dogfood_write_manifest "$rollback_manifest" "$previous"
  echo "ERROR: deployは失敗し、直前のcommitへrollbackしました: $previous" >&2
  exit 1
fi
echo "ERROR: 自動rollbackにも失敗しました。現在状態を確認してください" >&2
dogfood_report_current_deployment_state
exit 1
