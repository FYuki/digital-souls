#!/usr/bin/env bash

DOGFOOD_DEFAULT_ENV_FILE=/etc/digital-souls/dogfood.env
DOGFOOD_DEPRECATED_FIXED_ENV_FILE=/tmp/dogfood.env
DOGFOOD_TEMPORARY_ENV_FORBIDDEN_MODE_MASK=077
DOGFOOD_DEFAULT_SERVICE_HOME_DIR=/var/lib/digital-souls/home
DOGFOOD_DEFAULT_OLLAMA_MODELS_DIR=/var/lib/digital-souls/models/ollama
DOGFOOD_ALLOWED_ENV_KEYS=(
  DS_ENVIRONMENT_ID DOGFOOD_WSL_DISTRO DOGFOOD_SERVICE_USER
  DOGFOOD_SERVICE_GROUP DOGFOOD_REPOSITORY_URL DOGFOOD_CLONE_DIR
  DOGFOOD_CONFIG_DIR DS_DATA_DIR DOGFOOD_SERVICE_HOME_DIR
  DOGFOOD_OLLAMA_MODELS_DIR DOGFOOD_BACKUP_DIR
  DOGFOOD_BACKUP_RETENTION_COUNT DOGFOOD_BACKUP_AUTHENTICATION_KEY
  DOGFOOD_STATE_DIR DOGFOOD_LOG_DIR
  DOGFOOD_VOICEVOX_IMAGE DOGFOOD_VOICEVOX_CONTAINER
  DOGFOOD_LIVEKIT_IMAGE DOGFOOD_LIVEKIT_CONTAINER
  DOGFOOD_BACKEND_IMAGE DOGFOOD_FRONTEND_IMAGE DOGFOOD_WHISPER_IMAGE
  DOGFOOD_WHISPER_MODEL_CACHE WHISPER_MODEL_REVISION
  LIVEKIT_URL LIVEKIT_API_KEY LIVEKIT_API_SECRET
)
DOGFOOD_PATH_KEYS=(
  DOGFOOD_CLONE_DIR DOGFOOD_CONFIG_DIR DS_DATA_DIR DOGFOOD_SERVICE_HOME_DIR
  DOGFOOD_OLLAMA_MODELS_DIR DOGFOOD_BACKUP_DIR DOGFOOD_STATE_DIR DOGFOOD_LOG_DIR
  DOGFOOD_WHISPER_MODEL_CACHE
)
DOGFOOD_IMAGE_KEYS=(
  DOGFOOD_BACKEND_IMAGE DOGFOOD_FRONTEND_IMAGE DOGFOOD_WHISPER_IMAGE
)

dogfood_is_allowed_key() {
  local candidate=$1
  local allowed
  for allowed in "${DOGFOOD_ALLOWED_ENV_KEYS[@]}"; do
    if [ "$candidate" = "$allowed" ]; then
      return 0
    fi
  done
  return 1
}

