#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/load-environment.sh"
dogfood_load_environment
dogfood_require_identity
mapfile -t endpoints < <("$SCRIPT_DIR/resolve-inference-endpoints.py")
for endpoint in "${endpoints[@]}"; do
  case "$endpoint" in
    OLLAMA_PORT=*) OLLAMA_PORT=${endpoint#*=} ;;
    VOICEVOX_PORT=*) VOICEVOX_PORT=${endpoint#*=} ;;
    WHISPER_PORT=*) WHISPER_PORT=${endpoint#*=} ;;
    LIVEKIT_PORT=*) LIVEKIT_PORT=${endpoint#*=} ;;
  esac
done
: "${OLLAMA_PORT:?Ollama portを解決できません}"
: "${VOICEVOX_PORT:?VOICEVOX portを解決できません}"
: "${WHISPER_PORT:?Whisper portを解決できません}"
: "${LIVEKIT_PORT:?LiveKit portを解決できません}"

running_commit=$(git -C "$DOGFOOD_CLONE_DIR" rev-parse HEAD)
printf 'environment identity: %s\nprofile: %s\nruntime root: %s\nrunning commit: %s\n' \
  "$DS_ENVIRONMENT_ID" "$DS_ENVIRONMENT_ID" "$DS_DATA_DIR" "$running_commit"
application_unit_status=$(systemctl show digital-souls-application.service \
  --property=ActiveState)
printf '%s\n' "$application_unit_status"
systemctl show digital-souls-dogfood.target digital-souls-inference.target \
  digital-souls-application.service digital-souls-ollama.service digital-souls-voicevox.service \
  digital-souls-livekit.service \
  digital-souls-whisper.service \
  --property=Id,LoadState,ActiveState,SubState,MainPID,MemoryCurrent,CPUUsageNSec,ActiveEnterTimestamp
readiness_status=0
export DS_ENVIRONMENT_RUN_REPORT="$DS_DATA_DIR/runtime/dogfood/environment-run.json"
readiness_output=$("$DOGFOOD_CLONE_DIR/backend/.venv/bin/python" \
  "$DOGFOOD_CLONE_DIR/environments/environment_cli.py" readiness \
  --profile "$DS_ENVIRONMENT_ID") || readiness_status=$?
printf '%s\n' "$readiness_output"
inconsistent_status=0
if grep --fixed-strings --quiet 'ActiveState=active' \
  <<< "$application_unit_status" \
  && grep --fixed-strings --line-regexp --quiet 'orchestrator state=dead' \
    <<< "$readiness_output"; then
  echo "ERROR: application unitはactiveですがorchestrator processが存在しません。scripts/dogfood/restart-services.shを実行してください" >&2
  inconsistent_status=1
fi
ss -ltnp "sport = :$OLLAMA_PORT or sport = :$VOICEVOX_PORT or sport = :$WHISPER_PORT or sport = :$LIVEKIT_PORT"
ps -eo pid,pcpu,pmem,comm
free -h
printf '%s\n' 'GPU metadata:'
if ! nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv,noheader; then
  printf '%s\n' 'GPU metadata: 利用できません'
fi
docker ps --filter "name=$DOGFOOD_VOICEVOX_CONTAINER" \
  --filter "name=$DOGFOOD_LIVEKIT_CONTAINER" \
  --filter "name=digital-souls-whisper" \
  --filter "name=digital-souls-dogfood-backend" \
  --filter "name=digital-souls-dogfood-frontend" \
  --format '{{.Names}} {{.Status}} {{.Ports}}'
if [ "$inconsistent_status" -ne 0 ]; then
  exit "$inconsistent_status"
fi
exit "$readiness_status"
