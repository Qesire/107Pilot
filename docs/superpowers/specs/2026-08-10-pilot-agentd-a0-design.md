# 107Pilot `pilot-agentd` A0 与统一 LLM 包装设计

- 日期：2026-08-10
- 状态：已逐节确认，等待实施
- 上位设计：`docs/superpowers/specs/2026-08-10-pi-hpc-agent-core-design.md`
- 实施目标：建立独立 TypeScript `pilot-agentd`，统一校内 LLM 调用，并迁移现有 Python explain、contract patch 与 remediation 调用链
- 验证边界：远程 VM 当前不可用；A0 必须先通过本地 faux provider、模拟 OpenAI 网关与 Docker Compose 验证
- 本轮不包含：前端、生产工作区/Slurm 工具集、完整持久会话编排、课程批改和模板内容建设

## 1. 背景与问题

107Pilot 已经存在两套 Python OpenAI-compatible 客户端：

1. `pilot107.core.agent.OpenAICompatibleLLMProvider`，负责 evidence-bound explain 与 contract patch；
2. `pilot107.core.remediation_llm.OpenAICompatibleRemediationPlanProvider`，负责 remediation plan。

两者分别实现请求组装、HTTP、错误映射、结构化输出与重试。继续在 Python 中增加 Pi 所需的 streaming/tool-call 客户端，会形成第三套 LLM 语义，无法统一处理流式事件、工具调用、取消、checkpoint、模型兼容参数和指标。

同时，学校明确不希望每个学生在 Slurm 登录节点长期运行 Claude、Hermes 或完整 Pi CLI。A0 因此必须把 Pi Turn 放在 107Pilot 应用侧，并让登录节点保持为受限控制/数据中继。

## 2. A0 目标与完成定义

A0 完成时必须同时满足：

1. 存在独立、可构建的 TypeScript `services/pilot-agentd/`；
2. `pilot-agentd` 嵌入 Pi Agent Core，而不是启动完整 Pi CLI；
3. 校内 OpenAI-compatible 模型和 deterministic faux provider 经过同一个 Model Registry；
4. Python 与 Agentd 使用版本化 Turn、事件、checkpoint 和错误合同；
5. interactive Turn 可以流式输出、取消并从安全 checkpoint 恢复；
6. explain、contract patch 和 remediation plan 都经过 Agentd；
7. Python 生产代码不再直接访问 LLM `/chat/completions`；
8. 现有外部 HTTP 接口的状态码、字段和 deterministic fallback 行为保持兼容；
9. LLM API key 只进入 Agentd，不进入 Python API/Worker；
10. 本地单元、模拟网关、Python 集成、失败恢复和 Compose 安全门禁通过。

## 3. 方案决策

采用独立服务方案：

```text
107Pilot Python API / Worker
        │ internal bearer + NDJSON
        ▼
services/pilot-agentd
  ├── Pi Agent Core
  ├── Model Registry
  │    ├── campus OpenAI-compatible provider
  │    └── Pi faux provider
  ├── Turn executor
  ├── constrained-result tools
  └── event/error/checkpoint adapter
        │
        ▼
Campus LLM Gateway / local mock gateway
```

拒绝的替代方案：

- 每次 Python 调用临时启动 Node 子进程：启动成本高，不能形成共享 Worker Pool；
- Python 继续拥有 LLM HTTP，Pi 只消费代理流：保留多套流式与错误语义；
- A0 同时实现完整工具网关：范围过大，无法先验证模型、协议和恢复合同。

## 4. 运行时与依赖锁定

`pilot-agentd` 使用独立的：

- `package.json`；
- `package-lock.json`；
- `tsconfig.json`；
- Vitest 配置；
- Node 运行镜像与 Dockerfile。

不得复用当前前端根目录的 Node 18 依赖环境。A0 精确锁定：

- Node `22.19.x` 或更高的 Node 22 LTS 补丁版本，容器镜像最终使用 digest 固定；
- `@earendil-works/pi-agent-core@0.84.1`；
- `@earendil-works/pi-ai@0.84.1`。

