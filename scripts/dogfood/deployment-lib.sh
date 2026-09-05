#!/usr/bin/env bash

DOGFOOD_DEPLOYMENT_RETENTION=20
DOGFOOD_MANIFEST_GENERATION_ATTEMPTS=16
DOGFOOD_DEPLOYMENT_CONTRACT_MIGRATION_SCHEMA=2

dogfood_require_root() {
  if [ "$(id -u)" -eq 0 ]; then
    return
  fi
  if [ -t 0 ] && sudo -n true 2>/dev/null; then
    echo "ERROR: dogfood配備操作はroot権限で実行してください" >&2
    return 2
  fi
  printf 'ERROR: root操作をユーザーへ引き渡します。次を実行してください:\n  sudo env DOGFOOD_ENV_FILE=%q WSL_DISTRO_NAME=%q %q' \
    "$DOGFOOD_RESOLVED_ENV_FILE" "$DOGFOOD_RESOLVED_WSL_DISTRO" "$0" >&2
  if [ "$#" -gt 0 ]; then
    printf ' %q' "$@" >&2
  fi
  printf '\n' >&2
  return 3
}

dogfood_require_commit_sha() {
  if ! [[ "$1" =~ ^[0-9a-f]{40}$ ]]; then
    echo "ERROR: commitは完全な小文字SHAで指定してください" >&2
    return 2
  fi
}

