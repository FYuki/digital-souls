#!/usr/bin/env bash

PROFILE_LIBRARY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE_RESOLVER="$PROFILE_LIBRARY_DIR/../../environments/profile.py"

profile_get() {
  python3 "$PROFILE_RESOLVER" get --report "$DS_PROFILE_REPORT" --path "$1"
}

profile_validate_report() {
  python3 "$PROFILE_RESOLVER" validate-report --report "$DS_PROFILE_REPORT"
}

profile_resolve() {
  local default_profile="$1"
  local report_path
  report_path="$(python3 "$PROFILE_RESOLVER" resolve \
    --default-profile "$default_profile")" || return $?
  export DS_PROFILE_REPORT="$report_path"
  profile_export_derived_environment
}

profile_use_resolved_report_or_resolve() {
  local default_profile="$1"
  if [ "${DS_PROFILE_REPORT+x}" = "x" ] && [ -f "$DS_PROFILE_REPORT" ]; then
    profile_validate_report || return $?
    profile_export_derived_environment || return $?
    return
  fi
  profile_resolve "$default_profile"
}

profile_export_derived_environment() {
  local rag_enabled
  local ollama_mode
  local voicevox_mode
  local backend_mode
  local ollama_base_url
  local voicevox_base_url
  local backend_origin
  local model_environment_keys
  local key
  local value
  local environment_id
  local data_dir
  model_environment_keys="$(python3 "$PROFILE_RESOLVER" model-environment-keys)" || return $?
  rag_enabled="$(profile_get derivedEnvironment.RAG_ENABLED)" || return $?
  ollama_mode="$(profile_get dependencies.ollama.mode)" || return $?
  voicevox_mode="$(profile_get dependencies.voicevox.mode)" || return $?
  backend_mode="$(profile_get dependencies.backend.mode)" || return $?
  environment_id="$(profile_get derivedEnvironment.DS_ENVIRONMENT_ID)" || return $?
  data_dir="$(profile_get derivedEnvironment.DS_DATA_DIR)" || return $?
  if [ "$ollama_mode" = "real" ]; then
    ollama_base_url="$(profile_get derivedEnvironment.OLLAMA_BASE_URL)" || return $?
  fi
  if [ "$voicevox_mode" = "real" ]; then
    voicevox_base_url="$(profile_get derivedEnvironment.VOICEVOX_BASE_URL)" || return $?
  fi
  if [ "$backend_mode" = "real" ]; then
    backend_origin="$(profile_get derivedEnvironment.DS_BACKEND_ORIGIN)" || return $?
  fi

  unset OLLAMA_BASE_URL VOICEVOX_BASE_URL DS_BACKEND_ORIGIN
  for key in $model_environment_keys; do
    unset "$key"
  done
  export RAG_ENABLED="$rag_enabled"
  export DS_ENVIRONMENT_ID="$environment_id"
  export DS_DATA_DIR="$data_dir"
  if [ "$ollama_mode" = "real" ]; then
    export OLLAMA_BASE_URL="$ollama_base_url"
  fi
  if [ "$voicevox_mode" = "real" ]; then
    export VOICEVOX_BASE_URL="$voicevox_base_url"
  fi
  if [ "$backend_mode" = "real" ]; then
    export DS_BACKEND_ORIGIN="$backend_origin"
    for key in $model_environment_keys; do
      value="$(profile_get "derivedEnvironment.$key")" || return $?
      export "$key=$value"
    done
  fi
}

profile_require_managed_dependency() {
  local dependency="$1"
  local mode
  local source
  mode="$(profile_get "dependencies.$dependency.mode")" || return $?
  source="$(profile_get "dependencies.$dependency.source")" || return $?
  if [ "$mode" != "real" ] || [ "$source" != "managed" ]; then
    echo "ERROR: $dependency startup requires real/managed; received $mode/$source" >&2
    return 1
  fi
}
