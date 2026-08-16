#!/usr/bin/env bash
set -euo pipefail

deploy_arguments=("$@")
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

dogfood_report_unsafe_rollback_history() {
  echo "ERROR: rollback履歴を安全に検証できません。引数なし rollback や manifest の手編集を行わないでください" >&2
  echo "ERROR: 検証済みの保存世代から --to <SHA> で明示的に復旧してください" >&2
  echo "ERROR: 復旧手順は infra/dogfood/README.md の deployとrollback を参照してください" >&2
}

dogfood_load_environment_settings
dogfood_require_identity
dogfood_require_root "${deploy_arguments[@]}"
dogfood_validate_deployment_storage || {
  dogfood_report_unsafe_rollback_history
  exit 2
}
dogfood_verify_origin
dogfood_require_clean_checkout
current_head=$(git -C "$DOGFOOD_CLONE_DIR" rev-parse HEAD)
if [ ! -f "$DS_DATA_DIR/conversation-history.db" ]; then
  echo "ERROR: conversation-history.dbがありません。先にdigital-souls-dogfood.targetを起動してBackendの初回DB作成を完了してください" >&2
  exit 2
fi
dogfood_prepare_backend
dogfood_fetch_and_resolve_commit "$target"

previous=$current_head
current_manifest="$DOGFOOD_STATE_DIR/deployments/current.json"
if [ "$current_head" = "$target" ]; then
  if [ -e "$current_manifest" ] || [ -L "$current_manifest" ]; then
    dogfood_require_rollback_schema "$current_manifest" || {
      dogfood_report_unsafe_rollback_history
      exit 2
    }
    manifest_target=$(dogfood_manifest_field "$current_manifest" targetCommit) || {
      dogfood_report_unsafe_rollback_history
      exit 2
    }
    manifest_previous=$(dogfood_manifest_nullable_commit_field \
      "$current_manifest" previousCommit) || {
      dogfood_report_unsafe_rollback_history
      exit 2
    }
    dogfood_require_commit_sha "$manifest_target" || {
      dogfood_report_unsafe_rollback_history
      exit 2
    }
    if [ -n "$manifest_previous" ]; then
      dogfood_require_commit_sha "$manifest_previous" || {
        dogfood_report_unsafe_rollback_history
        exit 2
      }
    fi
    if [ -n "$manifest_previous" ] && [ "$manifest_previous" = "$manifest_target" ]; then
      dogfood_report_unsafe_rollback_history
      exit 2
    fi
    if [ "$manifest_target" = "$target" ]; then
      previous=$manifest_previous
    else
      previous=$manifest_target
    fi
  else
    revision_file="$DOGFOOD_CONFIG_DIR/dogfood.revision"
    if [ -e "$revision_file" ] || [ -L "$revision_file" ]; then
      dogfood_read_revision || {
        dogfood_report_unsafe_rollback_history
        exit 2
      }
      previous=$DOGFOOD_REPOSITORY_REVISION
      dogfood_require_commit_sha "$previous" || {
        dogfood_report_unsafe_rollback_history
        exit 2
      }
    else
      previous=
    fi
  fi
  if [ -n "$previous" ] && [ "$previous" = "$target" ]; then
    dogfood_report_unsafe_rollback_history
    exit 2
  fi
fi
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

if [ -z "$previous" ]; then
  echo "ERROR: 初回 deploy のため自動 rollback できない状態です。readiness 失敗の原因調査後、--to <SHA> での明示 rollback か再 deploy を行ってください" >&2
  exit 1
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