选择 `@earendil-works/*` 新作用域，不同时安装旧 `@mariozechner/*` 作用域。锁文件是可复现依赖真源，禁止 `^` 或 `latest` 使核心依赖漂移。

Pi 上游 0.84.1 的包元数据要求 Node `>=22.19.0`，并提供 `Agent`、自定义 provider、OpenAI compatibility flags 和 faux provider。A0 使用 `Agent` 类而不是低层 `agentLoop()`，因为 `Agent.subscribe()` 的异步监听器可作为事件持久化/输出背压屏障。

## 5. 进程职责与信任边界

### 5.1 Python control plane

Python 继续拥有：

- 用户身份和授权；
- Run、Evidence、Diagnosis、Remediation Session 等业务真源；
- prompt/task profile 选择；
- citation、evidence、policy 和 allowed action 的最终校验；
- deterministic fallback；
- 对外 HTTP 兼容；
- 后续生产工具的批准与副作用权限。

### 5.2 `pilot-agentd`

Agentd 只拥有：

- 服务器端 ModelProfile 和 LLM key；
- Pi 单次短时 Turn；
- provider streaming 和 tool-call loop；
- constrained result 工具；
- 事件归一化、错误归一化、取消和 checkpoint 生成；
- 活动 Turn 的短时内存注册表。

Agentd 不拥有用户 SSH/MFA、Slurm token、工作区挂载、数据库写权限或任意宿主 shell。

### 5.3 调用方限制

Python 请求只能引用 Agentd 预注册的：

- `model_profile_id`；
- `prompt_profile_id`；
- `task_kind`；
- `toolset_id`。

请求不能携带 API key、任意 provider URL、任意 system prompt、任意工具实现或任意 JSON Schema。A0 的任务模板和结构化结果 schema 由 Agentd 版本化代码定义。

## 6. ModelProfile 与校内网关包装

ModelProfile 是唯一模型配置入口，至少包含：

```yaml
id: campus-default
provider: campus-openai-compatible
api: openai-completions
base_url: https://example.edu/v1
model: model-id
auth_env: PILOT107_LLM_API_KEY
timeout_ms: 60000
max_output_tokens: 1200
max_attempts: 2
context_window: 32768
reasoning: false
input: [text]
compat:
  supports_store: false
  supports_developer_role: false
  supports_reasoning_effort: false
  supports_usage_in_streaming: false
  supports_strict_mode: false
  max_tokens_field: max_tokens
```

Profile 文件可以包含非秘密 URL、model ID 和能力声明；密钥只能通过 `auth_env` 在 Agentd 进程内解析。Profile ID 是 Python 可选择的能力档位，而不是可注入的 URL。

校内网关首版采用保守兼容参数：

- system prompt 使用 `system` role；
- 使用 `max_tokens`；
- 不发送 `store` 或 `reasoning_effort`；
- 不假定 strict tool schema；
- 不假定 streaming usage 可用。

经过本地/真实 smoke 证明的能力才可在具体 Profile 中单项开启。不得依赖 Pi 的 URL 猜测作为生产真源。

测试使用 Pi 官方 faux provider。每个测试 Turn 使用独立 faux handle，避免并发测试共享响应队列。

## 7. 内部 HTTP 协议

### 7.1 端点

| 端点 | 用途 | 认证 |
|---|---|---|
| `GET /healthz` | 进程存活，不探测 LLM | 无，响应不含配置细节 |
| `GET /readyz` | 协议、Profile 和密钥引用已配置，不调用 LLM | 内网可读，响应只含 ID/状态 |
| `POST /internal/v1/turns` | 创建并流式执行 Turn | internal bearer |
| `POST /internal/v1/turns/{turn_id}/cancel` | 取消当前活动 Turn | internal bearer |

Turn 响应为 `application/x-ndjson; charset=utf-8`。每行是一个完整 JSON 事件；写入必须等待 socket backpressure。客户端断开时 Agentd 触发对应 `AbortController`。

请求体、单行事件和累计响应都设硬上限。请求验证或认证失败发生在流启动前，使用普通 JSON HTTP 4xx；流启动后所有失败都通过唯一终止事件表达。

### 7.2 `AgentTurnRequest`

