# 107Pilot Template Market Agent 细化设计

- 日期：2026-08-10
- 状态：已完成设计确认，等待用户审阅
- 上层规格：docs/superpowers/specs/2026-08-10-pi-hpc-agent-core-design.md
- 验收环境：本地 Docker Slurm simulator；远程 VM 当前不可用

## 1. 目的

本规格细化 107Pilot Template Market 与 Pi Agent 的结合方式，覆盖：

1. 自然语言发现和解释市场候选；
2. curated TemplateRelease 与普通 RunPublication 的 Agent 强制应用；
3. 成功 Run 的可选分享；
4. 成功 Run 提升为 curated template 的严格发布；
5. 语义查重、版本和重复发布抑制；
6. 应用 Run、Evidence、verification 与市场推荐反馈；
7. owner、确认、幂等、恢复、撤回和审核边界；
8. 本地 simulator 的纵向验收。

本规格不把市场改造成静态 YAML 列表，也不让模型直接操作数据库、底层 adopt、publish 或 withdraw 方法。

## 2. 当前实现基线

当前项目已经具有：

- MarketReadService：合并 RunPublication 和 curated TemplateRelease；
- RunPublicationStore：成功 Run 的轻量分享、撤回和 Contract adoption；
- TemplateMarketStore：draft、review、publish、immutable release、adoption、verification 和 withdrawal；
- TemplatePublicationGate：secret、路径、compatibility、危险 shell 和 raw sbatch gate；
- TemplateVerificationService：从 adoption lineage、Run 和 Evidence 派生 verification；
- Contract、Run、Runtime Watch、Evidence、Workspace 和 Agent outbox 基础能力；
- Web 中直接采用市场条目并立即生成私有 Contract 的既有路径。

需要新增的是 Agent 驱动的领域编排、可选分享 manifest、语义查重、内容寻址 bundle、application outcome 和更严格的 verification lineage。现有 deterministic stores 继续复用，但不再作为面向用户的直接应用 API。

## 3. 已确认决策

1. 统一市场继续保留 RunPublication 与 curated TemplateRelease 两类条目。
2. 两类条目的应用都必须经过 Agent。
3. 共同持久 envelope 使用判别联合；领域层暴露两个强类型分支。
4. TemplateRelease 使用 TemplateApplicationSession，保证等级为 curated。
5. RunPublication 使用 ReferenceAdaptationSession，保证等级为 reference_only。
6. run reference 不继承 compatibility、verification 或可复现保证。
7. 没有可采用 Contract 的 RunPublication 只能作为说明性参考，不能启动适配会话。
8. 应用完成只表示私有 Draft、ChangeSet 或 Contract 已形成，不等于 Slurm 已提交。
9. 应用后的 Run 通过独立 MarketApplicationOutcome 回流。
10. 只有完整 curated lineage 才能形成 TemplateVerification。
11. 成功 Run 默认不分享；Agent 不能自动发布。
12. RunPublication 支持逐字段 ShareManifest。
13. curated template 的 sanitized bundle 是最低共享单元。
14. curated 发布必须经历严格脱敏、参数化、隔离复现、人工审核和 immutable release。
15. 语义查重在提取前和 sanitized bundle 形成后各运行一次。
16. 完全相同或只有展示 metadata 变化的内容不得频繁生成新 release。
17. 等价的新成功 Run 优先成为既有 release 的 verification，而不是重复发布。
18. Pi 只能调用高层 typed tools；用户确认与审批由 107Pilot 控制面执行。
19. 普通 owner-scoped Agent 上下文可使用校内自部署模型；严格脱敏集中在共享模板发布路径。
20. 所有 P0 声明必须先由本地 simulator Evidence 支持。

## 4. 市场条目与保证等级

| 类型 | 来源 | 市场承诺 | 应用会话 | 可形成 TemplateVerification |
|---|---|---|---|---|
| RunPublication | owner 确认的成功 Run | 该作业曾成功运行；不保证可移植 | ReferenceAdaptationSession | 否 |
| TemplateRelease | gate、复现和审核后的 bundle | 在声明环境与验证时点通过受控流程 | TemplateApplicationSession | 是 |

保证等级创建后不可提升。一次成功的 ReferenceAdaptationSession 不能把原 RunPublication 自动改成 curated；publisher 必须另行启动 TemplatePublicationSession。

## 5. 总体领域流

~~~text
自然语言目标 / 用户选择条目
                │
        MarketDiscoveryService
                │
        MarketApplicationSession
           ┌────┴────┐
           │         │
TemplateApplication  ReferenceAdaptation
           │         │
           └────┬────┘
                │
       AgentApplicationPlan
                │
           用户确认
                │
       类型专属 finalizer
                │
 private Draft / ChangeSet / Contract
                │
         用户另行提交 Run
                │
     Runtime Watch / Evidence
                │
    MarketApplicationOutcome
           │
           └─ curated eligible → TemplateVerification
~~~

供给侧：

~~~text
成功 Run
├─ 默认：不分享
├─ 轻量分享：RunPublication
└─ 可复用资产：TemplatePublicationSession
                  → 提取
                  → 分类
                  → 严格脱敏
                  → 参数化
                  → 语义查重
                  → 隔离复现
                  → review
                  → immutable TemplateRelease
~~~

Template Market 不复制 Workspace、ChangeSet、Contract、AgentTask、Run、Runtime Watch 或 Evidence；它只使用领域状态机组合这些基础能力。

## 6. 组件边界

### 6.1 MarketDiscoveryService

负责：

