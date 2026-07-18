# Phase A — 4 缺口修复设计

- 日期: 2026-07-18
- 范围: 让 107Pilot 设计中的 12 步演示闭环在 S1 VM (8C/16G, Docker Slurm simulator) 上真正跑通
- 前置: S1 已部署且 G3 功能链通过 (见 `docs/phase-3/s1_vm_deployment_evidence_20260718.md`)
- 后续: Phase B 扩展闭环新功能 (代码上传 / LLM 生成作业 / 自动 capsule / agent 热修 / 下载上传重试 / 分享)

## 背景

S1 部署后用户发现 5 个问题。经 4 条探索通道调查 (exp-1 VM 环境探测 / exp-2 设计文档+workspace UX / exp-3 LLM 接入 / exp-4 模板市场)，确认 4 个缺口 + 1 个扩展闭环。本 spec 覆盖 4 个缺口 (Phase A)；扩展闭环另起一轮 brainstorm (Phase B)。

### 4 个缺口根因 (reconciled)

| 缺口 | 根因 | 修复类型 |
|---|---|---|
| 1. VM 环境不显示 | 无 Slurm 实时事实自动采集; `latest_snapshot` 恒为 null | 代码: 新增 REST 采集器 + 启动/定期调用 |
| 2. 模板市场空 | 6 个预置 recipe 加载但从未发布为 market release; `TemplateMarketStore` 启动空表 | 代码: 新增 seed (完整发布流) |
| 3. LLM 未接入 | `.env` 三个 LLM 变量空 + UI `advanceRemediationSession` 发 `{}` → provider 默认 none | 配置 (3 env) + UI (provider 选择) |
| 4. workspace 绑 job | `/agent` `/terminal` 路由已顶级, 但空状态赶用户回 Run 页; session 创建只在 Run 页 | 前端 IA: 内联 RunPicker |

## 设计决策 (用户已确认)

1. **顺序**: Phase A (4 缺口) → Phase B (扩展闭环)
2. **LLM 端点**: USTC `ustc-107` provider — `baseURL https://api.llm.ustc.edu.cn/v1`, `model glm-5.2-107` (OpenAI-compatible, reasoning); apiKey 从 opencode 配置的 `ustc-107` 条目读取, 不写入本 spec
3. **模板 seed**: 完整发布流 (create_draft→submit_review→approve→publish), 幂等, 系统 bootstrap 身份
4. **迭代方式**: 全量重建 app 镜像 → 新 revision tag → 导出 bundle → VM 重部 (不用 bind-mount override, 保 frozen-digest 纪律)
5. **VM 环境事实**: 从 Slurmrestd REST 读取 (不是主机 probe); 平台绑定真实 Slurm 后有读权限, 无需盲探

## A-1 — Slurm 实时事实自动采集

### 问题重定义

平台绑定真实 Slurm (simulator) 后, 应直接从 Slurmrestd 读取分区/节点/QoS 事实。快照机制 (`PlatformSnapshotService` + `ObservationSourceType.REST` + `capability_profile_from_real107_probe` + `_partitions_from_snapshot`) 已存在, 缺的是启动时 + 定期自动调用, 所以 `latest_snapshot` 恒为 null, 用户只看到静态 profile。

### 方案

新增 REST 采集器 (不走 CLI — API 容器 `read_only:true` + `cap_drop:ALL`, 跑不了 `scontrol`, 但能访问 `http://slurmrestd:6820`):

1. 查询 slurmrestd `GET /slurm/v0.0.41/partitions` + `GET /slurm/v0.0.41/nodes`
2. 解析成 `PlatformSnapshot`, 存入 `PlatformSnapshotStore`, `source_type=ObservationSourceType.REST`
3. 启动时采集一次 + 后台线程每 5 分钟刷新 (TTL 300s, 与现有 `freshness_seconds` 对齐)
4. `/api/v1/platform/capabilities?owner=alice` 的 `latest_snapshot` 从 null 变为实时分区/节点事实

### 职责分离

- `CapabilityProfile` (静态 profile): 声明"这是什么环境" (CPU-RC, 4CPU/6GiB/4h) — 不变
- `latest_snapshot` (实时事实): "Slurm 此刻告诉你什么" (当前分区状态, 节点 up/down) — 从 REST 填充

符合 `capability_profile.md:11` "不替代 Slurm, 不把一次 probe 当永久真相"。

### 改动

| 文件 | 改动 |
|---|---|
| `src/pilot107/adapters/slurmrest_snapshot.py` (新) | REST 采集器: 复用 `adapters/slurm.py` REST 客户端 + `platform_parsers`; 查 partitions/nodes, 构造 `PlatformSnapshot` |
| `src/pilot107/api/service.py` | `build_api_service()` 启动时触发首次采集 + 起后台刷新线程 (daemon thread, 5min interval) |
| compose | 不改 (网络已通) |

### 边界

- 采集失败不阻塞启动 (记录 limitation, `latest_snapshot` 保持 null, 静态 profile 仍可用)
- 刷新线程是 daemon, 进程退出即终止
- 不在 worker 进程跑 (worker 无 slurmrestd 客户端配置)

