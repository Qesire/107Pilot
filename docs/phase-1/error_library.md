# Phase 1 已知错误库

> 状态：implemented
> 日期：2026-07-18
> 数据目录：`data/known_errors/`
> API：`GET /api/v1/diagnosis/known-errors`

## 目标

已知错误库把原先硬编码在 `src/pilot107/core/diagnosis.py` 的诊断规则迁移为 YAML 数据文件，并扩展 Wan–HiF4 通用 Slurm 经验与 107 特化故障模式。

每条规则提供：

- 稳定 `error_id`；
- `category` / `severity` / `retryable` / `stage`；
- 文本症状匹配（子串与 `regex:`）；
- `terminal_state_match` / `state_match` 等状态触发；
- `fix_template.patch`；
- `fix_guide.fix` / `prevention` / `automation`。

## 当前覆盖

当前 `data/known_errors/` 共 37 条规则：

- 旧 7 条规则完整迁移：
  - `SLURM.INVALID_QOS`
  - `SLURM.INVALID_PARTITION`
  - `RUNTIME.COMMAND_NOT_FOUND`
  - `RUNTIME.PYTHON_PACKAGE_MISSING`
  - `RUNTIME.TIMEOUT`
  - `RUNTIME.OOM`
  - `RUNTIME.NONZERO_EXIT`
- Wan–HiF4 平台错误记录：
  - HF4-005 合并进 `RUNTIME.PYTHON_PACKAGE_MISSING`
  - HF4-007 对应 `SLURM.ARRAY_DEPENDENCY_NEVER_SATISFIED`
  - 其余记录作为独立规则落地
- 107 特化规则：
  - `SLURM.WORKDIR_NOT_SHARED`
  - `SLURM.REST_AUTH_REJECTED`
  - `SLURM.SUBMISSION_UNCERTAIN`
  - `RUNTIME.PYTHON_PACKAGE_TRANSIENT`
  - `ARTIFACT.POSTPROCESS_FALSE_FAILURE`
- HIFX4 v4.9.2 实际工程新增抽象：
  - `RUNTIME.SOURCE_TREE_IMPORT_MISSING`
  - `PREFLIGHT.STRUCTURED_CONTRACT_DRIFT`
  - `RUNTIME.EFFECTIVE_CONFIG_MISSING`
  - `ARTIFACT.SHARD_SET_INCOMPLETE`

`data/known_errors/INDEX.yaml` 是人工可读索引；运行时加载器会跳过该索引，直接读取每条规则 YAML。

## 运行时行为

`DiagnosisService` 通过 `load_known_error_rules()` 加载规则。若 `data/known_errors/` 不存在或为空，会回退到旧 7 条内置规则，保证最小诊断能力。

匹配逻辑：

- 普通 `symptoms` 按小写子串匹配；
- `regex:` 前缀按正则匹配；
- `terminal_state_match` 匹配 Slurm terminal state；
- `state_match` 匹配 Run state 和 exit code 条件；
- 同一 `rule_id` 去重，保留第一条。

诊断结果通过 `DiagnosisRecord` 持久化，并包含 `category`、`stage`、`fix_guide`。

## API

列表：

```http
GET /api/v1/diagnosis/known-errors
```

单条：

```http
GET /api/v1/diagnosis/known-errors/SLURM.INVALID_QOS
```

Run 诊断：

```http
GET /api/v1/runs/{run_id}/diagnoses
```

Run Diagnostics 前端会展示 `category`、`stage`、`suggested_patch`、`retryable`、`confidence` 和 `fix_guide` 三段式指南。

## 维护规则

- 新规则应新增 YAML 文件，不改 Python 匹配代码；
- 旧 7 条规则的 `error_id`、关键症状和 patch 不得破坏；
- 症状文本应保持小写，技术术语可保留原拼写；
- `regex:` 只用于受信任规则文件，不接受用户输入；
- token、绝对个人路径和敏感输出不得写入 root cause 或 fix guide。
