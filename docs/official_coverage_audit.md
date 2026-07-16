# 107Pilot Official Coverage Audit

> Date: 2026-07-15  
> Scope: official `docs-main`, 107 platform supplemental docs, current 107Pilot implementation, Docker Slurm simulator.

## 1. Audit Conclusion

107Pilot already covers the core competition path:

- Slurm submit / get / cancel lifecycle through backend contracts.
- Resource preflight for partition, QOS, CPU, GPU, memory and walltime.
- CapabilityProfile API for partitions, QOS limits, REST capability, shared/local root semantics and dynamic-fact warnings.
- Evidence collection for submission snapshot, `sacct`, `scontrol show job`, stdout/stderr tails, environment summary and output inventory.
- Rule-based diagnosis for invalid QOS, invalid partition, timeout, OOM, missing package, missing command, non-zero exit, unshared workdir and related runtime failures.

However, the system is not yet complete against the official documentation standard of "if a shell command can obtain platform information, the system must include that ability." The largest remaining gaps are:

1. No first-class platform snapshot for CLI facts from `scontrol show part`, `sinfo`, `hostname`, `pwd`, `python -V`, `whoami`, `date`, `nvidia-smi`, `which python`, `pip list` / `pip freeze`, `conda env list`, file-size/hash checks and disk/storage facts.
2. Real107 probe currently uses Slurm REST read-only endpoints, but does not run the official CLI fact commands. It cannot fully reproduce the official docs' current-platform collection procedure.
3. Evidence environment summary only captures `hostname`, `id`, and filtered `env`; it does not capture Python/conda/GPU/runtime versions recommended by docs.
4. Simulator is intentionally scaled down. Exact CPU/GPU/memory/walltime/node-count values are representative fixtures; the critical fidelity target is Slurm behavior and permissions: user/account/QOS association, partition/QOS rejection, REST auth, path visibility, state transitions and evidence permissions.
5. Simulator README must make that fidelity boundary explicit. Numeric limits may be best-effort SlurmDBD fixtures or 107Pilot preflight checks; they must not be presented as exact real-107 capacity.

## 2. Official Coverage Inventory

### 2.1 Official Commands That Must Be Representable

From `docs-main/docs/basics/cli-index.md`, `slurm.md`, `jobs.md`, `quickstart.md`, `faq.md`, and `contributing/writing-guide.md`.

| Area | Official command / data source | Current 107Pilot coverage | Gap |
| --- | --- | --- | --- |
| Slurm submit | `sbatch scripts/train.sbatch` | Covered by command backend and REST submit abstraction. | Need user-visible submitted script plus exact sbatch argv is present; already mostly covered. |
| Job status | `squeue -u "$USER"` | Partially covered by backend `get_job`; no raw `squeue` artifact. | Add optional `squeue` collection artifact for platform snapshot / diagnosis. |
| Job detail | `scontrol show job <job_id>` | Covered in terminal accounting evidence. | Good. |
| Cancel | `scancel <job_id>` | Covered by backend cancel. | Good. |
| Partition config | `scontrol show part` | REST `/partitions` probe and parsed CapabilityProfile. | Add raw CLI `scontrol show part` artifact and parser for `PartitionName`, `AllowAccounts`, `AllowQos`, `MaxTime`, `State`, `TRES`, `Nodes`. |
| Node status | `sinfo` | REST `/nodes` probe partially covers nodes. | Add raw CLI `sinfo` artifact and normalized node states: `idle`, `mix`, `comp`, `down`, `drng`. |
| Interactive CPU | `srun -p Students --qos=qos_stu_default -c 1 -t 00:10:00 --pty bash` | Not represented as a first-class operation. | Add documented "interactive probe template" or explicitly mark out-of-scope for managed workflow. |
| Interactive GPU | `srun ... --gres=gpu:1 ... --pty bash` | Not represented as a first-class operation. | Same as above; at minimum expose generated smoke sbatch instead of interactive shell. |
| Node identity | `hostname`, `pwd`, `whoami`, `date` | Evidence has `hostname`, `id`, `env`; not `pwd`, `whoami`, `date`. | Add to environment/runtime snapshot. |
| Python runtime | `python -V`, `which python`, `python -m py_compile` | Not collected generically. | Add optional environment probe commands. |
| Conda / pip | `conda env list`, `conda activate`, `pip list`, `pip freeze` | Diagnosis detects missing packages, but no environment inventory. | Add sanitized environment inventory mode. |
| GPU runtime | `nvidia-smi`, `torch.cuda.is_available()` | User script may include it; 107Pilot does not inject/collect. | Add GPU sanity probe when `ResourcePlan` requests GPU. |
| File integrity | `ls -lh`, `sha256sum`, `tar -czf`, `tar -xzf` | Output inventory captures size/hash after run. | Add upload/pre-submit file-integrity helper if file transfer becomes a product feature. |
| Logs | `tail -n`, `tail -f`, `.out`, `.err` | Tail evidence covered for stdout/stderr. | Good; add source path from `#SBATCH -o/-e` when custom logs are supported. |
| Text search | `grep -n "Error" logs/job.err` | Diagnosis scans snippets. | Good internally; no user-facing grep artifact needed. |
| File editing | `nano`, `vim`, `sed -n` | Out of product scope. | Mark explicitly out-of-scope. |
| Shell/file ops | `mkdir`, `cd`, `cp`, `mv`, `rm`, `tar` | Workdir/output preflight and output inventory only. | Product should not expose arbitrary shell; document as user workflow, not required backend ability. |

