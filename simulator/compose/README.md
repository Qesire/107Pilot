# Docker Slurm Simulator

This directory holds the Docker Slurm simulator skeleton. The Phase 1 behavior
contract lives in `../../config/platform_profiles/simulator-real107-behavior.yaml`;
the Slurm config and smoke scripts are tested against that profile.

Initial target services:

```text
mariadb
slurmdbd
slurmctld
slurmrestd
login-node-sim
worker-1
worker-2
pilot107-api
pilot107-worker
pilot107-web
pilot-agentd
```

The simulator must model:

- `alice`, `bob`, `pilot107`, and `slurm` Linux users, where `alice` is the
  legal student carrier user and `bob` is intentionally QOS-limited;
- shared `/public` storage;
- worker-local `/tmp`;
- real 107-style partitions and QoS names;
- REST query/submit behavior;
- command backend whitelist;
- accounting delay;
- token expiration;
- path permission failures.

Only `worker-1` and `worker-2` are privileged for `slurmd` process isolation and
fake GRES registration in Docker. The 107Pilot application services remain
non-root with dropped capabilities.

## Files

```text
compose.yml              service topology
.env.example             local defaults
slurm/slurm.conf         scaled real107-style partitions plus legacy debug
slurm/gres.conf          fake A100/RTX5090 GRES mapping for scaled simulator
slurm/slurmdbd.conf      accounting database connection
slurm/cgroup.conf        cgroup defaults for simulator workers
mariadb/init.sql         accounting database bootstrap
scripts/init-public.sh   simulated shared storage layout
```

## Validation

```bash
cd simulator/compose
sh scripts/check-compose-config.sh
```

## Core cluster scripts

```bash
bash scripts/build-slurm-sim-image.sh
bash scripts/check-slurm-sim-image.sh
bash scripts/build-slurm-sim-25-image.sh
bash scripts/check-slurm-sim-25-image.sh
bash scripts/build-app-images.sh
bash scripts/check-app-images.sh
bash scripts/check-pilot-agentd.sh
bash scripts/smoke-pilot-agentd-faux.sh
bash scripts/start-sim-core.sh
bash scripts/smoke-sim-real107-profile.sh
bash scripts/report-sim-behavior-fidelity.sh
bash scripts/check-sim-core.sh
bash scripts/probe-sim-rest-auth.sh
bash scripts/probe-sim-rest-submit.sh
bash scripts/smoke-sim-web-mvp.sh
bash scripts/smoke-sim-apps-profile.sh
bash scripts/smoke-sim-web-interactions.sh
bash scripts/smoke-sim-api-container-submit.sh
bash scripts/smoke-sim-api-container-contract.sh
bash scripts/smoke-sim-backend-job.sh
bash scripts/smoke-sim-run-service.sh
bash scripts/smoke-sim-worker.sh
bash scripts/smoke-sim-worker-transitions.sh
bash scripts/smoke-sim-evidence.sh
bash scripts/smoke-sim-evidence-query.sh
bash scripts/smoke-sim-api-evidence.sh
bash scripts/smoke-sim-api-run-get.sh
bash scripts/smoke-sim-api-cancel.sh
bash scripts/smoke-sim-api-submit.sh
bash scripts/smoke-sim-evidence-permissions.sh
bash scripts/smoke-sim-evidence-transitions.sh
bash scripts/smoke-sim-capsule.sh
bash scripts/stop-sim.sh
```

## Internal Agent service

`pilot-agentd` is an independent, private-network TypeScript service built on
Pi Agent Core. It exposes no host port and mounts neither `/public` nor cluster
credentials. The API and Worker receive only its internal URL, bearer token,
and model-profile ID. Campus provider URL/model/key settings are present only
in Agentd's environment.

Set a non-default token in each deployed environment:

```bash
PILOT107_AGENTD_TOKEN=REPLACE_WITH_RANDOM_AGENTD_TOKEN
PILOT107_AGENTD_MODEL_PROFILE=campus-default
```

Keep the matching token in the deployment secret store and inject it into
Agentd, API, and Worker. Inject `PILOT107_LLM_API_KEY` into Agentd alone. Do not
place either secret in committed `.env` files. `/healthz` indicates process
liveness. `/readyz` reports configuration readiness without contacting the
model endpoint; a running but unconfigured campus profile is therefore visible
as degraded rather than preventing deterministic features from starting.

For local evidence without campus or remote-VM access, build the images and run
the fixed server-side faux scenario:

```bash
bash scripts/build-app-images.sh
bash scripts/smoke-pilot-agentd-faux.sh
```

The smoke starts only Agentd in a temporary Compose project, exercises
interactive, explain, Contract patch, and remediation Turn kinds from the
Python client, then cancels and restores an interactive Turn. The project is
removed on exit. For an optional configured campus profile, run:

```bash
bash scripts/smoke-campus-llm.sh
```

Missing `PILOT107_AGENTD_URL`, token, or campus profile is a safe zero-exit
skip. The campus smoke never needs direct access to the provider key.

### A1 Tool Gateway and durable Turns

