#!/usr/bin/env bash

PROFILE_LIBRARY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE_SCRIPTS_DIR="$(cd "$PROFILE_LIBRARY_DIR/.." && pwd)"
PROFILE_RESOLVER="$PROFILE_LIBRARY_DIR/../../environments/profile.py"
PROFILE_DEFAULT_REPORT="$PROFILE_LIBRARY_DIR/../../frontend/test-results/resolved-profile.json"

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
    --default-report "$PROFILE_DEFAULT_REPORT" \
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
  rag_enabled="$(profile_get derivedEnvironment.RAG_ENABLED)" || return $?
  ollama_mode="$(profile_get dependencies.ollama.mode)" || return $?
  voicevox_mode="$(profile_get dependencies.voicevox.mode)" || return $?
  backend_mode="$(profile_get dependencies.backend.mode)" || return $?
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
  export RAG_ENABLED="$rag_enabled"
  if [ "$ollama_mode" = "real" ]; then
    export OLLAMA_BASE_URL="$ollama_base_url"
  fi
  if [ "$voicevox_mode" = "real" ]; then
    export VOICEVOX_BASE_URL="$voicevox_base_url"
  fi
  if [ "$backend_mode" = "real" ]; then
    export DS_BACKEND_ORIGIN="$backend_origin"
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

_profile_start_http_dependency() {
  local dependency="$1"
  local label="$2"
  local command="$3"
  local mode
  local source
  local readiness_url
  mode="$(profile_get "dependencies.$dependency.mode")" || return $?
  if [ "$mode" = "disabled" ]; then
    return
  fi
  if [ "$mode" != "real" ]; then
    echo "ERROR: $dependency mode must be real or disabled; received $mode" >&2
    return 1
  fi

  source="$(profile_get "dependencies.$dependency.source")" || return $?
  readiness_url="$(profile_get "dependencies.$dependency.readinessUrl")" || return $?
  case "$source" in
    managed)
      process_start_child "$label" "$command"
      wait_for_http "$readiness_url" "$label" "$(process_last_started_pid)"
      ;;
    external)
      wait_for_http "$readiness_url" "$label"
      ;;
    *)
      echo "ERROR: $dependency source must be managed or external; received $source" >&2
      return 1
      ;;
  esac
}

_profile_start_voicevox() {
  local mode
  local source
  mode="$(profile_get dependencies.voicevox.mode)" || return $?
  if [ "$mode" = "disabled" ]; then
    return
  fi
  source="$(profile_get dependencies.voicevox.source)" || return $?
  case "$source" in
    managed) "$PROFILE_SCRIPTS_DIR/start-voicevox.sh" ;;
    external)
      local readiness_url
      readiness_url="$(profile_get dependencies.voicevox.readinessUrl)" || return $?
      wait_for_http "$readiness_url" "VOICEVOX"
      ;;
    *)
      echo "ERROR: voicevox source must be managed or external; received $source" >&2
      return 1
      ;;
  esac
}

_profile_start_frontend() {
  local source
  source="$(profile_get dependencies.frontend.source)" || return $?
  case "$source" in
    managed) process_start_child "Frontend" "$PROFILE_SCRIPTS_DIR/start-frontend.sh" ;;
    external)
      local readiness_url
      readiness_url="$(profile_get dependencies.frontend.readinessUrl)" || return $?
      wait_for_http "$readiness_url" "Frontend"
      ;;
    *)
      echo "ERROR: frontend source must be managed or external; received $source" >&2
      return 1
      ;;
  esac
}

profile_start_stack() {
  local backend_mode
  backend_mode="$(profile_get dependencies.backend.mode)" || return $?
  case "$backend_mode" in
    mock)
      _profile_start_frontend
      ;;
    real)
      local backend_source
      backend_source="$(profile_get dependencies.backend.source)" || return $?
      if [ "$backend_source" = "managed" ]; then
        "$PROFILE_SCRIPTS_DIR/setup-backend.sh" || return $?
      elif [ "$backend_source" != "external" ]; then
        echo "ERROR: backend source must be managed or external; received $backend_source" >&2
        return 1
      fi
      _profile_start_http_dependency ollama "Ollama" "$PROFILE_SCRIPTS_DIR/start-ollama.sh" || return $?
      _profile_start_voicevox || return $?
      _profile_start_http_dependency backend "Backend" "$PROFILE_SCRIPTS_DIR/start-backend.sh" || return $?
      _profile_start_frontend
      ;;
    disabled)
      echo "ERROR: backend is disabled and cannot serve this startup boundary" >&2
      return 1
      ;;
    *)
      echo "ERROR: unknown backend mode: $backend_mode" >&2
      return 1
      ;;
  esac
}
