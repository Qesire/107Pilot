# HIFX4 实际工程脚本到 107Pilot 模板的映射评审

日期：2026-07-18

## 来源边界

输入是用户提供的 HIFX4 v4.9.2 LightX2V Distill runbook。其脚本和报告证明这些模式曾在特定实际平台工程中使用；它们不授权 107Pilot 声明相同的账号、分区、QoS、GPU、路径、模型或性能。

## 提取的权威模式

| 实际脚本模式 | 107Pilot 抽象 |
| --- | --- |
| CPU preflight 固化 contract、scheduler、weight layout、environment report | `recipe_structured_preflight_gate` |
| 80-task GPU array，task 边界、GPU guard、本地 tmp、有限重试 | `recipe_gpu_shard_array_atomic` |
| 非空 shard + metadata + COMPLETE 后才复用 | 三件套 success protocol 与 scanner |
| 严格 Hessian/cache merge，缺片不允许 partial | `recipe_fail_closed_merge_gate` |
| smoke → gate matrix → full matrix | 工程规范的渐进 gate |
| missing-task scanner + array spec 重提 | `scan-array-artifacts.py` |
| `afterok` DAG、同层 GPU 峰值 ceiling | 资源合同与 preflight checklist |
| HF9 单一 JSON 契约 | `PREFLIGHT.STRUCTURED_CONTRACT_DRIFT` 规则 |
| HF10 源码树 import bootstrap | `RUNTIME.SOURCE_TREE_IMPORT_MISSING` 规则 |
| HF11 结构化校验替代源码字符串搜索 | structured preflight 规范 |
| HF12 同一 effective-config builder | `RUNTIME.EFFECTIVE_CONFIG_MISSING` 规则 |

## Findings first

### 已修正

1. 旧 GPU array 模板只保证 artifact + marker，新 v2 模板增加 metadata size/hash、GPU/Slurm guard、task 边界和节点本地 staging。
2. 旧模板描述 DAG 峰值但没有完整工程规范；新规范明确同层聚合、`afterok` fail-closed 和 gate 顺序。
3. 错误库原先不能区分缺包、源码树 closure、effective config 缺字段和结构化契约漂移；现已拆分。

### 保留边界

1. 107Pilot 当前 Contract 表达单 Run dependency，不自动从一个 recipe 展开整套多阶段 DAG；模板分别覆盖 stage，DAG 仍由显式 Contract/Run 关系编排。
2. 模板提供接口合同，用户命令必须实现对应的 `--output`、`--task-id` 或 `--input-dir` 参数。
3. 未在本机执行用户的 A100 模型、校准、量化或评测；只验证模板加载、materialization、scanner 与静态合同。

### 最终验证

- 三份示例 Contract 均由真实 `ContractService` 返回 `OK`，materialized script 通过 `bash -n`；
- 缺 account/contract、GPU array 并发大于 2 等负面合同返回结构化 `BLOCK`；
- scanner 覆盖完整集合、连续缺片、空 metadata、缺 marker、symlink 逃逸和不安全 pattern；
- 四条新增错误规则均由 diagnosis engine 实际命中；
- 完整门禁：608 passed、13 PostgreSQL integration skipped、5 subtests；Ruff 和 strict mypy（73 source files）通过；
- 未发现阻断本次模板/规范交付的 P0/P1 finding。

## 通过条件

- 新模板全部进入 INDEX 和 RecipeCatalog；
- 必需 runtime/resource 字段缺失时结构化 BLOCK；
- 合法 Contract materialize 后无未解析 Jinja；
- scanner 对完整、缺片、空文件、缺 metadata/marker 和路径逃逸 fail closed；
- 全量 Python、Ruff、mypy 与模板 YAML 门禁通过。
