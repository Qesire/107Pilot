# Simulator Behavior Fidelity Rebuild Plan

> 状态：design plan  
> 日期：2026-07-15  
> 目标：重构 Docker Slurm simulator，使其在行为和权限语义上与官方文档描述的 107 平台工作流一致。具体资源数值只作为代表性 fixture，不作为真实平台承诺。
> 
> 注：重新遍历官方 `docs-main` 与 PDF 后形成的当前执行规范为 `docs/phase-1/docker_simulator_real_behavior_rebuild_plan.md`。本文保留为 v1 设计历史和细节补充。

## 1. 设计判断

真实 Web 接入后，分区、QOS、节点、GPU、CPU、内存、walltime 等具体数值可以通过平台页面、CLI、REST 或管理员说明获得。因此模拟器不应追求精确复刻某一次真实平台数值。

但 Slurm 主版本不是普通数值。版本会改变 REST API 路径、认证头、OpenAPI schema、错误码、partial payload、SlurmDBD/TRES 行为和调度语义。真实 107 已观测为 Slurm `25.11.2`，因此模拟器的目标态必须是尽量对齐 Slurm 25.11 行为；当前 Ubuntu Slurm 23.11 只能作为临时 fallback，不应被视为足够真实的长期方案。

模拟器必须真实的是平台固有行为：

- 登录节点不运行重计算，计算任务必须通过 Slurm。
- 作业必须经历 Slurm submit / pending / running / terminal 生命周期。
- partition 与 QOS 组合受 Slurm association 限制。
- 用户 account / QOS entitlement 影响是否能提交。
- shared storage 与 node-local path 行为不同。
- stdout/stderr、`scontrol show job`、`squeue`、`sacct` 能支持诊断。
- REST auth / API version 行为必须按当前模拟器 Slurm 版本真实暴露，不能伪装成真实 107。
- GPU 运行时在无宿主 GPU 默认模拟器中不可验证，必须显式返回 limitation。

## 2. 当前问题分类

### 2.1 需要修复的行为问题

| 问题 | 影响 | 修复方向 |
| --- | --- | --- |
| QOS/account/user association 初始化分散在脚本中 | 可能与 profile、README、测试漂移 | 建立单一 simulator profile，脚本从 profile 生成或校验 |
| partition `MaxTime` 曾低于 profile 允许值 | 预检允许但 Slurm 拒绝 | 保证模拟器不会因代表性数值自相矛盾 |
| fake GPU 可调度但无真实 runtime | 用户可能误解为可验证 CUDA/NVML | 将 scheduler GPU fidelity 与 runtime GPU fidelity 分开声明 |
| REST 23.11 行为与真实 25.11 不同 | 误把 simulator REST 行为外推真实 107 | 显式建立 REST matrix 和版本标签 |
| README 路径与脚本位置容易混淆 | 运维误操作 | 文档和 check 脚本共同验证 |

### 2.2 不应作为本轮目标的问题

| 问题 | 原因 |
| --- | --- |
| 精确复刻真实 107 CPU/GPU/内存数量 | 数值动态且 Web 接入可实时获得 |
| 默认模拟器提供真实 A100/RTX5090 | 需要宿主 GPU、驱动、容器 runtime，成本高且不必要 |
| 不计成本地盲目替换到 Slurm 25.11 | 需要先证明镜像来源、JWT plugin、REST、SlurmDBD、command backend 全部可跑；但 25.11 对齐本身是目标态，不是 optional polish |
| 模拟 InfiniBand / 高速网络 | 官方用户文档没有要求，且不影响 M1 行为闭环 |

## 3. 重构目标架构

```text
config/platform_profiles/simulator-real107-behavior.yaml
        │
        ├── generate/check slurm.conf
        ├── generate/check gres.conf
        ├── apply SlurmDBD account/QOS/user association
        ├── feed 107Pilot CapabilityProfile fixture
        ├── feed smoke tests
        └── render simulator README capability table

simulator image
        ├── Slurm runtime and REST daemon
        ├── deterministic simulator identities
        ├── deterministic MUNGE/JWT test keys
        ├── fake GRES scheduler devices
        └── explicit runtime limitations

behavior tests
        ├── official CLI commands work
        ├── valid carrier profile accepted
        ├── invalid QOS/partition rejected
        ├── path visibility rules enforced
        ├── REST auth/version behavior documented
        ├── evidence collection works
        └── GPU runtime unavailable is explicit
```

## 4. Single Simulator Profile

新增：

```text
config/platform_profiles/simulator-real107-behavior.yaml
```

建议 schema：

