# Official Coverage Next Slice Design

> 状态：design  
> 日期：2026-07-15  
> 前置状态：Docker simulator 已切到 Slurm `25.11.2` target；REST `v0.0.41` live smoke supported；Evidence 已并行写入 `run/request/*`、`run/environment/basic.json`、`run/timeline/events.jsonl`。

## 1. 目标

下一切片只补官方覆盖链路中的剩余关键缺口，不扩展 Web Terminal，不引入任意 Shell。

目标链路：

```text
Submit request
→ sbatch wrapper
→ compute-job runtime probe
→ squeue state/reason timeline
→ evidence-indexed artifacts
→ multi-evidence diagnosis
→ Docker regression
```

本切片的核心判断：

- 当前 `run/environment/basic.json` 仍由 worker 在作业结束后以用户身份采集，足以做登录节点/服务侧证据，但不能替代“用户程序运行前、计算节点内部”的 runtime probe。
- 当前 `run/timeline/events.jsonl` 已从 `RunStore` 事件导出，但还不是 Slurm `squeue` 事实时间线，缺少 Pending Reason 变化。
- 当前诊断规则已数据化，但仍偏文本匹配；下一步只接入少量结构化信号，不一次性重写完整规则引擎。

## 2. 非目标

本切片不做：

- 任意命令执行接口。
- Web Terminal。
- 自动安装 Conda 或 Python 包。
- 真实 GPU Docker 仿真。
- 真实 107 submit/cancel 作为阻塞项。
- 大规模 UI 重做。

## 3. Runtime Probe 设计

### 3.1 位置

runtime probe 必须在 Slurm 作业内部执行，且在用户脚本之前执行。

实现位置：

```text
src/pilot107/core/submission_templates.py 或新建 runtime_probe.py
src/pilot107/adapters/slurm.py
```

现有 backend 不应各自拼接一套 wrapper；应提取一个共享 builder：

```text
build_sbatch_script(user_script, resource_plan, run_id) -> GeneratedSubmission
```

`GeneratedSubmission` 至少包含：

- `submitted_script`
- `runtime_probe_script`
- `user_script`
- `sbatch_argv_metadata`
- `artifact_manifest`

Command backend 可以把这些文件写入 workdir；REST backend 可以把合成后的 `submitted_script` 放进 REST payload。两者必须得到同构 Evidence。

### 3.2 作业内文件路径

作业内只写用户 workdir 下的受控目录：

```text
<workdir>/.107pilot/runs/<run_id>/
├── request/
│   ├── submitted-script.sbatch
│   └── user-script.sh
├── environment/
│   ├── basic.json
│   └── gpu.json
└── timeline/
    └── runtime-events.jsonl
```

Worker 收集时复制或索引到 EvidenceStore 的逻辑路径：

```text
run/request/submitted-script.sbatch
run/environment/basic.json
run/environment/gpu.json
run/timeline/runtime-events.jsonl
```

保留当前服务侧 `run/environment/basic.json` 时，必须区分 scope：

```json
{
  "scope": "compute_job",
  "source": "slurm_wrapper"
}
```

如果暂时同时存在 post-run worker probe，命名为：

```text
run/environment/collector-side-basic.json
```

避免用登录节点事实冒充计算节点事实。

### 3.3 Basic Probe 内容

默认采集：

- `pwd`
- `whoami`
- `date -Is`
- `hostname`
- `python -V`
- `which python`
- `CONDA_DEFAULT_ENV`
- allowlist Slurm env：`SLURM_JOB_ID`、`SLURM_JOB_NAME`、`SLURM_JOB_PARTITION`、`SLURM_JOB_QOS`、`SLURM_JOB_NODELIST`、`SLURM_SUBMIT_DIR`、`SLURM_CPUS_ON_NODE`、`SLURM_GPUS`、`CUDA_VISIBLE_DEVICES`
- workdir path 与 expected workdir 是否一致

Probe 失败不得导致用户作业失败。失败写入：

```json
{
  "status": "degraded",
  "failed_commands": [...]
}
```

### 3.4 GPU Probe

当 `ResourcePlan` 请求 GPU 时，在用户脚本前执行：

```text
nvidia-smi --query-gpu=name,uuid,driver_version,memory.total --format=csv,noheader
```

输出：

```text
run/environment/gpu.json
```

GPU UUID 默认脱敏。

在默认 Docker simulator 中，没有真实 CUDA/NVML 是预期结果：

```json
{
  "status": "unavailable",
  "reason": "command_not_found | no_device | nvml_error | permission_denied",
  "simulator_limitation": true
}
```

这不能标记为作业失败，但必须进入 result summary 和 diagnosis evidence refs。

## 4. Squeue Timeline 设计

### 4.1 现有事件时间线

保留当前：

```text
run/timeline/events.jsonl
```

它表达 107Pilot 内部事件，如 `JOB_ACCEPTED`、`SLURM_STATE_OBSERVED`、`EVIDENCE_COLLECTION_PROGRESS`。

### 4.2 新增 Slurm 事实时间线

新增：

```text
run/slurm/squeue-timeline.jsonl
```

每行格式：

```json
{
  "schema": "pilot107.run.slurm.squeue_event.v1",
  "captured_at": "...",
  "job_id": "123",
  "state": "PENDING",
  "reason": "QOSMaxWallDurationPerJobLimit",
  "partition": "Students",
  "name": "pilot107-run",
  "source": "squeue",
  "command": ["squeue", "-h", "-j", "123", "-o", "%i|%u|%T|%R|%P|%j"]
}
```

### 4.3 采集时机

不做高频轮询。只在这些时机写入：

