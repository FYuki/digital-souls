#!/usr/bin/env bash

DOGFOOD_DEFAULT_ENV_FILE=/etc/digital-souls/dogfood.env
DOGFOOD_ALLOWED_ENV_KEYS=(
  DS_ENVIRONMENT_ID DOGFOOD_WSL_DISTRO DOGFOOD_SERVICE_USER
  DOGFOOD_SERVICE_GROUP DOGFOOD_REPOSITORY_URL DOGFOOD_REPOSITORY_REVISION
  DOGFOOD_CLONE_DIR
  DOGFOOD_CONFIG_DIR DS_DATA_DIR DOGFOOD_STATE_DIR DOGFOOD_LOG_DIR
  DOGFOOD_VOICEVOX_IMAGE DOGFOOD_VOICEVOX_CONTAINER
)
DOGFOOD_PATH_KEYS=(
  DOGFOOD_CLONE_DIR DOGFOOD_CONFIG_DIR DS_DATA_DIR
  DOGFOOD_STATE_DIR DOGFOOD_LOG_DIR
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

dogfood_load_environment() {
  local env_file=${DOGFOOD_ENV_FILE:-$DOGFOOD_DEFAULT_ENV_FILE}
  local key value allowed
  local -A seen=()
  if [ ! -f "$env_file" ]; then
    echo "ERROR: dogfood設定ファイルがありません: $env_file" >&2
    return 2
  fi
  export DOGFOOD_RESOLVED_ENV_FILE="$env_file"
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
  done < "$env_file"
  dogfood_validate_environment
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
  if ! [[ "$DOGFOOD_REPOSITORY_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
    echo "ERROR: DOGFOOD_REPOSITORY_REVISIONは完全なcommit SHAで指定してください" >&2
    return 2
  fi
}

dogfood_require_identity() {
  if [ "$DS_ENVIRONMENT_ID" != "dogfood" ]; then
    echo "ERROR: DS_ENVIRONMENT_IDがdogfoodではありません" >&2
    return 2
  fi
  if [ "${WSL_DISTRO_NAME-}" != "$DOGFOOD_WSL_DISTRO" ]; then
    echo "ERROR: WSL distributionがDOGFOOD_WSL_DISTROと一致しません" >&2
    return 2
  fi
}