dogfood_verify_bootstrap_container_prerequisites() {
  local key image expected_digest observed_digest
  local runtimes
  if ! command -v nvidia-ctk >/dev/null 2>&1; then
    echo "ERROR: bootstrap前にNVIDIA Container Toolkitをインストールしてください" >&2
    return 2
  fi
  runtimes=$(docker info --format '{{json .Runtimes}}') || {
    echo "ERROR: Docker runtime情報を取得できません" >&2
    return 2
  }
  if ! python3 -c '
import json
import sys

try:
    runtimes = json.load(sys.stdin)
except (json.JSONDecodeError, TypeError):
    raise SystemExit(1)
if not isinstance(runtimes, dict) or "nvidia" not in runtimes:
    raise SystemExit(1)
' <<< "$runtimes"; then
    echo "ERROR: Dockerにnvidia runtimeが登録されていません。nvidia-ctk runtime configure後にDockerを再起動してください" >&2
    return 2
  fi
  for key in "${DOGFOOD_IMAGE_KEYS[@]}"; do
    image=${!key}
    expected_digest=${image##*@}
    observed_digest=$(docker buildx imagetools inspect "$image" \
      --format '{{.Manifest.Digest}}') || {
      echo "ERROR: $key をGHCRから解決できません。rootでdocker login ghcr.ioを確認してください" >&2
      return 2
    }
    if [ "$observed_digest" != "$expected_digest" ]; then
      echo "ERROR: $key の取得結果が設定済みimmutable digestと一致しません" >&2
      return 2
    fi
  done
  if ! docker run --rm --runtime=nvidia --gpus all \
    --entrypoint nvidia-smi "$DOGFOOD_WHISPER_IMAGE" -L >/dev/null; then
    echo "ERROR: immutable Whisper imageからNVIDIA GPUを利用できません" >&2
    return 2
  fi
}

dogfood_verify_origin() {
  local current_remote
  current_remote=$(git -C "$DOGFOOD_CLONE_DIR" remote get-url origin)
  if [ "$current_remote" != "$DOGFOOD_REPOSITORY_URL" ]; then
    echo "ERROR: cloneのoriginがDOGFOOD_REPOSITORY_URLと一致しません" >&2
    return 2
  fi
}

dogfood_require_clean_checkout() {
  local worktree_status
  worktree_status=$(git -C "$DOGFOOD_CLONE_DIR" status --porcelain --untracked-files=all)
  if [ -n "$worktree_status" ]; then
    echo "ERROR: repositoryのworking treeに変更があります" >&2
    printf '%s\n' "$worktree_status" >&2
    return 2
  fi
}

dogfood_require_detached_clean_checkout_for_convergence() {
  local target=$1
  local current_head worktree_status
  current_head=$(git -C "$DOGFOOD_CLONE_DIR" rev-parse HEAD)
  worktree_status=$(git -C "$DOGFOOD_CLONE_DIR" status --porcelain --untracked-files=all)
  if [ -n "$worktree_status" ]; then
    echo "ERROR: 既存cloneのworking treeに変更があるためrevisionへ収束できません" >&2
    printf '%s\n' "$worktree_status" >&2
    printf '現在のHEAD: %s\n期待するrevision: %s\n' "$current_head" "$target" >&2
    return 2
  fi
  if git -C "$DOGFOOD_CLONE_DIR" symbolic-ref --quiet HEAD >/dev/null; then
    echo "ERROR: 既存cloneがdetached HEADではないためrevisionへ収束できません" >&2
    printf '現在のHEAD: %s\n期待するrevision: %s\n' "$current_head" "$target" >&2
    return 2
  fi
}

dogfood_report_current_deployment_state() {
  local current_revision current_head
  if dogfood_read_revision; then
    current_revision=$DOGFOOD_REPOSITORY_REVISION
    echo "現在のrevision: $current_revision" >&2
  else
    echo "現在のrevision: 取得不能" >&2
  fi
  if current_head=$(git -C "$DOGFOOD_CLONE_DIR" rev-parse HEAD); then
    echo "現在のHEAD: $current_head" >&2
  else
    echo "現在のHEAD: 取得不能" >&2
  fi
}

dogfood_fetch_and_resolve_commit() {
  local target=$1
  local resolved shallow
  dogfood_require_commit_sha "$target"
  shallow=$(git -C "$DOGFOOD_CLONE_DIR" rev-parse --is-shallow-repository)
  if [ "$shallow" = true ]; then
    git -C "$DOGFOOD_CLONE_DIR" fetch --unshallow origin main
  fi
  git -C "$DOGFOOD_CLONE_DIR" fetch origin "$target"
  git -C "$DOGFOOD_CLONE_DIR" fetch origin main
  resolved=$(git -C "$DOGFOOD_CLONE_DIR" rev-parse --verify "$target^{commit}")
  if [ "$resolved" != "$target" ]; then
    echo "ERROR: originから解決したcommitが指定SHAと一致しません" >&2
    return 2
  fi
  if ! git -C "$DOGFOOD_CLONE_DIR" merge-base --is-ancestor "$target" origin/main; then
    echo "ERROR: 指定commitはorigin/main上に存在しません" >&2
    return 2
  fi
}

dogfood_verify_detached_clean_revision() {
  local target=$1
  local checked_out_revision
  checked_out_revision=$(git -C "$DOGFOOD_CLONE_DIR" rev-parse HEAD)
  if [ "$checked_out_revision" != "$target" ]; then
    echo "ERROR: checkout後のcommitが指定SHAと一致しません" >&2
    return 2
  fi
  if git -C "$DOGFOOD_CLONE_DIR" symbolic-ref --quiet HEAD; then
    echo "ERROR: repositoryがdetached HEADではありません" >&2
    return 2
  fi
  dogfood_require_clean_checkout
}

dogfood_converge_service_git_trust() (
  local service_gitconfig="$DOGFOOD_SERVICE_HOME_DIR/.gitconfig"
  local normalized_clone staging_dir staged_gitconfig effective_safe_directories
  local git_config_status origin _value
  normalized_clone=$(realpath -- "$DOGFOOD_CLONE_DIR") || return

  if [ -L "$DOGFOOD_SERVICE_HOME_DIR" ] || [ ! -d "$DOGFOOD_SERVICE_HOME_DIR" ]; then
    echo "ERROR: service userのhomeが通常ディレクトリではありません" >&2
    return 2
  fi
  if [ -L "$service_gitconfig" ]; then
    echo "ERROR: service userの.gitconfigにsymlinkは使用できません" >&2
    return 2
  fi
  if [ -e "$service_gitconfig" ] && [ ! -f "$service_gitconfig" ]; then
    echo "ERROR: service userの.gitconfigが通常ファイルではありません" >&2
    return 2
  fi

  staging_dir=$(mktemp -d "$(dirname -- "$DOGFOOD_SERVICE_HOME_DIR")/.gitconfig.tmp.XXXXXX") \
    || return
  staged_gitconfig="$staging_dir/config"
  effective_safe_directories="$staging_dir/effective-safe-directories"
  trap 'rm -f -- "$staged_gitconfig" "$staged_gitconfig.lock" "$effective_safe_directories"; rmdir -- "$staging_dir"' EXIT

  if [ -e "$service_gitconfig" ]; then
    cp --no-dereference -- "$service_gitconfig" "$staged_gitconfig" || return
    if [ -L "$staged_gitconfig" ] || [ ! -f "$staged_gitconfig" ]; then
      echo "ERROR: service userの.gitconfigが通常ファイルではありません" >&2
      return 2
    fi
  else
    : > "$staged_gitconfig"
  fi

  HOME="$DOGFOOD_SERVICE_HOME_DIR" git config --file "$staged_gitconfig" \
    --replace-all safe.directory "$normalized_clone" || return

  if [ -L "$DOGFOOD_SERVICE_HOME_DIR" ] || [ ! -d "$DOGFOOD_SERVICE_HOME_DIR" ]; then
    echo "ERROR: service userのhomeが処理中に通常ディレクトリ以外へ変更されました" >&2
    return 2
  fi
  if [ -e "$service_gitconfig" ]; then
    if HOME="$DOGFOOD_SERVICE_HOME_DIR" \
      GIT_CONFIG_GLOBAL="$service_gitconfig" \
      git -C "$normalized_clone" config --global --includes --show-origin --null \
        --get-all safe.directory > "$effective_safe_directories"; then
      git_config_status=0
    else
      git_config_status=$?
    fi
    if [ "$git_config_status" -ne 0 ] && [ "$git_config_status" -ne 1 ]; then
      return "$git_config_status"
    fi
    while IFS= read -r -d '' origin && IFS= read -r -d '' _value; do
      if [ "$origin" != "file:$service_gitconfig" ]; then
        echo "ERROR: include経由のsafe.directoryは使用できません" >&2
        return 2
      fi
    done < "$effective_safe_directories"
  fi

  chown "$DOGFOOD_SERVICE_USER:$DOGFOOD_SERVICE_GROUP" "$staged_gitconfig" || return
  chmod 0640 "$staged_gitconfig" || return

  if [ -L "$DOGFOOD_SERVICE_HOME_DIR" ] || [ ! -d "$DOGFOOD_SERVICE_HOME_DIR" ]; then
    echo "ERROR: service userのhomeが処理中に通常ディレクトリ以外へ変更されました" >&2
    return 2
  fi
  if [ -L "$service_gitconfig" ]; then
    echo "ERROR: service userの.gitconfigが処理中にsymlinkへ変更されました" >&2
    return 2
  fi
  python3 - "$staged_gitconfig" "$DOGFOOD_SERVICE_HOME_DIR" <<'PYTHON'
import os
import stat
import subprocess
import sys

staged_gitconfig, service_home = sys.argv[1:]
service_home_fd = os.open(
    service_home,
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
)
try:
    opened_home = os.fstat(service_home_fd)
    current_home = os.stat(service_home, follow_symlinks=False)
    opened_identity = (opened_home.st_dev, opened_home.st_ino)
    current_identity = (current_home.st_dev, current_home.st_ino)
    if not stat.S_ISDIR(current_home.st_mode) or current_identity != opened_identity:
        raise SystemExit(2)

    destination = f"/proc/self/fd/{service_home_fd}/.gitconfig"
    placement = subprocess.run(
        ["mv", "-T", "--", staged_gitconfig, destination],
        pass_fds=(service_home_fd,),
        check=False,
    )
    if placement.returncode != 0:
        raise SystemExit(placement.returncode)

    current_home = os.stat(service_home, follow_symlinks=False)
    current_identity = (current_home.st_dev, current_home.st_ino)
    if not stat.S_ISDIR(current_home.st_mode) or current_identity != opened_identity:
        raise SystemExit(2)
finally:
    os.close(service_home_fd)
PYTHON
)

dogfood_update_revision() {
  local target=$1
  local temporary prepared
  temporary=$(mktemp "$DOGFOOD_CONFIG_DIR/.dogfood.revision.XXXXXX")
  prepared=$(mktemp "$DOGFOOD_CONFIG_DIR/.dogfood.revision.ready.XXXXXX")
  printf '%s\n' "$target" > "$temporary"
  install -m 0640 -o root -g "$DOGFOOD_SERVICE_GROUP" \
    "$temporary" "$prepared"
  rm -f -- "$temporary"
  mv -T -- "$prepared" "$DOGFOOD_CONFIG_DIR/dogfood.revision"
  export DOGFOOD_REPOSITORY_REVISION="$target"
}

dogfood_prepare_backend() {
  (
    umask 0027
    "$DOGFOOD_CLONE_DIR/scripts/setup-backend.sh"
  )
}

dogfood_resolve_target_images() {
  local target=$1
  local key current repository digest resolved variable
  dogfood_require_commit_sha "$target" || return
  for key in "${DOGFOOD_IMAGE_KEYS[@]}"; do
    current=${!key}
    repository=${current%@sha256:*}
    if [ "$repository" = "$current" ]; then
      echo "ERROR: $key からGHCR repositoryを解決できません" >&2
      return 2
    fi
    digest=$(docker buildx imagetools inspect "$repository:$target" \
      --format '{{.Manifest.Digest}}') || {
      echo "ERROR: $key のtarget imageを解決できません: $target" >&2
      return 2
    }
    if ! [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
      echo "ERROR: $key のmanifest digestが不正です" >&2
      return 2
    fi
    resolved="$repository@$digest"
    variable="DOGFOOD_TARGET_${key#DOGFOOD_}"
    printf -v "$variable" '%s' "$resolved"
    export "$variable"
  done
}

dogfood_write_active_images() (
  set -e
  local backend_image=$1 frontend_image=$2 whisper_image=$3
  local destination="$DOGFOOD_CONFIG_DIR/dogfood-images.env"
  local temporary prepared
  DOGFOOD_BACKEND_IMAGE=$backend_image
  DOGFOOD_FRONTEND_IMAGE=$frontend_image
  DOGFOOD_WHISPER_IMAGE=$whisper_image
  dogfood_validate_images
  temporary=$(mktemp "$DOGFOOD_CONFIG_DIR/.dogfood-images.XXXXXX")
  prepared=$(mktemp "$DOGFOOD_CONFIG_DIR/.dogfood-images.ready.XXXXXX")
  trap 'rm -f -- "$temporary" "$prepared"' EXIT
  printf 'DOGFOOD_BACKEND_IMAGE=%s\nDOGFOOD_FRONTEND_IMAGE=%s\nDOGFOOD_WHISPER_IMAGE=%s\n' \
    "$backend_image" "$frontend_image" "$whisper_image" > "$temporary"
  install -m 0600 -o root -g root "$temporary" "$prepared"
  mv -T -- "$prepared" "$destination"
  prepared=
)

dogfood_archive_pre_migration_manifests() {
  local from_commit=$1
  local target_commit=$2
  python3 - "$DOGFOOD_STATE_DIR/deployments" "$from_commit" "$target_commit" <<'PYTHON'
import json
import os
import re
import stat
import sys
from pathlib import Path

deployments = Path(sys.argv[1])
from_commit, target_commit = sys.argv[2:]
commit_pattern = re.compile(r"^[0-9a-f]{40}$")
if commit_pattern.fullmatch(from_commit) is None or commit_pattern.fullmatch(target_commit) is None:
    raise SystemExit(2)
archive_name = f"pre-migration-{from_commit[:12]}-to-{target_commit[:12]}"
archive = deployments / archive_name
expected_owner = os.geteuid()

legacy_keys = {
    "previousCommit",
    "targetCommit",
    "profileSchemaVersion",
    "dataSchemaVersion",
    "backupId",
    "deployedAt",
}
current_keys = legacy_keys | {"images"}
image_keys = {"backend", "frontend", "whisper"}
image_pattern = re.compile(
    r"^ghcr\.io/[a-z0-9](?:[a-z0-9._-]*/)+"
    r"[a-z0-9][a-z0-9._-]*@sha256:[0-9a-f]{64}$"
)

def read_manifest(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        status = os.fstat(descriptor)
        path_status = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != expected_owner
            or stat.S_IMODE(status.st_mode) != 0o640
            or (status.st_dev, status.st_ino) != (path_status.st_dev, path_status.st_ino)
        ):
            raise OSError
        with os.fdopen(descriptor, "rb") as source:
            contents = source.read()
        descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        payload = json.loads(contents)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise OSError from None
    if not isinstance(payload, dict) or set(payload) not in (legacy_keys, current_keys):
        raise OSError
    previous = payload["previousCommit"]
    target = payload["targetCommit"]
    if previous is not None and (
        not isinstance(previous, str) or commit_pattern.fullmatch(previous) is None
    ):
        raise OSError
    if not isinstance(target, str) or commit_pattern.fullmatch(target) is None:
        raise OSError
    if previous is not None and previous == target:
        raise OSError
    for field in ("profileSchemaVersion", "dataSchemaVersion"):
        value = payload[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise OSError
    for field in ("backupId", "deployedAt"):
        if not isinstance(payload[field], str) or not payload[field]:
            raise OSError
    if set(payload) == current_keys:
        images = payload["images"]
        if not isinstance(images, dict) or set(images) != image_keys:
            raise OSError
        if any(
            not isinstance(image, str) or image_pattern.fullmatch(image) is None
            for image in images.values()
        ):
            raise OSError
    return contents

sources = sorted(deployments.glob("*.json"))
validated_sources = []
for source in sources:
    try:
        validated_sources.append((source, read_manifest(source)))
    except OSError:
        print(
            f"ERROR: deployment manifestを検証できません: {source.name}",
            file=sys.stderr,
        )
        raise SystemExit(2) from None

if archive.exists():
    archive_status = archive.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(archive_status.st_mode)
        or archive_status.st_uid != expected_owner
        or stat.S_IMODE(archive_status.st_mode) != 0o750
    ):
        print("ERROR: deployment archiveが安全なdirectoryではありません", file=sys.stderr)
        raise SystemExit(2)
else:
    archive.mkdir(mode=0o750)

for source, contents in validated_sources:
    destination = archive / source.name
    if destination.exists():
        try:
            archived_contents = read_manifest(destination)
        except OSError:
            print(
                f"ERROR: 既存deployment archiveを検証できません: {source.name}",
                file=sys.stderr,
            )
            raise SystemExit(2) from None
        if archived_contents != contents:
            print(
                f"ERROR: deployment archiveの同名manifestが一致しません: {source.name}",
                file=sys.stderr,
            )
            raise SystemExit(2)
        source.unlink()
    else:
        os.replace(source, destination)

for archived in archive.glob("*.json"):
    if archived.name == "migration.json":
        continue
    try:
        read_manifest(archived)
    except OSError:
        print(
            f"ERROR: deployment archiveを検証できません: {archived.name}",
            file=sys.stderr,
        )
        raise SystemExit(2) from None

print(archive_name)
PYTHON
}

dogfood_write_deployment_contract_migration() (
  set -e
  local from_commit=$1
  local target_commit=$2
  local archive_name=$3
  local marker="$DOGFOOD_STATE_DIR/deployment-contract-migration.json"
  local temporary prepared
  temporary=$(mktemp "$DOGFOOD_STATE_DIR/.deployment-contract-migration.XXXXXX")
  prepared=$(mktemp "$DOGFOOD_STATE_DIR/.deployment-contract-migration.ready.XXXXXX")
  trap 'rm -f -- "$temporary" "$prepared"' EXIT
  python3 - "$from_commit" "$target_commit" "$archive_name" \
    "$DOGFOOD_DEPLOYMENT_CONTRACT_MIGRATION_SCHEMA" > "$temporary" <<'PYTHON'
import json
import sys
from datetime import datetime, timezone

from_commit, target_commit, archive_name, schema = sys.argv[1:]
print(json.dumps({
    "schemaVersion": int(schema),
    "fromCommit": from_commit,
    "targetCommit": target_commit,
    "archiveDirectory": archive_name,
    "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
}, separators=(",", ":")))
PYTHON
  install -m 0640 -o root -g "$DOGFOOD_SERVICE_GROUP" "$temporary" "$prepared"
  mv -T -- "$prepared" "$marker"
  prepared=
)

dogfood_read_deployment_contract_migration() {
  local marker="$DOGFOOD_STATE_DIR/deployment-contract-migration.json"
  local output
  local -a migration_fields
  output=$(python3 - "$marker" "$DOGFOOD_STATE_DIR/deployments" \
    "$DOGFOOD_DEPLOYMENT_CONTRACT_MIGRATION_SCHEMA" <<'PYTHON'
import json
import os
import re
import stat
import sys
from pathlib import Path

marker = Path(sys.argv[1])
deployments = Path(sys.argv[2])
expected_schema = int(sys.argv[3])
pattern = re.compile(r"^[0-9a-f]{40}$")
try:
    descriptor = os.open(marker, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    status = os.fstat(descriptor)
    marker_status = marker.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != 0o640
        or (status.st_dev, status.st_ino) != (marker_status.st_dev, marker_status.st_ino)
    ):
        raise OSError
    with os.fdopen(descriptor, encoding="utf-8") as source:
        payload = json.load(source)
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
if set(payload) != {
    "schemaVersion", "fromCommit", "targetCommit", "archiveDirectory", "createdAt"
}:
    raise SystemExit(1)
schema = payload["schemaVersion"]
if isinstance(schema, bool) or schema not in (1, expected_schema):
    raise SystemExit(1)
for field in ("fromCommit", "targetCommit"):
    if not isinstance(payload[field], str) or pattern.fullmatch(payload[field]) is None:
        raise SystemExit(1)
if payload["fromCommit"] == payload["targetCommit"]:
    raise SystemExit(1)
archive_name = payload["archiveDirectory"]
if (
    not isinstance(archive_name, str)
    or not archive_name
    or archive_name != Path(archive_name).name
):
    raise SystemExit(1)
expected_archive_name = (
    f"legacy-v0-{payload['fromCommit'][:12]}-to-{payload['targetCommit'][:12]}"
    if schema == 1
    else f"pre-migration-{payload['fromCommit'][:12]}-to-{payload['targetCommit'][:12]}"
)
if archive_name != expected_archive_name:
    raise SystemExit(1)
if not isinstance(payload["createdAt"], str) or not payload["createdAt"]:
    raise SystemExit(1)
archive = deployments / archive_name
try:
    archive_status = archive.stat(follow_symlinks=False)
except OSError:
    raise SystemExit(1)
if (
    not stat.S_ISDIR(archive_status.st_mode)
    or archive_status.st_uid != os.geteuid()
    or stat.S_IMODE(archive_status.st_mode) != 0o750
):
    raise SystemExit(1)
print(payload["fromCommit"])
print(payload["targetCommit"])
print(archive_name)
PYTHON
) || {
    echo "ERROR: deployment contract migration markerを検証できません" >&2
    return 2
  }
  mapfile -t migration_fields <<< "$output"
  if [ "${#migration_fields[@]}" -ne 3 ]; then
    echo "ERROR: deployment contract migration markerのfield数が不正です" >&2
    return 2
  fi
  export DOGFOOD_MIGRATION_FROM_COMMIT=${migration_fields[0]}
  export DOGFOOD_MIGRATION_TARGET_COMMIT=${migration_fields[1]}
  export DOGFOOD_MIGRATION_ARCHIVE_DIRECTORY=${migration_fields[2]}
}

dogfood_prepare_deployment_contract_migration() {
  local from_commit=$1
  local target_commit=$2
  local marker="$DOGFOOD_STATE_DIR/deployment-contract-migration.json"
  local archive_name
  if [ -e "$marker" ] || [ -L "$marker" ]; then
    dogfood_read_deployment_contract_migration || return
    if [ "$DOGFOOD_MIGRATION_FROM_COMMIT" != "$from_commit" ] \
      || [ "$DOGFOOD_MIGRATION_TARGET_COMMIT" != "$target_commit" ]; then
      echo "ERROR: 既存deployment contract migrationが指定commitと一致しません" >&2
      return 2
    fi
    return
  fi
  archive_name=$(dogfood_archive_pre_migration_manifests \
    "$from_commit" "$target_commit") || return
  dogfood_write_deployment_contract_migration \
    "$from_commit" "$target_commit" "$archive_name"
}

dogfood_complete_deployment_contract_migration() {
  local target_commit=$1
  local marker="$DOGFOOD_STATE_DIR/deployment-contract-migration.json"
  local destination
  dogfood_read_deployment_contract_migration || return
  if [ "$DOGFOOD_MIGRATION_TARGET_COMMIT" != "$target_commit" ]; then
    echo "ERROR: 完了対象とdeployment contract migrationのtargetが一致しません" >&2
    return 2
  fi
  destination="$DOGFOOD_STATE_DIR/deployments/$DOGFOOD_MIGRATION_ARCHIVE_DIRECTORY/migration.json"
  if [ -e "$destination" ] || [ -L "$destination" ]; then
    echo "ERROR: deployment archiveのmigration記録が既に存在します" >&2
    return 2
  fi
  mv -T -- "$marker" "$destination"
}

dogfood_activate_revision() {
  local target=$1
  local backend_image=$2
  local frontend_image=$3
  local whisper_image=$4
  dogfood_update_revision "$target" || return
  git -c core.hooksPath=/dev/null -C "$DOGFOOD_CLONE_DIR" checkout --detach "$target" || return
  dogfood_verify_detached_clean_revision "$target" || return
  dogfood_prepare_backend || return
  dogfood_require_clean_checkout || return
  chown -R "root:$DOGFOOD_SERVICE_GROUP" "$DOGFOOD_CLONE_DIR" || return
  chmod -R g-w,o-rwx "$DOGFOOD_CLONE_DIR" || return
  dogfood_write_active_images \
    "$backend_image" "$frontend_image" "$whisper_image" || return
  "$DOGFOOD_CLONE_DIR/scripts/dogfood/restart-services.sh" || return
}

dogfood_check_readiness() {
  "$DOGFOOD_CLONE_DIR/backend/.venv/bin/python" \
    "$DOGFOOD_CLONE_DIR/environments/environment_cli.py" wait-readiness \
    --profile "$DS_ENVIRONMENT_ID" \
    --service frontend \
    --service backend \
    --max-attempts 180 \
    --interval-seconds 1 \
    --request-timeout-seconds 2
}

dogfood_backup() {
  local python="$DOGFOOD_CLONE_DIR/backend/.venv/bin/python"
  local cli="$DOGFOOD_CLONE_DIR/environments/environment_cli.py"
  local backup_result backup_directory
  backup_result=$(sudo --preserve-env=DOGFOOD_BACKUP_AUTHENTICATION_KEY \
    -u "$DOGFOOD_SERVICE_USER" env \
    HOME="$DOGFOOD_SERVICE_HOME_DIR" \
    GIT_CONFIG_GLOBAL="$DOGFOOD_SERVICE_HOME_DIR/.gitconfig" \
    DS_ENVIRONMENT_ID="$DS_ENVIRONMENT_ID" \
    DS_DATA_DIR="$DS_DATA_DIR" \
    "$python" "$cli" backup \
    --environment "$DS_ENVIRONMENT_ID" \
    --repository-root "$DOGFOOD_CLONE_DIR" \
    --backup-root "$DOGFOOD_BACKUP_DIR" \
    --retention-count "$DOGFOOD_BACKUP_RETENTION_COUNT") || return
  backup_directory=$(printf '%s\n' "$backup_result" | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
if (
    set(payload) != {"status", "backupDirectory"}
    or payload["status"] != "ok"
    or not isinstance(payload["backupDirectory"], str)
    or not payload["backupDirectory"]
):
    raise SystemExit(1)
print(payload["backupDirectory"])
') || return
  sudo --preserve-env=DOGFOOD_BACKUP_AUTHENTICATION_KEY \
    -u "$DOGFOOD_SERVICE_USER" env \
    HOME="$DOGFOOD_SERVICE_HOME_DIR" \
    GIT_CONFIG_GLOBAL="$DOGFOOD_SERVICE_HOME_DIR/.gitconfig" \
    "$python" "$cli" backup-verify --backup-directory "$backup_directory" || return
  printf '%s\n' "$backup_directory"
}

dogfood_manifest_metadata() {
  local previous=$1
  local target=$2
  local backup_id=$3
  local backend_image=$4
  local frontend_image=$5
  local whisper_image=$6
  python3 - "$DOGFOOD_CLONE_DIR/environments/profiles/dogfood.json" \
    "$DS_DATA_DIR/conversation-history.db" "$previous" "$target" "$backup_id" \
    "$backend_image" "$frontend_image" "$whisper_image" <<'PYTHON'
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

(
    profile_path,
    database_path,
    previous,
    target,
    backup_id,
    backend_image,
    frontend_image,
    whisper_image,
) = sys.argv[1:]
profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
    data_schema = connection.execute("PRAGMA user_version").fetchone()[0]
print(json.dumps({
    "previousCommit": previous if previous else None,
    "targetCommit": target,
    "profileSchemaVersion": profile["schemaVersion"],
    "dataSchemaVersion": data_schema,
    "backupId": backup_id,
    "images": {
        "backend": backend_image,
        "frontend": frontend_image,
        "whisper": whisper_image,
    },
    "deployedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
}, separators=(",", ":")))
PYTHON
}

dogfood_write_manifest() (
  set -e
  local manifest=$1
  local target=$2
  local deployments="$DOGFOOD_STATE_DIR/deployments"
  local timestamp generation suffix
  local temporary= prepared= current_temporary= current_prepared=
  local attempt generation_created=false
  cleanup_manifest_temporaries() {
    [ -z "$temporary" ] || rm -f -- "$temporary"
    [ -z "$prepared" ] || rm -f -- "$prepared"
    [ -z "$current_temporary" ] || rm -f -- "$current_temporary"
    [ -z "$current_prepared" ] || rm -f -- "$current_prepared"
  }
  trap cleanup_manifest_temporaries EXIT
  dogfood_validate_deployment_storage
  timestamp=$(python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"))')
  temporary=$(mktemp "$deployments/.manifest.XXXXXX")
  printf '%s\n' "$manifest" > "$temporary"
  prepared=$(mktemp "$deployments/.manifest.ready.XXXXXX")
  install -m 0640 -o root -g "$DOGFOOD_SERVICE_GROUP" "$temporary" "$prepared"
  rm -f -- "$temporary"
  temporary=
  for ((attempt = 1; attempt <= DOGFOOD_MANIFEST_GENERATION_ATTEMPTS; attempt++)); do
    suffix=$(python3 -c 'import secrets; print(secrets.token_hex(6))')
    generation="$deployments/$timestamp-${target:0:12}-$suffix.json"
    if ln -- "$prepared" "$generation" 2>/dev/null; then
      generation_created=true
      break
    fi
  done
  if [ "$generation_created" != true ]; then
    echo "ERROR: deployment manifest世代を$DOGFOOD_MANIFEST_GENERATION_ATTEMPTS回試行しても作成できません" >&2
    return 1
  fi
  rm -f -- "$prepared"
  prepared=
  current_temporary=$(mktemp "$deployments/.current.XXXXXX")
  printf '%s\n' "$manifest" > "$current_temporary"
  current_prepared=$(mktemp "$deployments/.current.ready.XXXXXX")
  install -m 0640 -o root -g "$DOGFOOD_SERVICE_GROUP" \
    "$current_temporary" "$current_prepared"
  rm -f -- "$current_temporary"
  current_temporary=
  mv -T -- "$current_prepared" "$deployments/current.json"
  current_prepared=
  mapfile -t generations < <(find "$deployments" -maxdepth 1 -type f -name '*.json' ! -name current.json -printf '%f\n' | sort)
  while [ "${#generations[@]}" -gt "$DOGFOOD_DEPLOYMENT_RETENTION" ]; do
    rm -f -- "$deployments/${generations[0]}"
    generations=("${generations[@]:1}")
  done
)

dogfood_validate_deployment_storage_path() {
  local missing_policy=$1
  python3 - "$DOGFOOD_STATE_DIR" "$DOGFOOD_STATE_DIR/deployments" "$EUID" "$missing_policy" <<'PYTHON'
import os
import stat
import sys

state_dir, deployments_dir, expected_owner, missing_policy = sys.argv[1:]
expected_owner = int(expected_owner)
allow_missing = missing_policy == "allow"
root_owner = os.stat(os.path.sep, follow_symlinks=False).st_uid
if missing_policy not in {"allow", "reject"}:
    raise SystemExit(2)

for configured_path in (state_dir, deployments_dir):
    if not os.path.isabs(configured_path) or os.path.normpath(configured_path) != configured_path:
        print("ERROR: deployment manifest保存先は正規化済み絶対pathで指定してください", file=sys.stderr)
        raise SystemExit(2)
    current = os.path.sep
    missing = False
    for component in configured_path.split(os.path.sep)[1:]:
        current = os.path.join(current, component)
        if missing:
            continue
        try:
            status = os.stat(current, follow_symlinks=False)
        except FileNotFoundError:
            if allow_missing:
                missing = True
                continue
            print("ERROR: deployment manifest保存先を安全に参照できません", file=sys.stderr)
            raise SystemExit(2)
        except OSError:
            print("ERROR: deployment manifest保存先を安全に参照できません", file=sys.stderr)
            raise SystemExit(2)
        mode = stat.S_IMODE(status.st_mode)
        sticky_root_directory = status.st_uid == root_owner and mode & stat.S_ISVTX
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid not in {root_owner, expected_owner}
            or (mode & 0o022 and not sticky_root_directory)
        ):
            print("ERROR: deployment manifest保存先のpath要素が安全ではありません", file=sys.stderr)
            raise SystemExit(2)

for path in (state_dir, deployments_dir):
    try:
        status = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        if allow_missing:
            continue
        print("ERROR: deployment manifest保存先を安全に参照できません", file=sys.stderr)
        raise SystemExit(2)
    except OSError:
        print("ERROR: deployment manifest保存先を安全に参照できません", file=sys.stderr)
        raise SystemExit(2)
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != expected_owner
        or stat.S_IMODE(status.st_mode) != 0o750
    ):
        print("ERROR: deployment manifest保存先の所有者または権限が不正です", file=sys.stderr)
        raise SystemExit(2)
PYTHON
}

dogfood_validate_deployment_storage_location() {
  dogfood_validate_deployment_storage_path allow
}

dogfood_validate_deployment_storage() {
  dogfood_validate_deployment_storage_path reject || return
  python3 - "$DOGFOOD_STATE_DIR/deployments" "$EUID" <<'PYTHON'
import os
import stat
import sys

deployments_dir, expected_owner = sys.argv[1:]
try:
    entries = tuple(os.scandir(deployments_dir))
except OSError:
    print("ERROR: deployment manifest保存先を走査できません", file=sys.stderr)
    raise SystemExit(2)
for entry in entries:
    if not entry.name.endswith(".json"):
        continue
    status = entry.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != int(expected_owner)
        or stat.S_IMODE(status.st_mode) != 0o640
    ):
        print("ERROR: deployment manifestが安全な通常ファイルではありません", file=sys.stderr)
        raise SystemExit(2)
PYTHON
}

_dogfood_manifest_string_field() {
  local manifest=$1
  local field=$2
  local null_policy=$3
  python3 - "$manifest" "$field" "$null_policy" <<'PYTHON'
import json
import os
import stat
import sys

path, field, null_policy = sys.argv[1:]
if null_policy not in {"allow", "reject"}:
    raise SystemExit(2)
try:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    status = os.fstat(descriptor)
    path_status = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != 0o640
        or (status.st_dev, status.st_ino) != (path_status.st_dev, path_status.st_ino)
    ):
        raise OSError
    with os.fdopen(descriptor, encoding="utf-8") as source:
        value = json.load(source)[field]
except (OSError, json.JSONDecodeError, KeyError):
    raise SystemExit(1)
if value is None and null_policy == "allow":
    raise SystemExit(0)
if not isinstance(value, str) or not value:
    raise SystemExit(1)
print(value)
PYTHON
}