The A1 path is API → durable outbox → Worker → Agentd → private API Tool
Gateway → durable Store. Agentd calls
`http://pilot107-api:8080/internal/v1/agent-tools/invoke` over the private app
network; neither endpoint receives a host port. A separate
`pilot107-agent-capability-hmac` Compose secret is mounted read-only into API
and Worker only. Agentd and web must not receive that secret. For local use,
`scripts/init-local-secrets.sh` creates the ignored
`secrets/agent-capability-hmac.local`; deployments must replace it through
`PILOT107_AGENT_CAPABILITY_HMAC_SECRET_FILE` with secret-store material.

The `hpc-readonly-v1` profile permits only bounded platform, Run, log,
Evidence, workspace-list/search/file reads. It deliberately provides no write,
shell, SSH, Slurm submit/cancel, or publish authority. Durable event
replay accepts `after_event_id`/`Last-Event-ID`, and retries use request and tool
idempotency keys plus state-version and lease fencing.

After building the app images, run the local vertical and deterministic
restart matrix:

```bash
bash scripts/build-app-images.sh
bash scripts/smoke-pilot-agent-a1.sh
bash scripts/fault-pilot-agent-a1.sh
```

The smoke reports the shared Agentd CPU/memory observation as a local baseline;
it is not a production capacity threshold. The D0 companion test exercises 100
persisted idle Sessions and 10 concurrent faux Turns and verifies release,
queue/event timing, invocation counts, and returned-byte bounds.

The first runnable version depends on a local Slurm simulator image named by
`SLURM_SIM_IMAGE`. This lets the project use either a later local Dockerfile or
a school-provided/imported Slurm image without changing the Compose contract.

## Real 107 Profile

The live simulator is intentionally scaled down. Only `anode16` and `anode17`
are backed by Docker `slurmd` containers, but the Slurm configuration exposes
the real 107-style partition and QoS surface:

```text
CPU-6530
CPU-8358P
GPU-RTX5090
GPU-A100
P107-RTX5090
P107-A100
Students
```

Fidelity priority is behavior first, exact numbers second. The simulator must
faithfully exercise Slurm permission and scheduling semantics that affect
107Pilot correctness:

- partition/QoS association;
- user/account/QoS entitlement;
- invalid partition or QoS rejection;
- REST auth differences between simulator and real 107;
- shared `/public` vs node-local `/tmp` path behavior;
- pending/running/terminal state transitions;
- evidence collection permissions.

### Application workspace boundary

The Docker Compose app services explicitly set
`PILOT107_ALLOWED_ROOTS=/public/home/{user}`. `{user}` is expanded only for
the authenticated run owner, so the `alice`/`bob` fixture is a real isolation
check rather than a global two-home allow-list. The same template is passed to
the simulator command gateway, which powers the audited readonly terminal
diagnostics. A non-simulator deployment must replace it
with its actual per-user shared path (for example `/home/scc/{user}`) and set
`PILOT107_CAPABILITY_PROFILE_PATH` to its own probe/profile source.

CPU, memory, GPU count, walltime, and node-count values are representative
fixtures. They should avoid contradicting the 107Pilot profile, but they are not
intended to be exact copies of the live platform. Live deployments must use
current platform facts from CLI/REST/platform pages.

`../../scripts/apply-sim-real107-profile.sh` creates the simulator QoS and user
associations. `scripts/smoke-sim-real107-profile.sh` verifies:

- invalid QoS is rejected;
- a limited student user is rejected from `qos_stu_medium_2gpu`;
- a default student account cannot overreach into the P107 competition account
  partitions;
- `Students/qos_stu_default` accepts a limited user CPU job;
- `Students/qos_stu_medium_2gpu` accepts `--gres=gpu:A100:1`;
- accounting records `Partition=Students` and `QOS=qos_stu_medium_2gpu`.

`../../scripts/report-sim-behavior-fidelity.sh` writes a timestamped JSON report
under `../../simulator/reports/behavior-fidelity/`. The report records the
observed Slurm version, REST/JWT probe status, scheduler fidelity checks,
runtime limitations, and known differences such as unavailable real GPU/NVML
runtime evidence.

Current limitations:

- The source-built `pilot107/slurm-sim:25.11-real107` image is the target path
  for real107-aligned scheduler and REST/JWT behavior. The older Ubuntu 23.11
  image is retained only as a compatibility fallback and must not be used for
  final parity claims.
- `simulator/images/slurm/version-manifest.25.11.json` is copied into the
  source-built target image. `simulator/images/slurm/version-manifest.json`
  describes the retained 23.11 fallback.
- The simulator accepts fake GPU GRES for scheduling, but it does not provide
  real GPU devices or CUDA/NVML runtime evidence.
- Runtime GPU fidelity is intentionally absent: no real CUDA driver, NVML,
  A100/RTX5090 device, or GPU cgroup binding is provided by default.
- 当前真实对照证据是一个已分配的 A100-SXM4-80GB（80 GiB、driver 580.159.03）作业；
  Docker fake GRES 不能假装该运行时，详见
  `artifacts/probes/real107-compute-ssh-20260726T063524Z/`。