- owner、visibility 和 course scope 过滤；
- withdrawn、adoption availability 和 current gate 过滤；
- PlatformSnapshot 兼容性分层；
- 任务、输入输出和 parameter schema 匹配；
- 候选事实与可解释排序。

它不调用 LLM，不执行 adoption，也不创建 Contract。

### 6.2 MarketApplicationService

负责：

- MarketApplicationSession envelope；
- TemplateApplication 与 ReferenceAdaptation 判别联合；
- 输入解析、计划 snapshot、确认 digest；
- 乐观并发、幂等、恢复和类型专属 finalizer；
- 应用输出 lineage。

### 6.3 TemplatePublicationService

负责：

- 来源 Run eligibility；
- 来源 snapshot 锁定；
- 值分类、sanitization、parameterization；
- duplicate check；
- bundle manifest；
- reproduction AgentTask；
- draft、review 和 release 编排。

### 6.4 TemplateBundleStore

负责：

- 小型文本 bundle 的内容寻址存储；
- manifest、entry digest、大小和 media type 校验；
- release 对 immutable manifest 的引用；
- 外部大文件 descriptor，不复制其内容。

### 6.5 MarketOutcomeService

负责：

- application session、Contract、Run 和 Evidence lineage 对账；
- verification eligibility；
- failure attribution facts；
- application outcome 和 verification append-only 写入。

### 6.6 TemplateTrustReadModel

负责：

- verification 新鲜度；
- 当前环境可信等级；
- failure warning；
- duplicate family、supersedes 和 withdrawal；
- 面向 Agent 与市场查询的解释性排序。

### 6.7 既有 Store 的收缩

TemplateMarketStore 和 RunPublicationStore 继续承担 deterministic persistence。现有 adopt_release() 与 RunPublicationStore.adopt() 的 copy-only 行为不足以应用已确认的参数和 ChangeSet；实现时把其授权、幂等和 lineage 写入提取为内部 transaction helper，由 MarketApplicationService 的强类型 finalizer 调用。旧方法不能注册为用户可达 HTTP action。

## 7. 市场发现与推荐

### 7.1 发现请求

~~~yaml
MarketDiscoveryRequest:
  owner:
  intent:
  platform_snapshot_id:
  course_scopes:
  source_kinds:
  required_inputs:
  expected_outputs:
  limit:
~~~

limit 的服务端最大值为 20；Agent 默认只接收前 5 个候选，比较时最多向用户展示 3 个。

### 7.2 候选事实

~~~yaml
MarketCandidate:
  source_kind: curated_template | run_publication
  item_id:
  source_digest:
  assurance: curated | reference_only
  applicability: compatible | adaptable | needs_probe | incompatible
  task_match:
  compatibility_findings:
  missing_prerequisites:
  verification_summary:
  adoption_available:
  evidence_refs:
  recommendation_reasons:
~~~

### 7.3 硬过滤

排序前必须过滤：

1. 不可见或不属于当前 course scope；
2. withdrawn；
3. current publication/adoption gate 阻塞；
4. 当前平台明确不兼容；
5. RunPublication 未分享可采用 Contract；
6. source digest 或 lineage 无法读取；
7. 当前 owner 无法创建目标私有工作区。

不可采用的 RunPublication 可作为普通市场说明卡片存在，但不能出现在 application candidate 集合。

### 7.4 可解释排序

采用字典序层级，不依赖一个黑盒总分：

1. 当前平台兼容等级；
2. assurance：同等条件下 curated 优先；
3. 任务、输入输出和参数 schema 匹配；
4. 当前环境 verification tier、新鲜度和 Evidence 完整度；
5. 保守 outcome 信号；
6. adoption count 与发布时间。

热门度不能覆盖不兼容或可信失败。

### 7.5 Agent 行为

~~~text
一个明显候选
→ 解释依据
→ 创建应用会话
→ 最终 ApplicationPlan 一次确认

多个接近候选
→ 比较最多三个关键差异
→ 用户选择
→ 创建应用会话

没有候选
→ no_suitable_template
→ ExperimentProjectSession(origin=blank | existing)
~~~

LLM 只解释 MarketCandidate 中的事实，不自行发明 compatibility 或 verification。

## 8. MarketApplicationSession

### 8.1 共同 envelope

~~~yaml
MarketApplicationSession:
  session_id:
  owner:
  source:
    kind: curated_template | run_publication
    item_id:
    content_digest:
    assurance: curated | reference_only
  user_intent:
  platform_snapshot_id:
  workspace_snapshot_id:
  phase:
  status:
  state_version:
  agent_session_id:
  resource_envelope:
  resolved_inputs:
  assumptions:
  findings:
  application_plan:
  plan_digest:
  confirmation:
  outputs:
  created_at:
  updated_at:
~~~

source.kind、source.item_id 和 owner 创建后不可改变。自然语言搜索先产生 MarketDiscoverySnapshot；选定条目后才创建应用会话。

### 8.2 阶段和状态

业务阶段：

~~~text
evaluating
→ collecting_inputs
→ adapting
→ planning
→ awaiting_confirmation
→ finalizing
→ completed
~~~

正交状态：

~~~text
active | waiting_user | waiting_task | paused_auth
| blocked | cancelled | completed
~~~

阶段表达业务位置，状态表达当前是否可推进，避免 waiting_user_planning 等状态爆炸。

### 8.3 阶段语义

