#!/usr/bin/env bash

HTTP_READINESS_DEFAULT_MAX_ATTEMPTS=30
HTTP_READINESS_DEFAULT_INTERVAL_SECONDS=1
HTTP_READINESS_MAX_SAFE_ATTEMPTS=999999999

_readiness_validate_config() {
  local max_attempts="$1"
  local interval_seconds="$2"

  if [[ ! "$max_attempts" =~ ^[1-9][0-9]*$ ]] ||
    [ "${#max_attempts}" -gt "${#HTTP_READINESS_MAX_SAFE_ATTEMPTS}" ]; then
    echo "ERROR: HTTP_READINESS_MAX_ATTEMPTS must be an integer between 1 and $HTTP_READINESS_MAX_SAFE_ATTEMPTS; received \"$max_attempts\"" >&2
    return 1
  fi
  if [[ ! "$interval_seconds" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "ERROR: HTTP_READINESS_INTERVAL_SECONDS must be a non-negative number; received \"$interval_seconds\"" >&2
    return 1
  fi
}

_readiness_process_status() {
  local pid="$1"
  local child_status

  if wait "$pid" 2>/dev/null; then
    child_status=0
  else
    child_status="$?"
  fi
  if [ "$child_status" -eq 0 ]; then
    child_status=1
  fi

  return "$child_status"
}

wait_for_http() {
  local url="$1"
  local label="$2"
  local related_pid="${3-}"
  local max_attempts="${HTTP_READINESS_MAX_ATTEMPTS-$HTTP_READINESS_DEFAULT_MAX_ATTEMPTS}"
  local interval_seconds="${HTTP_READINESS_INTERVAL_SECONDS-$HTTP_READINESS_DEFAULT_INTERVAL_SECONDS}"
  local attempt=1

  _readiness_validate_config "$max_attempts" "$interval_seconds" || return $?
  echo "Waiting for $label to be ready at $url..."
  while ! curl -sf "$url" > /dev/null 2>&1; do
    if [ -n "$related_pid" ] && ! kill -0 "$related_pid" 2>/dev/null; then
      local child_status=0
      _readiness_process_status "$related_pid" || child_status="$?"
      echo "ERROR: $label process exited before becoming ready" >&2
      return "$child_status"
    fi
    if [ "$attempt" -ge "$max_attempts" ]; then
      echo "ERROR: $label did not become ready within $max_attempts seconds" >&2
      return 1
    fi
    attempt=$((attempt + 1))
    sleep "$interval_seconds"
  done
  echo "$label is ready."
}
