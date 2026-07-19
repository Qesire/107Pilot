# Studio 协同编辑重构设计

- 日期: 2026-07-19
- 范围: Contract Studio 重构为三栏布局（表单 + 编辑框 + Agent 协同面板），修复 adopt→Studio hydration bug，模板可自定义提示，全局字号放大

## 问题

1. **bug**: adopt 模板后 Studio 看不到内容（后端 GET contract 200 含完整 payload，前端 hydration 失败）
2. **设计**: Studio 是 5 个分散 tab，用户要"一个可编辑框 + 表单 + agent 面板"三合一
3. **模板占位无提示**: `echo ok` 等占位值没有"可自定义"标记
4. **字号太小**: 当前 CSS 字号难以辨认

## 方案

### 三栏布局

```
┌─────────────────────────────────────────────────┐
│ Studio toolbar: Recipe | Contract ID | Digest   │
│ [服务端校验] [创建 Contract] [准备 Run]          │
├──────────┬──────────────────┬───────────────────┤
│ 表单栏    │ 编辑框（源码）    │ Agent 协同面板    │
│ 基础字段  │ YAML/JSON        │ 用户描述需求      │
│ 资源     │ 实时同步         │ LLM 返回 patch    │
│          │                  │ [应用建议][拒绝]   │
├──────────┴──────────────────┴───────────────────┤
│ 校验结果 + 脚本预览（可折叠）                     │
└─────────────────────────────────────────────────┘
```

### 新增后端 API

```
POST /api/v1/contracts/agent/suggest
Request: {
  current_contract: JsonObject,
  recipe_version_id: string,
  user_intent: string,
  provider: "local" | "none"
}
Response: {
  suggested_patch: Record<string, any>,
  explanation_zh: string,
  needs_user_confirmation: true
}
```

复用 `OpenAICompatibleLLMProvider`，构造 contract-editing prompt。

### 模板可自定义提示

占位值（`echo ok`、空字符串、`python3 main.py` 等默认值）在表单字段显示灰色 placeholder + "🔧 可自定义"标签。

### 字号放大

全局 base font-size 从当前值增大（如 14px → 16px），确保表单、编辑框、按钮文字清晰可读。

## 改动文件

| 文件 | 改动 |
|---|---|
| `src/pilot107/core/agent.py` | 新增 `suggest_contract_patch` 函数 |
| `src/pilot107/api/http_app.py` | 新增 `_contract_agent_suggest` handler + 路由 |
| `apps/web/src/api.ts` | 新增 `suggestContractPatch` 请求函数 |
| `apps/web/src/StudioPage.tsx` | 重构三栏布局 + 修复 hydration |
| `apps/web/src/AgentCoeditPanel.tsx`（新） | agent 协同面板组件 |
| `apps/web/src/styles.css` 或等效 | 字号放大 |

## 不变

- Contract 创建/校验/preflight/prepare/submit 链路
- 三层脚本预览
- RunLaunchPanel
- LLM provider 配置