- evaluating：重新检查 visibility、withdrawal、source digest、current gate 和 PlatformSnapshot；
- collecting_inputs：填写有事实依据的默认值，只询问真正未知项；
- adapting：生成 owner 私有路径、资源、环境和文件变更；
- planning：形成完整 AgentApplicationPlan；
- awaiting_confirmation：冻结 plan/source/platform/workspace/policy digests；
- finalizing：重验所有 digest，消费一次性 capability；
- completed：私有输出已全部完成并经过 saga 对账，不代表提交 Slurm。

### 8.4 强类型 detail

~~~yaml
TemplateApplicationDetail:
  release_id:
  template_id:
  release_version:
  bundle_digest:
  parameter_bindings:
  compatibility_findings:
  verification_snapshot:

ReferenceAdaptationDetail:
  publication_id:
  source_run_id:
  source_contract_digest:
  share_manifest_digest:
  portability_findings:
  missing_assets:
  unsupported_fields:
  reference_warning_acknowledged:
~~~

服务端通过判别 schema 校验 detail；不能把 run reference 的字段写进 TemplateApplicationDetail，也不能在会话中切换分支。

## 9. AgentApplicationPlan

~~~yaml
AgentApplicationPlan:
  schema_version:
  session_id:
  source_digest:
  assurance:
  resolved_parameters:
  workspace_changes:
  contract_changes:
  environment_plan:
  resource_plan:
  external_assets:
  assumptions:
  unresolved_warnings:
  validation_options:
  guarantees:
  non_guarantees:
  generated_at:
~~~

计划必须显示：

- 将创建或修改的文件；
- Contract 的关键字段；
- 大文件是否仅作为 manifest；
- 当前平台 finding；
- curated 或 reference_only 保证等级；
- 需要用户后续独立确认的正式 Slurm 提交。

确认绑定：

~~~text
owner
+ session_id
+ plan_digest
+ source_digest
+ platform_snapshot_id
+ workspace_snapshot_id
+ policy_version
+ expiry
~~~

任一绑定项变化都使确认失效。

## 10. Curated Template 应用

### 10.1 输入解析

参数来源按优先级：

1. 用户显式输入；
2. WorkspaceSnapshot 或 PlatformSnapshot 的确定性事实；
3. release schema 的安全默认值；
4. Agent 建议；
5. unresolved，向用户提问。

Agent 建议不能覆盖 schema enum、范围、路径根、partition/QoS 或资源上限。

### 10.2 Compatibility

compatibility finding 至少包含：

- required partition/QoS；
- CPU/GPU 类型和数量；
- modules、container digest 或 runtime；
- 文件系统和 workdir 约束；
- 输入输出要求；
- verification environment 与当前环境差异。

needs_probe 可以创建 AgentTask 进行受限 capability probe；等待期间 Pi Turn 必须释放。

### 10.3 Finalizer

TemplateAdoptionFinalizer 在可恢复 saga 中：

1. 重验 release 可见、未撤回和 current gate；
2. 重验 plan 与 confirmation；
3. 校验 planned target Contract payload 与 plan digest；
4. 以 conflict-safe 方式发布 WorkspaceChangeSet；
5. 通过内部 transaction helper 创建 private draft、精确 target Contract 和 adoption lineage；
6. 写 application outputs 和审计事件；
7. 使用 session_id + plan_digest 保证幂等，并在响应丢失时对账。

finalizer 不允许先创建固定复制 Contract 再进行未确认修改。创建 Contract 与正式 Slurm submit 分离。

## 11. Run Reference 适配

### 11.1 基本边界

RunPublication 只证明 source Run 成功。ReferenceAdaptationSession 必须重新检查：

- source Contract 是否仍可读取；
- workdir、输入和输出路径；
- 未分享代码、脚本、数据和制品；
- container/module 依赖；
- 当前 partition/QoS 和资源约束；
- 来源 Run 的偶然成功条件。

### 11.2 Source materialization

publisher 只有在 ShareManifest.contract_for_adaptation=true 时授权创建私有派生 Contract。MarketApplicationService 生成 owner-scoped ReferenceSourceSnapshot，绑定 source Contract digest；Agent 只读取适配所需投影，不读取未分享日志、Evidence 或制品。

contract_for_adaptation 不授权传递 credential、secret、socket 或不可重绑定的私有路径；ReferenceSourceSnapshot gate 命中这些字段时必须 blocked。该 gate 是所有分享路径的基本安全线，不等同于 curated 发布的完整语义脱敏。

### 11.3 Finalizer

ReferenceAdoptionFinalizer 使用相同确认和幂等规则，校验授权的 source snapshot、planned target payload 和 ChangeSet，再通过内部 transaction helper 创建 adoption 与精确 target Contract。它不能先调用旧 adopt() 生成固定 Contract 后再修改。最终 Contract 必须保留：

~~~text
derivation_reason = run_publication_adaptation
source_publication_id
market_application_session_id
assurance = reference_only
~~~

即使后续运行成功，也不能改变 source RunPublication 的 assurance。

## 12. 可选分享

### 12.1 默认行为

成功 Run 不自动创建任何市场记录。Agent 可以创建一次 owner-only ShareSuggestion：

~~~yaml
ShareSuggestion:
  run_id:
  owner:
  suggested_path: none | run_publication | curated_template
  reasons:
  duplicate_precheck:
  state: pending | accepted | dismissed | expired
~~~

同一 Run 被 dismissed 后不再重复提醒。

### 12.2 RunPublication ShareManifest

~~~yaml
ShareManifest:
  schema_version:
  visibility:
  scope_key:
  title:
  description: true | false
  resource_summary: true | false
  result_summary: true | false
  contract_for_adaptation: true | false
  script: true | false
  evidence_previews: true | false
  small_assets:
  manifest_digest:
~~~

默认值：

