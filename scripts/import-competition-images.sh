#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
images_archive="${1:-$root/images/pilot107-images.tar.gz}"

if [[ ! -f "$images_archive" ]]; then
  echo "image archive not found: $images_archive" >&2
  exit 1
fi

docker load -i "$images_archive"

for image in pilot107/slurm-sim:local pilot107/api:local pilot107/worker:local pilot107/web:local; do
  docker image inspect "$image" >/dev/null
  echo "loaded $image"
done

echo "competition images imported"
