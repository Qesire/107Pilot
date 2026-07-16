# Phase 1 已知错误库与优秀模板融入计划

> 状态：planned
> 日期：2026-07-13
> 来源：`/home/knowingthesea/文档/107/wan_hifx4_platform_error_playbook_zh.md` + `wan_hifx4_platform_error_records.jsonl`
> 融入路径：C 两者联动(数据驱动重构 + Wan–HiF4 模式提取)
> 覆盖范围：通用 Slurm + 107 特化
> 前置：REST 专项收敛已完成(269 测试,mypy 32 files)

## 1. 背景与目标

107Pilot 当前诊断引擎有 7 条硬编码规则(QoS/分区/命令未找到/包缺失/超时/OOM/非零退出),纯 `if "xxx" in lowered` 子串匹配,无数据驱动加载,扩展需改 Python 代码。

Wan–HiF4 平台故障模板提供 16 个结构化错误模式,含 4 类分类原则、重试分类、array 恢复规范、成功协议。

**目标**:
1. 重构诊断引擎为数据驱动(从 YAML 加载规则),向后兼容现有 7 规则的 rule_id 和 API;
2. 纳入 Wan–HiF4 的通用 Slurm 模式(~10 条)+ 107 特化模式(~5-8 条);
3. 每条错误自带 fix_template(结构化 patch)+ fix/prevention/automation 三层指南;
4. 新增优秀提交模板(成功协议 + 107 特化 sbatch 模板);
5. API 暴露错误库和模板列表;
6. AgentExplain 把 fix/prevention/automation 纳入解释。

## 2. 数据 Schema(共享契约,决定 Lane 1/2 可并行)

错误库文件格式:YAML,每文件一条规则,存放 `data/known_errors/`。

```yaml
# data/known_errors/SLURM_INVALID_QOS.yaml
error_id: SLURM.INVALID_QOS          # 稳定 ID,向后兼容现有 rule_id
category: resource_policy             # user_shell_state / slurm_runtime_path / dataset_path /
                                      # optional_dependency / resource_policy / dag_recovery /
                                      # shell_strict_mode / shell_error_handling / filesystem_cwd /
                                      # filesystem_transient / marker_reconciliation / log_noise /
                                      # metric_semantics / artifact_deployment / runtime
severity: error                       # error / warn / info
retryable: true
stage: submit                         # preflight / submit / runtime / postprocess
title: "QoS 不存在或不可用"
symptoms:                             # 匹配 evidence 文本,支持子串和正则
  - "invalid qos"                     # 子串匹配(小写)
  - "invalid qos specification"
  - "regex:qos.*not (found|allowed)"  # regex: 前缀表示正则
evidence_paths:                       # 该规则需要读取的 evidence 逻辑路径
  - submission/stderr
  - logs/stderr_tail
root_cause: "提交时指定的 QoS 在集群上不存在或用户无权使用。"
fix_template:                         # 结构化修复 patch(兼容现有 suggested_patch)
  type: structured_patch
  patch:
    resources.qos: null               # null = 清空,使用默认
fix_guide:                            # 三层文本指南(Wan–HiF4 风格)
  fix: "将 resources.qos 改为集群支持的 QoS,或清空使用默认。"
  prevention: "提交前通过 /api/v1/platform/capabilities 查询可用 QoS。"
  automation: "CapabilityProfile preflight 在提交前校验 QoS。"
confidence: high                      # high / medium / low(有 evidence → high)
kb_article: null                      # 可选 KB 文档路径
```

**向后兼容**:现有 7 规则的 rule_id 保持不变(`SLURM.INVALID_QOS` 等),数据文件用相同 error_id。现有 `DiagnosisRecord` 的 `suggested_patch` 字段映射到 `fix_template.patch`。

## 3. 任务拆分

### Lane 1 — 错误库数据层(与 Lane 2 并行,共享 §2 schema)