### 2.2 Official Platform Facts

| Fact | Official requirement | Current coverage | Gap |
| --- | --- | --- | --- |
| Default partition/QOS | Usually `Students` / `qos_stu_default`; platform page is authoritative. | CapabilityProfile has docs-main QOS table; Docker default is `Students/qos_stu_medium_2gpu`. | API should expose both "docs default" and "competition carrier default" to avoid ambiguity. |
| Student QOS table | `default`, `small`, `medium_2gpu`, `long`, `cpu_long` with CPU/GPU/memory/walltime. | Covered in `_docs_main_qos_capabilities()`. | `qos_stu_medium` and `qos_stu001` remain numeric-unknown; OK if explicitly shown as unknown. |
| Partition fields | `PartitionName`, `AllowAccounts`, `AllowQos`, `Nodes`, `State`, `TRES`, `MaxTime`. | Current `PartitionCapability` has name, nodes, total_nodes, allow_qos, state, gpu_types. | Missing `AllowAccounts`, `TRES`, `MaxTime`. |
| Node states | `idle`, `mix`, `comp`, `down`, `drng`. | Static profile mostly uses `UP`; real probe may preserve REST state. | Need normalized `sinfo`/node state view. |
| GPU models | A100, RTX5090 / 5090 are dynamic facts. | Static profile has A100 and RTX5090. | Need CLI/probe source, driver, CUDA compatibility and per-node distribution. |
| Shared/local paths | Shared `/public`; local `/tmp`, `/usr`, `/var`, `/opt`. | Covered in CapabilityProfile and workdir preflight. | Good. Consider adding storage quota/capacity when docs confirm source. |
| Login vs compute nodes | Login shell not for heavy compute; compute node via Slurm. | Mostly represented in docs/profile. | Need environment snapshot to distinguish scope: login-node probe vs compute-job probe. |
| Resource application | Apply page and QOS grant workflow. | Not modeled. | Add external/manual status field, not automated unless API/source exists. |

### 2.3 Official Runtime Evidence

The docs recommend preserving:

- job ID;
- submit script;
- resource request fields;
- `.out` and `.err` logs;
- `scontrol show job <job_id>`;
- `squeue -u "$USER"` when pending/running;
- environment info (`hostname`, Python, conda);
- GPU info (`nvidia-smi`) for GPU jobs;
- output paths and file inventory;
- steps already tried when asking for help.

