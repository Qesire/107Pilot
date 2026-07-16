#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$root/scripts/real107_probe"
out_dir="$root/artifacts/probes"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
package_dir="$out_dir/pilot107-real107-probe-$stamp"
archive="$out_dir/pilot107-real107-probe-$stamp.tar.gz"

mkdir -p "$out_dir"
rm -rf "$package_dir"
mkdir -p "$package_dir"

cp "$src/probe_real107_snapshot.py" "$package_dir/"
cp "$src/probe_real107_cli_snapshot.py" "$package_dir/"
cp "$src/real107_configuration_snapshot_probe.sbatch" "$package_dir/"
cp "$src/README.md" "$package_dir/"
cp -R "$root/src/pilot107" "$package_dir/pilot107"
chmod 0755 "$package_dir/probe_real107_snapshot.py"
chmod 0755 "$package_dir/probe_real107_cli_snapshot.py"

(
  cd "$out_dir"
  tar -czf "$archive" "$(basename "$package_dir")"
  sha256sum "$(basename "$archive")" >"$(basename "$archive").sha256"
)

printf 'real107 probe package=%s\n' "$archive"
printf 'sha256=%s.sha256\n' "$archive"
