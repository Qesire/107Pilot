# Phase-aware Experiment Builder operations

## Purpose

The phase-aware Builder reduces model/tool round trips by exposing only two
model-visible tools for `experiment_builder`:

- `builder_context_get` returns the bound Project version, live Workspace
  manifest, current durable phase, Blueprint, and approved resource envelope.
- `builder_build_submit` accepts one typed Blueprint and one atomic,
  digest-guarded patch batch. Application code persists the ChangeSet, runs the
  network-disabled bubblewrap Sandbox, and schedules one server-derived Slurm
  validation task after Sandbox success.

The model never supplies Project, Workspace, Session, or Turn bindings and
cannot construct raw scheduler fields. The validation authority remains
`vm-slurm`; publication and formal Run submission still require explicit user
approval.

## Rollout configuration

Set the same flag in `pilot-agentd`, `pilot107-api`, and `pilot107-worker`:

```text
PILOT107_PHASE_AWARE_BUILDER=1
```

The CPU-RC environment template enables it. The default is disabled so an older
deployment retains the eight legacy Project tools without data migration.
Deploy immutable `:cpu-rc-<revision>` image tags together; do not combine a new
agentd catalog with an old API handler image.

For VM acceptance, keep the USTC-107 profile fixed to
`deepseek-v4-flash`. Do not use the Ascend endpoint with the same model family
name.

## Progress and safeguards

The expected successful path is one context call and one submission. A Sandbox
failure returns a compact `repair_required` receipt; a later submission must
continue from the latest failed ChangeSet and change file content.

There is no small semantic call budget. Progressive repairs do not fail merely
because they exceed an arbitrary target count. Stale ChangeSets, identical
patches, post-schedule calls, binding mismatches, and envelope violations still
fail closed. The existing 20 Pi-step ceiling and 32 gateway-invocation ceiling
remain high-level protection against pathological loops until at least 30
representative Builder Turns have been measured.

Successful terminal events expose `pi_steps`, `provider_calls`,
`tool_invocations`, initial and repair submission counts, no-progress rejections,
and `terminal_phase`. Prometheus labels are restricted to the fixed profile,
tool, outcome, and phase catalogs; they never contain owners, IDs, or paths.

## Health and acceptance

After deployment, verify:

```bash
curl -kfsS https://<host>:8443/api/v1/health/live
curl -kfsS https://<host>:8443/api/v1/health/ready
curl -kfsS https://<host>:8443/metrics
```

Then run the heat-diffusion acceptance. It must show only
`builder_context_get` and `builder_build_submit` as model-called Builder tools,
at most eight Pi steps for the acceptance target, exactly one AgentTask, one
linked validation Run, trusted Evidence, a ready Capsule, and a passing
scientific output audit.

## Rollback

Set `PILOT107_PHASE_AWARE_BUILDER=0` for all three services and redeploy the
previous immutable release. Existing Projects, Blueprints, ChangeSets, and
Builder submission records remain readable; the legacy Builder catalog is
selected without schema conversion. Do not toggle only one service, because
capability issuance, model-visible schemas, and API handlers must agree.
