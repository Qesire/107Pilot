#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
images_archive="${1:-$root/images/pilot107-cpu-rc-images.tar.gz}"
images_file="${2:-$root/images/images.txt}"

if [[ ! -f "$images_archive" || ! -f "$images_file" ]]; then
  echo "CPU RC image archive or manifest is missing" >&2
  exit 1
fi

mapfile -t images < <(grep -v '^[[:space:]]*$' "$images_file")
if [[ "${#images[@]}" -ne 5 ]]; then
  echo "CPU RC image manifest must contain exactly five images" >&2
  exit 1
fi

revision="${images[0]#pilot107/slurm-sim:cpu-rc-}"
if [[ -z "$revision" || "$revision" == "${images[0]}" ]]; then
  echo "CPU RC image manifest has an invalid slurm-sim reference" >&2
  exit 1
fi
expected_images=(
  "pilot107/slurm-sim:cpu-rc-$revision"
  "pilot107/api:cpu-rc-$revision"
  "pilot107/worker:cpu-rc-$revision"
  "pilot107/web:cpu-rc-$revision"
  "pilot107/agentd:cpu-rc-$revision"
)
for index in "${!expected_images[@]}"; do
  if [[ "${images[$index]}" != "${expected_images[$index]}" ]]; then
    echo "CPU RC image manifest entry $index must be ${expected_images[$index]}" >&2
    exit 1
  fi
done

docker load -i "$images_archive"
for image in "${images[@]}"; do
  docker image inspect "$image" >/dev/null
  echo "loaded $image"
done

echo "CPU RC images imported"