- visibility 为 private，用户必须主动选择更大范围；
- title 为必需字段；
- description 默认为 true；
- 其余全部为 false；
- workdir、用户名、凭据、socket 和未选择内容永不出现在 read model。

用户确认绑定 manifest_digest 与 visibility/scope。contract_for_adaptation=false 时，市场卡片的 adoption.available=false。

RunPublication 不执行 curated template 的参数化和完整语义 sanitization，但用户选择的 script、Evidence preview 或小型制品必须先通过 forbidden secret/path 检查，并展示精确公开预览。

### 12.3 Curated 分享

curated template 发布本身表示分享 sanitized bundle；为完成复现所必需的 Contract、parameter schema、validation recipe 和小型文本文件不能逐项隐藏。日志、结果预览、原始 Evidence 和非必要制品仍由用户选择是否作为附加引用。

## 13. TemplatePublicationSession

### 13.1 Eligibility

普通用户来源必须满足：

- source Run owner 为当前用户；
- Run state=SUCCEEDED 且 exit code 为 0:*；
- canonical Contract 存在；
- terminal Evidence 存在；
- Run 绑定的 WorkspaceSnapshot 或 source bundle 可读取；
- 同一 source Run 没有 active publication session。

官方种子和课程维护者可以使用 curated_import 来源，但仍必须经过 gate、隔离复现和 review；该入口不对普通用户开放。

### 13.2 数据模型

~~~yaml
TemplatePublicationSession:
  session_id:
  owner:
  source_kind: successful_run | curated_import
  source_run_id:
  source_contract_id:
  source_workspace_snapshot_id:
  source_evidence_digest:
  source_bundle_digest:
  phase:
  status:
  state_version:
  classifications:
  sanitization_report:
  duplicate_report:
  parameter_schema:
  compatibility:
  validation_recipe:
  bundle_manifest_digest:
  draft_id:
  reproduction:
  confirmation:
  review_id:
  release_id:
  release_version:
~~~

### 13.3 阶段

~~~text
selecting_source
→ extracting
→ classifying
→ sanitizing
→ parameterizing
→ drafting
→ deduplicating
→ validating
→ awaiting_publication_confirmation
→ submitted
├─ rejected → revising
└─ approved → publishing → completed
~~~

只读取与 source Run 绑定的 immutable Contract、WorkspaceSnapshot 和 Evidence，不读取后来变化的 live workspace。

## 14. 分类、脱敏与参数化

### 14.1 值分类

每个候选值产生：

~~~yaml
PublicationClassification:
  source_path:
  value_digest:
  class: invariant | user_parameter | platform_parameter
         | runtime_derived | external_asset | forbidden
  replacement:
  rationale:
  evidence_refs:
  confidence:
  decided_by: rule | agent | user | reviewer
~~~

规则分类优先于 Agent。凭据、secret 和 socket 必须为 forbidden，模型不能覆盖。

### 14.2 严格脱敏

确定性 gate 至少扫描：

- 用户名、home/public 绝对路径和工作区根；
- Run、Job、account、课程和项目标识；
- token、credential、secret、socket 和私有 endpoint；
- 数据集、输入、输出、checkpoint 和制品路径；
- 未声明的下载地址；
- 偶然存在的 partition、memory、time 和 GPU 值；
- 危险 shell 和 unsupported raw sbatch。

Agent 负责识别语义上的偶然值和隐藏假设。仅执行字符串替换不能通过 gate。

### 14.3 owner-scoped 上下文

严格脱敏只在共享 curated 发布路径强制。普通 owner-scoped Agent 可以读取授权的代码和日志并使用校内自部署模型；但任何路径都不把凭据、secret 或 SSH/MFA 材料送入模型。

## 15. Template bundle

### 15.1 Manifest

~~~yaml
TemplateBundleManifest:
  schema_version:
  media_type:
  template_contract_digest:
  parameter_schema_digest:
  compatibility_digest:
  validation_recipe_digest:
  entries:
    - path:
      role: contract | scaffold | config | test | documentation
      media_type:
      size:
      sha256:
  external_assets:
    - logical_name:
      role:
      media_type:
      size:
      sha256:
      acquisition:
  created_from:
    run_id:
    workspace_snapshot_id:
    evidence_digest:
  manifest_sha256:
~~~

manifest_sha256 是 release 的内容身份之一。entry 按 digest 内容寻址，release 只引用 immutable blob。

### 15.2 大文件

- 5GB 以上文件绝不进入 bundle；
- dataset、checkpoint 和权重默认只进入 external_assets；
- acquisition 只能描述用户提供、平台已有、课程分发或允许的传输任务；
- Agent 不自动下载；
- 复现时缺少必需 external asset 必须显式 blocked，而不是回退使用 source workspace。

## 16. 两阶段语义查重

### 16.1 指纹

早期 family fingerprint 基于：

- entry command 结构；
- Contract topology；
- runtime/container/module；
- 输入输出类型；
- parameter schema 形状；
- compatibility 维度；
- validation recipe 类型。

最终 content fingerprint 基于 sanitized TemplateBundleManifest、parameter schema、compatibility 和 validation recipe。

两种指纹均排除：

- title、description、tags；
- 时间戳；
- owner、用户名和绝对路径；
- Run/Job ID；
- 仅用于展示的结果；
- runtime-derived 值。

### 16.2 DuplicateCheckReport

