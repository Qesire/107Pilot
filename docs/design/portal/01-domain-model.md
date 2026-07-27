# 01. 领域模型与生命周期

## 1. 复用的现有聚合

| 聚合 | 现有职责 | 在门户中的角色 |
|---|---|---|
| `Contract` | 版本化的项目、入口、资源、输出和自动化策略 | 用户采用/编辑后准备 Run 的唯一提交依据 |
| `Run` | owner、workdir、materialized script、job id、状态、工作流、事件 | 门户的核心事实对象 |
| `EvidenceObject` / `Capsule` | 日志、accounting、环境、输出清单与完整性 | 成功、失败、市场摘要和 Agent 的事实来源 |
| `Diagnosis` / `AgentAdvice` / remediation session | 规则诊断、LLM 解释、批准和派生 Run | 外置 Agent 的受控闭环 |
| `TemplateDraft` / `TemplateRelease` | 审核、不可变 release、adoption、verification | 官方、课程和严肃维护模板的 curated 路径 |
| `CapabilityProfile` / `PlatformSnapshot` | 静态策略和带来源的运行时平台观察 | Studio 预检、集群页和 Agent 平台事实 |

不得复制 `Run` 状态机或另建“市场运行”状态机。市场对运行事实只读引用 `Run`。

## 2. 新增聚合：RunPublication

普通用户市场分享应新增一个轻量聚合，暂定名 `RunPublication`。它是一次**发布者选择公开的成功 Run 投影**，不是对代码可复现性的保证，也不是现有 `TemplateRelease` 的替代品。

```text
RunPublication
  publication_id          immutable identity
  source_run_id           required; points to one Run
  owner                   immutable, equals source Run owner
  visibility              private | course | campus | public
  scope_key               required only for course
  title / description     publisher-authored
  tags                    publisher-authored, bounded list
  reproduction_note       free text; no semantic validation
  share_options           explicit fields selected for public read model
  created_at / updated_at
  withdrawn_at / reason   soft withdrawal only
```

`source_run_id` 是市场条目的唯一成功证明。没有必要把一组 Run 聚合为“成功率”、也没有必要验证作者没有私有依赖。

### 2.1 发布前置条件

在一个原子事务中检查：

```text
source_run.owner == requester
source_run.state == SUCCEEDED
source_run.exit_code starts with "0:"
request.confirm_share == true
no active RunPublication uses the same source_run_id
```

不检查：

- 是否有完整代码仓库；
- 远端 workdir 是否对他人可读；
- 依赖、数据集、私有脚本或运行时环境是否可迁移；
- 是否有 Capsule、expected output 或二次验证；
- 是否在真实 107 而非模拟 Slurm 运行。

Evidence/Capsule 可以在可用时丰富详情页，但它们不是普通发布门槛。

### 2.2 分享内容

市场 API 默认只暴露可安全稳定呈现的字段：标题、描述、标签、来源 Run 的状态/完成时间、资源摘要、发布者说明和可选 Contract 摘要。完整 script、Evidence object 预览、结果文件和远端路径只有在发布者明确选择、且对应 access policy 允许时才进入公开 read model。

这不是“复杂校验”，而是防止一次勾选隐式公开未预期内容。发布页必须在确认前显示将要公开的字段预览。

### 2.3 生命周期

```text
Run: PREPARED → SUBMITTED → RUNNING → SUCCEEDED
                                      │
                                      └─ owner confirms share
                                             │
                                             ▼
RunPublication: PUBLISHED ─────────→ WITHDRAWN
```

`RunPublication` 不修改来源 Run、Contract、Evidence 或 Capsule；withdraw 只撤销市场可见性，保留审计记录。来源 Run 被删除或访问收缩时，publication read model 必须降级为“原始详情已不可用”，不能泄漏原对象。

## 3. curated 模板与普通成功作业的并存

| 类别 | 领域对象 | 发布路径 | 适用场景 |
|---|---|---|---|
| 普通成功作业分享 | `RunPublication` | 成功 Run + 所有者确认 | 私有代码、个人实验、可参考作业 |
| 官方/课程模板 | `TemplateDraft → Review → TemplateRelease` | 保留现有 publication gate 和审核 | 教学、官方基线、长期维护资产 |

门户 read model 将二者统一为 `MarketItem`，但写路径保持独立，避免为了普通分享而削弱课程/官方模板的治理。

```text
MarketItem.kind = run_publication | curated_template
MarketItem.source_id = publication_id | release_id
```

现有 `/api/v1/templates` 保持向后兼容，只返回 curated release。新 `/api/v1/market/items` 返回统一市场列表；前端逐步迁移到新 endpoint。

## 4. 采用与 lineage

### 4.1 从 RunPublication 采用

采用不是“重新运行别人的作业”。它创建当前用户拥有的 private Contract：

```text
RunPublication
→ source Run.contract snapshot
→ user-owned Contract
→ optional Studio edit
→ user-owned Run
```

若来源没有可读 Contract，API 返回 `MARKET.ADOPTION_UNAVAILABLE`，而不是伪造脚本。若 Contract 中的 workdir 是发布者私有路径，Studio 应以警告显示该事实；用户自行替换路径和依赖，不需要后台判断能否复现。

新 Contract metadata 应记录：

```json
{
  "lineage": {
    "source_kind": "run_publication",
    "source_publication_id": "publication_...",
    "source_run_id": "run_..."
  }
}
```

### 4.2 从 curated template 采用

维持既有 `TemplateAdoptionRecord` 和不可变 release 行为。前端可在统一 MarketItem 卡片上使用同一个“采用”动作，但 API 根据 `kind` 分派。

## 5. 迁移与存储

新增独立 migration，不修改旧模板表的语义：

```sql
CREATE TABLE run_publications (
  publication_id TEXT PRIMARY KEY,
  source_run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
  owner TEXT NOT NULL,
  visibility TEXT NOT NULL,
  scope_key TEXT,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  tags_json TEXT NOT NULL,
  reproduction_note TEXT NOT NULL,
  share_options_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('published', 'withdrawn')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  withdrawn_at TEXT,
  withdrawal_reason TEXT
);
CREATE INDEX run_publications_visible_idx
  ON run_publications(state, visibility, created_at DESC);
```

实现时必须同时提供 SQLite migration 和 PostgreSQL domain schema/migration；不得再为门户核心对象只实现 SQLite。

## 6. 新增聚合：ArtifactManifest 与 RepairTicket

这两个对象不要求上传完整代码：

```text
ArtifactManifest
  run_id, owner, revision, dirty_diff_digest, bundle_digest,
  remote_workdir, local_test_summary, created_at

RepairTicket
  ticket_id, source_run_id, owner, diagnosis ids,
  evidence refs, optional code-context refs,
  requested change, allowed disclosure, resolved_at
```

它们先作为可选关联对象实现；Run 的提交和市场发布都不依赖它们，避免把私有代码工作流变成所有用户的硬门槛。

## 7. 领域事件

至少记录以下事件到既有 Run event/control trace 体系或新的 publication event 表：

```text
market.publication.created
market.publication.withdrawn
market.publication.adopted
artifact.manifest.attached
repair_ticket.created
repair_ticket.resolved
ssh_session.auth_required
ssh_session.recovered
```

事件 payload 不含源码、token、OTP、私有绝对路径或完整日志。

