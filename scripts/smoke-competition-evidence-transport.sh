#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

PYTHONPATH="$root/src" python3 "$root/scripts/smoke_competition_evidence_transport.py"