~~~yaml
DuplicateCheckReport:
  stage: pre_extract | pre_review
  family_fingerprint:
  content_fingerprint:
  matched_items:
    - item_id:
      opaque_match_token:
      disclosure: visible | opaque
      owner_relation: same_owner | other_owner
      scope_relation:
      match_type:
      structural_diff:
  conclusion: unique | exact_duplicate | metadata_only_difference
              | new_version_candidate | near_duplicate
              | meaningfully_distinct
  blocking:
  recommended_action:
  policy_version:
~~~

### 16.3 决策规则

| 结论 | 行为 |
|---|---|
| exact_duplicate | 阻止新条目；使用既有 release |
| metadata_only_difference | 不创建 release；写 catalog annotation/revision |
| new_version_candidate | 进入既有 template_id 的新版本流程 |
| near_duplicate | reviewer 查看结构化 diff；Agent 只解释 |
| meaningfully_distinct | 允许独立发布 |

P0 中，family fingerprint 相同且存在功能 diff 时归为 new_version_candidate；family fingerprint 不同，但 normalized entry command、runtime identity 和 I/O schema 三者均相同时归为 near_duplicate。其他模糊语义相似只产生非阻塞 finding。

硬阻塞只依赖 digest、canonical structural diff 和明确规则。embedding 或 LLM 相似度只能产生 review finding，不能单独阻止发布。

### 16.4 频繁发布抑制

- 同一 owner、scope 和 semantic family 只允许一个 active RunPublication；
- 同一 owner、template family 只允许一个 pending curated review；
- 同一 sanitized content digest 不能创建第二个 release；
- 等价的新成功 Run 优先创建既有 release 的 verification；
- RunPublication 展示信息修订写 append-only RunPublicationRevision；
- curated title、description、tags 和弃用说明写 append-only TemplateCatalogAnnotation；
- release bundle 和原始发布 provenance 保持 immutable。

跨 owner 的 near duplicate 不自动阻塞；reviewer 必须区分独立实现、课程变体和无意义复制。完全相同的公开 bundle digest 则应复用既有 release 或建立 catalog reference。

DuplicateCheckReport 只对当前 actor 可见的条目返回 item_id；不可见的匹配只返回本次检查可用的 opaque token，不泄漏 owner、scope、title 或内容。

## 17. 隔离复现与审核

### 17.1 复现

复现必须：

1. 使用独立 verifier principal；
2. 使用全新 workspace；
3. 只读取 TemplateBundleManifest 与 declared external assets；
4. 通过同一 TemplateApplicationEngine 的 verifier-only CandidateApplicationSession 实例化；
5. 创建独立 Contract 和 Run；
6. 等待 terminal Evidence；
7. 记录 docker、real107_cpu 或 real107_gpu 环境及 fingerprint。

CandidateApplicationSession 复用 TemplateApplicationSession 的 schema 解析、compatibility、bundle materialization、Contract 和 Workspace 规则，但 source 只能是当前 private draft digest，不能出现在市场、不能获得 release assurance，也不能调用 direct adoption。若复现依赖 source workspace、未声明路径或私有缓存，必须失败。

### 17.2 送审确认

用户确认绑定：

~~~text
sanitized bundle digest
+ duplicate report digest
+ reproduction evidence digest
+ visibility/scope
+ release version
+ publication policy version
~~~

任何变化使确认失效。

### 17.3 审核

reviewer 检查：

- gate report；
- sanitization findings；
- parameter schema；
- duplicate report；
- reproduction Evidence；
- visibility/scope；
- SemVer 和 supersedes 关系。

只有 approved review 可调用内部 publish()。Agent 不能批准或发布。

## 18. 版本与 metadata

- TemplateRelease bundle 不可变；
- 实际行为、参数 schema 或 compatibility 改变时创建新 release version；
- 只改变市场说明时创建 TemplateCatalogAnnotation，不创建 release；
- Agent 可以建议 patch、minor 或 major；
- patch：兼容的 validation、测试或默认值修正，不删除参数/输出，也不使既有 bindings 失效；
- minor：向后兼容地增加参数、scaffold、环境或输出能力；
- major：entry、必需参数、输入输出或 compatibility 出现不兼容变化；
- owner 确认版本，reviewer 检查兼容语义；
- supersedes_release_id 显式记录替代关系；
- 客户端不得接受比当前选定 lineage 更旧的替代目标；
- withdrawal 阻止新的 application finalizing，但不删除历史。

## 19. Application outcome 与 verification

### 19.1 Outcome

~~~yaml
MarketApplicationOutcome:
  outcome_id:
  application_session_id:
  source_kind:
  source_item_id:
  target_contract_id:
  workspace_changeset_digest:
  run_id:
  platform_snapshot_id:
  environment:
  result:
  evidence_digest:
  verification_eligibility:
  recorded_at:
~~~

Outcome 对两种 source kind 都存在。

### 19.2 VerificationAttempt

~~~yaml
TemplateVerificationAttempt:
  verification_id:
  release_id:
  application_session_id:
  adoption_id:
  target_contract_id:
  run_id:
  platform_snapshot_id:
  environment:
  environment_fingerprint:
  source_bundle_digest:
  evidence_digest:
  result: passed | failed
  attribution:
  policy_version:
  verified_at:
  expires_at:
~~~

### 19.3 Eligibility

必须满足：

- source kind 为 curated_template；
- release、session、adoption、Contract 和 Run lineage 一致；
- source bundle digest 与 release 一致；
- 变化仅限 declared parameters 和批准的 AgentApplicationPlan；
- terminal Evidence 完整；
- environment 由平台事实推导。

用户在应用后进行计划外代码修改时，Outcome 仍保存，但 verification_eligibility=false。

### 19.4 失败归因

~~~text
template_defect
platform_incompatibility
user_input
external_dependency
infrastructure
unknown
~~~