```json
{
  "schema_version": "pilot107.agent-turn-request/v1",
  "turn_id": "uuid",
  "task_kind": "interactive",
  "model_profile_id": "campus-default",
  "prompt_profile_id": "hpc-assistant-v1",
  "toolset_id": "a0-none",
  "input": {},
  "checkpoint": null,
  "limits": {
    "timeout_ms": 60000,
    "max_output_tokens": 1200
  },
  "trace": {
    "correlation_id": "opaque-id"
  }
}
```

约束：

- `turn_id` 由 Python 生成并在单次活动期间唯一；
- `limits` 只能收紧 ModelProfile，不能扩大；
- `input` 由 `task_kind` 判别并严格校验，未知字段拒绝；
- `trace` 只接受不包含秘密的 opaque ID；
- checkpoint 必须通过版本、大小、digest 和 Profile/Prompt 一致性校验。

### 7.3 任务输入

A0 支持：

- `interactive`：用户消息、受类型和大小限制的 context blocks；
- `explain`：现有 evidence-bound explanation payload；
- `contract_patch`：current contract、recipe version 和 user intent；
- `remediation_plan`：现有 `RemediationPlanningContext.prompt_payload()`。

context block 显式标记来源和可信级别。Evidence、日志和源代码始终作为数据插入，不能改变 system policy。

## 8. 事件合同

每个事件都包含：

```json
{
  "schema_version": "pilot107.agent-turn-event/v1",
  "turn_id": "uuid",
  "sequence": 1,
  "timestamp": "2026-08-10T00:00:00Z",
  "type": "turn_started",
  "payload": {}
}
```

要求：

- `sequence` 从 1 开始严格单调递增且无重复；
- 同一流中的 `turn_id` 必须一致；
- 恰好一个终止事件；
- 终止后不能再出现事件；
- 未知 schema major、未知事件类型、乱序、截断或无终止事件均由 Python fail closed。

A0 事件类型：

1. `turn_started`；
2. `message_delta`；
3. `tool_call_requested`；
4. `tool_call_started`；
5. `tool_call_progress`；
6. `tool_call_completed`；
7. `checkpoint`；
8. `turn_completed`；
9. `turn_failed`。

Pi 事件映射必须保持源顺序。`message_delta` 只公开面向用户的文本，不公开原始 chain-of-thought。工具名称、经 schema 校验的参数摘要、结果摘要和错误状态保留，以便 Agent 获得足够的运行信息。

`turn_completed` 至少包含：

- 类型化 `result`；
- provider/model/profile 标识；
- input/output/cache token usage（provider 缺失时明确为 unavailable，而不是伪造 0）；
- provider call 次数；
- checkpoint digest；
- duration。

## 9. 结构化 Turn

`explain`、`contract_patch` 和 `remediation_plan` 使用任务专属的内部 `emit_result` AgentTool：

1. 参数 schema 与现有 Python JSON contract 对齐；
2. 工具没有外部副作用；
3. 参数验证成功后，把参数作为类型化 result；
4. 工具返回 `terminate: true`，阻止无意义的后续模型调用；
5. Agentd 仍执行基础 schema/大小验证；
6. Python 保留引用完整性、允许动作、patch 字段和策略校验。

模型若返回普通文本、调用错误工具或给出无效参数，Agentd 允许一次格式修复。修复 prompt 只说明合同错误，不回显秘密。第二次仍失败时返回 `output_contract_violation`。

结构化结果只是提案，不授予 remediation、contract 修改或作业执行权限。

## 10. 错误、重试和取消

稳定 Turn 错误码：

| code | retryable | 说明 |
|---|---:|---|
| `provider_auth` | false | 401/403 或密钥不可用 |
| `provider_rate_limited` | true | 429 |
| `provider_timeout` | true | provider deadline |
| `provider_unavailable` | true | transport、408、5xx |
| `provider_invalid_response` | 视情况 | 畸形 SSE/envelope |
| `output_contract_violation` | false | 格式修复后仍不合法 |
| `aborted` | false | 用户取消、客户端断开或上层 deadline |
| `internal_error` | false | 未分类内部错误，响应必须脱敏 |

重试规则：

