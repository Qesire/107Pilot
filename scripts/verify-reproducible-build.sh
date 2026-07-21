#!/usr/bin/env bash
#
# verify-reproducible-build.sh — double clean-build reproducibility gate.
#
# Builds the app image twice with --no-cache and verifies reproducibility by:
#   1. Comparing image IDs (may differ due to file mtime non-determinism in
#      Docker/BuildKit layers — reported but not the hard gate).
#   2. Comparing normalized rootfs content (sha256 of every file + permissions +
#      ownership, sorted). This is the hard gate: if the file content matches,
#      the images are functionally identical regardless of layer mtime drift.
#
# For the slurm image: single build + verify no /dev/urandom in any layer
# (the JWT key is generated at container start, not build time) + verify the
# key file is NOT in the image.
#
# Usage:
#   bash scripts/verify-reproducible-build.sh
#
# Env:
#   PILOT107_REPRO_NETWORK   docker build --network value (default: host)
#   PILOT107_REPRO_SKIP_SLURM  if "1", skip the slurm image check
#
# Exit 0 if all checks pass, 1 if any fail.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
network="${PILOT107_REPRO_NETWORK:-host}"

log() { printf '%s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# App image: double clean-build, compare IDs + normalized rootfs content.
# ---------------------------------------------------------------------------
log "=== App image: double clean-build reproducibility check ==="

log "[1/5] Building pilot107/api:repro-a (--no-cache)..."
docker build --no-cache --provenance=false --build-arg SOURCE_DATE_EPOCH=0 \
  --network="$network" \
  -t pilot107/api:repro-a \
  -f "$root/apps/Dockerfile" \
  "$root" >/dev/null

log "[2/5] Building pilot107/api:repro-b (--no-cache)..."
docker build --no-cache --provenance=false --build-arg SOURCE_DATE_EPOCH=0 \
  --network="$network" \
  -t pilot107/api:repro-b \
  -f "$root/apps/Dockerfile" \
  "$root" >/dev/null

id_a="$(docker image inspect pilot107/api:repro-a --format '{{.Id}}')"
id_b="$(docker image inspect pilot107/api:repro-b --format '{{.Id}}')"
log "  repro-a ID: $id_a"
log "  repro-b ID: $id_b"

if [[ "$id_a" == "$id_b" ]]; then
  log "  PASS: image IDs match (byte-for-byte reproducible)"
else
  log "  WARN: image IDs differ (expected: Docker layers carry build-time file mtimes;"
  log "        --timestamp flag is not available in this BuildKit version)."
  log "  Falling back to normalized rootfs content comparison..."
fi

log "[3/5] Computing normalized rootfs content hash for repro-a..."
# Hash all file contents (sha256sum), file permissions, ownership, and paths.
# Sort for deterministic ordering. This captures the full observable filesystem
# state independent of file mtimes.
# Exclude Docker runtime-injected files (/etc/hosts, /etc/hostname,
# /etc/resolv.conf) which are generated per-container and are NOT part of the
# image — they differ between container runs even for the same image.
rootfs_a="$(docker run --rm pilot107/api:repro-a sh -c '
  find / -xdev -type f \
    ! -path /etc/hosts \
    ! -path /etc/hostname \
    ! -path /etc/resolv.conf \
    -exec sha256sum {} + 2>/dev/null | sort;
  find / -xdev \
    ! -path /etc/hosts \
    ! -path /etc/hostname \
    ! -path /etc/resolv.conf \
    -printf "%m %u %g %p\n" 2>/dev/null | sort
' | sha256sum | awk '{print $1}')"

log "[4/5] Computing normalized rootfs content hash for repro-b..."
rootfs_b="$(docker run --rm pilot107/api:repro-b sh -c '
  find / -xdev -type f \
    ! -path /etc/hosts \
    ! -path /etc/hostname \
    ! -path /etc/resolv.conf \
    -exec sha256sum {} + 2>/dev/null | sort;
  find / -xdev \
    ! -path /etc/hosts \
    ! -path /etc/hostname \
    ! -path /etc/resolv.conf \
    -printf "%m %u %g %p\n" 2>/dev/null | sort
' | sha256sum | awk '{print $1}')"

log "  rootfs-a: $rootfs_a"
log "  rootfs-b: $rootfs_b"

if [[ "$rootfs_a" != "$rootfs_b" ]]; then
  fail "non-reproducible app image: rootfs content differs (sha256 mismatch)"
fi
log "  PASS: rootfs content matches (functionally reproducible)"

# Also verify import still works.
log "[5/5] Verifying import works..."
docker run --rm pilot107/api:repro-a python3 -c "import pilot107; print('import ok')" >/dev/null
log "  PASS: import ok"

# ---------------------------------------------------------------------------
# Slurm image: single build + verify no /dev/urandom in layers + no key in image.
# ---------------------------------------------------------------------------
if [[ "${PILOT107_REPRO_SKIP_SLURM:-0}" != "1" ]]; then
  log "=== Slurm image: build + verify no /dev/urandom in layers ==="

  log "  Building pilot107/slurm-sim:repro-check..."
  docker build --provenance=false --build-arg SOURCE_DATE_EPOCH=0 \
    --network="$network" \
    -t pilot107/slurm-sim:repro-check \
    -f "$root/simulator/images/slurm/Dockerfile" \
    "$root/simulator/images/slurm" >/dev/null

  log "  Checking docker history for /dev/urandom..."
  if docker history pilot107/slurm-sim:repro-check --no-trunc --format '{{.CreatedBy}}' \
      | grep -q '/dev/urandom'; then
    fail "slurm image history contains /dev/urandom — JWT key is baked into a build layer (non-deterministic)"
  fi
  log "  PASS: no /dev/urandom in slurm image layers"

  # Verify the key file is NOT in the image (entrypoint should create it).
  # Use --entrypoint to bypass the entrypoint (which would generate the key
  # on start, creating a false positive).
  if docker run --rm --entrypoint sh pilot107/slurm-sim:repro-check \
      -c 'test -f /etc/slurm/jwt_hs256.key' 2>/dev/null; then
    fail "slurm image contains /etc/slurm/jwt_hs256.key — should be created at runtime"
  fi
  log "  PASS: /etc/slurm/jwt_hs256.key not present in image"
fi

log "=== ALL REPRODUCIBILITY CHECKS PASSED ==="
