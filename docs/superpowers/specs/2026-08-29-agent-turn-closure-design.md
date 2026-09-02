# Agent Turn 闭环增量设计

## 目标

让 107Pilot 的 Agent 对用户表现为普通的简易 Agent：自然语言持续可见，工具只是受控执行细节；底层仍由短时 Pi Turn 和持久 Session/Task/receipt 实现时分复用。

## 范围

本轮只修 Agent 闭环与契约，不重做前端布局。工具详情默认折叠、独立流式窗口和更强“所见即所得”留到后续前端设计；本轮最后只用固定挂载的本地网页验证现有界面的真实入口。

## 设计

1. `builder_build_submit` 必须携带 `approval_summary_zh`。服务端将它与 ChangeSet、Sandbox 结果和验证 Task 一起写入持久 receipt，使自然语言说明与待审批结构化对象共享同一提交边界。
2. `workspace_patch` 同样必须携带 `approval_summary_zh`，并在工具结果中与 ChangeSet 同步返回。它服务于修复、市场应用和模板发布入口；后续前端可直接消费该字段，不必从自由文本反推。
3. Sandbox 失败 receipt 返回下一次提交所需的权威 `expected_project_version`、`expected_workspace_snapshot_digest` 和 `base_change_set_id`，并明确修改内容时必须使用新 `request_key`。
4. 允许同一 Session/Project/Workspace 在新 Turn 中继续最新 `sandbox_failed` 提交；基线 ChangeSet、版本和 Workspace 摘要仍必须精确匹配。Turn 是租约和并发边界，不是工作流所有权边界。
5. 已解析 invocation 后发生的网关错误使用完整 ToolResult 错误信封返回，即使 HTTP 状态为非 2xx；Agentd 只接受严格校验且 invocation ID 匹配的错误信封，其他响应仍统一拒绝并隐藏正文。
6. 工具权限按入口最小化并在中文系统提示词中明示：
   - `interactive`：无工具；
   - `interactive_readonly` / `platform_coach`：只读平台、Run、日志和 Evidence，且按绑定裁剪；
   - phase-aware `experiment_builder`：仅 Builder context/submit 两个门面；
   - `run_diagnosis_repair`、`market_application`、`template_publication`：Project/Workspace 读取、补丁、diff 与 Sandbox 验证；不含 Blueprint 保存和 Slurm 验证调度。
7. 所有系统提示、恢复提示和结构化结果工具文案改为中文。Agent 应输出简短自然语言进度/结论；工具内容保持结构化，不要求在正文重复大段代码或 JSON。
8. 轮数控制以提示词和门面设计为主。Pi step 上限提升到 64、Capability 工具调用上限提升到 128，仅用于阻断异常循环；不得把正常的多轮修复当作小预算失败。

## 错误与安全边界

- 不放宽 owner、Session、Project、Workspace、fencing token、digest、版本、deadline、资源信封或审批校验。
- HTTP 错误正文只有在满足闭合 ToolResult schema 且 invocation ID 精确匹配时才进入模型上下文。
- `approval_summary_zh` 是说明，不授予发布、正式 Run、市场采用或模板发布权限。
- 不增加 shell、SSH、网络或通用调度工具。

## 验收

- 跨 Turn repair 使用 receipt 给出的权威字段可继续并最终 schedule。
- request key 冲突和 no-progress 错误可被 Agent 读取，而不是退化成统一 gateway rejected。
- Builder/Workspace patch 缺少中文审批说明时 schema/服务均拒绝；成功时工具结果与 receipt 同步返回说明。
- Python capability、Python/TypeScript protocol pairing 与 Agentd 实际注册的工具集合一致。
- 所有系统/恢复提示为中文且明确入口权限。
- 21 个连续修复步骤不会因旧的 20-step 阈值失败，异常循环仍受 64/128 与 Turn timeout 保护。