## A-2 — 模板市场 seed (完整发布流)

### 问题

6 个预置 recipe (`data/submission_templates/*.yaml`) 加载到 `RecipeCatalog` (`contracts.py:125-139`), 但 `TemplateMarketStore` 启动空表 (`service.py:230-254` 只跑 migrations, 无 seed)。`template_releases` 只能通过 `create_draft→submit_review→approve→publish` 填充 (`template_market.py:623` 唯一 INSERT)。`scripts/smoke_sim_phase3c.py` 证明这是手动 4 步流程。所以 `/api/v1/templates` 返回空, 市场页显示"没有匹配的 release"。

### 方案

新增 `src/pilot107/core/template_market_seed.py`, 在 `build_api_service()` 末尾调用:

1. **系统 bootstrap 身份**: 注入 `pilot107-system` (加入 `template_admins` + `template_reviewers`), 绕过自审禁止 (系统种子非用户行为)
2. **遍历发布**: 对 `RecipeCatalog.list_versions()` 中每个尚无 `template_releases` 行的 recipe:
   - `create_draft` (从 recipe 生成 draft payload) → `submit_review` → `decide_review(approve=True)` → `publish`
   - `visibility=public`, `idempotency_key=pilot107-seed-{recipe_id}-{version}`
3. **幂等**: 已发布的 `template_id`+`release_version` 跳过 (重启用)
4. **容错**: 发布闸门 (`TemplatePublicationGate`) 对 GPU recipe 可能因无 OCI 能力阻塞 — seed 记录 `gate-blocked` 并继续, 不中断启动; CPU recipe 必须成功
5. **不过滤**: CPU-RC profile 下 GPU recipe 被 API 隐藏是显示层行为, seed 仍应发布全部 6 个

### 改动

| 文件 | 改动 |
|---|---|
| `src/pilot107/core/template_market_seed.py` (新) | seed 函数: 遍历 recipe, 走发布流, 幂等, 容错 |
| `src/pilot107/api/service.py:254` 后 | 插入 seed 调用 (在 `return Pilot107HttpApi(...)` 前) |
| `src/pilot107/core/template_market.py` | `decide_review` 接受系统身份 (或 seed 用内部直写 — 不推荐, 架空审核) |

### 风险与缓解

- **风险**: 发布闸门阻塞 GPU recipe → **缓解**: seed 容错, CPU recipe 必须成功, GPU recipe 记录 gate-blocked
- **风险**: 系统身份绕过审核模型 → **缓解**: 仅用于 seed, 运行时用户走正常审核流

## A-3 — LLM 接入 (USTC glm-5.2-107) + UI provider 选择

### 问题

LLM 客户端 (`OpenAICompatibleLLMProvider`, `agent.py:149-355`) production-ready, 但:
- (a) `.env.cpu-rc` 三个 LLM 变量空 → `_build_llm_provider` 返回 None → 始终确定性降级 (`service.py:513-514`)
- (b) 即使配好, UI `advanceRemediationSession` (`api.ts:325-331`) 发 `{}` → `remediation_routes.py:195` 默认 `provider="none"` → 永远不调 LLM

### 方案 (两层)

**配置层** (立即可做, apiKey 不入仓库):
```
PILOT107_LLM_BASE_URL=https://api.llm.ustc.edu.cn/v1
PILOT107_LLM_API_KEY=<从 opencode 配置 ustc-107 条目读取, 注入 .env.cpu-rc, 不 commit>
PILOT107_LLM_MODEL=glm-5.2-107
PILOT107_LLM_STRUCTURED_OUTPUT_MODE=prompt_json
```
重启 api 容器 → `llm_enabled` 翻为 `configured`, `provider="local"` 调用命中 USTC 网关。

**UI 层** (让浏览器能用 LLM):
- `apps/web/src/api.ts:325-331`: `advanceRemediationSession` body 改为 `{"provider":"local"}`
- `apps/web/src/AgentPage.tsx`: remediation session 详情加 provider 选择器 (none=确定性规则 / local=USTC LLM), 默认 local
- Agent explain 同理: `POST /runs/{id}/agent/explain` 的 `provider` 在 UI 暴露选择

### 不改

- worker 进程 (`worker/service.py:359` 硬编码 `llm_provider=None`) — LLM 只在 API 进程, 设计如此
- `remediation_llm.py` 的 `RemediationPlanV1` 结构化提案路径 — 未接入 live `_plan_turn`, 归 Phase B

### 降级

LLM 不可用时 (`explain` 抛 `AgentProviderError`) 现有逻辑捕获并转 `local_llm_fallback` 警告 (`agent.py:427-439`), 规则诊断仍工作。`prompt_json` 模式兼容任何 OpenAI-compatible 端点 (不在 prompt 里要求 `response_format`)。

## A-4 — workspace 内 Run 绑定器

### 问题

`/agent` 和 `/terminal` 路由已是顶级 (`App.tsx:23-31`), 但空状态把用户赶回 Run 页:
- `AgentPage.tsx:80`: "从失败 Run 的诊断页启动"
- `pages.tsx:298`: "从 Run 摘要中的终端协同进入"