Agent 可以提出 attribution proposal。只有 Evidence 足以证明的 template_defect 或 platform_incompatibility 进入负向推荐信号。

### 19.5 过期事件

历史 attempt 不修改。系统追加 TemplateVerificationStatusEvent：

~~~yaml
TemplateVerificationStatusEvent:
  event_id:
  verification_id:
  state: expired | revoked | superseded
  reason:
  policy_version:
  created_at:
~~~

## 20. 推荐与治理

推荐等级：

1. 当前环境有新鲜通过验证；
2. 兼容环境有新鲜通过验证；
3. gate 通过但尚无环境验证；
4. 当前环境存在可信失败；
5. 不兼容或 withdrawn，禁止应用。

同等级内使用任务匹配、验证样本的保守下界、最近验证时间和 adoption count。市场不把 1/1 显示成足以代表稳定性的 100% 可信结论。

可比较样本只包含 eligible pass 与有 Evidence 支持的 template_defect/platform_incompatibility failure；同环境内使用 95% Wilson lower bound 作为排序信号，不把该值展示成真实成功率。

治理规则：

- 新鲜可信失败可自动降低推荐等级；
- 不自动 withdraw；
- Agent 可以提出 new_version、deprecate、withdraw 或 supersede；
- publisher 或 reviewer 作最终决定；
- 历史 Contract、Run、Outcome、Evidence 和 verification 永久可追溯；
- RunPublication 不显示 TemplateVerification rate 或 curated trust tier。

## 21. Agent Profile 与工具

### 21.1 Application Profile

只读：

- market_discover
- market_candidate_compare
- market_application_get
- platform_get_snapshot
- workspace_list/search/read
- template_release_get
- run_publication_get

状态动作：

- market_application_start
- market_application_resolve_inputs
- market_application_build_plan

确认后动态加入：

- market_application_finalize

finalize 工具要求一次性 confirmation capability，使用后失效。

### 21.2 Publication Profile

只读：

- run_get
- evidence_read
- workspace_snapshot_read
- template_duplicate_precheck
- template_release_compare

状态动作：

- template_publication_start
- template_publication_extract
- template_publication_classify
- template_sanitization_preview
- template_parameterization_build
- template_bundle_build
- template_duplicate_check
- template_reproduction_schedule

用户确认后动态加入：

- template_publication_submit_review

submit_review 消费绑定 sanitized bundle、duplicate report、reproduction Evidence、visibility/scope、version 和 policy 的一次性 capability。

Pi 不可调用：

- user confirmation；
- reviewer decision；
- publish()；
- withdraw_release()；
- adopt_release()；
- RunPublicationStore.adopt()；
- 通用 SSH、sbatch、srun 或远端 shell。

### 21.3 Feedback/Governance Profile

只读：

- market_application_outcome_get
- template_verification_get
- evidence_read
- runtime_watch_get

仅允许 proposal：

- template_failure_attribution_propose
- template_new_version_propose
- template_withdraw_propose

VerificationAttempt 和 status event 由 MarketOutcomeService 根据 lineage/Evidence 确定性创建；withdraw、deprecate 和 supersede 由 publisher/reviewer 决定。

### 21.4 模型不可用

- 用户手选 curated template：继续 schema 填充和确定性 compatibility；
- 自然语言发现：退化为关键词、metadata 和平台过滤；
- 仅需确定性路径 rebasing 的 reference：可以继续；
- 需要理解或修改代码：blocked:model_unavailable；
- publication 的 rule-based secret/path gate 继续，但语义分类未完成时不能送审。

## 22. API

用户侧 application：

~~~http
POST /api/v1/market/applications
GET  /api/v1/market/applications/{session_id}
POST /api/v1/market/applications/{session_id}/responses
POST /api/v1/market/applications/{session_id}/confirmation
POST /api/v1/market/applications/{session_id}/cancel
GET  /api/v1/market/applications/{session_id}/events
~~~

用户侧 publication：

~~~http
GET  /api/v1/runs/{run_id}/share-suggestion
POST /api/v1/runs/{run_id}/share-suggestion/decision
POST /api/v1/runs/{run_id}/publications
POST /api/v1/runs/{run_id}/template-publication-sessions
GET  /api/v1/template-publication-sessions/{session_id}
POST /api/v1/template-publication-sessions/{session_id}/responses
POST /api/v1/template-publication-sessions/{session_id}/confirmation
POST /api/v1/template-publication-sessions/{session_id}/cancel
GET  /api/v1/template-publication-sessions/{session_id}/events
~~~

HTTP 客户端不能指定内部 phase。

### 22.1 直接 adopt 迁移

1. 新增 session service 和 API；
2. 更新第一方调用方与契约测试；
3. 在同一发布中关闭直接采用；
4. 旧 adopt endpoints 返回 409 MARKET.AGENT_APPLICATION_REQUIRED 和 application URL；
5. 底层 adopt 方法只保留内部调用。

不静默改变旧响应结构。

## 23. 持久化

新增或扩展：

- market_application_sessions；
- market_application_events；
- template_application_details；
- reference_adaptation_details；
- market_application_outcomes；
- template_candidate_application_sessions；
- share_suggestions；
- run_publication_share_manifests；
- run_publication_revisions；
- template_publication_sessions；
- template_publication_events；
- template_bundle_manifests；
- template_duplicate_reports；
- template_catalog_annotations；
- template_verification_status_events。

共同 envelope 的身份、source kind、phase、status、version 和 digest 使用关系字段；Agent proposal、findings 和 plan 使用受 JSON Schema 约束的 canonical JSON。

