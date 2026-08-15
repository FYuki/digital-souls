#!/usr/bin/env bash

DOGFOOD_DEPLOYMENT_RETENTION=20
DOGFOOD_MANIFEST_GENERATION_ATTEMPTS=16

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

dogfood_activate_revision() {
  local target=$1
  dogfood_update_revision "$target" || return
  git -c core.hooksPath=/dev/null -C "$DOGFOOD_CLONE_DIR" checkout --detach "$target" || return
  dogfood_verify_detached_clean_revision "$target" || return
  dogfood_prepare_backend || return
  npm --prefix "$DOGFOOD_CLONE_DIR/frontend" run build || return
  chown -R "root:$DOGFOOD_SERVICE_GROUP" "$DOGFOOD_CLONE_DIR" || return
  chmod -R g-w,o-rwx "$DOGFOOD_CLONE_DIR" || return
  "$DOGFOOD_CLONE_DIR/scripts/dogfood/restart-services.sh" || return
}

dogfood_check_readiness() {
  "$DOGFOOD_CLONE_DIR/backend/.venv/bin/python" \
    "$DOGFOOD_CLONE_DIR/environments/environment_cli.py" readiness \
    --profile "$DS_ENVIRONMENT_ID"
}

dogfood_backup() {
  local python="$DOGFOOD_CLONE_DIR/backend/.venv/bin/python"
  local cli="$DOGFOOD_CLONE_DIR/environments/environment_cli.py"
  local backup_result backup_directory
  backup_result=$(sudo --preserve-env=DOGFOOD_BACKUP_AUTHENTICATION_KEY \
    -u "$DOGFOOD_SERVICE_USER" env \
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
    "$python" "$cli" backup-verify --backup-directory "$backup_directory" || return
  printf '%s\n' "$backup_directory"
}

dogfood_manifest_metadata() {
  local previous=$1
  local target=$2
  local backup_id=$3
  python3 - "$DOGFOOD_CLONE_DIR/environments/profiles/dogfood.json" \
    "$DS_DATA_DIR/conversation-history.db" "$previous" "$target" "$backup_id" <<'PYTHON'
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

profile_path, database_path, previous, target, backup_id = sys.argv[1:]
profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
    data_schema = connection.execute("PRAGMA user_version").fetchone()[0]
print(json.dumps({
    "previousCommit": previous,
    "targetCommit": target,
    "profileSchemaVersion": profile["schemaVersion"],
    "dataSchemaVersion": data_schema,
    "backupId": backup_id,
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
  dogfood_validate_deployment_storage_path reject
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

dogfood_manifest_field() {
  local manifest=$1
  local field=$2
  python3 - "$manifest" "$field" <<'PYTHON'
import json
import os
import stat
import sys

path, field = sys.argv[1:]
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
if not isinstance(value, str) or not value:
    raise SystemExit(1)
print(value)
PYTHON
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
