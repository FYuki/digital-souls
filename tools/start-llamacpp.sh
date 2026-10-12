#!/usr/bin/env bash
set -euo pipefail

# Run in Ubuntu-dogfood as the normal operator. Never acquire sudo here.
if [[ $(id -u) == 0 ]]; then
  echo 'Run as the normal operator, not root.' >&2
  exit 1
fi
if ! ollama_load=$(systemctl show digital-souls-ollama.service --property=LoadState --value); then
  echo 'Cannot verify the Ollama unit load state; refusing to start llama.cpp.' >&2
  exit 1
fi
case "$ollama_load" in
  not-found)
    # A successful not-found query confirms the unit is absent; no ActiveState is needed.
    ;;
  loaded)
    if ! ollama_state=$(systemctl show digital-souls-ollama.service --property=ActiveState --value) ||
       [[ $ollama_state != inactive ]]; then
      echo 'Ollama must be a known, stopped service. Explicitly stop it before switching; existing Ollama clients will be unavailable.' >&2
      exit 1
    fi
    ;;
  *)
    # Empty or unknown load states (including masked/error) cannot prove it is stopped.
    echo 'Ollama unit must be absent or loaded and inactive; refusing an unknown load state.' >&2
    exit 1
    ;;
esac
: "${CORE_LLAMACPP_MODEL:?Set the verified user-owned model GGUF path}"
expected=1278394b693672ac2799eadc9a83fd98259a6a88a40acfb1dcaa6c6fc895a606
actual=$(sha256sum -- "$CORE_LLAMACPP_MODEL")
if [[ ${actual%% *} != "$expected" ]]; then
  echo 'Model SHA-256 does not match the verified Gemma 4 weights.' >&2
  exit 1
fi
: "${CORE_LLAMACPP_EMBEDDING_MODEL:?Set the verified user-owned bge-m3 GGUF path}"
embedding_expected=aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173
embedding_actual=$(sha256sum -- "$CORE_LLAMACPP_EMBEDDING_MODEL")
if [[ ${embedding_actual%% *} != "$embedding_expected" ]]; then
  echo 'Model SHA-256 does not match the verified bge-m3 Q8_0 weights.' >&2
  exit 1
fi
export CORE_LLAMACPP_UID="$(id -u)" CORE_LLAMACPP_GID="$(id -g)"
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
docker compose -f "$repo_dir/compose.llamacpp.yml" config --quiet
docker compose -f "$repo_dir/compose.llamacpp.yml" up -d --no-build --pull never