- 1.1 从 Wan–HiF4 records 提取通用 Slurm 模式(~10 条):
  - `SLURM.SARRAY_DEPENDENCY_NEVER_SATISFIED`(HF4-007,afterok 链失效)
  - `SLURM.ARRAY_PARTIAL_FAILURE`(HF4-008,分片偶发失败)
  - `SLURM.QOS_CONCURRENCY_EXCEEDED`(HF4-006,array 并发叠加超 QOS)
  - `SHELL.UNBOUND_VARIABLE`(HF4-009,local 同行赋值)
  - `SHELL.ERR_TRAP_BREAKS_RETRY`(HF4-010,ERR trap 使重试失效)
  - `FILESYSTEM.CWD_INVALID`(HF4-011,cwd 失效假失败)
  - `FILESYSTEM.TRANSIENT_IMPORT`(HF4-012,共享 FS 瞬时缺失)
  - `ARTIFACT.MARKER_MISSING`(HF4-013,COMPLETE 缺失对账)
  - `LOG.NOISE_FUTURE_WARNING`(HF4-014,非致命 warning 误判)
  - `SLURM.SCRIPT_PATH_FROM_ZERO`(HF4-003,$0/BASH_SOURCE 定位失败)
- 1.2 新增 107 特化模式(~5-8 条):
  - `SLURM.INVALID_PARTITION`(已有,增强:107 分区名)
  - `SLURM.WORKDIR_NOT_SHARED`(WorkDirPreflight 失败,关联 /public)
  - `SLURM.REST_AUTH_REJECTED`(REST JWT 认证失败,500/401 区分)
  - `SLURM.SUBMISSION_UNCERTAIN`(幂等对账 uncertain)
  - `RUNTIME.PYTHON_PACKAGE_TRANSIENT`(区分稳定缺失 vs 瞬时,增强现有规则)
  - `ARTIFACT.POSTPROCESS_FALSE_FAILURE`(产物完整但退出错误,假失败)
- 1.3 迁移现有 7 规则为 YAML(向后兼容):
  - `SLURM.INVALID_QOS` / `SLURM.INVALID_PARTITION` / `RUNTIME.COMMAND_NOT_FOUND` /
    `RUNTIME.PYTHON_PACKAGE_MISSING` / `RUNTIME.TIMEOUT` / `RUNTIME.OOM` /
    `RUNTIME.NONZERO_EXIT`
- 1.4 创建 `data/known_errors/` 目录,每规则一文件。

门:所有 YAML 文件 schema 合法;现有 7 规则 error_id 不变。

### Lane 2 — 引擎重构(与 Lane 1 并行,共享 §2 schema)

- 2.1 重构 `src/pilot107/core/diagnosis.py`:
  - 新增 `KnownErrorRule` dataclass(对应 §2 schema);
  - 新增 `load_known_errors(path) -> list[KnownErrorRule]`(从 YAML 加载);
  - 新增 `match_rule(rule, context) -> DiagnosisDraft | None`(数据驱动匹配,支持子串 + regex);
  - 重构 `diagnose_run()` 为:加载规则 → 逐规则匹配 → 去重 → 返回;
  - **向后兼容**:现有 7 规则的 rule_id 和 API 不变;`DiagnosisRecord` schema 不变(suggested_patch 仍可用);
- 2.2 扩展 `DiagnosisDraft` / `DiagnosisRecord`:
  - 新增可选字段:`fix_guide`(dict: fix/prevention/automation)、`category`、`stage`;
  - `suggested_patch` 从 `fix_template.patch` 映射;
  - DB schema 加列(migration,保持现有数据兼容);
- 2.3 扩展 `DiagnosisContextBuilder`:
  - 从固定 8 路径白名单 → 动态聚合所有规则的 `evidence_paths` union;
  - 保持现有 8 路径为默认 superset;
- 2.4 保持 `DiagnosisService` API 不变(自动受益于数据驱动)。

门:现有 `tests/test_diagnosis.py` 4 个测试全过(向后兼容);mypy strict + ruff。

### Lane 3 — AgentExplain + API 扩展(依赖 Lane 1 + 2)

- 3.1 扩展 `AgentExplainService`:
  - `explain_without_llm()` 把 `fix_guide`(fix/prevention/automation)纳入 fact 输出;
  - LLM system prompt 增加约束:只引用 fix_guide 中的内容,不编造;
- 3.2 API 层(`src/pilot107/api/http_app.py`):
  - `GET /api/v1/diagnosis/known-errors` — 列出所有已知错误(category/severity/retryable/symptoms);
  - `GET /api/v1/diagnosis/known-errors/{error_id}` — 单条详情(含 fix_guide);
  - 诊断结果(`GET /api/v1/runs/{run_id}/diagnoses`)响应体新增 `fix_guide` 字段;