dogfood_read_environment() {
  local key value allowed
  local -A seen=()
  for allowed in "${DOGFOOD_ALLOWED_ENV_KEYS[@]}"; do
    unset "$allowed"
  done
  while IFS='=' read -r key value || [ -n "$key$value" ]; do
    if [ -z "$key" ] || [[ "$key" == \#* ]]; then
      continue
    fi
    if ! [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || ! dogfood_is_allowed_key "$key"; then
      echo "ERROR: 未知のdogfood設定キーです: $key" >&2
      return 2
    fi
    if [ "${seen[$key]+defined}" = "defined" ]; then
      echo "ERROR: dogfood設定キーが重複しています: $key" >&2
      return 2
    fi
    if [ -z "$value" ]; then
      echo "ERROR: dogfood設定値が空です: $key" >&2
      return 2
    fi
    seen[$key]=defined
    export "$key=$value"
  done
}

dogfood_load_environment() {
  dogfood_load_environment_settings || return
  dogfood_read_revision
}

dogfood_load_environment_settings() {
  local env_file=${DOGFOOD_ENV_FILE:-$DOGFOOD_DEFAULT_ENV_FILE}
  local environment_contents expected_owner normalized_default_env_file
  local normalized_env_file read_status
  if ! normalized_env_file=$(realpath --canonicalize-missing -- "$env_file"); then
    echo "ERROR: dogfood設定ファイルpathを正規化できません: $env_file" >&2
    return 2
  fi
  if [ "$normalized_env_file" = "$DOGFOOD_DEPRECATED_FIXED_ENV_FILE" ]; then
    echo "ERROR: 固定されたbootstrap用一時設定pathは使用できません" >&2
    return 2
  fi
  if ! normalized_default_env_file=$(realpath --canonicalize-missing -- "$DOGFOOD_DEFAULT_ENV_FILE"); then
    echo "ERROR: dogfood既定設定ファイルpathを正規化できません" >&2
    return 2
  fi
  if [ ! -f "$env_file" ]; then
    echo "ERROR: dogfood設定ファイルがありません: $env_file" >&2
    return 2
  fi
  expected_owner=$EUID
  if [ "$EUID" -eq 0 ] && [[ "${SUDO_UID-}" =~ ^[0-9]+$ ]]; then
    expected_owner=$SUDO_UID
  fi
  if [ "$normalized_env_file" = "$normalized_default_env_file" ]; then
    expected_owner=0
  fi
  if ! environment_contents=$(
    python3 - "$env_file" "$normalized_env_file" "$expected_owner" \
      "$normalized_default_env_file" \
      "$DOGFOOD_TEMPORARY_ENV_FORBIDDEN_MODE_MASK" <<'PYTHON'
import os
import stat
import sys

(
    env_file,
    normalized_env_file,
    expected_owner,
    default_env_file,
    forbidden_mode_mask,
) = sys.argv[1:]
try:
    descriptor = os.open(env_file, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
except OSError:
    print(f"ERROR: dogfood設定ファイルを安全に開けません: {env_file}", file=sys.stderr)
    raise SystemExit(2)

try:
    descriptor_status = os.fstat(descriptor)
    path_status = os.stat(env_file, follow_symlinks=False)
    if (
        not stat.S_ISREG(descriptor_status.st_mode)
        or not stat.S_ISREG(path_status.st_mode)
        or (descriptor_status.st_dev, descriptor_status.st_ino)
        != (path_status.st_dev, path_status.st_ino)
    ):
        print("ERROR: dogfood設定ファイルが検証中に変更されました", file=sys.stderr)
        raise SystemExit(2)

    if descriptor_status.st_uid != int(expected_owner):
        print("ERROR: dogfood設定ファイルの所有者が不正です", file=sys.stderr)
        raise SystemExit(2)

    if normalized_env_file != default_env_file:
        if stat.S_IMODE(descriptor_status.st_mode) & int(forbidden_mode_mask, 8):
            print(
                "ERROR: bootstrap用一時設定ファイルの権限が安全ではありません",
                file=sys.stderr,
            )
            raise SystemExit(2)

    with os.fdopen(descriptor, "rb", closefd=False) as environment_file:
        sys.stdout.buffer.write(environment_file.read())
except (OSError, ValueError):
    print("ERROR: dogfood設定ファイルを検証できません", file=sys.stderr)
    raise SystemExit(2)
finally:
    os.close(descriptor)
PYTHON
  ); then
    return 2
  fi
  export DOGFOOD_RESOLVED_ENV_FILE="$normalized_env_file"
  read_status=0
  dogfood_read_environment < <(printf '%s\n' "$environment_contents") \
    || read_status=$?
  if [ "$read_status" -ne 0 ]; then
    return "$read_status"
  fi
  if [ -z "${DOGFOOD_SERVICE_HOME_DIR+x}" ]; then
    export DOGFOOD_SERVICE_HOME_DIR="$DOGFOOD_DEFAULT_SERVICE_HOME_DIR"
  fi
  if [ -z "${DOGFOOD_OLLAMA_MODELS_DIR+x}" ]; then
    export DOGFOOD_OLLAMA_MODELS_DIR="$DOGFOOD_DEFAULT_OLLAMA_MODELS_DIR"
  fi
  dogfood_validate_environment || return
  if [ "$normalized_env_file" = "$normalized_default_env_file" ]; then
    dogfood_load_active_images || return
    dogfood_validate_images || return
  fi
}

dogfood_load_active_images() {
  local image_file="$DOGFOOD_CONFIG_DIR/dogfood-images.env"
  local contents key value
  local -A seen=()
  [ -e "$image_file" ] || return 0
  if ! contents=$(python3 - "$image_file" <<'PYTHON'
import os
import stat
import sys

path = sys.argv[1]
try:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    opened = os.fstat(descriptor)
    current = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != 0
        or stat.S_IMODE(opened.st_mode) != 0o600
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise OSError
    with os.fdopen(descriptor, encoding="utf-8") as source:
        sys.stdout.write(source.read())
except (OSError, UnicodeError):
    print("ERROR: active image設定ファイルを安全に読み取れません", file=sys.stderr)
    raise SystemExit(2)
PYTHON
  ); then
    return 2
  fi
  while IFS='=' read -r key value || [ -n "$key$value" ]; do
    if [ -z "$key" ] || [[ "$key" == \#* ]]; then
      continue
    fi
    case "$key" in
      DOGFOOD_BACKEND_IMAGE|DOGFOOD_FRONTEND_IMAGE|DOGFOOD_WHISPER_IMAGE) ;;
      *) echo "ERROR: active image設定に未知のkeyがあります: $key" >&2; return 2 ;;
    esac
    if [ "${seen[$key]+defined}" = defined ] || [ -z "$value" ]; then
      echo "ERROR: active image設定が重複または空です: $key" >&2
      return 2
    fi
    seen[$key]=defined
    export "$key=$value"
  done <<< "$contents"
  for key in "${DOGFOOD_IMAGE_KEYS[@]}"; do
    if [ "${seen[$key]+defined}" != defined ]; then
      echo "ERROR: active image設定がありません: $key" >&2
      return 2
    fi
  done
}

dogfood_validate_images() {
  local key
  for key in "${DOGFOOD_IMAGE_KEYS[@]}"; do
    if ! [[ "${!key}" =~ ^ghcr\.io/[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$ ]]; then
      echo "ERROR: $key はGHCRのimmutable digestで指定してください" >&2
      return 2
    fi
  done
}

dogfood_read_revision() {
  local revision_file="$DOGFOOD_CONFIG_DIR/dogfood.revision"
  local revision
  if ! revision=$(python3 - "$revision_file" "$DOGFOOD_SERVICE_GROUP" <<'PYTHON'
import grp
import os
import stat
import sys

revision_file, expected_group = sys.argv[1:]
try:
    expected_group_id = grp.getgrnam(expected_group).gr_gid
except KeyError:
    print("ERROR: dogfood service groupを解決できません", file=sys.stderr)
    raise SystemExit(2)
try:
    descriptor = os.open(revision_file, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
except OSError:
    print("ERROR: dogfood revisionファイルを安全に開けません", file=sys.stderr)
    raise SystemExit(2)

try:
    descriptor_status = os.fstat(descriptor)
    path_status = os.stat(revision_file, follow_symlinks=False)
    if (
        not stat.S_ISREG(descriptor_status.st_mode)
        or not stat.S_ISREG(path_status.st_mode)
        or (descriptor_status.st_dev, descriptor_status.st_ino)
        != (path_status.st_dev, path_status.st_ino)
    ):
        print("ERROR: dogfood revisionファイルが検証中に変更されました", file=sys.stderr)
        raise SystemExit(2)
    if descriptor_status.st_uid != 0:
        print("ERROR: dogfood revisionファイルの所有者が不正です", file=sys.stderr)
        raise SystemExit(2)
    if descriptor_status.st_gid != expected_group_id:
        print("ERROR: dogfood revisionファイルのgroupが不正です", file=sys.stderr)
        raise SystemExit(2)
    if stat.S_IMODE(descriptor_status.st_mode) != 0o640:
        print("ERROR: dogfood revisionファイルの権限が不正です", file=sys.stderr)
        raise SystemExit(2)
    with os.fdopen(descriptor, "r", encoding="ascii", closefd=False) as source:
        value = source.read()
except (OSError, UnicodeError):
    print("ERROR: dogfood revisionファイルを読み取れません", file=sys.stderr)
    raise SystemExit(2)
finally:
    os.close(descriptor)

if not value.endswith("\n") or value.count("\n") != 1:
    print("ERROR: dogfood revisionファイルはcommit SHA 1行で指定してください", file=sys.stderr)
    raise SystemExit(2)
print(value[:-1], end="")
PYTHON
  ); then
    return 2
  fi
  if ! [[ "$revision" =~ ^[0-9a-f]{40}$ ]]; then
    echo "ERROR: dogfood revisionファイルは完全なcommit SHAで指定してください" >&2
    return 2
  fi
  export DOGFOOD_REPOSITORY_REVISION="$revision"
}

dogfood_validate_environment() {
  local key left_key right_key left right
  local left_index right_index
  local -A normalized_paths=()
  for key in "${DOGFOOD_ALLOWED_ENV_KEYS[@]}"; do
    if [ -z "${!key-}" ]; then
      echo "ERROR: 必須設定がありません: $key" >&2
      return 2
    fi
  done
  if ! [[ "$DOGFOOD_WSL_DISTRO" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "ERROR: DOGFOOD_WSL_DISTROが不正です" >&2
    return 2
  fi
  for key in DOGFOOD_SERVICE_USER DOGFOOD_SERVICE_GROUP; do
    if ! [[ "${!key}" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
      echo "ERROR: $key が不正です" >&2
      return 2
    fi
  done
  for key in "${DOGFOOD_PATH_KEYS[@]}"; do
    if ! [[ "${!key}" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
      echo "ERROR: $key は安全な絶対pathで指定してください" >&2
      return 2
    fi
    normalized_paths[$key]=$(realpath --canonicalize-missing -- "${!key}")
  done
  for left_index in "${!DOGFOOD_PATH_KEYS[@]}"; do
    for right_index in "${!DOGFOOD_PATH_KEYS[@]}"; do
      if [ "$left_index" -ge "$right_index" ]; then
        continue
      fi
      left_key=${DOGFOOD_PATH_KEYS[$left_index]}
      right_key=${DOGFOOD_PATH_KEYS[$right_index]}
      left=${normalized_paths[$left_key]}
      right=${normalized_paths[$right_key]}
      if [ "$left" = "$right" ] || [[ "$left/" == "$right/"* ]] || [[ "$right/" == "$left/"* ]]; then
        echo "ERROR: $left_key と $right_key のpathが重複しています" >&2
        return 2
      fi
    done
  done
  if ! [[ "$DOGFOOD_REPOSITORY_URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?/[A-Za-z0-9._~/%+-]+(/[A-Za-z0-9._~/%+-]+)*$ ]]; then
    echo "ERROR: DOGFOOD_REPOSITORY_URLはhttps URLで指定してください" >&2
    return 2
  fi
  if ! [[ "$DOGFOOD_BACKUP_RETENTION_COUNT" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: DOGFOOD_BACKUP_RETENTION_COUNTは正の整数で指定してください" >&2
    return 2
  fi
  if ! [[ "$DOGFOOD_BACKUP_AUTHENTICATION_KEY" =~ ^[0-9a-fA-F]{64}$ ]]; then
    echo "ERROR: DOGFOOD_BACKUP_AUTHENTICATION_KEYは64桁の16進数で指定してください" >&2
    return 2
  fi
  if [ "$LIVEKIT_URL" != "ws://127.0.0.1:17880" ]; then
    echo "ERROR: LIVEKIT_URLはdogfood profileのws://127.0.0.1:17880を指定してください" >&2
    return 2
  fi
  if [ "$LIVEKIT_API_KEY" = "replace-with-livekit-api-key" ] \
    || ! [[ "$LIVEKIT_API_KEY" =~ ^[A-Za-z0-9_-]{8,128}$ ]]; then
    echo "ERROR: LIVEKIT_API_KEYが不正です" >&2
    return 2
  fi
  if [ "$LIVEKIT_API_SECRET" = "replace-with-livekit-api-secret" ] \
    || ! [[ "$LIVEKIT_API_SECRET" =~ ^[A-Za-z0-9_-]{32,256}$ ]]; then
    echo "ERROR: LIVEKIT_API_SECRETが不正です" >&2
    return 2
  fi
  if ! [[ "$DOGFOOD_LIVEKIT_CONTAINER" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
    echo "ERROR: DOGFOOD_LIVEKIT_CONTAINERが不正です" >&2
    return 2
  fi
  for key in DOGFOOD_VOICEVOX_IMAGE DOGFOOD_VOICEVOX_CONTAINER DOGFOOD_LIVEKIT_IMAGE; do
    if ! [[ "${!key}" =~ ^[A-Za-z0-9][A-Za-z0-9._/:@-]*$ ]]; then
      echo "ERROR: $key が不正です" >&2
      return 2
    fi
  done
  dogfood_validate_images || return
  if ! [[ "$WHISPER_MODEL_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
    echo "ERROR: WHISPER_MODEL_REVISIONは40桁のcommit SHAで指定してください" >&2
    return 2
  fi
}

dogfood_resolve_wsl_distro() {
  local explicit=${WSL_DISTRO_NAME-}
  local detected=
  if command -v wslinfo >/dev/null 2>&1; then
    detected=$(wslinfo --name 2>/dev/null) || detected=
  fi
  if [ -n "$explicit" ] && [ -n "$detected" ] && [ "$explicit" != "$detected" ]; then
    echo "ERROR: WSL_DISTRO_NAMEとwslinfoのdistributionが一致しません" >&2
    return 2
  fi
  if [ -n "$explicit" ]; then
    DOGFOOD_RESOLVED_WSL_DISTRO=$explicit
  elif [ -n "$detected" ]; then
    DOGFOOD_RESOLVED_WSL_DISTRO=$detected
  else
    echo "ERROR: WSL distributionを解決できません" >&2
    return 2
  fi
  export DOGFOOD_RESOLVED_WSL_DISTRO
}

dogfood_require_identity() {
  if [ "$DS_ENVIRONMENT_ID" != "dogfood" ]; then
    echo "ERROR: DS_ENVIRONMENT_IDがdogfoodではありません" >&2
    return 2
  fi
  dogfood_resolve_wsl_distro || return
  if [ "$DOGFOOD_RESOLVED_WSL_DISTRO" != "$DOGFOOD_WSL_DISTRO" ]; then
    echo "ERROR: WSL distributionがDOGFOOD_WSL_DISTROと一致しません" >&2
    return 2
  fi
}