所有 migration 同时支持当前 SQLite simulator 和既有 Postgres migration 流程。append-only event 与 outbox 在同一事务写入。

## 24. 并发、幂等与恢复

- 所有命令要求 expected_state_version；
- Agent Turn 使用 lease 和 fencing token；
- phase 变更与 outbox event 原子提交；
- finalizer request key 为 session_id + plan_digest；
- reproduction 和 Slurm 等待保存 durable AgentTask；
- Worker 恢复先对账，不能重复创建 Contract、Run、review 或 release；
- LLM retry 只重建 proposal；
- stale fencing token 的写入被拒绝；
- source、plan、platform、workspace、policy、bundle、duplicate report 或 version 变化使 confirmation 失效。

主要恢复：

| 情况 | 行为 |
|---|---|
| missing input | waiting_user，保留已解析值 |
| model unavailable | deterministic fallback 或 blocked |
| SSH/MFA unavailable | paused_auth |
| PlatformSnapshot stale | 回到 evaluating |
| source withdrawn | 阻止 finalizing，提供替代候选 |
| workspace conflict | 保留 ChangeSet，进入冲突处理 |
| finalizer response lost | request key 对账 |
| reproduction failed | publication 回到 revising |
| review rejected | 保留 note，创建新 draft version |
| Worker/agentd restart | 从 version、event、Task 恢复 |

## 25. 错误码

Application：

- MARKET.NO_SUITABLE_TEMPLATE
- MARKET.AGENT_APPLICATION_REQUIRED
- MARKET.SOURCE_NOT_VISIBLE
- MARKET.SOURCE_WITHDRAWN
- MARKET.SOURCE_DIGEST_CHANGED
- MARKET.SOURCE_NOT_ADAPTABLE
- MARKET.ASSURANCE_MISMATCH
- MARKET.PLATFORM_SNAPSHOT_STALE
- MARKET.PLAN_STALE
- MARKET.CONFIRMATION_REQUIRED
- MARKET.CONFIRMATION_STALE
- MARKET.APPLICATION_CONFLICT
- MARKET.MODEL_UNAVAILABLE

Publication：

- TEMPLATE.PUBLICATION_SOURCE_INELIGIBLE
- TEMPLATE.PUBLICATION_SOURCE_STALE
- TEMPLATE.SANITIZATION_BLOCKED
- TEMPLATE.EXTERNAL_ASSET_UNDECLARED
- TEMPLATE.DUPLICATE_EXACT
- TEMPLATE.DUPLICATE_METADATA_ONLY
- TEMPLATE.VERSION_REQUIRED
- TEMPLATE.REPRODUCTION_FAILED
- TEMPLATE.REPRODUCTION_EVIDENCE_MISSING
- TEMPLATE.PUBLICATION_CONFIRMATION_STALE
- TEMPLATE.REVIEW_NOT_APPROVED

Verification：

- TEMPLATE.VERIFICATION_LINEAGE_INVALID
- TEMPLATE.VERIFICATION_BUNDLE_MISMATCH
- TEMPLATE.VERIFICATION_EVIDENCE_MISSING
- TEMPLATE.VERIFICATION_INELIGIBLE
- TEMPLATE.VERIFICATION_ENVIRONMENT_INVALID

## 26. 安全与权限

- 每个 transition 重新检查 owner；
- course/campus/public visibility 由可信 identity 解析；
- capability 不能改变 owner、source、allowed roots 或 budget；
- application sessions、bundle staging 和 Evidence owner 隔离；
- 项目文件中的提示不能增加工具或绕过确认；
- RunPublication ShareManifest 是 source Contract materialization 的授权边界；
- curated bundle 只包含 sanitized 内容；
- credential、SSH 私钥和 MFA 从不进入 Agent context；
- reviewer role 不由用户请求参数指定；
- withdrawn 不删除历史，但阻止新 finalization。

## 27. 成熟方案采纳

107Pilot 不直接引入下列系统，而是采用其稳定抽象：