- 3.3 前端(如已有诊断面板):展示 fix/prevention/automation 三层(设计意图保持,仅数据接入)。

门:API 测试 pass;AgentExplain 测试 pass;mypy + ruff。

### Lane 4 — 优秀提交模板(依赖 Lane 1,可与 Lane 2/3 并行)

- 4.1 基于 Wan–HiF4 成功协议的提交模板规范:
  - 原子写协议(`tmp → 校验 → atomic mv → COMPLETE`);
  - array 恢复规范(缺失 task scanner、只补缺失编号);
  - shell 严格模式规范(`set -Eeuo pipefail`、`local` 拆行、`|| return $?`);
  - QOS 资源模板(DAG 层并发总和计算);
- 4.2 107 特化提交模板(融入 RecipeCatalog 或独立 `data/submission_templates/`):
  - `recipe_student_cpu_basic`(学生 CPU 基础作业,107 分区/QoS);
  - `recipe_student_gpu_array`(学生 GPU array 作业,含并发控制);
  - `recipe_resilient_submission`(含原子写 + COMPLETE + 恢复入口的健壮模板);
- 4.3 每个模板包含:`sbatch_template`、`preflight_checks`、`recovery_script`、`success_protocol`。

门:模板 schema 合法;与现有 RecipeCatalog 兼容(如融入)或独立可加载。

### Lane 5 — 测试 + 文档(收尾)

- 5.1 测试:
  - `tests/test_known_errors.py` — 数据驱动加载、规则匹配(子串 + regex)、向后兼容;
  - 扩展 `tests/test_diagnosis.py` — 新规则匹配、去重、evidence_paths 动态聚合;
  - 扩展 `tests/test_agent.py` — fix_guide 纳入 explanation;
  - API 测试 — known-errors endpoint;
- 5.2 文档:
  - 新增 `docs/phase-1/error_library.md`(错误库总览 + 分类 + 使用指南);
  - 新增 `docs/phase-1/submission_templates.md`(优秀模板总览);
  - 更新 `docs/phase-1/interface_hardening_status.md`;
  - 追加 `docs/phase-0/development_log.md` 第五十九批。

门:全量 pytest + mypy + ruff 无回归;check-competition.sh 无回归。

## 4. 阶段门

- [ ] 诊断引擎数据驱动(从 YAML 加载规则);
- [ ] 现有 7 规则向后兼容(rule_id + API 不变);
- [ ] Wan–HiF4 通用模式纳入(~10 条);
- [ ] 107 特化模式纳入(~5-8 条);
- [ ] 每条错误自带 fix_template + fix_guide;
- [ ] 优秀提交模板(成功协议 + 107 特化);
- [ ] API 暴露错误库和模板;
- [ ] AgentExplain 纳入 fix_guide;
- [ ] 全量 pytest / mypy / ruff / check-competition.sh 无回归。

## 5. 并行性与派工

- Lane 1(数据层 `data/known_errors/`)与 Lane 2(引擎 `diagnosis.py`)共享 §2 schema → 可并行;
- Lane 4(模板 `data/submission_templates/` 或 RecipeCatalog)依赖 Lane 1 schema 风格 → 可与 Lane 2/3 并行;
- Lane 3 依赖 Lane 1 + 2;
- Lane 5 收尾。

## 6. 风险与缓解

- **向后兼容**:现有 7 规则的 rule_id 和 DB schema 必须不变;新增字段可选;migration 保持现有数据可读;
- **正则注入**:symptoms regex 来自受信任的 YAML 文件(非用户输入),但仍需限制编译时间/复杂度;
- **evidence_paths 动态聚合**:需确保不破坏现有 8 路径白名单的测试;
- **模板与 RecipeCatalog 边界**:如融入 RecipeCatalog 需保持现有 contract API 不变;独立则需新增加载器。

## 7. 验证命令

```bash
PYTHONPATH=src uv run --extra dev pytest
uv run --extra dev mypy src/pilot107
uv run --extra dev ruff check src/pilot107 tests
bash scripts/check-competition.sh
```

## 8. 下一步

```text
已知错误库与优秀模板融入(本计划)
→ M1 HTTPS/reverse proxy 与两机部署脚本
→ 前端设计包接入前的后端交互回归固化
```
