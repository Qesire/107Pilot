# Agent Runtime Reliability Handoff

- 更新时间：2026-09-01（Asia/Shanghai）
- 当前阶段：Phase 1 Task 6 暂停，等待接手
- 产品运行时模型：USTC107 配置中的 `deepseek v4 flash`
- 禁止的模型替换：qwen、Ascend 版本或未获用户确认的 fallback
- 工作区：`/home/knowingthesea/107pilot`

## 1. 接手前必须阅读

1. `docs/superpowers/specs/2026-08-31-agent-runtime-reliability-closure-design.md`
2. `docs/superpowers/plans/2026-08-31-agent-runtime-reliability-roadmap.md`
3. `docs/superpowers/plans/2026-08-31-agent-task-evidence-gate.md`
4. 本文

继续实现前先检查 `AGENTS.md`、`git status --short` 和当前 HEAD。工作区存在大量用户及其他任务的
未提交修改；禁止清理、覆盖、reset 或顺带提交。所有修复继续遵守 RED → 最小实现 → GREEN → 独立复审。

## 2. 已冻结的架构结论

107Pilot 采用：

```text
持久化控制面：Session / Turn / AgentTask / Outbox
                     ↓
瞬态执行面：Run / Slurm allocation / Evidence / Capsule
```

Pi 进程和 Slurm allocation 都不是 Agent 连续性的真源。schedule receipt 仅表示已接受调度；只有
terminal Run、SEALED Evidence、显式 completion policy 所需的 verified Capsule 和 Task finalizer CAS
全部成立，才能唤醒 follow-up Turn。跨数据库、文件系统和外部 Slurm 的链路通过 durable state、receipt、
fence 与 reconciliation 收敛，不宣称不存在的分布式单事务。

## 3. Phase 1 已完成内容

### Task 1 — Domain / wire contract

关键提交：`c2abf03`、`e03882a`、`7076a89`、`70e51b1`、`a958f54`。

- completion policy、gate receipt、Task/Run identity 校验已冻结；
- schedule receipt 明确非终态；
- Capsule policy 分支和 terminal Run 约束已覆盖。

### Task 2 — Task store / migration / CAS

关键提交：`033f1f1`、`dd2efdd`、`f4203cd`、`86b4c8b`、`7a06843`。

- SQLite/PostgreSQL schema、lease/version/fence、stage operation key、causation root；
- 成功 Task 不可通过 `complete_task()` 绕过 gate；
- terminal replay 幂等；schedule 与 gate 使用不同 operation identity。

### Task 3 — Evidence terminal gate

关键提交：`03116a1`、`054e434`、`2215229`、`7dfea10`、`5d2bc7e`、`2d37804`、`8eb547f`。

- 只信持久化 Run provenance，不接受请求或模型 payload 自证；
- Phase 1 使用 immutable workspace snapshot，`workspace_revision=null`、`legacy_boundary=true`；
- canonical source URI、manifest exact set、exit/size/SHA/integrity 校验；
- SQLite/PostgreSQL Evidence metadata immutable guard。

### Task 4 — Finalizer / Evidence seal / exactly-once follow-up

关键提交：`4d09e77`、`de5b012`、`b5a4d48`、`2220b32`、`dcb983e`、`6b67b63`。

- Task terminal CAS 后才产生 durable ready intent；enqueue/clear 崩溃可恢复；
- schedule ack 重放使用稳定 receipt；
- Evidence `OPEN → PREPARING_SEAL → SEALED/INVALID`；
- seal owner、lease、单调 fence，并发 sealer 不可把 SEALED 降级；
- marker 使用 `dir_fd + O_NOFOLLOW`，文件 `0444`、目录 `0555`；
- Run provenance/resource plan immutable trigger；
- fd 异常路径已独立验证无泄漏。

独立 reviewer 最终 PASS。实现阶段曾得到全量 `1651 passed, 27 skipped`；跳过主要来自未配置
PostgreSQL runtime DSN。

### Task 5 — Runtime ordering / Capsule authority and fencing

关键提交：`c5637da`、`e8f217c`、`9abeaeb`、`262b355`。

真实 worker 顺序已改为：

```text
Run terminal
→ Evidence collection
→ Evidence seal / gate
→ optional Capsule durable outbox / build / verify
→ AgentTask finalizer
→ next-tick ready follow-up
```

- Evidence-only 不受 Capsule 阻塞；required policy 必须 verified Capsule；
- Capsule artifact 由 Run + seal 确定，publish-once，不覆盖既有 artifact；
- exact files/dirs/checksum set，拒绝 extra/missing/empty-resigned/partial/tamper；
- 拒绝 symlink、FIFO、hardlink 和路径交换；
- fd-relative snapshot reader，TemplateVerification 不再 verify 后按普通路径重开；
- Run Capsule operation key + build fence；旧 worker 失租后不能 READY/FAILED；
- marker 和当前 SEALED Evidence bytes 在所有 authority 入口重验；
- 两个正式 Capsule smoke 已迁移到实际 `RunStore + EvidenceStore.root + run_id`。

