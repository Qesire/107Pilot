# 03. 成功作业市场与分享

> 实现状态：本章的统一 `/market/items` read model 已实现；`/market` 与 `/templates` 保留为兼容 endpoint。精确契约见 [07-m0-implemented-contract.md](07-m0-implemented-contract.md)。

## 1. 产品语义

市场条目的承诺只有一句：

> 发布者曾经成功运行过这个作业，并主动选择把这份作业信息分享出来。

它不承诺代码公开、数据可得、路径可访问、依赖可安装，或其他用户必定成功。因此市场不应以“验证通过数、成功率、可信等级”作为普通用户作业的准入条件。

对普通条目，唯一运行事实是来源 Run：

```text
state = SUCCEEDED
exit_code = 0:*
```

## 2. 两条市场供给路径

```text
普通用户成功 Run ──→ RunPublication ──┐
                                       ├─→ GET /market/items → 同一 Web 市场
官方/课程 TemplateRelease ─────────────┘
```

| 属性 | `RunPublication` | `TemplateRelease` |
|---|---|---|
| 主要目的 | 分享一次成功作业 | 维护可治理的官方/课程模板 |
| 发布门槛 | 成功 Run + 本人勾选 | 草稿、publication gate、审核、发布 |
| 可移植性 | 发布者自行说明 | 可由维护者声明 compatibility |
| 验证 | 不强制 Capsule/二次 verification | 保留现有 strict verification |
| 市场卡片 | 成功作业 | curated template |

不要删除旧模板工作流；将它从“所有分享的唯一入口”收缩为 curated 内容路径。

## 3. 发布 API

### 3.1 创建

```http
POST /api/v1/runs/{run_id}/publications
Content-Type: application/json

{
  "request_key": "web-publish-...",
  "confirm_share": true,
  "visibility": "campus",
  "scope_key": null,
  "title": "A100 PyTorch smoke training",
  "description": "使用私有课程仓库的训练入口。",
  "tags": ["pytorch", "a100", "course"],
  "reproduction_note": "代码和数据需向作者申请；请根据自己的目录修改 workdir。",
  "share_options": {
    "contract_summary": true,
    "script": false,
    "resource_summary": true,
    "result_summary": true,
    "evidence_previews": false
  }
}
```

响应 `201`：

```json
{
  "publication_id": "publication_...",
  "source_run_id": "run_...",
  "state": "published",
  "published_at": "..."
}
```

错误规范：

| code | 条件 |
|---|---|
| `MARKET.RUN_NOT_FOUND` | 来源 Run 不存在 |
| `MARKET.RUN_FORBIDDEN` | 请求者不是 owner |
| `MARKET.RUN_NOT_SUCCEEDED` | Run 未成功或 exit code 非零 |
| `MARKET.CONFIRMATION_REQUIRED` | `confirm_share` 非真 |
| `MARKET.PUBLICATION_EXISTS` | 来源 Run 已有 active publication |
| `MARKET.VISIBILITY_INVALID` | visibility/scope 不符合基本格式 |

这些是业务和权限校验；不增加代码扫描、依赖安装、文件可访问性或复现实验校验。

### 3.2 列表与详情

```http
GET /api/v1/market/items?kind=run_publication&visibility=campus&q=pytorch
GET /api/v1/market/items/{item_id}
POST /api/v1/market/items/{item_id}/adopt
POST /api/v1/market/items/{item_id}/withdraw
```

统一列表 payload：

```json
{
  "items": [{
    "item_id": "publication_...",
    "kind": "run_publication",
    "title": "...",
    "description": "...",
    "visibility": "campus",
    "tags": ["pytorch"],
    "published_at": "...",
    "source": {
      "run_id": "run_...",
      "completed_at": "...",
      "resource_summary": {"partition": "Students", "qos": "..."}
    },
    "adoption": {"available": true, "reason": null}
  }],
  "next_cursor": null
}
```

`kind=curated_template` 映射现有 release；`kind` 为空时返回双方。统一 API 不泄漏内部表名或远端绝对路径。

## 4. 前端页面与交互

### 4.1 市场列表 `/market`

市场列表从“审核 release”调整为“成功作业与 curated 模板”并列：

- 顶部说明：`成功作业由发布者自行说明其代码、数据和可复现性。`；
- 筛选：关键词、可见性、标签、类型；
- `RunPublication` 卡片显示完成时间、资源摘要、发布者说明和“来自成功 Run”；
- `TemplateRelease` 卡片保留版本、课程/官方标记和严格验证信息；
- 不对普通条目显示“验证通过”“成功率”“保证可复现”等容易误导的指标。

### 4.2 Run 详情 `/runs/:run_id`

当 Run 成功且当前用户是 owner 时，显示“发布到市场”按钮。按钮只在：

```text
run state = SUCCEEDED
AND exit code = 0:*
AND no active publication
```

点击后打开确认 sheet，而非立即发布。sheet 包含：

1. 标题、描述、标签和复现说明；
2. 可见性；
3. 将公开字段预览；
4. 显式确认 checkbox；
5. `发布成功作业` 按钮。

该页面不要求用户选择“作业类型”。长代码、私有脚本、多阶段执行脚本都是同一个 Run。

### 4.3 条目详情 `/market/:item_id`

详情固定为三个事实区域：

```text
发布者说明 | 成功运行摘要 | 可采用的 Contract 摘要
```

未公开脚本、日志、证据、代码和数据以“发布者未共享”呈现，不能用空白或 404 暗示系统故障。

采用动作创建用户私有 Contract 后跳转 Studio；若 Contract 不可采用，展示来源说明并允许用户复制公开摘要作为参考。

## 5. 与现有前后端的改动边界

### 后端

- 新增 `RunPublicationStore/Service`，复用 `RunStore`、identity、pagination、visibility 校验和 control trace；
- 在 `Pilot107HttpApi` 增加 publication 和 unified market routes；
- 现有 `TemplateMarketStore`、`TemplateVerificationService` 和 `/api/v1/templates` 保持兼容；
- 为 unified market 提供 read service，不在 HTTP handler 中手写跨表 SQL；
- publication 创建、withdraw、adopt 均使用 request key 与并发保护。

### 前端

- `api.ts` 增加 `createRunPublication`、`listMarketItems`、`getMarketItem`、`adoptMarketItem`、`withdrawMarketItem`；
- `types.ts` 增加 discriminated union `MarketItem`；
- `MarketPages.tsx` 从只消费 `TemplateMarketItem` 迁移为消费 `MarketItem`；
- `RunEvidencePanel` 或 Run overview 增加 owner-only publication action；
- TanStack Query mutation 成功后失效 `runs`, `market-items`, `market-item`, `run-publication`；
- 保留旧 `/templates/:id` 深链，新增 `/market/:item_id`，避免一次性破坏已有共享链接。

## 6. 测试

后端至少覆盖：

- 成功 Run 可发布；失败、取消、非零退出码不可发布；
- owner 与 visibility/scope 边界；
- request key 幂等、双请求并发、withdraw；
- 默认 read model 不含 workdir、store path、未选择分享的 script/evidence；
- adoption 创建正确 lineage 的 private Contract；
- 模拟 Slurm 成功 Run 与未来真实 SSH 成功 Run 走相同发布服务测试。

前端至少覆盖：

- 成功 Run 才显示发布入口；
- checkbox 未勾选时不能提交；
- 私有脚本条目的诚实空状态；
- market union 的筛选、详情、采用和 query invalidation；
- 既有 curated template Adopt → Studio 路径不回归。
