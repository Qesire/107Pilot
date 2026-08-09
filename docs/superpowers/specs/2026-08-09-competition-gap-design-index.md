# 107Pilot 本轮比赛缺口设计索引

- 日期：2026-08-09
- 状态：持续更新
- 目的：统一保存本轮已确认设计、范围决策和后续缺口顺序，避免设计只存在于对话中

## 1. 本轮范围决策

- 课程实验指导与课程批改暂不补产品闭环。
- 四类领域模板内容暂不补最小纵向闭环。
- 初学者通用平台问答暂不补最小纵向闭环。
- 上述三项当前只要求纳入 Agent 能力边界，不作为独立产品功能实现。
- 作业代码错误属于重要场景，Agent 必须能提出并在审批后执行受控代码修复。
- 远程 VM 当前不可用，所有设计必须能够先在本地 Docker Slurm 模拟环境验证。
- 尚未合并的文件传输与在线文件系统前端改动已经先行整理并提交到本地 `main`。

## 2. 已固化设计

### 2.1 受控作业修复 Agent

- 文档：[2026-08-09-agent-repair-closed-loop-design.md](2026-08-09-agent-repair-closed-loop-design.md)
- 状态：设计已确认，尚未进入实现计划。
- 核心：Evidence-bound 诊断、结构化计划、用户输入、审批、隔离代码补丁、派生 Run 和结果评价。

### 2.2 文件发现、可靠传输与集群连接

- 文档：[2026-08-09-file-discovery-transfer-design.md](2026-08-09-file-discovery-transfer-design.md)
- 状态：设计已确认，等待文档审阅。
- 核心：有界搜索、异步归档、Range 下载、tusd、5GB+ 大文件、SSH/SFTP 双通道、ClusterAsset、提交依赖门禁和本地同构模拟。

## 3. 已完成的代码整理前置

以下本地提交保存了进入本轮设计前已经存在但未整理的实现：

- `6cac1a3 feat(files): integrate resumable transfer backend`
- `92363ab feat(web): integrate online filesystem and workbench UI`

验证记录：相关 Python 测试 150 通过，ruff、mypy、TypeScript typecheck、144 个 Vitest 和生产构建通过。远程 VM 与真实集群 live E2E 尚未执行，不应据此宣称真实环境已经验收。

## 4. 下一缺口选择规则

完成本文件传输规格审阅后，再从项目其余比赛缺口中选择一个独立子项目进行设计。选择时按以下优先级：

1. 能形成可演示纵向闭环，而不是只增加静态页面。
2. 能复用现有 Run、Evidence、Dashboard、模板和文件能力。
3. 可在本地模拟器中提供客观验收证据。
4. 不进入本轮明确暂缓的课程批改、领域模板内容和通用问答。
5. 每个缺口单独形成规格文档，逐节确认后再写实现计划。

下一缺口尚未在本索引中预先指定，避免未经完整项目复核就锁定方向。