dogfood_manifest_field() {
  _dogfood_manifest_string_field "$1" "$2" reject
}

dogfood_manifest_nullable_commit_field() {
  _dogfood_manifest_string_field "$1" "$2" allow
}

dogfood_manifest_images() {
  local manifest=$1
  python3 - "$manifest" <<'PYTHON'
import json
import os
import re
import stat
import sys

path = sys.argv[1]
pattern = re.compile(
    r"^ghcr\.io/[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$"
)
try:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    status = os.fstat(descriptor)
    path_status = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != 0o640
        or (status.st_dev, status.st_ino) != (path_status.st_dev, path_status.st_ino)
    ):
        raise OSError
    with os.fdopen(descriptor, encoding="utf-8") as source:
        images = json.load(source)["images"]
except (OSError, json.JSONDecodeError, KeyError):
    raise SystemExit(1)
if not isinstance(images, dict) or set(images) != {"backend", "frontend", "whisper"}:
    raise SystemExit(1)
for key in ("backend", "frontend", "whisper"):
    value = images[key]
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise SystemExit(1)
    print(value)
PYTHON
}

dogfood_read_manifest_images() {
  local manifest=$1
  local output
  local -a images
  output=$(dogfood_manifest_images "$manifest") || {
    echo "ERROR: deployment manifestのimage digestを検証できません" >&2
    return 2
  }
  mapfile -t images <<< "$output"
  if [ "${#images[@]}" -ne 3 ]; then
    echo "ERROR: deployment manifestのimage digest数が不正です" >&2
    return 2
  fi
  export DOGFOOD_MANIFEST_BACKEND_IMAGE=${images[0]}
  export DOGFOOD_MANIFEST_FRONTEND_IMAGE=${images[1]}
  export DOGFOOD_MANIFEST_WHISPER_IMAGE=${images[2]}
}