```yaml
schema: pilot107.simulator_profile.v1
source_authority: simulator_fixture

fidelity:
  scheduler: true
  accounting: partial
  rest: slurm_23_11
  gpu_runtime: unavailable
  exact_capacity: false

defaults:
  docs_default:
    partition: Students
    qos: qos_stu_default
  competition_carrier_default:
    partition: Students
    qos: qos_stu_medium_2gpu

users:
  - name: alice
    uid: 11001
    account: students
    default_qos: qos_stu_medium_2gpu
  - name: bob
    uid: 11002
    account: students
    default_qos: qos_stu_medium_2gpu

roots:
  shared:
    - /public
  local:
    - /tmp
    - /usr
    - /var
    - /opt

partitions:
  - name: Students
    nodes: anode[16-17]
    default: true
    max_time_fixture: 3-00:00:00
    allow_qos:
      - qos_stu001
      - qos_stu_default
      - qos_stu_small
      - qos_stu_medium
      - qos_stu_medium_2gpu
      - qos_stu_long
      - qos_stu_cpu_long

qos:
  - name: qos_stu_default
    behavior_role: default_small_job
    limits_fixture:
      cpus: 4
      gpus: 1
      memory: 16G
      walltime: 04:00:00
  - name: qos_stu_medium_2gpu
    behavior_role: competition_carrier
    limits_fixture:
      cpus: 24
      gpus: 2
      memory: 128G
      walltime: 12:00:00

limitations:
  - simulator Slurm version differs from real107 observed Slurm 25.11.2
  - GPU scheduling uses fake GRES files
  - CUDA/NVML runtime is unavailable unless a separate GPU-enabled profile is added
```

This profile is not the source of real platform numbers. It is the source of simulator behavior fixtures.

## 5. Image Rebuild Design

### 5.1 Current image responsibilities

Current image includes:

- Ubuntu 24.04 userspace.
- Slurm 23.11 runtime packages.
- `auth_jwt.so` compiled from Ubuntu Slurm source.
- `slurmctld`, `slurmd`, `slurmdbd`, `slurmrestd`.
- local MariaDB server/client.
- simulator users: `alice`, `bob`, `pilot107`, `slurm`.
- static MUNGE key and generated JWT key.

This is acceptable for a simulator image, but responsibilities should be made explicit.

### 5.2 Target image layers

Split the image conceptually, even if still built as one Dockerfile initially:

```text
base-os
  └── Ubuntu 24.04 + CA + gosu + tini + python3

slurm-runtime
  └── slurm-client/slurm-wlm/slurmdbd/slurmrestd/munge/mariadb-client

jwt-plugin-builder
  └── build auth_jwt.so matching installed Slurm package ABI

sim-users
  └── slurm, pilot107, alice, bob fixed fixture users

sim-runtime
  └── static MUNGE key, JWT key, directories, entrypoint
```

Do not add application code into the Slurm simulator image except command-gateway scripts mounted read-only by compose.

### 5.3 Slurm Version Strategy

Version fidelity is behavior fidelity. The rebuild has two tracks, but they are not equal priority.

#### Track A: real107-matched Slurm 25.11 target

Goal image:

```text
pilot107/slurm-sim:25.11-real107
```

Target:

- Slurm release matches or is close to observed real107 `25.11.2`.
- REST API supports the same effective API version family observed on real107 (`v0.0.41` in current probe).
- Auth behavior can model real107 Bearer-token REST path and, where needed, simulator command-gateway path.
- OpenAPI digest can be captured and compared against real107 probe artifacts.
- SlurmDBD/TRES/QOS behavior is closer to real107 than Ubuntu 23.11.
- All official behavior tests pass.

Implementation routes to evaluate:

```text
Route A: build Slurm 25.11.x from source in Docker
Route B: use an upstream/SchedMD-compatible package source if stable and reproducible
Route C: import a school-provided Slurm 25.11-compatible base image, if available
```

The route is an engineering choice; the acceptance criteria are behavioral.

Acceptance before making it default:

- `slurmctld`, `slurmd`, `slurmdbd`, `slurmrestd` start reliably.
- JWT auth works.
- REST query/submit/cancel smoke tests pass on the target API version.
- REST error behavior and partial payload handling match real107 probes or are explicitly diffed.
- command backend still works.
- all behavior tests pass.
- build is reproducible enough for competition delivery, either from checked scripts plus pinned source digest or a controlled base image.

#### Track B: temporary Slurm 23.11 fallback

Current Ubuntu 23.11 image remains only as a fallback while Track A is being built.

Rules:

- It must be labeled `legacy-23.11` or equivalent in reports.
- It must never be presented as real107-equivalent.
- REST results from it must not be used to infer real107 behavior.
- Behavior tests may continue to run on it to preserve Docker competition progress.
- Any divergence from real107 25.11 must appear in the behavior fidelity report.

Exit condition for fallback:

- 25.11 target image passes S1-S4 behavior matrix and replaces 23.11 in the default compose path.

