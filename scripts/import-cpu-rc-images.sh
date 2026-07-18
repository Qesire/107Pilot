#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
images_archive="${1:-$root/images/pilot107-cpu-rc-images.tar.gz}"
images_file="${2:-$root/images/images.txt}"

if [[ ! -f "$images_archive" || ! -f "$images_file" ]]; then
  echo "CPU RC image archive or manifest is missing" >&2
  exit 1
fi

docker load -i "$images_archive"
while IFS= read -r image; do
  [[ -z "$image" ]] && continue
  docker image inspect "$image" >/dev/null
  echo "loaded $image"
done <"$images_file"

echo "CPU RC images imported"
