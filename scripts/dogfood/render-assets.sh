#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "ERROR: render-assets.sh requires template and output directories" >&2
  exit 2
fi

template_dir=$1
output_dir=$2
for key in DOGFOOD_SERVICE_USER DOGFOOD_SERVICE_GROUP DOGFOOD_CONFIG_DIR DOGFOOD_CLONE_DIR DOGFOOD_WSL_DISTRO DS_DATA_DIR; do
  : "${!key:?$keyが必要です}"
done
if [ ! -d "$template_dir" ] || [ ! -d "$output_dir" ]; then
  echo "ERROR: template and output directories must exist" >&2
  exit 2
fi

render_template() {
  local source_path=$1
  local output_path=$2
  local escaped_data_dir
  escaped_data_dir=$(printf '%s' "$DS_DATA_DIR" | sed 's/[\\&|]/\\&/g')
  sed \
    -e "s|@DOGFOOD_SERVICE_USER@|$DOGFOOD_SERVICE_USER|g" \
    -e "s|@DOGFOOD_SERVICE_GROUP@|$DOGFOOD_SERVICE_GROUP|g" \
    -e "s|@DOGFOOD_CONFIG_DIR@|$DOGFOOD_CONFIG_DIR|g" \
    -e "s|@DOGFOOD_CLONE_DIR@|$DOGFOOD_CLONE_DIR|g" \
    -e "s|@DOGFOOD_WSL_DISTRO@|$DOGFOOD_WSL_DISTRO|g" \
    -e "s|@DS_DATA_DIR@|$escaped_data_dir|g" \
    "$source_path" > "$output_path"
}

render_template "$template_dir/digital-souls-ollama.service.template" "$output_dir/digital-souls-ollama.service"
render_template "$template_dir/digital-souls-voicevox.service.template" "$output_dir/digital-souls-voicevox.service"
render_template "$template_dir/digital-souls-application.service.template" "$output_dir/digital-souls-application.service"
render_template "$template_dir/start-dogfood-wsl.ps1.template" "$output_dir/start-dogfood-wsl.ps1"