## 6. Compose And Runtime Design

### 6.1 Services

Keep these simulator services:

- `mariadb`
- `slurmdbd`
- `slurmctld`
- `slurmrestd`
- `login-node-sim`
- `worker-1`
- `worker-2`
- optional `pilot107-command-gateway`
- app services under profile

### 6.2 Node semantics

Use a small node set but preserve semantics:

- one login node that can submit and query but should not be used for heavy compute;
- at least two worker nodes for scheduling and state transitions;
- optional down/draining placeholder node for failure-state fixtures.

Numerical resources are representative. They should be internally consistent, not real-capacity exact.

### 6.3 Storage semantics

Must preserve:

- `/public` shared between login and compute.
- `/public/home/alice` and `/public/home/bob` separated by Unix ownership.
- `/tmp` local per container.
- API/Worker read permissions only through allowed roots.

Required behavior tests:

- shared workdir succeeds;
- `/tmp` workdir is rejected by 107Pilot preflight;
- unauthorized user path is rejected;
- symlink/path escape is rejected by EvidenceTransport.

## 7. SlurmDBD / QOS / Association Design

`apply-sim-real107-profile.sh` should become generated or profile-driven.

Required behavior:

- creates `students` account;
- creates all QOS names used by profile;
- adds `alice` and `bob` to account;
- grants student QOS set;
- sets default QOS for carrier profile;
- is idempotent;
- prints a verification table;
- exits non-zero when behavior-critical setup fails.

Best-effort numeric limits:

- walltime / CPU / memory may be set if SlurmDBD accepts fields;
- GPU TRES may be set only if `gres/gpu` appears in accounting TRES;
- failure to set GPU numeric accounting is a limitation, not a blocker, if behavior tests still cover invalid QOS and valid carrier profile.

Do not treat exact numeric SlurmDBD limits as the main acceptance criterion.

## 8. Official Behavior Matrix

The simulator is accepted only if these official-doc workflows behave correctly:

| Official workflow | Required simulator behavior |
| --- | --- |
| `sbatch scripts/train.sbatch` | job accepted for valid partition/QOS/workdir |
| `squeue -u "$USER"` | pending/running job visible with state/reason |
| `scontrol show job <job_id>` | job detail visible for evidence |
| `scancel <job_id>` | owned job can be cancelled |
| `scontrol show part` | exposes partition/QOS/state/MaxTime/TRES-like fields |
| `sinfo` | exposes node states including idle/mix/down/drain fixture when configured |
| invalid QOS | Slurm rejects without relying on 107Pilot preflight |
| invalid partition/QOS combination | Slurm rejects |
| shared `/public` workdir | compute node can see it |
| local `/tmp` workdir | 107Pilot rejects before submit |
| GPU request | scheduler can accept fake GRES for carrier profile |
| GPU runtime probe | returns explicit unavailable/limitation without pretending CUDA exists |
| REST query | works with simulator-specific auth/version |
| REST behavior vs real 107 | target image matches real107-observed API/version/auth where possible; any difference is diffed and justified |

## 9. Version Fidelity Matrix

The version matrix is a release gate, not a nice-to-have.

| Dimension | 23.11 fallback | 25.11 target | Gate |
| --- | --- | --- | --- |
| Slurm release | Documented mismatch | Matches/near-matches real107 observed `25.11.2` | Target required before final parity claim |
| REST API version | `v0.0.40` style behavior | `v0.0.41` style behavior if supported | Query/submit/cancel smoke |
| Auth behavior | `X-SLURM-USER-*` simulator behavior | Bearer-token behavior or explicitly matched real107 mode | Auth smoke + negative auth tests |
| OpenAPI | simulator-specific digest | captured and compared to real107 digest | digest artifact |
| SlurmDBD/TRES | known GPU accounting gap | re-evaluated against 25.11 | QOS/TRES smoke |
| Error semantics | documented legacy quirks | compared against real107 probes | failure fixture tests |

## 10. Test Plan

### 10.1 Image checks

Extend `scripts/check-slurm-sim-image.sh`:

- `slurmctld`, `slurmd`, `slurmdbd`, `slurmrestd` exist.
- `sbatch`, `squeue`, `sinfo`, `scontrol`, `sacct`, `sacctmgr`, `scancel` exist.
- `auth_jwt.so` exists.
- users exist.
- static MUNGE key exists and is simulator-scoped.
- image labels or README state Slurm package version.
- `scontrol --version` output is captured.
- `slurmrestd` supported API versions are captured.

### 10.2 Config checks

Add a non-Docker parser test:

- `slurm.conf` exposes expected partition names.
- `Students` allows all student QOS names.
- `Students` `MaxTime` does not contradict long-QOS behavior fixture.
- `debug` remains marked legacy.
- `gres.conf` exposes fake GPU scheduler devices and README states runtime limitation.