- 最大次数来自 ModelProfile，范围 1–3；
- 408、429、5xx 和传输错误只能在尚未产生有意义 interactive 输出时重试；
- interactive 已产生文本或对外工具事件后不自动重放；
- constrained Turn 因唯一工具无副作用，可在 provider 中断后重做，仍受总次数限制；
- output contract 只有一次独立格式修复机会；
- backoff 有上限，测试使用可注入 clock/sleeper 保持确定性；
- 错误详情不包含 key、Authorization header、完整 provider body 或敏感 prompt。

Agentd 使用每 Turn `AbortController`。取消请求幂等：活动 Turn 返回 accepted，已经终止返回 terminal/not-active 状态。Pi 的 aborted partial message 不作为成功结果。

## 11. Checkpoint 与恢复

Checkpoint 是 Python 可持久化、Agentd 可恢复的版本化安全状态，而不是 Pi 私有对象的任意序列化。至少包含：

- checkpoint schema version；
- turn/session lineage；
- model/prompt profile ID；
- 已归一化的 user/assistant/toolResult 消息；
- 已完成工具调用与结果；
- usage aggregate；
- checkpoint digest。

不包含：

- LLM key、internal bearer、Authorization headers；
- provider URL 中的秘密 query；
- SSH/Slurm/MFA 凭据；
- 未公开 chain-of-thought；
- 函数、AgentTool 实现或不可验证的任意对象。

A0 必须验证：faux Turn 中途取消后得到安全 checkpoint，新 Agent 实例加载 checkpoint 后可以继续，事件 sequence 和 terminal invariant 仍成立。

## 12. Python 迁移

新建 `src/pilot107/agent/`：

- `protocol.py`：版本化请求、事件、checkpoint 和错误类型；
- `client.py`：同步 NDJSON Agentd 客户端、认证、大小/序列/终止验证；
- `providers.py`：现有业务 Protocol 的 Agentd 适配器；
- `config.py`：`PILOT107_AGENTD_*` 配置。

迁移策略：

1. 保留 `OpenAICompatibleLLMProvider` 和 `OpenAICompatibleRemediationPlanProvider` 的导入兼容名称，但实现委托给 Agentd adapter；
2. 删除两处 Python `/chat/completions` 请求、provider payload 和 SSE/JSON envelope 逻辑；
3. `AgentExplainService`、contract suggest route 和 `RemediationPlanService` 的业务输入/输出保持不变；
4. Agentd 错误分别映射为现有 `AgentProviderError` 与 `RemediationPlanError`；
5. explain 仍在 provider 失败时回退到 deterministic explanation；
6. contract suggest 仍返回 HTTP 200 degraded payload；
7. remediation 仍经过 Python parse、evidence、policy 和 action parameter 校验；
8. 现有 LLM observer 由 Agentd terminal usage 驱动，保留 provider/model/outcome 指标维度；
9. provider 未配置时仍安全降级，不阻止确定性功能。

外部 API 兼容指状态码、字段、fallback 和权限语义兼容；内部 Python Provider 构造参数可以迁移为 Agentd URL/Profile，因为它不是对外协议。

## 13. 配置与 Compose

Python API/Worker 只接收：

```text
PILOT107_AGENTD_URL
PILOT107_AGENTD_TOKEN
PILOT107_AGENTD_MODEL_PROFILE
```

Agentd 接收：

```text
PILOT107_AGENTD_LISTEN_HOST
PILOT107_AGENTD_LISTEN_PORT
PILOT107_AGENTD_TOKEN
PILOT107_AGENTD_MODEL_PROFILE
PILOT107_LLM_BASE_URL
PILOT107_LLM_API_KEY
PILOT107_LLM_MODEL
PILOT107_LLM_TIMEOUT_SECONDS
PILOT107_LLM_MAX_TOKENS
PILOT107_LLM_MAX_ATTEMPTS
```

`PILOT107_LLM_STRUCTURED_OUTPUT_MODE` 不再控制 Python response_format；结构化输出由 Pi tool contract 统一实现。迁移期可以读取该变量并给出弃用提示，但不得形成第二条执行路径。

Compose 增加 `pilot-agentd` 服务：

