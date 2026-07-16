# Phase 3B Review Report

日期：2026-07-15  
范围：平台快照、用户 entitlement、动态 preflight、运行态/终态 Evidence、诊断规则与发布产物。

## Findings

### P1：共享 migration 可能乱序应用

状态：已修复。

PlatformSnapshotStore 与 UserEntitlementStore 原先各自维护 migration 列表，新数据库可能先应用
`003b.002`。现已集中为 `PLATFORM_MIGRATIONS`，两个 store 均按固定顺序和 checksum 应用完整注册表，
并以测试锁定两张表和历史 digest。

### P1：直接 prepare 路径绕过动态 preflight

状态：已修复。

Contract 路径会校验 platform/entitlement，但原始 `/runs/prepare` 路径没有。现在两条路径均合并
static、platform 和 entitlement finding；BLOCK 返回 422，成功路径也返回并持久化安全的
`run.preflight` 事件。

### P1：运行态任务没有 collector，collection_state 可失真

状态：已修复。

`runtime_status` 此前会以 warning 被标为 succeeded，却没有 `squeue` 证据。现在 Docker collector
执行固定 argv 查询，记录 state/reason/partition/资源；非终态 reconcile 会重新激活任务，任务变化后
立即重算 collection_state，artifact 同步进入 manifest 和证据索引。

### P1：全局 QoS 列表可能被误当成用户权限

状态：已修复。

新增 owner-scoped UserEntitlementSnapshot、TTL、data_quality、owner API 和提交时复检。权限不足、
快照过期或输出异常均为 UNKNOWN；只有 fresh authoritative entitlement 可以确认或阻止提交。

### P1：多账号用户可能被错误授权

状态：已修复。

首版只要任一 association 允许 QoS 就会确认，但当前提交契约没有 account 字段，Slurm 实际使用
DefaultAccount。采集命令已改为官方 `show user WithAssoc` 形式并保存 DefaultAccount；预检只使用默认
账号下的 association。默认账号未知时返回 UNKNOWN，不做授权结论。

### P1：entitlement service 存在跨 owner 误接线风险

状态：已修复。

`collect_and_store(owner, username)` 现在强制二者一致。管理员代理采集必须另建显式管理员接口，
不能复用用户自助服务绕过 owner 边界。

### P1：生产 wheel 丢失诊断规则

状态：已修复。

源码模式可加载 33 条规则，但原 wheel 不包含 `data/known_errors`，安装后只剩 7 条 fallback。规则现
作为 setuptools data files 进入 wheel/sdist，加载器通过 distribution metadata 定位；脱离源码目录
安装 wheel 后实测仍加载 33 条规则。

### P2：association 限制与缺失 association 被混为一类

状态：已修复。

`AssociationJobLimit` 不再作为 `SLURM.INVALID_ASSOCIATION` 症状；`InvalidQOS` 和
`QOSNotAllowed` 的无空格运行态 reason 已加入 QoS 规则。Conda 专用规则覆盖泛化
command-not-found；QoS walltime、CPU 请求和聚合 CPU 容量按不同处置规则识别；NVML mismatch
只形成管理员处置建议，不允许 Agent 重提或修改驱动。

## Residual Risks

- 当前只支持 DefaultAccount；高级用户显式选择 Slurm account 尚未进入 ResourcePlan/Contract/API。
- entitlement 尚未结构化 association/QoS 的 CPU、GPU、walltime 和并发 limits；当前可从运行态
  reason 诊断，但还不能在所有请求提交前动态阻止。
- `runtime_status.json` 保存最新状态，历史 reason 主要依赖 `run.snapshot` 事件；长作业指标尚未产品化。
- Docker simulator 没有真实 CUDA/NVML，本阶段验证了 unavailable 路径，没有真实 GPU 正路径。
- ASGI SSE、多副本 outbox、PostgreSQL、OIDC/RBAC、审计归档和定时 snapshot 调度仍未完成。
- Agent 仍是证据绑定、审批后的单动作闭环，不是多轮 RemediationSession。
- 当前目录没有 `.git` 元数据，无法提供 diff、commit、blame 和 CI 分支保护证据。

## Verification

- `PYTHONPATH=src uv run --extra dev pytest -q`：411 passed；
- `ruff check src tests scripts/smoke_sim_evidence.py scripts/smoke_sim_platform_snapshot.py`：通过；
- 进入 3C 后已清理脚本存量，`ruff check scripts`：通过；
- `mypy src/pilot107`：52 source files 通过；
- `uv build`：wheel 与 sdist 成功，wheel 包含 33 条 known-error YAML；
- wheel 脱离源码安装：33 条规则可加载，新增平台与 QoS limit 规则存在；
- Docker evidence smoke：job `33`，运行态 `RUNNING`、节点 `anode16`，18 个 evidence object；
- Docker platform/entitlement smoke：job `34`，DefaultAccount `students`，8 个 QoS，GPU probe 为
  `unavailable`；
- 两个 live smoke 均先通过 `scripts/check-sim-core.sh`。

## Decision

Phase 3B review 的 P0/P1 已清零，允许进入 Phase 3C 模板建立、发布审核、市场和采用纵向闭环。
这不代表生产控制面或 Agent 多轮自治已经完成；上述 residual risks 继续作为后续阶段门禁。
