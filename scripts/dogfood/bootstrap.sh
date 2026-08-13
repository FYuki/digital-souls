#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/load-environment.sh"
dogfood_load_environment
dogfood_require_identity

bootstrap_temporary_environment=
backup_clone=
generated_assets=
if [ "$DOGFOOD_RESOLVED_ENV_FILE" != "$DOGFOOD_DEFAULT_ENV_FILE" ]; then
  bootstrap_temporary_environment=$DOGFOOD_RESOLVED_ENV_FILE
fi
cleanup_bootstrap() {
  if [ -n "$generated_assets" ]; then
    rm -rf -- "$generated_assets"
  fi
  if [ -n "$backup_clone" ]; then
    rm -rf -- "$backup_clone"
  fi
  if [ -n "$bootstrap_temporary_environment" ]; then
    rm -f -- "$bootstrap_temporary_environment"
  fi
}
trap cleanup_bootstrap EXIT

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: bootstrapはroot権限で実行してください" >&2
  exit 2
fi
if ! getent group docker >/dev/null; then
  echo "ERROR: bootstrap前にDockerをインストールしてください" >&2
  exit 2
fi
getent group "$DOGFOOD_SERVICE_GROUP" >/dev/null || groupadd --system "$DOGFOOD_SERVICE_GROUP"
id "$DOGFOOD_SERVICE_USER" >/dev/null 2>&1 || useradd --system --gid "$DOGFOOD_SERVICE_GROUP" --home-dir "$DS_DATA_DIR" --shell /usr/sbin/nologin "$DOGFOOD_SERVICE_USER"
if id -nG "$DOGFOOD_SERVICE_USER" | tr ' ' '\n' | grep --fixed-strings --line-regexp --quiet docker; then
  gpasswd --delete "$DOGFOOD_SERVICE_USER" docker
fi
if [ -d "$DOGFOOD_CLONE_DIR/.git" ]; then
  current_remote=$(git -C "$DOGFOOD_CLONE_DIR" remote get-url origin)
  if [ "$current_remote" != "$DOGFOOD_REPOSITORY_URL" ]; then
    echo "ERROR: 既存cloneのoriginがDOGFOOD_REPOSITORY_URLと一致しません" >&2
    exit 2
  fi
else
  mkdir -p "$DOGFOOD_CLONE_DIR"
  git clone --no-checkout "$DOGFOOD_REPOSITORY_URL" "$DOGFOOD_CLONE_DIR"
fi
install -d -m 0750 -o "$DOGFOOD_SERVICE_USER" -g "$DOGFOOD_SERVICE_GROUP" \
  "$DOGFOOD_BACKUP_DIR"
