# 05. 前后端协调与 read model 契约

> 实现状态：M0 的统一市场已完成。M1 的连接状态 API、全局状态条、
> Cluster 连接面板和 Run backend provenance 已实现；真实 107 live
> acceptance 尚待完成。当前事实见
> [07-m0-implemented-contract.md](07-m0-implemented-contract.md) 与
> [08-m1-implemented-contract.md](08-m1-implemented-contract.md)。

## 1. 原则

1. 后端写模型维护领域不变量；前端不根据页面数据自行推导权限、成功或可执行性。
2. 前端只消费公开的 read model；不接触服务器文件路径、Evidence store path、SSH session socket 或原始凭据。
3. mutating request 均使用现有 BFF 身份转发、request id、request key、Origin 防护和错误 envelope。
4. 每个页面都要区分 `loading / empty / forbidden / stale / degraded / action required`，不能把 SSH session 过期显示成普通网络错误。
5. 后端先稳定 JSON schema 与契约测试，前端再接入 TanStack Query；禁止同时改字段语义和 UI 解释。

## 2. 页面信息架构

| 页面 | 主对象 | 主操作 | 后端 read model |
|---|---|---|---|
| `/projects` | 当前用户工作 | 继续处理 Run / 新建 Contract | personal workspace summary |
| `/market` | 市场条目 | 查看 / 采用 | `MarketItemPage` |
| `/market/:item_id` | 成功作业或 curated template | 采用、查看说明 | `MarketItemDetail` |
| `/studio/:contract_id` | 用户 Contract | 预检、准备、提交 | Contract + capability + preflight |
| `/runs`、`/runs/:id` | Run | 观察、取消、发布、修复 | Run timeline + evidence + diagnosis |
| `/agent` | remediation session | 审核计划、创建 repair ticket | Agent session / action plan |
| `/cluster` | 平台事实 | 刷新或查看来源 | capability + PlatformSnapshot |
| `/terminal` | 受限诊断视图 | 查询固定事实 | typed terminal/relay read model |

`/templates/:id` 作为旧 curated-template 深链继续支持；新市场详情统一使用 `/market/:item_id`。

## 3. 新 API 契约

### 3.1 Market

```text
GET    /api/v1/market/items
GET    /api/v1/market/items/{item_id}
POST   /api/v1/market/items/{item_id}/adopt
POST   /api/v1/market/items/{item_id}/withdraw
POST   /api/v1/runs/{run_id}/publications
GET    /api/v1/runs/{run_id}/publication
```

`GET /market/items` 的过滤参数：

```text
q, kind, visibility, tag, cursor, limit
```

不要把旧模板专用的 `verified`, `verification_environment`, `gpu`, `partition` 过滤语义直接施加给普通成功 Run。若未来需要资源过滤，基于 publication source Contract 的明确资源摘要增加新字段和测试。

### 3.2 Run publishability

Run summary 增加后端计算字段：

```json
{
  "publication": {
    "status": "eligible | published | ineligible",
    "reason": "run_not_succeeded | exit_nonzero | not_owner | already_published | null",
    "publication_id": null
  }
}
```

前端仅依赖该字段决定是否展示发布入口；后端创建时仍重新验证，不相信客户端缓存。

### 3.3 SSH session

```text
GET  /api/v1/platform/connections
POST /api/v1/platform/connections/{connection_id}/check
```

响应提供：

```json
{
  "connection_id": "real107",
  "state": "active | auth_required | unavailable",
  "owner": "current-user-only",
  "checked_at": "...",
  "expires_at": "...",
  "message": "需要重新进行 MFA 验证"
}
```

不返回 target hostname、socket path、key fingerprint 或认证材料。`auth_required` 在 topbar、Run 页和 Agent 页使用相同的状态文案。

## 4. TypeScript 数据模型

建议在 `apps/web/src/types.ts` 使用 discriminated union：

```ts
type MarketItem = RunPublicationMarketItem | CuratedTemplateMarketItem;

interface RunPublicationMarketItem {
  kind: "run_publication";
  item_id: string;
  title: string;
  description: string;
  tags: string[];
  visibility: Visibility;
  source: SuccessfulRunSummary;
  adoption: { available: boolean; reason: string | null };
}

interface CuratedTemplateMarketItem {
  kind: "curated_template";
  item_id: string;
  template_id: string;
  release_version: string;
  // existing release metrics remain here only
}
```

不得把 `verification_passed`、`success_rate` 设为所有市场项目的必填字段。组件通过 `item.kind` 渲染不同事实区。

## 5. Query 与 mutation 规则

| 动作 | mutation key | 成功后失效的 query key |
|---|---|---|
| 发布成功 Run | `publish-run` | `run`, `runs`, `market-items`, `market-item` |
| withdraw | `withdraw-market-item` | `market-items`, `market-item`, `run` |
| adopt | `adopt-market-item` | `contracts`, `market-item`, `runs` |
| submit/cancel/retry | 现有 Run mutation | `run`, `runs`, `evidence`, `diagnoses`, `market-items` when terminal |
| session check | `check-connection` | `connections`, affected `run`/`agent` status |

SSE/polling 更新 Run 到 `SUCCEEDED` 时，只刷新该 Run；不要自动发布，也不要自动弹出分享确认。

## 6. 前后端分阶段交付

### Slice A：成功 Run 发布

后端先交付：

```text
schema migration → store/service → eligibility read model → API contract tests
```

前端随后交付：

```text
Run success CTA → confirmation sheet → publication mutation → MarketItem card/detail
```

完成条件是模拟 Slurm live Run 成功后，用户在浏览器完成发布、市场可看到条目、另一个模拟用户能采用并进入 Studio。

### Slice B：统一市场

后端交付 unified market read service；前端把现有 `MarketPages.tsx` 迁移到 union。旧 `/templates` API 和深链暂时保留，避免破坏既有测试和共享链接。

### Slice C：真实 SSH 状态

后端交付 session read model 与 `real107-ssh` backend；前端仅展示连接状态、明确 action-required、Run 后端 provenance 和受限诊断，不暴露连接细节。

### Slice D：Agent 工具闭环

后端提供 action-plan/read model、approval mutation、RepairTicket；前端复用现有 Agent workbench 的 session/takeover 模式，不另建聊天页面。

## 7. UI 文案约束

必须使用事实性文案：

| 场景 | 正确文案 |
|---|---|
| 普通市场条目 | `发布者曾成功运行此作业` |
| 未公开代码 | `代码与数据由发布者自行说明` |
| 私有路径 adoption | `采用后请替换自己的工作目录与依赖` |
| 模拟器环境 | `模拟 Slurm 环境` |
| 真实 SSH 失效 | `连接需要重新验证，未执行新的作业操作` |
| Agent 建议 | `建议，需确认后执行` |

禁止使用 `已认证可复现`、`保证成功`、`Agent 已修复`，除非有相应的更严格产品语义和可验证证据。

## 8. 契约测试

- Python HTTP tests 对每个 Market/connection error code 固化 payload；
- TypeScript schema tests 验证 union 解析与未知 `kind` fail closed；
- Vitest 覆盖 eligibility、checkbox、withdraw、私有内容空状态；
- Playwright 覆盖 `Market → Adopt → Studio → Run → Success → Publish → Market`；
- 模拟器 e2e 与真实 backend contract 使用同一场景描述，真实路径只在明确授权时执行。