session 创建只在 Run 页触发 (`RunEvidencePanel.tsx:112` `createRemediationSession`)。

### 方案 (纯前端 IA, 后端契约不变)

1. **RunPicker 组件**: 新增 `apps/web/src/RunPicker.tsx`, 复用 `useRuns` (`query.ts`), 展示 owner-scoped Run 列表, 按状态过滤 (failed/active), 选中返回 `run_id`
2. **`/agent` workspace**:
   - 空状态改为内联 RunPicker (替代"去 Run 页"文案)
   - 用户选 failed Run → `createRemediationSession(run_id)` → 进入 session 视图 (现有逻辑)
   - 保留 Run 页"进入 Agent"入口 (两种入口并存)
3. **`/terminal` workspace**:
   - 空状态改为内联 RunPicker
   - 用户选 Run → 设 `?run=run_id` → `TerminalCollaborationPage` 现有逻辑接管 (命令绑定 job_id+workdir)
4. **保留硬约束** (设计 invariant):
   - Agent 仍 Evidence-bound + per-Run, 不扫描任意作业 (安全声明改为"选择 Run 后 Agent 只处理该 Run 的 Evidence")
   - 用户确认不变 (invariant #5)
   - trusted-header 身份不变 (无 `?owner=`)

### 改动

| 文件 | 改动 |
|---|---|
| `apps/web/src/RunPicker.tsx` (新) | Run 列表 + 状态过滤 + 选中回调 |
| `apps/web/src/AgentPage.tsx` | 空状态改 RunPicker + 绑定流程 |
| `apps/web/src/pages.tsx` | `TerminalCollaborationPage` 空状态改 RunPicker + 绑定 |
| `apps/web/src/RunEvidencePanel.tsx` | 不改 (Run 页入口保留) |

### 不改

- 后端 API (`/runs/{id}/remediation-sessions`, `/runs/{id}/agent/explain`, Run 读模型)
- `api.ts` 请求函数, `query.ts` hooks

## 实现顺序与并行

```
A-1 slurmrest_snapshot.py (新) + service.py 启动段     ──┐
A-2 template_market_seed.py (新) + service.py 启动段     ──┼─ 同碰 service.py 不同段, 协调
A-3 .env.cpu-rc + api.ts + AgentPage.tsx provider 选择   ──┤
A-4 RunPicker.tsx (新) + AgentPage.tsx + pages.tsx       ──┘ A-3 和 A-4 都改 AgentPage.tsx, 顺序做
```

- A-1 和 A-2 后端并行 (不同 `service.py` 段)
- A-3 配置层先做 (立即生效), UI 层和 A-4 串行 (都改 `AgentPage.tsx`)
- 全部完成 → 本地重建 app 镜像 → 新 revision → 导出 bundle → VM 重部 → 验证 12 步闭环

## 验证

Phase A 完成后在 VM 上验证设计中的 12 步闭环 (`16_风险登记与交付物.md:83-106`):

1. 连接平台 — `/platform/capabilities` 显示静态 CPU-RC profile + `latest_snapshot` 实时 Slurm 事实
2. 选择 Recipe/模板 — `/market` 显示 6 个预置模板 (CPU 可见), adopt 进 Studio
3. 创建 Contract + preflight — QoS-aware 校验, BLOCK/WARN
4. 三层脚本预览 — original/resolved/wrapper + SHA256
5. 真实 Slurm 提交 — 实际 job_id (非 demo-)
6. Run 时间线 + 状态机 — Pending/Running/Succeeded
7. Evidence 采集 — submission/slurm/logs/outputs
8. 失败 → 诊断 → retry — 规则诊断 + LLM 解释 (provider=local)
9. Agent remediation — workspace 内选 Run 启动, Evidence-bound, 用户确认
10. Capsule — verifiable, checksum, capsule_state=ready
11. 重启恢复 — 已验证 (S1 部署时)
12. LLM 不可用降级 — 规则仍工作

## 边界 (Phase A 不做)

- 真实 107 (Slurm 为模拟器)
- 校园多用户生产 (单租户 fixed_user=alice)
- G4 供应链 CI 扫描
- 扩展闭环新功能 (代码上传 / LLM 生成作业 / 自动 capsule / agent 热修 / 下载上传重试 / 分享) — Phase B
- `RemediationPlanV1` 结构化提案接入 live `_plan_turn` — Phase B

## 交叉引用

- 设计基线: `文档/107/design_v1.4/` (12_Web前端模块, 10_诊断与Agent模块, 06_Recipe与Contract模块, 13_测试验收与运维, 16_风险登记与交付物)
- 当前实现: `docs/phase-3/current_actual_and_execution_plan.md` (§9.2 IA, §11 workbench)
- S1 部署证据: `docs/phase-3/s1_vm_deployment_evidence_20260718.md`
- 探索发现: exp-1 (VM 环境) / exp-2 (设计+workspace UX) / exp-3 (LLM) / exp-4 (模板市场)
