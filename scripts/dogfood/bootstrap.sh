#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/load-environment.sh"
source "$SCRIPT_DIR/deployment-lib.sh"
dogfood_load_environment_settings
dogfood_require_identity

bootstrap_temporary_environment=
generated_assets=
if [ "$DOGFOOD_RESOLVED_ENV_FILE" != "$DOGFOOD_DEFAULT_ENV_FILE" ]; then
  bootstrap_temporary_environment=$DOGFOOD_RESOLVED_ENV_FILE
fi
cleanup_bootstrap() {
  if [ -n "$generated_assets" ]; then
    rm -rf -- "$generated_assets"
  fi
  if [ -n "$bootstrap_temporary_environment" ]; then
    rm -f -- "$bootstrap_temporary_environment"
  fi
}
trap cleanup_bootstrap EXIT

dogfood_require_root
dogfood_validate_deployment_storage_location
if ! getent group docker >/dev/null; then
  echo "ERROR: bootstrap前にDockerをインストールしてください" >&2
  exit 2
fi
getent group "$DOGFOOD_SERVICE_GROUP" >/dev/null || groupadd --system "$DOGFOOD_SERVICE_GROUP"
id "$DOGFOOD_SERVICE_USER" >/dev/null 2>&1 || useradd --system \
  --gid "$DOGFOOD_SERVICE_GROUP" --home-dir "$DS_DATA_DIR" \
  --shell /usr/sbin/nologin "$DOGFOOD_SERVICE_USER"
dogfood_read_revision
if id -nG "$DOGFOOD_SERVICE_USER" | tr ' ' '\n' | grep --fixed-strings --line-regexp --quiet docker; then
  gpasswd --delete "$DOGFOOD_SERVICE_USER" docker
fi

initial_clone=false
if [ -d "$DOGFOOD_CLONE_DIR/.git" ]; then
  dogfood_verify_origin
else
  initial_clone=true
  mkdir -p "$DOGFOOD_CLONE_DIR"
  git clone --no-checkout "$DOGFOOD_REPOSITORY_URL" "$DOGFOOD_CLONE_DIR"
  dogfood_fetch_and_resolve_commit "$DOGFOOD_REPOSITORY_REVISION"
  git -c core.hooksPath=/dev/null -C "$DOGFOOD_CLONE_DIR" checkout --detach \
    "$DOGFOOD_REPOSITORY_REVISION"
  dogfood_verify_detached_clean_revision "$DOGFOOD_REPOSITORY_REVISION"
fi

chown -R "root:$DOGFOOD_SERVICE_GROUP" "$DOGFOOD_CLONE_DIR"
chmod -R g-w,o-rwx "$DOGFOOD_CLONE_DIR"
chmod 0750 "$DOGFOOD_CLONE_DIR"
install -d -m 0750 -o "$DOGFOOD_SERVICE_USER" -g "$DOGFOOD_SERVICE_GROUP" \
  "$DS_DATA_DIR" "$DOGFOOD_BACKUP_DIR" "$DOGFOOD_LOG_DIR"
install -d -m 0750 -o root -g "$DOGFOOD_SERVICE_GROUP" \
  "$DOGFOOD_STATE_DIR" "$DOGFOOD_STATE_DIR/deployments"
dogfood_validate_deployment_storage
install -d -m 0750 -o root -g "$DOGFOOD_SERVICE_GROUP" "$DOGFOOD_CONFIG_DIR"
generated_assets=$(mktemp -d)
"$DOGFOOD_CLONE_DIR/scripts/dogfood/render-assets.sh" \
  "$DOGFOOD_CLONE_DIR/infra/dogfood/templates" "$generated_assets"
install -m 0640 -o root -g "$DOGFOOD_SERVICE_GROUP" \
  "$DOGFOOD_RESOLVED_ENV_FILE" "$DOGFOOD_CONFIG_DIR/dogfood.env"
install -m 0644 -o root -g "$DOGFOOD_SERVICE_GROUP" \
  "$generated_assets/start-dogfood-wsl.ps1" \
  "$DOGFOOD_CONFIG_DIR/start-dogfood-wsl.ps1"
install -m 0644 \
  "$DOGFOOD_CLONE_DIR/infra/dogfood/systemd/digital-souls-inference.target" \
  /etc/systemd/system/
install -m 0644 "$generated_assets"/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable digital-souls-inference.target
if [ "$initial_clone" = true ]; then
  echo "初期cloneをrevisionへ固定しました。"
fi
echo "bootstrapが完了しました。サービス起動はstart-services.shで明示的に実行してください。"