- `JOB_ACCEPTED` 后第一次 reconcile。
- 首次观测到 `PENDING`。
- Pending Reason 变化。
- 首次观测到 `RUNNING`。
- terminal 前最后一次 `squeue` 为空时，补一条 `terminal_transition`。
- 用户主动请求诊断时，若作业未终态，追加一次 snapshot。

### 4.4 实现边界

推荐在 `RunStore` 中增加每个 run 的轻量 timeline cursor，或在 `RunService.reconcile_once` 中读取最后一条 squeue artifact 做去重。

更稳妥的第一版：

- `JobSnapshot.raw_response` 保留 `squeue` stdout。
- `RunStore.apply_snapshot` 在 payload 中保存 `reason`。
- Evidence collector 的 `terminal_accounting` 阶段把历史 `run.snapshot` 事件导出为 `run/slurm/squeue-timeline.jsonl`。

第二版再增加 pending 阶段实时 artifact。

## 5. 多证据诊断设计

### 5.1 保守扩展而非重写

当前 `data/known_errors/*.yaml` 已可维护规则。下一步只增加结构化条件字段：

```yaml
conditions:
  run_state: FAILED
  terminal_state: TIMEOUT
  pending_reason_regex: "QOSMax.*Limit"
  resource_request:
    gpus_gt: 0
  evidence_json:
    path: run/environment/gpu.json
    field: probe.reason
    equals: command_not_found
```

旧字段 `symptoms`、`state_match` 继续兼容。

### 5.2 第一批规则

第一批只做最影响官方覆盖的 5 条：

- `SLURM.QOS_WALLTIME_LIMIT`
- `SLURM.QOS_CPU_LIMIT`
- `SLURM.PENDING_RESOURCE_OR_QOS`
- `RUNTIME.NVIDIA_SMI_NO_GPU`
- `RUNTIME.CONDA_NOT_INITIALIZED`

其余规则保留在后续：

- `RUNTIME.NVML_DRIVER_MISMATCH`
- `RUNTIME.LOG_PATH_MISSING`
- `RUNTIME.PYTORCH_CPU_ONLY`

### 5.3 Evidence refs

每条新规则必须至少引用一条具体证据：

- `run/request/resource-plan.json`
- `run/slurm/squeue-timeline.jsonl`
- `slurm/job_detail.json`
- `run/environment/basic.json`
- `run/environment/gpu.json`
- `logs/stderr.tail.json`

如果没有 evidence refs，测试必须失败。

## 6. Docker 验收矩阵

### 6.1 Runtime Probe

新增或增强：

```text
scripts/smoke-sim-evidence-transitions.sh
scripts/smoke-sim-api-submit.sh
```

必须验证：

- `run/environment/basic.json.scope == compute_job`
- `hostname` 来自执行节点或明确标记 simulator scope
- `python_version` 和 `python_path` 存在
- GPU 请求在无真实 GPU simulator 中生成 `run/environment/gpu.json` 且 status=`unavailable`
- runtime probe 失败不导致用户脚本失败

### 6.2 Pending Reason

新增：

```text
scripts/smoke-sim-pending-reason-timeline.sh
```

构造一个会 PENDING 的作业，至少捕获：

- `JOB_ACCEPTED`
- `JOB_PENDING`
- `PENDING_REASON_CHANGED` 或一条带 reason 的 pending snapshot
- `run/slurm/squeue-timeline.jsonl`

如果 Docker Slurm 不能稳定制造某个官方 reason，报告中记录 simulator limitation，但不能静默跳过。

### 6.3 Diagnosis

新增 fixtures：

```text
tests/fixtures/diagnosis/qos-walltime-limit/
tests/fixtures/diagnosis/pending-resource-or-qos/
tests/fixtures/diagnosis/nvidia-smi-no-gpu/
tests/fixtures/diagnosis/conda-not-initialized/
```

每个 fixture 包含最小 evidence 文件和期望 rule id。

## 7. 实施顺序

### N1：Submission wrapper 统一

改造提交脚本生成逻辑，让 command backend 和 REST backend 使用同一个 builder。

退出条件：

- 当前 submit/API/REST tests 不回退。
- Evidence 中仍有旧 `submission/*` 路径。
- 新 `run/request/*` 由真实 submitted script 驱动，而不是仅复制原始 user script。

### N2：Compute runtime probe

把 basic/GPU probe 放进 sbatch wrapper 用户脚本之前。

退出条件：

- Docker 成功/失败/取消作业都有 compute-job `run/environment/basic.json`。
- GPU 作业有 `run/environment/gpu.json` 或明确 warning。
- Probe 失败不会改变用户脚本退出码。

### N3：Squeue timeline

落地 `run/slurm/squeue-timeline.jsonl` 和 Pending Reason 去重。

退出条件：

- Pending 作业至少有一条 reason 证据。
- Running/terminal transition 能进入 timeline。
- 行为报告不再包含 “Pending Reason fidelity is not covered”。

### N4：结构化诊断第一批

扩展 YAML rule schema 和 matcher，补 4 个 fixture。

退出条件：

- 新规则均有 evidence refs。
- `DiagnosisService` 对旧规则完全兼容。
- Docker smoke 能触发至少一个结构化 Slurm/QOS 诊断。

## 8. 当前风险

- REST backend 若只提交一段 inline script，收集 submitted wrapper 和 user script 的边界要设计清楚。
- 取消作业可能在 probe 写完前被终止，Evidence 必须允许 `basic.json` 缺失并给出 warning。
- Pending Reason 在小型 Docker Slurm 中可能不稳定，需要用可重复资源占用或 QOS 限制构造。
- 真实 107 后续开放 SSH/登录节点时，只读 probe 可以校准命令输出，但不应改变本切片的 Docker-first 验收顺序。
