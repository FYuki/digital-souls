#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "ERROR: render-assets.sh requires template and output directories" >&2
  exit 2
fi

template_dir=$1
output_dir=$2
for key in DOGFOOD_SERVICE_USER DOGFOOD_SERVICE_GROUP DOGFOOD_CONFIG_DIR DOGFOOD_CLONE_DIR DOGFOOD_WSL_DISTRO DOGFOOD_SERVICE_HOME_DIR DS_DATA_DIR LIVEKIT_URL LIVEKIT_API_KEY LIVEKIT_API_SECRET; do
  : "${!key:?$keyが必要です}"
done
if [ ! -d "$template_dir" ] || [ ! -d "$output_dir" ]; then
  echo "ERROR: template and output directories must exist" >&2
  exit 2
fi

escape_replacement() {
  printf '%s' "$1" | sed 's/[\\&|]/\\&/g'
}

render_template() {
  local source_path=$1
  local output_path=$2
  local escaped_service_user escaped_service_group escaped_config_dir
  local escaped_clone_dir escaped_wsl_distro escaped_service_home_dir escaped_data_dir
  local escaped_livekit_api_key escaped_livekit_api_secret
  escaped_service_user=$(escape_replacement "$DOGFOOD_SERVICE_USER")
  escaped_service_group=$(escape_replacement "$DOGFOOD_SERVICE_GROUP")
  escaped_config_dir=$(escape_replacement "$DOGFOOD_CONFIG_DIR")
  escaped_clone_dir=$(escape_replacement "$DOGFOOD_CLONE_DIR")
  escaped_wsl_distro=$(escape_replacement "$DOGFOOD_WSL_DISTRO")
  escaped_service_home_dir=$(escape_replacement "$DOGFOOD_SERVICE_HOME_DIR")
  escaped_data_dir=$(escape_replacement "$DS_DATA_DIR")
  escaped_livekit_api_key=$(escape_replacement "$LIVEKIT_API_KEY")
  escaped_livekit_api_secret=$(escape_replacement "$LIVEKIT_API_SECRET")
  sed \
    -e "s|@DOGFOOD_SERVICE_USER@|$escaped_service_user|g" \
    -e "s|@DOGFOOD_SERVICE_GROUP@|$escaped_service_group|g" \
    -e "s|@DOGFOOD_CONFIG_DIR@|$escaped_config_dir|g" \
    -e "s|@DOGFOOD_CLONE_DIR@|$escaped_clone_dir|g" \
    -e "s|@DOGFOOD_WSL_DISTRO@|$escaped_wsl_distro|g" \
    -e "s|@DOGFOOD_SERVICE_HOME_DIR@|$escaped_service_home_dir|g" \
    -e "s|@DS_DATA_DIR@|$escaped_data_dir|g" \
    -e "s|@LIVEKIT_API_KEY@|$escaped_livekit_api_key|g" \
    -e "s|@LIVEKIT_API_SECRET@|$escaped_livekit_api_secret|g" \
    "$source_path" > "$output_path"
}

render_template "$template_dir/digital-souls-ollama.service.template" "$output_dir/digital-souls-ollama.service"
render_template "$template_dir/digital-souls-voicevox.service.template" "$output_dir/digital-souls-voicevox.service"
render_template "$template_dir/digital-souls-livekit.service.template" "$output_dir/digital-souls-livekit.service"
render_template "$template_dir/digital-souls-application.service.template" "$output_dir/digital-souls-application.service"
render_template "$template_dir/livekit.yaml" "$output_dir/livekit.yaml"
render_template "$template_dir/start-dogfood-wsl.ps1.template" "$output_dir/start-dogfood-wsl.ps1"
printf 'LIVEKIT_URL=%s\nLIVEKIT_API_KEY=%s\nLIVEKIT_API_SECRET=%s\n' \
  "$LIVEKIT_URL" "$LIVEKIT_API_KEY" "$LIVEKIT_API_SECRET" \
  > "$output_dir/livekit-backend.env"
