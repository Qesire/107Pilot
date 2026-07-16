# Phase 1 优秀提交模板

> 状态：implemented
> 日期：2026-07-14
> 数据目录：`data/submission_templates/`

## 目标

优秀提交模板把 Wan–HiF4 复盘中的 Slurm 作业成功协议和 107 平台资源习惯固化为可复用 YAML。当前模板暂作为独立数据文件存在，尚未接入 `RecipeCatalog` API。

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

`data/submission_templates/INDEX.yaml` 列出所有模板。

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

## 后续接入

后续可选工作：

- 将模板加载进 `RecipeCatalog`；
- 新增 `GET /api/v1/submission-templates`；
- 在前端提交页提供模板选择；
- 将模板参数渲染为 Contract 草稿；
- 将 `preflight_checks` 映射到现有 `validate_resource_plan` 和 `WorkDirPreflight`。

