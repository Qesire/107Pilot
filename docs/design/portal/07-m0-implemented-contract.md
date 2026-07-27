# 07. M0 已实现契约与后续边界

状态：2026-07-26 已实现并通过后端、前端、命令网关模拟器与浏览器验证。本文描述当前代码事实。

## 1. 写模型

实现位于 `src/pilot107/core/run_publications.py`，存储迁移位于 `run_publication_migrations.py`，并同步写入 PostgreSQL native domain schema。PostgreSQL 使用追加式 `004a.002.run_publications`，不改写既有 `004a.001` 的 checksum。

```text
run_publications
  publication_id, source_run_id (unique), source_contract_id, owner
  title, description, visibility, scope_key, tags_json, reproduction_note
  request_key (unique per owner), published_at, updated_at
  withdrawn_at, withdrawal_actor, withdrawal_reason

run_publication_adoptions
  adoption_id, publication_id, adopter, request_key (unique per adopter)
  target_contract_id, created_at
```

发布必须同时满足：来源 Run 是当前用户所有、`state == SUCCEEDED`、`exit_code` 以 `0:` 开头、`confirm_share == true`，且该 Run 未曾发布。发布、撤回和采用会在来源 Run 的既有 event 流中记录：

```text
market.run_published
market.run_withdrawn
market.run_adopted
```

市场 read model 绝不包含 submitted script、远端 workdir、源 Contract payload、Evidence store path 或原始日志。

## 2. 当前 HTTP 契约

| 操作 | 路径 | 说明 |
|---|---|---|
| 发布 | `POST /api/v1/runs/{run_id}/publish` | 需 `request_key`、标题、可见性、`confirm_share: true` |
| 统一浏览 | `GET /api/v1/market/items?q=&kind=&visibility=&tag=&cursor=&limit=` | `run_publication` 与 `curated_template` 按发布时间稳定分页 |
| 统一详情 | `GET /api/v1/market/items/{item_id}` | 使用各领域原有 visibility 规则 |
| 统一采用 | `POST /api/v1/market/items/{item_id}/adopt` | Body 仅含 `request_key` |
| 统一撤回 | `POST /api/v1/market/items/{item_id}/withdraw` | 仅发布者，Body 含 `reason` |
| 普通成功作业兼容路径 | `/api/v1/market...` | 保留旧客户端兼容，不再作为 Web 主 read model |
| curated 模板（保留） | `/api/v1/templates/...` | 现有审核/验证路径没有改动 |

统一条目使用 discriminated union：`kind == run_publication | curated_template`。普通条目的 `adoption.available` 只代表来源 Run 有 Contract 可克隆，不代表它能在采用者环境中复现。

当前错误语义：`MARKET.RUN_NOT_SUCCESSFUL`、`MARKET.CONFIRMATION_REQUIRED`、`MARKET.RUN_ALREADY_PUBLISHED`、`MARKET.FORBIDDEN`、`MARKET.IDEMPOTENCY_CONFLICT`、`MARKET.SOURCE_CONTRACT_UNAVAILABLE`、`MARKET.ADOPTION_CONTRACT_INVALID`。

## 3. 采用的私有 Contract

采用在同一个领域数据库事务中创建确定性 ID 的私有 Contract，避免重试产生重复对象。它带有：

```text
parent_contract_id = source contract
derivation_reason = run_publication_adoption
field_sources[0].source = run_publication
field_sources[0].source_publication_id / source_run_id / source_contract_id
field_sources[0].needs_user_confirmation = true
```

若 `project.workdir` 是 `/public/home/{someone}/...` 或 `/home/{someone}/...`，会仅将个人根改为采用者用户名。这个狭窄处理避免新 Contract 直接指向已知的他人个人目录；它不是依赖、代码、数据或环境的可移植性验证。采用者仍须在 Studio 中检查、修改并重新预检。

## 4. 已接入的前端

- `RunEvidencePanel`：仅当 `SUCCEEDED` 且退出码为零时显示发布表单；必须勾选主动分享确认。
- Run read model 返回服务器计算的 `publication.status/reason/publication_id`；前端不再自行决定发布资格。
- `MarketPages`：消费统一 MarketItem union，提供列表、统一详情、采用和发布者撤回。普通卡片不显示验证通过数或成功率；采用后进入私有 Contract Studio。
- `api.ts` / `types.ts` / `query.ts`：已为上述独立 endpoint 建立类型与 TanStack Query 失效规则。

此切片没有自动发布、没有把公开代码或工作目录放进页面、也没有根据模拟器环境改变 UI 行为。

## 5. 下一里程碑边界

M0 的统一市场与浏览器闭环已经完成。真实 SSH backend/session/Evidence transport 属于 M1；ArtifactManifest/RepairTicket 属于 M2；校园身份、多用户 session 与完整 PostgreSQL 生产运行属于 M3。M0 始终只依赖统一 `SlurmBackend`，没有模拟器专用业务分支。

## 6. 本轮验证

```text
pytest market / publication / template / API subset   passed
npm run typecheck                                     passed
vitest API / RunEvidencePanel / market-state           18 passed
npm run build                                         passed
Docker Slurm command-gateway web smoke                passed
pilot-browser Market → Detail → Adopt → Studio        passed
pilot-browser publisher withdraw → Market             passed
```

`test_run_publications.py` 的源 Run 经 `InMemorySlurmBackend` 提交、推进为 `COMPLETED / 0:0` 并 reconcile；之后完成发布、可见性读取、采用、个人工作目录重定向、lineage 与撤回验证。该 backend 只是 SlurmBackend 契约的测试实现。

Docker smoke 则通过 Web BFF 提交到 command-gateway 下的 Slurm 模拟器，并实际验证 `SUCCEEDED → Evidence collection succeeded → publish confirmation → Bob adopts a private Contract`。其中 API 与 Worker 显式配置为相同的 command-gateway endpoint 与认证引用；该配置一致性是 Run 生命周期的一部分，而不是模拟器特例。
