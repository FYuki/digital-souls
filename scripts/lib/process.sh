#!/usr/bin/env bash

PROCESS_MANAGER_PIDS=()
PROCESS_MANAGER_LAUNCHER_PIDS=()
PROCESS_MANAGER_CLEANED_UP=0
PROCESS_MANAGER_PENDING_SIGNAL=0

process_cleanup() {
  if [ "$PROCESS_MANAGER_CLEANED_UP" -eq 1 ]; then
    return
  fi

  PROCESS_MANAGER_CLEANED_UP=1

  local pid
  for pid in "${PROCESS_MANAGER_PIDS[@]}"; do
    if kill -0 -- "-$pid" 2>/dev/null; then
      kill -- "-$pid" 2>/dev/null || true
    fi
  done

  for pid in "${PROCESS_MANAGER_LAUNCHER_PIDS[@]}"; do
    wait "$pid" 2>/dev/null || true
  done
}

process_handle_signal() {
  local exit_code="$1"
  process_cleanup
  exit "$exit_code"
}

process_defer_signal() {
  PROCESS_MANAGER_PENDING_SIGNAL="$1"
}

process_manager_init() {
  PROCESS_MANAGER_PIDS=()
  PROCESS_MANAGER_LAUNCHER_PIDS=()
  PROCESS_MANAGER_CLEANED_UP=0
  PROCESS_MANAGER_PENDING_SIGNAL=0
  trap process_cleanup EXIT
  trap 'process_handle_signal 130' INT
  trap 'process_handle_signal 143' TERM
}

process_start_child() {
  local label="$1"
  shift

  echo "==> Starting $label..."
  local ready_fd
  local group_pid
  local launcher_pid
  local stdout_fd
  exec {stdout_fd}>&1
  trap 'process_defer_signal 130' INT
  trap 'process_defer_signal 143' TERM
  exec {ready_fd}< <(
    trap 'exit 143' TERM
    exec setsid --wait bash -c '
      stdout_fd="$1"
      shift
      printf "%s\n" "$BASHPID"
      exec "$@" </dev/null >&"$stdout_fd" {stdout_fd}>&-
    ' bash "$stdout_fd" "$@"
  )
  launcher_pid="$!"
  if ! IFS= read -r group_pid <&"$ready_fd"; then
    wait "$launcher_pid" 2>/dev/null || true
    exec {ready_fd}<&-
    exec {stdout_fd}>&-
    trap 'process_handle_signal 130' INT
    trap 'process_handle_signal 143' TERM
    if [ "$PROCESS_MANAGER_PENDING_SIGNAL" -ne 0 ]; then
      process_handle_signal "$PROCESS_MANAGER_PENDING_SIGNAL"
    fi
    echo "ERROR: failed to start $label in a managed process group" >&2
    return 1
  fi
  PROCESS_MANAGER_PIDS+=("$group_pid")
  PROCESS_MANAGER_LAUNCHER_PIDS+=("$launcher_pid")
  trap 'process_handle_signal 130' INT
  trap 'process_handle_signal 143' TERM
  if [ "$PROCESS_MANAGER_PENDING_SIGNAL" -ne 0 ]; then
    process_handle_signal "$PROCESS_MANAGER_PENDING_SIGNAL"
  fi
  exec {ready_fd}<&-
  exec {stdout_fd}>&-
}

process_last_started_pid() {
  printf '%s\n' "${PROCESS_MANAGER_PIDS[-1]}"
}

process_wait_all() {
  local status=0
  local child_status
  local pid

  for pid in "${PROCESS_MANAGER_LAUNCHER_PIDS[@]}"; do
    if wait "$pid"; then
      child_status=0
    else
      child_status="$?"
    fi
    if [ "$child_status" -ne 0 ]; then
      status="$child_status"
    fi
  done

  return "$status"
}
