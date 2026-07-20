#!/usr/bin/env bash
#
# accept-cpu-rc-release.sh — DEPRECATED unified acceptance entry.
#
# This script is kept as a thin backward-compatibility wrapper. The封版门 #4
# acceptance matrix is now split into two scripts:
#
#   scripts/accept-source-release.sh   — source-tree CI bound to the git SHA
#                                        (ruff/mypy/pytest/typecheck/vitest/
#                                        playwright/build/static-drift/compose).
#   scripts/accept-runtime-bundle.sh   — offline runtime-bundle validation
#                                        (manifest + digests + image import +
#                                        compose stack + cpu-rc smokes).
#
# Source acceptance runs against a git checkout; runtime acceptance runs
# against an exported bundle (PILOT107_BUNDLE_DIR or PILOT107_BUNDLE_ARCHIVE).
# This wrapper runs them in sequence (source first, then runtime) and exits
# non-zero if either FAILs.
#
# DUAL-CONTEXT CONTRACT: unlike the original accept-cpu-rc-release.sh which
# ran entirely inside the bundle, this wrapper requires BOTH contexts to be
# satisfiable in the calling environment:
#   - source half: a git checkout at $root (accept-source-release.sh asserts
#     it is inside a git worktree and binds to `git rev-parse HEAD`).
#   - runtime half: PILOT107_BUNDLE_DIR (extracted bundle) OR
#     PILOT107_BUNDLE_ARCHIVE (.tar.gz) plus PILOT107_PUBLIC_URL (full public
#     origin) must be set; accept-runtime-bundle.sh errors out otherwise.
# Running this wrapper without both halves will FAIL the missing half.
#
# If you only need one half, invoke the new scripts directly:
#   - For source-only CI validation: bash scripts/accept-source-release.sh
#   - For bundle-only runtime validation:
#     PILOT107_BUNDLE_DIR=... PILOT107_PUBLIC_URL=... \
#       bash scripts/accept-runtime-bundle.sh
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "NOTE: accept-cpu-rc-release.sh is deprecated; prefer accept-source-release.sh + accept-runtime-bundle.sh" >&2

rc_overall=0

echo "=== PHASE 1: source acceptance (accept-source-release.sh) ==="
if bash "$root/scripts/accept-source-release.sh"; then
  echo "=== PHASE 1: source acceptance PASSED ==="
else
  rc=$?
  echo "=== PHASE 1: source acceptance FAILED (rc=$rc) ===" >&2
  rc_overall=1
fi

echo "=== PHASE 2: runtime bundle acceptance (accept-runtime-bundle.sh) ==="
if bash "$root/scripts/accept-runtime-bundle.sh"; then
  echo "=== PHASE 2: runtime bundle acceptance PASSED ==="
else
  rc=$?
  echo "=== PHASE 2: runtime bundle acceptance FAILED (rc=$rc) ===" >&2
  rc_overall=1
fi

if [[ "$rc_overall" -ne 0 ]]; then
  echo "=== ACCEPT-CPU-RC-RELEASE FAILED ===" >&2
else
  echo "=== ACCEPT-CPU-RC-RELEASE PASSED ==="
fi
exit "$rc_overall"