dogfood_manifest_schema_version() {
  local manifest=$1
  python3 - "$manifest" <<'PYTHON'
import json
import os
import stat
import sys

path = sys.argv[1]
try:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    status = os.fstat(descriptor)
    path_status = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != 0o640
        or (status.st_dev, status.st_ino) != (path_status.st_dev, path_status.st_ino)
    ):
        raise OSError
    with os.fdopen(descriptor, encoding="utf-8") as source:
        value = json.load(source)["dataSchemaVersion"]
except (OSError, json.JSONDecodeError, KeyError):
    raise SystemExit(1)
if isinstance(value, bool) or not isinstance(value, int) or value < 0:
    raise SystemExit(1)
print(value)
PYTHON
}

dogfood_current_data_schema_version() {
  python3 - "$DS_DATA_DIR/conversation-history.db" <<'PYTHON'
import sqlite3
import sys

path = sys.argv[1]
try:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        value = connection.execute("PRAGMA user_version").fetchone()[0]
except (OSError, sqlite3.Error, TypeError):
    raise SystemExit(1)
if isinstance(value, bool) or not isinstance(value, int) or value < 0:
    raise SystemExit(1)
print(value)
PYTHON
}

dogfood_require_rollback_schema() {
  local manifest=$1
  local target_schema current_schema
  target_schema=$(dogfood_manifest_schema_version "$manifest") || {
    echo "ERROR: rollback先manifestのdata schema versionを検証できません" >&2
    return 2
  }
  current_schema=$(dogfood_current_data_schema_version) || {
    echo "ERROR: 現在のdata schema versionを検証できません" >&2
    return 2
  }
  if [ "$current_schema" != "$target_schema" ]; then
    echo "ERROR: rollback先と現在のdata schemaが一致しません: target=$target_schema current=$current_schema" >&2
    echo "ERROR: 保存済みbackupを検証・restoreしてからrollbackを再実行してください" >&2
    return 2
  fi
}

dogfood_find_saved_manifest() {
  local target=$1
  python3 - "$DOGFOOD_STATE_DIR/deployments" "$target" <<'PYTHON'
import json
import os
import stat
import sys
from pathlib import Path

deployments = Path(sys.argv[1])
target = sys.argv[2]
matches = []
for path in deployments.glob("*.json"):
    if path.name == "current.json":
        continue
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        status = os.fstat(descriptor)
        path_status = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.geteuid()
            or stat.S_IMODE(status.st_mode) != 0o640
            or (status.st_dev, status.st_ino) != (path_status.st_dev, path_status.st_ino)
        ):
            raise OSError
        with os.fdopen(descriptor, encoding="utf-8") as source:
            payload = json.load(source)
    except (OSError, json.JSONDecodeError):
        raise SystemExit(1)
    if payload.get("targetCommit") == target:
        matches.append(path)
if not matches:
    raise SystemExit(1)
print(sorted(matches)[-1])
PYTHON
}