Current 107Pilot evidence already preserves job ID, scripts, resource plan indirectly, `sacct`, `scontrol show job`, logs and output inventory. Missing or partial:

- raw `squeue` during pending/running;
- explicit resource request summary artifact independent of submit response;
- Python/conda/pip/runtime snapshot;
- GPU snapshot for GPU jobs;
- `pwd`, `whoami`, `date`;
- run-step timeline suitable for "what has already been tried".

## 3. Implementation Coverage

### 3.1 CapabilityProfile

Implemented in `src/pilot107/core/platform.py`.

Covered:

- `profile_id`, `source_authority`, `captured_at`, freshness;
- shared/local roots;
- default partition/QOS;
- partition list and allowed QOS;
- QOS numeric limits from docs-main;
- REST capability and OpenAPI digest;
- dynamic facts and limitations.

Missing fields for complete official coverage:

- partition `AllowAccounts`;
- partition `MaxTime`;
- partition `TRES`;
- Slurm raw version source by CLI;
- node state inventory from `sinfo`;
- GPU driver/CUDA compatibility;
- storage capacity/cleanup policy;
- module/software stack.

### 3.2 Slurm Backend

Implemented in `src/pilot107/adapters/slurm.py`.

Covered:

- submit, get job, cancel;
- command backend with structured argv;
- REST native backend;
- sbatch options: `--partition`, `--nodes`, `--ntasks`, `--cpus-per-task`, `--time`, `--qos`, `--mem`, `--gres`, `--gpus`, `--gpus-per-node`, `--array`;
- basic path/value safety.

Missing:

- first-class read-only command inventory service for official collection commands;
- `squeue`, `sinfo`, `scontrol show part` exposed as platform snapshot artifacts;
- `sacctmgr list qos` or equivalent when available;
- `srun --pty` is not represented and should either be explicitly out-of-scope or implemented as a guided template.

### 3.3 Preflight

Implemented in `src/pilot107/core/resources.py` and `src/pilot107/core/preflight.py`.

Covered:

- positive resource fields;
- GPU count consistency;
- required walltime;
- partition/QOS existence and allowed-QOS matching;
- QOS CPU/GPU/memory/walltime limits;
- absolute workdir;
- allowed roots;
- local laptop paths;
- local ephemeral paths such as `/tmp`;
- shared-vs-local classification;
- filesystem existence/read/execute/write checks.

Gaps:

- no preflight against partition `MaxTime` from live `scontrol show part`;
- no preflight against live `TRES` or per-node GPU availability;
- no account-level `AllowAccounts` check;
- no filesystem quota/capacity check.

### 3.4 Evidence And Diagnosis

Implemented in `src/pilot107/worker/evidence.py` and `src/pilot107/core/diagnosis.py`.

Covered:

- submission scripts and wrapper;
- Slurm accounting via `sacct`;
- job detail via `scontrol -o show job`;
- stdout/stderr tail with hashes;
- basic environment summary;
- output inventory with size, mtime and hash;
- derived result summary;
- known-error rule engine.

Gaps:

- environment summary is too thin for official docs;
- GPU jobs do not automatically capture `nvidia-smi`;
- pending/running diagnosis should collect `squeue` and pending reason earlier;
- known error library lacks explicit rules for:
  - `QOSMaxWallDurationPerJobLimit`;
  - `QOSMaxCpuPerUserLimit`;
  - `nvidia-smi` unavailable because job did not request GPU;
  - `Failed to initialize NVML: Driver/library version mismatch`;
  - missing log directory / bad `#SBATCH -o/-e`;
  - conda not initialized in batch script;
  - CPU PyTorch installed due to login-node/no-GPU install context.

## 4. Real107 Probe Coverage

Implemented in `scripts/real107_probe/probe_real107_snapshot.py`.

Covered:

- short-lived token via env or `scontrol token lifespan=600`;
- REST GET only;
- `/ping`, `/partitions`, `/nodes`, `/jobs`, OpenAPI;
- redacted output;
- configuration snapshot and report;
- partial REST payload handling.