独立 reviewer 最终 PASS。最终定点结果包括 `76 passed, 11 subtests`；实现 clean 广集曾得到
`303 passed, 3 skipped`。

## 4. 当前未完成：Phase 1 Task 6

Task 6 只启动了浏览器测试准备，随后按用户要求暂停；没有 Task 6 代码提交，也不能声称 UI 已通过。

接手时必须使用真实前端入口和直接 `agent-browser`，不能用保留后端接口代替。先从浏览器观察和记录 RED，
复现功能问题后才能读代码诊断。前端视觉重设计不在本次范围，只修阻断 Agent 闭环的功能问题。

### 场景 A：不懂编程的本科生

从 UI 用中文请求一个小型真实科学计算，例如 Monte Carlo 估算 π 并报告误差。逐步验证：

- 平台资源事实来自被视为真实 Slurm 的 VM 模拟集群；
- Agent 使用 `deepseek v4 flash`，中文自然语言持续流式输出，不再只返回 `\n\n` 后 timeout；
- 工具事件默认折叠；
- 待审批内容同时有自然语言说明和结构化字段；
- 作业编辑展示 sbatch 正文而非 JSON；
- 审批后走真实 AgentTask → Run → 模拟 Slurm → SEALED Evidence → follow-up；
- 刷新后仍能恢复同一 Session/Turn/Run。

### 场景 B：研究生实验

从 UI 请求参数扫描并输出表格、图或统计摘要，使用 Evidence+Capsule policy。验证：

- 同一 Run、一个数值 Slurm Job ID、无重复 submit；
- Capsule 为 verified，绑定 seal/manifest/object-set；
- Evidence、结果摘要和可下载/可查看引用一致；
- Worker/API 重启或浏览器刷新后仍恰好一个 follow-up。

### Task 6 已知前置

- 先在用户的 opencode/107 配置中找到 `deepseek v4 flash` 的实际 profile ID；不得自造 ID。
- 使用 `agent-browser skills get core --full` 与 `agent-browser skills get dogfood --full`，保留截图、视频和增量报告。
- 文件路径输入/搜索、模板分享和整体前端视觉重设计只记录现状，除非直接阻断上述闭环，不在 Task 6 扩张实现。

## 5. 已知环境/配置阻塞

1. A5 sandbox：`bwrap: loopback: Failed to create NETLINK_ROUTE socket: Operation not permitted`。
   不得通过禁用 sandbox 规避；需要修部署权限或提供等价安全 fallback。
2. Capsule smoke：模拟 Slurm 缺少 `Students` partition，提交前即失败。资源策略应读取真实模拟集群事实，
   不应继续硬编码该 partition。
3. Phase3C smoke：adopt API 返回 `409 MARKET.AGENT_APPLICATION_REQUIRED`。必须从公开 UI 判断这是否是
   合理的申请/权限流程，不能由 smoke 直接绕过。
4. PostgreSQL：无 `PILOT107_TEST_POSTGRES_DSN`，当前只有 migration/trigger 静态验证；live PG 测试仍需补。
5. 最新代码尚未部署到 `114.214.241.31:8443`；当前要求先固定本地网页完成验收，再考虑远端部署。

## 6. Phase 1 之后的工作

- Phase 2：通用 ToolInvocation durable ledger、exactly-once、副作用恢复与孤儿调用对账。
- Phase 3：live workspace revision、单写者 journal、手动路径输入、搜索和 stale revision gate。
- Phase 4：上下文压缩、64-step yield/continuation、长任务续 Turn；工具预算是软分段边界，不是小预算即失败。

不得把 Phase 1 的 AgentTask/Capsule fencing 误称为所有 Agent 工具都已事务化，也不得把 immutable snapshot
误称为 live workspace revision。

## 7. 下一位模型的第一组动作

1. 只读检查 `git status --short`、HEAD 和本文列出的提交，不清理 dirty worktree。
2. 从 opencode 配置解析 `deepseek v4 flash` 的真实 profile ID，并更新 107Pilot profile mapping；先写失败测试。
3. 复用或启动本地固定网页，按场景 A 做一次纯浏览器 RED，立即保存 report、console、network 和截图。
4. 仅修第一个阻断闭环的功能问题；完成单元/集成验证后从 UI 重放。
5. 再执行场景 B 和 Task 6 VM/live smoke。
6. 只有公开 UI、live Slurm causality 和 completion gate 全部 GREEN，才勾选 Phase 1。