1. [Backstage Software Templates](https://backstage.io/docs/features/software-templates/) 的 parameter form、review-before-create、独立 task 和失败日志，以及其 [dry-run testing](https://backstage.io/docs/features/software-templates/dry-run-testing/)；107Pilot 将 dry-run 扩展为 Sandbox 与 Slurm reproduction。
2. [Backstage template authoring](https://backstage.io/docs/features/software-templates/adding-templates/) 的 schema parameters、ordered steps 和 skeleton files；107Pilot 用 AgentApplicationPlan 和 typed tools 代替任意 action plugin。
3. [OCI Descriptor](https://github.com/opencontainers/image-spec/blob/main/descriptor.md) 的 mediaType、size 和 digest；107Pilot 用它组织 TemplateBundleManifest，但首版不要求 OCI registry。
4. [SLSA Provenance](https://slsa.dev/spec/v1.2/build-provenance) 的 subject digest、external/internal parameters、resolved dependencies 和 builder/run details；107Pilot 借此组织 Evidence，不宣称达到特定 SLSA level。
5. [The Update Framework specification](https://theupdateframework.github.io/specification/) 的 version、expiry 和 consistent snapshot 思路；107Pilot 用于 verification freshness 与防止替代关系回退，首版不引入完整签名角色体系。

## 28. 本地测试

### 28.1 Unit

- 判别联合禁止 source kind 切换；
- CandidateApplicationSession 只接受 verifier principal 和 private draft digest；
- phase/status transition table；
- plan 和 confirmation digest；
- ShareManifest 默认关闭；
- RunPublication optional field 的 forbidden secret/path gate；
- source Contract materialization 权限；
- family/content fingerprint canonicalization；
- exact、metadata-only、new-version 和 near-duplicate 分类；
- sanitization rule 和 classification schema；
- verification eligibility 和 failure attribution；
- trust tier 与 expiry event；
- request key、expected version 和 fencing。

### 28.2 Integration

- 所有市场采用必须创建 Agent session；
- curated 与 reference finalizer 创建正确 lineage；
- run reference 不产生 TemplateVerification；
- direct adopt endpoint 被拒绝；
- source withdrawal/platform stale 使确认失效；
- concurrent finalize 只创建一个 Contract/adoption；
- publication bundle 不读取 live workspace；
- reproduction candidate 不出现在 market read model，也不能获得 release assurance；
- duplicate release 被阻止；
- 不可见 duplicate 只返回 opaque token；
- metadata revision 不创建 release；
- equivalent successful Run 转 verification；
- review/publish 只能使用 reproduction Evidence；
- restart 后不重复提交 Task 或 release。

### 28.3 Simulator 纵向矩阵

| 场景 | 预期 |
|---|---|
| 手选 curated | Agent plan 后才创建 Contract |
| 自然语言目标 | 推荐、比较和 no-suitable fallback |
| 采用 run reference | reference_only 且重新检查路径/依赖 |
| RunPublication 未分享 Contract | 不能启动适配 |
| 可选分享字段含 token/私有 socket | forbidden gate 阻止 |
| 确认后 source withdrawn | finalizer 阻止 |
| PlatformSnapshot 改变 | 回 evaluating |
| 两请求并发 finalize | 一个 adoption/Contract |
| Agent/Worker 重启 | session/outbox/Task 恢复 |
| 5GB 权重 | external asset manifest only |
| bundle 含用户名/path/token | sanitization block |
| 隐式依赖 source workspace | 新 workspace reproduction fail |
| curated 复现和审核 | immutable release |
| 等价 bundle 再发布 | duplicate block |
| metadata-only 变化 | catalog annotation |
| 等价新 Run | verification，不发新 release |
| 应用后修改代码 | outcome 存在，verification ineligible |
| curated 原样成功 | 完整 verification |
| reference 成功 | 无 TemplateVerification |
| 可信模板失败 | 降权，不自动撤回 |
| 模型不可用 | schema 路径继续，代码推理 blocked |
| 两 owner 并发 | session/bundle/Contract/Evidence 隔离 |
| 登录节点进程检查 | 无 Pi/Node/Python Agent 常驻 |

## 29. 比赛演示

~~~text
学生 A 成功 Run
→ 默认不分享
→ 主动选择制作模板
→ duplicate precheck
→ Agent 提取、分类、严格脱敏
→ 5GB 权重只写 manifest
→ isolated verifier 通过 CandidateApplicationSession 复现
→ 用户确认 sanitized preview
→ reviewer 批准
→ immutable curated release

学生 B 描述实验目标
→ Agent 发现并解释该 release
→ 解析参数、适配当前 PlatformSnapshot
→ 用户确认 ApplicationPlan
→ private Contract
→ Slurm Run
→ Runtime Watch / Evidence
→ TemplateVerification
→ 市场显示新的可信环境事实

学生 A 再次运行等价作业
→ duplicate check 命中既有 release
→ 记录新 verification
→ 不产生重复模板
~~~

另演示一个 RunPublication，证明 reference_only、逐字段分享和 Agent 重新适配与 curated 保证等级不同。

## 30. 实现切片边界

后续 implementation plan 应拆为：

1. M0：schema、session envelope、新 API contract 和事件基础；
2. M1：Discovery、TemplateApplication、confirmation/finalizer，并关闭 curated direct adopt；
3. M2：ShareManifest、ReferenceAdaptation，并关闭 RunPublication direct adopt；
4. M3：TemplatePublication、bundle、sanitization 和 duplicate check；
5. M4：isolated reproduction、review/publish；
6. M5：Outcome、verification、trust read model 和治理。

每个切片必须单独具备 migration、unit、integration 和 simulator evidence，不能等到 M5 才验证 owner、幂等和恢复。

## 31. 非目标

- 本规格不设计前端布局；
- 不创建机器学习、数值计算、第一性原理或分子动力学模板内容；
- 不建设课程批改；
- 不建设完整初学者问答知识库；
- 不自动下载数据集或模型；
- 不把成功运行等同于科学正确；
- 不在登录节点运行 Agent brain；
- 不以远程 VM 作为本轮验收条件。

## 32. 完成门槛

1. 两种市场条目均不能绕过 Agent 应用；
2. curated 与 reference_only 保证在 schema、UI read model、Contract lineage 和 Evidence 中保持隔离；
3. 成功 Run 默认不分享，ShareManifest 逐字段授权；
4. 用户确认绑定精确 digest，模型不能代替确认；
5. direct adopt API 关闭；
6. TemplatePublication 使用 Run-bound snapshot，不读 live workspace；
7. strict sanitization、parameterization、duplicate check、reproduction、review 和 immutable release 全部存在；
8. exact 与 metadata-only 重复不能产生新 release；
9. 等价新 Run 可形成 verification；
10. run reference outcome 不能形成 TemplateVerification；
11. withdrawal、stale snapshot、并发、响应丢失和重启均可恢复；
12. 5GB 以上制品只进入 external manifest；
13. owner、scope、reviewer 和 Evidence 不串线；
14. 登录节点无 Agent 常驻进程；
15. 上述结论均由本地 simulator 可复现 Evidence 支持。
