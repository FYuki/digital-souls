#!/usr/bin/env bash
set -euo pipefail

arguments=("$@")
target=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --commit)
      [ "$#" -ge 2 ] || { echo "ERROR: --commitにはSHAが必要です" >&2; exit 2; }
      target=$2
      shift 2
      ;;
    *)
      echo "ERROR: 未知のmigration引数です" >&2
      exit 2
      ;;
  esac
done
if [ -z "$target" ]; then
  echo "ERROR: --commitは必須です" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/load-environment.sh"
source "$SCRIPT_DIR/deployment-lib.sh"
dogfood_require_commit_sha "$target"
dogfood_load_environment_settings --with-images
dogfood_require_identity
dogfood_require_root "${arguments[@]}"
dogfood_validate_deployment_storage
dogfood_verify_origin
dogfood_require_clean_checkout
current_head=$(git -C "$DOGFOOD_CLONE_DIR" rev-parse HEAD)
dogfood_require_commit_sha "$current_head"
dogfood_read_revision
if [ "$DOGFOOD_REPOSITORY_REVISION" != "$current_head" ] \
  && [ "$DOGFOOD_REPOSITORY_REVISION" != "$target" ]; then
  echo "ERROR: dogfood.revisionが現在HEADまたはmigration targetと一致しません" >&2
  exit 2
fi
if [ "$current_head" = "$target" ]; then
  echo "ERROR: deployment contract migrationにはtargetと異なる移行元HEADが必要です" >&2
  exit 2
fi
dogfood_fetch_and_resolve_commit "$target"
dogfood_prepare_deployment_contract_migration "$current_head" "$target"
dogfood_update_revision "$target"
echo "deployment contract migrationを準備しました: $current_head -> $target"
echo "legacy manifestは検証後に保全されています。続けて同じtargetのbootstrapとdeployを実行してください。"