### 10.3 Live simulator smoke

Use existing scripts and add missing checks:

- `check-sim-core.sh`
- `probe-sim-rest-auth.sh`
- `probe-sim-rest-submit.sh`
- `smoke-sim-real107-profile.sh`
- `smoke-sim-evidence.sh`
- `smoke-sim-evidence-permissions.sh`

Required additions:

- invalid partition/QOS combination smoke;
- shared vs local path behavior smoke;
- `squeue` pending reason snapshot smoke;
- GPU runtime unavailable warning smoke.

### 10.4 Official CLI snapshot smoke

Run inside `login-node-sim`:

```bash
python3 probe_real107_cli_snapshot.py --out-dir /public/home/alice/platform-snapshot
```

Acceptance:

- `platform_snapshot.json` created;
- `parsed/partitions.json` includes `Students`;
- `parsed/nodes.json` includes worker nodes;
- `raw/scontrol-show-part.txt`, `raw/sinfo.txt`, `raw/squeue.txt` exist;
- redaction report exists.

## 11. Final Confirmation Procedure

Before declaring simulator behavior aligned with official docs:

1. Build image.
2. Run image checks.
3. Start core simulator.
4. Apply simulator real107 behavior profile.
5. Capture Slurm version, supported REST API versions and OpenAPI digest.
6. Compare simulator version/API/auth facts against real107 probe facts.
7. Run official CLI snapshot smoke.
8. Run valid carrier submit smoke.
9. Run invalid QOS and invalid partition/QOS smoke.
10. Run REST auth/query/submit smoke.
11. Run evidence and permission smoke.
12. Generate a behavior report:

```text
artifacts/simulator/behavior-fidelity-report.json
artifacts/simulator/behavior-fidelity-report.md
```

Report fields:

```json
{
  "schema": "pilot107.simulator_behavior_fidelity_report.v1",
  "generated_at": "...",
  "image": "...",
  "slurm_version": "...",
  "rest_api_version": "...",
  "version_fidelity": "matched|near_match|legacy_fallback",
  "real107_observed_version": "25.11.2",
  "scheduler_fidelity": "passed",
  "runtime_gpu_fidelity": "unavailable",
  "official_workflows": {
    "sbatch": "passed",
    "squeue": "passed",
    "scontrol_show_job": "passed",
    "scontrol_show_part": "passed",
    "sinfo": "passed",
    "scancel": "passed"
  },
  "permission_semantics": {
    "valid_carrier_profile": "passed",
    "invalid_qos_rejected": "passed",
    "invalid_partition_qos_rejected": "passed",
    "shared_path_visible": "passed",
    "local_tmp_rejected": "passed"
  },
  "limitations": [
    "Slurm version differs from real107 observed 25.11.2, if using fallback",
    "No real CUDA/NVML/GPU runtime in default simulator"
  ]
}
```

## 12. Implementation Order

### Phase S1: Documentation and config truth

- Add simulator behavior profile YAML.
- Update README fidelity table.
- Add static config tests.

Exit: static tests prove no documented script/path/config drift.

### Phase S2: SlurmDBD behavior profile

- Refactor `apply-sim-real107-profile.sh` to read or validate against profile.
- Strengthen smoke test for invalid QOS and valid carrier.
- Add invalid partition/QOS smoke.

Exit: Slurm itself enforces behavior-critical permission semantics.

### Phase S3: 25.11 image rebuild

- Build `pilot107/slurm-sim:25.11-real107` candidate.
- Pin source/package provenance.
- Rebuild or replace JWT plugin path for 25.11.
- Capture `scontrol --version`, REST API versions and OpenAPI digest.
- Run REST/auth/submit/cancel smoke.
- Keep 23.11 only as fallback until this passes.

Exit: 25.11 candidate is runnable and passes core Slurm/REST smoke, or a written blocker explains exactly what external dependency prevents it.

### Phase S4: Official behavior report

- Add behavior fidelity report script.
- Run full simulator smoke matrix.
- Archive report in `artifacts/simulator/`.

Exit: report shows official workflows passed, version fidelity reported and limitations explicit.

### Phase S5: Default switch

- Switch compose default image tag to 25.11 target after S1-S4 pass.
- Keep 23.11 fallback path documented for emergency only.

Exit: default simulator no longer depends on 23.11 for normal validation.

## 13. Non-goals

- Exact live capacity reproduction.
- Real GPU runtime in default simulator.
- Web Terminal.
- SSH command proxy.
- InfiniBand / multi-node performance simulation.
- Hiding simulator-vs-real Slurm version differences.
- Declaring parity while still using a mismatched Slurm version without a report.
