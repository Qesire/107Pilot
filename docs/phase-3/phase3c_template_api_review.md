# Phase 3C 可信身份适配与模板 API 切片审查

日期：2026-07-16  
范围：服务端角色目录、模板 HTTP/FastAPI API、并发与幂等、可见性、审核队列。  
结论：本切片 P0/P1 已清零；Phase 3C 整体仍在进行中。

## Findings-first 结果

### 已修复 P1：客户端可在无认证模式下冒充课程成员

初版 adopt/withdraw 在 identity 缺失时允许 body 提供 actor，这会把服务端课程目录的授权映射到
客户端自报身份。现 adopt/withdraw 必须具有已解析的 `UserIdentity`；body 中的 actor/role/scope
均为未知字段并被拒绝。review decision 同样始终要求 identity。

### 已修复 P1：采用幂等检查晚于撤回状态检查

原顺序会使已经成功的 adoption 在 release 撤回后重试时返回 `RELEASE_WITHDRAWN`，破坏 request
key 的稳定结果。现同一 `BEGIN IMMEDIATE` 事务先查询 adopter/request key：已完成请求返回原
adoption；新请求才检查可见性、门禁和撤回状态。

### 已修复 P1：课程发布者和 admin 可能无法读取课程 release

课程可见性初版只看成员 scope，未把 publisher 本身纳入；admin 也未获得服务端目录中的全部已知
课程。现 publisher 对自己的任意可见性 release 始终可读，admin 的课程 scope 来自可信目录全部
已知课程，普通成员仍只能访问其课程。

### 已修复 P2：草稿列表静默截断

原 list API 固定返回 `has_more=false`。现按 `updated_at,draft_id` 使用 `limit+1` keyset pagination，
cursor 绑定 owner，跨 owner 或过滤条件复用会失败。

### 已修复 P2：审核流程不可发现

只有 review ID 的 decision API 无法支持实际 reviewer 工作流。现提供 pending queue，并在 SQL 中按
reviewer/admin/course instructor/TA 和 course scope 过滤，同时排除自审；队列使用正向 keyset
cursor。

### 已修复 P2：撤回原因在 release detail 中丢失

release 查询现返回 `withdrawal_actor`、`withdrawal_reason` 和 `withdrawn_at`，满足市场展示与审计。

## API 公共面

- `GET/POST /api/v1/template-drafts`
- `GET/PATCH /api/v1/template-drafts/{draft_id}`
- `POST /api/v1/template-drafts/{draft_id}/validate`
- `POST /api/v1/template-drafts/{draft_id}/reviews`
- `GET /api/v1/template-reviews`
- `POST /api/v1/template-reviews/{review_id}/decision`
- `POST /api/v1/template-drafts/{draft_id}/publish`
- `GET /api/v1/templates/{template_id}/releases/{release_version}`
- `POST .../adopt`
- `POST .../withdraw`

PATCH 与审核决策要求 expected version；publish/adopt 要求 request key。发布 request key 有数据库唯一
索引，release 与 adoption 重试返回同一领域对象。

## 验证证据

- 模板 API、store、service config、ASGI/OpenAPI 定向测试：34 项通过；
- 全量测试：431 项通过；
- `ruff check src tests scripts`：通过；
- `mypy src/pilot107`：55 个源模块通过；
- OpenAPI snapshot 已包含新增模板操作，隐藏兼容 forwarder 不进入公共 schema。

本切片没有新增 Slurm 执行语义，因此未运行 Docker 作业。Docker 验证仍在采用后 canonical
Contract、Run/Evidence verification 链闭合后执行，并在执行前运行模拟器健康门禁。

## 剩余风险与下一动作

1. 服务端静态 role directory 需要替换为学校身份和课程目录适配器；
2. 实现 `/api/v1/templates` 市场搜索、兼容性过滤、验证状态和稳定排序；
3. adoption 生成 adopter owner-scoped canonical Contract，而不只复制 private draft；
4. verification 只接受受控 Run/Evidence 事实并区分 Docker、GPU 与 real107；
5. 完成 publish-to-real-job Docker smoke 后执行 Phase 3C 整体结项 review。