- 仅连接应用内部网络，不发布宿主端口；
- read-only rootfs、non-root user、drop all capabilities、no-new-privileges；
- API/Worker 依赖 Agentd readiness；
- LLM key 仅位于 Agentd environment/secret；
- Agentd 不挂载 `/public`、数据库、SSH socket 或 Slurm 配置。

## 14. 本地验证矩阵

### 14.1 TypeScript 单元测试

- ModelProfile 合法/非法配置与秘密不出现在错误中；
- campus compatibility flags 映射；
- faux text streaming；
- faux tool-call 与 `emit_result`；
- malformed structured result 和单次 repair；
- event 顺序、唯一 terminal、usage unavailable；
- abort、client disconnect、timeout；
- checkpoint sanitize、digest、restore；
- internal bearer、body limit、unknown fields 和 cancel 幂等。

### 14.2 模拟 OpenAI SSE 网关

本地 mock server 覆盖：

- 正常文本增量；
- SSE 行和 JSON 被任意分片；
- 标准 tool-call arguments 增量；
- 缺少 streaming usage；
- 401、403、408、429、500/503；
- 超时、畸形 JSON、无终止帧、中途断开；
- Authorization 发送到 mock，但不进入事件/日志；
- provider retry 与 partial-output no-replay。

### 14.3 Python 单元测试

- NDJSON 部分读取、UTF-8、行/累计大小上限；
- schema、turn ID、sequence、唯一 terminal 校验；
- Agentd 错误映射；
- explain、contract patch、remediation adapter；
- observer usage/outcome；
- deterministic fallback 和 degraded response 不变。

### 14.4 纵向集成

- Node 22 Agentd + faux provider + Python adapter；
- explain API 纵向结果；
- contract suggest API 纵向结果；
- remediation planning 纵向结果与 Python policy validation；
- interactive Turn streaming、cancel、checkpoint/restore；
- Agentd 不可用和流截断时 fail closed；
- API/Worker 容器内不存在 `PILOT107_LLM_API_KEY`；
- 静态扫描确认 Python 生产代码不再请求 `/chat/completions`。

真实校园 LLM smoke 在没有 key 时必须明确安全跳过，不伪装成功；远程 VM 不作为 A0 完成前置条件。

## 15. 实施顺序

1. 建立独立 Node 22/Pi 锁定包和测试基线；
2. 定义共享 JSON Schema 与 TypeScript/Python 协议模型；
3. 实现 ModelProfile、faux provider 和 campus provider；
4. 实现 Pi Turn executor、结构化工具、事件/错误/checkpoint；
5. 实现 Agentd HTTP/NDJSON 与取消；
6. 实现 Python client 和 provider adapters；
7. 逐条迁移 explain、contract patch、remediation；
8. 加入 Compose、安全配置和本地 smoke；
9. 执行完成审计，确认不存在旧直连或未覆盖目标。

所有生产函数与行为变更按 TDD 实施：先写失败测试并确认失败原因，再写最小实现，最后重构和运行相关回归。

## 16. 验收门禁

A0 不得以“服务能启动”作为完成。最终必须同时提供：

- 精确锁定的 Pi/Node 构建证据；
- faux provider 的 deterministic golden tests；
- 模拟 OpenAI SSE 的正常与失败矩阵；
- Python 三条调用链的纵向测试；
- cancel/checkpoint/restore 证据；
- 外部 API 回归；
- Compose secret-isolation 检查；
- Python 无 LLM 直连扫描；
- 完整相关测试、类型检查和 lint 结果。

任何一项缺失时 Goal 继续保持未完成。

## 17. 上游依据

- Pi Agent Core 包与 Node 要求：<https://github.com/earendil-works/pi/blob/main/packages/agent/package.json>
- Pi Agent Core 事件、工具、取消与 `Agent` 屏障语义：<https://github.com/earendil-works/pi/blob/main/packages/agent/README.md>
- Pi AI 自定义 provider、OpenAI compatibility 与 faux provider：<https://github.com/earendil-works/pi/blob/main/packages/ai/README.md>
- Pi 仓库许可：MIT，见 <https://github.com/earendil-works/pi>