repository_root=$(realpath --canonicalize-existing -- "$SCRIPT_DIR/../..")
resolved_sqlite_path=$(python3 - "$repository_root" <<'PYTHON'
import os
import sys
from pathlib import Path

repository_root = Path(sys.argv[1])
sys.path.insert(0, str(repository_root / "backend"))
from app.runtime_paths import resolve_runtime_paths

print(resolve_runtime_paths(os.environ, repository_root).sqlite_path)
PYTHON
)
if [ -f "$resolved_sqlite_path" ]; then
  backup_clone=$(mktemp -d)
  chown "root:$DOGFOOD_SERVICE_GROUP" "$backup_clone"
  chmod 2750 "$backup_clone"
  (
    umask 0027
    git clone --no-checkout "$DOGFOOD_REPOSITORY_URL" "$backup_clone"
  )
  git -C "$backup_clone" fetch --depth 1 origin "$DOGFOOD_REPOSITORY_REVISION"
  backup_revision=$(git -C "$backup_clone" rev-parse --verify "$DOGFOOD_REPOSITORY_REVISION^{commit}")
  if [ "$backup_revision" != "$DOGFOOD_REPOSITORY_REVISION" ]; then
    echo "ERROR: backup用cloneのcommitがDOGFOOD_REPOSITORY_REVISIONと一致しません" >&2
    exit 2
  fi
  git -c core.hooksPath=/dev/null -C "$backup_clone" checkout --detach "$DOGFOOD_REPOSITORY_REVISION"
  checked_out_backup_revision=$(git -C "$backup_clone" rev-parse HEAD)
  if [ "$checked_out_backup_revision" != "$DOGFOOD_REPOSITORY_REVISION" ]; then
    echo "ERROR: backup用cloneのcheckout後commitがDOGFOOD_REPOSITORY_REVISIONと一致しません" >&2
    exit 2
  fi
  (
    umask 0027
    "$backup_clone/scripts/setup-backend.sh"
  )
  backup_python="$backup_clone/backend/.venv/bin/python"
  backup_result=$(sudo --preserve-env=DOGFOOD_BACKUP_AUTHENTICATION_KEY \
    -u "$DOGFOOD_SERVICE_USER" env \
    DS_ENVIRONMENT_ID="$DS_ENVIRONMENT_ID" \
    DS_DATA_DIR="$DS_DATA_DIR" \
    "$backup_python" \
    "$backup_clone/environments/environment_cli.py" backup \
    --environment "$DS_ENVIRONMENT_ID" \
    --repository-root "$backup_clone" \
    --backup-root "$DOGFOOD_BACKUP_DIR" \
    --retention-count "$DOGFOOD_BACKUP_RETENTION_COUNT")
  backup_directory=$(printf '%s\n' "$backup_result" | \
    "$backup_python" -c \
    'import json, sys
payload = json.load(sys.stdin)
if set(payload) != {"status", "backupDirectory"} or payload["status"] != "ok" or not isinstance(payload["backupDirectory"], str) or not payload["backupDirectory"]:
    raise SystemExit(1)
print(payload["backupDirectory"])')
  sudo --preserve-env=DOGFOOD_BACKUP_AUTHENTICATION_KEY \
    -u "$DOGFOOD_SERVICE_USER" env \
    "$backup_python" \
    "$backup_clone/environments/environment_cli.py" backup-verify \
    --backup-directory "$backup_directory"
  rm -rf -- "$backup_clone"
  backup_clone=
fi
git -C "$DOGFOOD_CLONE_DIR" fetch --depth 1 origin "$DOGFOOD_REPOSITORY_REVISION"
resolved_revision=$(git -C "$DOGFOOD_CLONE_DIR" rev-parse --verify "$DOGFOOD_REPOSITORY_REVISION^{commit}")
if [ "$resolved_revision" != "$DOGFOOD_REPOSITORY_REVISION" ]; then
  echo "ERROR: 取得したcommitがDOGFOOD_REPOSITORY_REVISIONと一致しません" >&2
  exit 2
fi
git -c core.hooksPath=/dev/null -C "$DOGFOOD_CLONE_DIR" checkout --detach "$DOGFOOD_REPOSITORY_REVISION"
checked_out_revision=$(git -C "$DOGFOOD_CLONE_DIR" rev-parse HEAD)
if [ "$checked_out_revision" != "$DOGFOOD_REPOSITORY_REVISION" ]; then
  echo "ERROR: checkout後のcommitがDOGFOOD_REPOSITORY_REVISIONと一致しません" >&2
  exit 2
fi
if git -C "$DOGFOOD_CLONE_DIR" symbolic-ref --quiet HEAD; then
  echo "ERROR: repositoryがdetached HEADではありません" >&2
  exit 2
fi
worktree_status=$(git -C "$DOGFOOD_CLONE_DIR" status --porcelain --untracked-files=all)
if [ -n "$worktree_status" ]; then
  echo "ERROR: repositoryのworking treeが設定revisionと一致しません" >&2
  exit 2
fi
chown -R "root:$DOGFOOD_SERVICE_GROUP" "$DOGFOOD_CLONE_DIR"
chmod -R g-w,o-rwx "$DOGFOOD_CLONE_DIR"
chmod 0750 "$DOGFOOD_CLONE_DIR"
install -d -m 0750 -o "$DOGFOOD_SERVICE_USER" -g "$DOGFOOD_SERVICE_GROUP" \
  "$DS_DATA_DIR" "$DOGFOOD_STATE_DIR" "$DOGFOOD_LOG_DIR"
install -d -m 0750 -o root -g "$DOGFOOD_SERVICE_GROUP" "$DOGFOOD_CONFIG_DIR"
generated_assets=$(mktemp -d)
"$DOGFOOD_CLONE_DIR/scripts/dogfood/render-assets.sh" \
  "$DOGFOOD_CLONE_DIR/infra/dogfood/templates" "$generated_assets"
install -m 0640 -o root -g "$DOGFOOD_SERVICE_GROUP" "$DOGFOOD_RESOLVED_ENV_FILE" "$DOGFOOD_CONFIG_DIR/dogfood.env"
install -m 0644 -o root -g "$DOGFOOD_SERVICE_GROUP" "$generated_assets/start-dogfood-wsl.ps1" "$DOGFOOD_CONFIG_DIR/start-dogfood-wsl.ps1"
install -m 0644 "$DOGFOOD_CLONE_DIR/infra/dogfood/systemd/digital-souls-inference.target" /etc/systemd/system/
install -m 0644 "$generated_assets"/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable digital-souls-inference.target
echo "bootstrapが完了しました。サービス起動はstart-services.shで明示的に実行してください。"