Gaps against official docs:

- no CLI `scontrol show part`;
- no CLI `sinfo`;
- no CLI `squeue -u "$USER"`;
- no `hostname`, `pwd`, `whoami`, `date`, `python -V`;
- no `nvidia-smi` or GPU driver/CUDA probe;
- no storage/root capacity probe such as `df`;
- no software/module/conda inventory;
- no `sacctmgr list qos` or equivalent QOS numeric source.

The real107 probe should gain a second, explicitly read-only "CLI fact snapshot" path. It must redact username, home path, node names if needed, and never persist tokens.

## 5. Simulator Fidelity Audit

### 5.1 Matched / Acceptable

| Official / real107 characteristic | Simulator status |
| --- | --- |
| Ubuntu 24.04 family | Uses `ubuntu:24.04`. |
| Slurm REST port 6820 | Exposes `slurmrestd` on 6820. |
| JWT REST auth path | Uses `rest_auth/jwt` and shared HS256 key. |
| Shared storage root | Mounts Docker volume at `/public`. |
| Local roots | Container-local `/tmp`, `/usr`, `/var`, `/opt` semantics exist. |
| SlurmDBD/accounting path | Uses SlurmDBD with MariaDB. |
| TRES selector | Uses `SelectType=select/cons_tres`, `SelectTypeParameters=CR_Core_Memory`. |
| 107-style partition names | Includes `CPU-6530`, `CPU-8358P`, `GPU-RTX5090`, `GPU-A100`, `P107-RTX5090`, `P107-A100`, `Students`. |
| Student QOS names on Students | `AllowQos` includes official student QOS names. |

### 5.2 Known Fidelity Gaps

| Area | Simulator | Official / observed real107 | Impact |
| --- | --- | --- | --- |
| Slurm version | Ubuntu package Slurm 23.11 runtime. | Probe observed Slurm release 25.11.2. | REST/OpenAPI and scheduler behavior may differ. |
| Node count | 3 defined nodes; 2 live workers. | Observed `Students` spans `anode[05-17]`; probe saw 19 node records. | Scheduling and queue-state realism limited. |
| GPU count | anode05 RTX5090 x4 placeholder, anode16/17 A100 x2 each. | Official examples mention larger node resources and dynamic GPU distribution. | Cannot represent real capacity or contention. |
| GPU device enforcement | fake files in `/tmp`, `ConstrainDevices=no`. | Real GPUs and driver stack. | `nvidia-smi`/CUDA cannot be realistically validated. |
| Memory | 4GB / 8GB simulated nodes. | Official table/examples imply much larger memory. | Memory-limit behavior does not match. |
| Partition MaxTime | All visible partitions `MaxTime=01:00:00`. | Docs QOS table includes 4h, 8h, 12h, 72h. | Live Slurm rejects longer jobs even when 107Pilot profile allows them. |
| QOS / association behavior | Simulator must reject invalid QOS and accept valid Students carrier profile. | Real platform behavior is governed by Slurm association/QOS. | Behavior is more important than exact numeric limit matching. |
| Missing script | README references `scripts/apply-sim-real107-profile.sh`; file absent. | Should seed or document QOS/accounting setup. | Documentation/config drift. |
| Scheduler parameters | no explicit `SchedulerType=sched/backfill`, priority/fairshare/preempt config. | Official/platform details are dynamic/architecture-specific. | Queue behavior differs. |
| Job accounting gather | `JobAcctGatherType=jobacct_gather/none`. | Real cluster may gather richer stats. | Runtime CPU/memory/GPU usage accounting unavailable. |
| Cgroups | `ConstrainCores=no`, `ConstrainRAMSpace=no`, `ConstrainDevices=no`. | Real cluster likely enforces. | Overuse and GPU binding behavior not realistic. |
| Module/software env | No Lmod/environment-modules baseline. | Official docs mention software/env as dynamic. | Environment troubleshooting differs. |
| Monitoring | No Prometheus/Grafana/exporters. | Platform may have admin monitoring; user docs do not expose full details. | Operational fidelity limited. |
| Network | Docker bridge, no IB/fabric. | Real cluster likely has HPC network topology. | Multi-node performance not modeled. |

