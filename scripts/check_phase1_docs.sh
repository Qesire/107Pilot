#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

required=(
  "README.md"
  "docs/phase-1/implementation_plan.md"
  "docs/phase-0/docker_mainline_plan.md"
  "docs/phase-0/competition_deployment_plan.md"
  "docs/phase-0/real_platform_compatibility_plan.md"
  "docs/phase-0/server_questions.md"
  "docs/phase-1/production_access_report.md"
  "docs/phase-1/auth_decision.md"
  "docs/phase-1/evidence_transport_decision.md"
  "docs/phase-1/submission_strategy.md"
  "artifacts/probes/README.md"
)

missing=0
for path in "${required[@]}"; do
  if [[ ! -s "$root/$path" ]]; then
    printf 'missing or empty: %s\n' "$path" >&2
    missing=1
  fi
done

if [[ "$missing" -ne 0 ]]; then
  exit 1
fi

printf 'Phase 0 planning document skeleton is present.\n'
