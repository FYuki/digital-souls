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
  esac
done
: "${OLLAMA_PORT:?Ollama portを解決できません}"
: "${VOICEVOX_PORT:?VOICEVOX portを解決できません}"

printf 'environment identity: %s\nruntime root: %s\n' "$DS_ENVIRONMENT_ID" "$DS_DATA_DIR"
systemctl show digital-souls-inference.target digital-souls-ollama.service digital-souls-voicevox.service \
  --property=Id,LoadState,ActiveState,SubState,MainPID,MemoryCurrent,CPUUsageNSec
ss -ltnp "sport = :$OLLAMA_PORT or sport = :$VOICEVOX_PORT"
ps -eo pid,pcpu,pmem,comm
free -h
printf '%s\n' 'GPU metadata:'
if ! nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv,noheader; then
  printf '%s\n' 'GPU metadata: 利用できません'
fi
docker ps --filter "name=$DOGFOOD_VOICEVOX_CONTAINER" --format '{{.Names}} {{.Status}} {{.Ports}}'