## 6. Priority Remediation Plan

### P0: Add Official Platform Snapshot

Create a read-only `PlatformSnapshot` collector with raw and parsed artifacts:

- `hostname`
- `pwd`
- `whoami`
- `date`
- `python -V`
- `squeue -u "$USER"`
- `scontrol show part`
- `sinfo`
- optional `sacctmgr list qos` if permission exists
- optional `df -h /public "$HOME"` if accepted by product scope

Parse into:

- partitions: `PartitionName`, `AllowAccounts`, `AllowQos`, `Nodes`, `State`, `TRES`, `MaxTime`;
- nodes: state, partition, GRES, CPU/memory if available;
- observed login environment.

Expose through:

- artifact under `platform/snapshot.json`;
- API endpoint, for example `GET /api/v1/platform/snapshot`;
- merged fields in `CapabilityProfile` when snapshot source is fresh.

### P0: Expand Runtime Evidence

For every run:

- add `pwd`, `whoami`, `date`, `python -V`, `which python`;
- include resource request summary as a stable JSON artifact;
- collect `squeue -u "$USER"` when run is pending/running.

For GPU runs:

- inject or recommend `nvidia-smi` collection;
- capture output as `environment/gpu.json`;
- optionally collect `python - <<PY ... torch.cuda.is_available() ... PY` when project declares PyTorch.

### P1: Fix Simulator Behavior Drift

Prioritize behavior and permission fidelity over exact numeric matching:

- ensure account/user/QOS associations are initialized;
- ensure invalid partition/QOS combinations are rejected by Slurm;
- ensure the valid `Students/qos_stu_medium_2gpu` carrier profile is accepted;
- ensure partition `MaxTime` does not contradict the representative 107Pilot profile;
- document that exact CPU/GPU/memory/walltime values are representative and must be replaced by live CLI/REST/platform facts in real deployments.

Do not spend effort making simulator numbers exact unless those values affect a behavior test.

### P1: Extend Diagnosis Rules

Add known-error YAMLs for:

- `SLURM.QOS_WALLTIME_LIMIT`;
- `SLURM.QOS_CPU_LIMIT`;
- `RUNTIME.NVIDIA_SMI_NO_GPU`;
- `RUNTIME.NVML_DRIVER_MISMATCH`;
- `RUNTIME.LOG_PATH_MISSING`;
- `RUNTIME.CONDA_NOT_INITIALIZED`;
- `RUNTIME.PYTORCH_CPU_ONLY`;
- `SLURM.PENDING_RESOURCE_OR_QOS`.

### P2: Clarify Product Boundaries

Explicitly document which official commands are product capabilities and which remain user workflow:

- product capability: Slurm query/submit/cancel, platform snapshot, evidence, diagnosis;
- guided user workflow: `nano`, `vim`, `cp`, `mv`, `rm`, `tar`, interactive `srun --pty`;
- out-of-scope: arbitrary shell execution from web UI, unless routed through a narrow command gateway with allowlisted commands.

## 7. Acceptance Checklist

The official-coverage work should not be considered complete until:

- `CapabilityProfile` includes parsed `MaxTime`, `TRES`, `AllowAccounts` or explicitly marks each as unavailable.
- A real107 CLI snapshot artifact exists beside the REST probe artifact.
- The Web/API can show partition/QOS values with source authority and freshness.
- Evidence for a GPU run includes `nvidia-smi` or an explicit warning that GPU runtime probing was skipped.
- Evidence for all runs includes Python path/version and current directory.
- Diagnosis can recognize the FAQ errors called out in docs-main.
- Simulator README no longer references absent scripts.
- Simulator QOS/walltime mismatch is either fixed or prominently marked as a known limitation.
