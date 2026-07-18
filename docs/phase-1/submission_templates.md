# Phase 1 优秀提交模板

> 状态：implemented
> 日期：2026-07-18
> 数据目录：`data/submission_templates/`

## 目标

优秀提交模板把 Wan–HiF4 复盘和 HIFX4 v4.9.2 实际平台 runbook 中的 Slurm 作业成功协议固化为可复用 YAML。模板已由 `RecipeCatalog` 加载，可经 Recipe API、Contract validation 和 `sbatch_template_v1` materializer 使用。

模板字段对齐现有 `RecipeVersion` 的核心结构：

- `template_id`
- `title`
- `description`
- `trust_level`
- `parameter_schema`
- `compatibility`
- `risk_declaration`

并额外提供：

- `sbatch_template`
- `preflight_checks`
- `recovery`
- `success_protocol`

## 当前模板

`recipe_student_cpu_basic@1.0.0`

- 面向学生 CPU 基础作业；
- 使用共享 workdir；
- 显式 `KIT_ROOT`；
- `tmp -> validate -> atomic mv -> COMPLETE`。

`recipe_student_gpu_array@1.0.0`

- 面向 GPU array 作业；
- 使用 array throttle；
- 记录 DAG 层 GPU 峰值约束；
- 每个 task 独立 shard 和 COMPLETE；
- 提供 missing task scanner / resubmit missing 策略。

`recipe_resilient_submission@1.0.0`

- 集中编码 Wan–HiF4 硬化经验；
- `set -Eeuo pipefail`；
- 显式 `KIT_ROOT` / `DATA_ROOT`；
- import probe；
- 稳定 cwd；
- 原子写和 marker reconcile；
- artifact integrity 优先于 Slurm exit code。

`recipe_structured_preflight_gate@1.0.0`

- CPU allocation 内完成依赖、数据和运行契约检查；
- 结构化比较 contract/effective config/report；
- 节点本地报告验证后原子发布；
- 记录 contract SHA-256 后才写 COMPLETE。

`recipe_gpu_shard_array_atomic@2.0.0`

- account/partition/QoS/GPU type/memory 显式且唯一；
- 强制 Slurm/GPU guard、task 边界和 array 并发硬上限 2；
- 节点本地计算，共享 tmp 验证后原子发布；
- artifact + size/hash metadata + COMPLETE 三件套判定成功。

`recipe_fail_closed_merge_gate@1.0.0`

- merge 前严格扫描完整三件套；
- 缺片输出压缩 Slurm array spec；
- 禁止 partial merge 绕过；
- 合并产物生成 size/hash manifest 和 gate COMPLETE。

`data/submission_templates/INDEX.yaml` 列出所有模板。
三份可 materialize 的 Contract 示例位于 `data/submission_templates/examples/`。

## 成功协议

模板共同遵守：

```text
tmp -> 内容校验 -> atomic mv -> COMPLETE
```

诊断和恢复优先级：

```text
artifact integrity
→ COMPLETE marker
→ Slurm exit code
```

Array/DAG 恢复：

- 不以 Slurm 聚合作业状态作为唯一真源；
- 扫描缺失 task；
- 只重提缺失编号；
- 已完整产物不重复生成。

## 运行时接入

- `RecipeCatalog` 启动时从 YAML 加载并持久化版本；
- Contract validation 检查必填字段、partition compatibility、数值上下限和模板 array ceiling；
- materializer 安全注入 account、memory、GPU type/count 和 runtime environment；
- `scripts/scan-array-artifacts.py` 执行缺片对账；
- 完整工程规范见 [`../engineering/experiment_pipeline_contract.md`](../engineering/experiment_pipeline_contract.md)。
