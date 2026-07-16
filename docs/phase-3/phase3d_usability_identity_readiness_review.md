# Phase 3D 新生验收准备度与身份准入审查

> 后续产品决策（2026-07-16）：固定五人和总体 `passed/failed` 可用性门禁已取消，改用 [`phase3d_user_feedback_protocol.md`](phase3d_user_feedback_protocol.md) 收集和处理可获得用户的反馈。因此本文关于 `0/5` 阻塞 Phase 3E 的结论仅记录当时评审状态，不再是当前准入规则；校园多用户生产身份 NO-GO 结论保持不变。

日期：2026-07-16  
范围：新生可用性验收协议与机读门禁、Web/BFF 身份来源、competition 单用户身份、Docker/浏览器负面测试、校园生产身份准入边界。  
结论：本切片的 P0/P1 已清零，机器与部署准备度通过；**Phase 3D 真人可用性门禁仍为 pending（0/5），校园多用户生产身份仍为 NO-GO，因此不能宣称整个 Phase 3D 已验收完成，也不能据此进入 Phase 3E。**

## Findings-first 结果

### 已修复 P1：浏览器可通过用户名请求头选择 BFF 身份

原 Web BFF 将 `X-Pilot107-User` 当作演示用户选择器，这适合 alice/bob 本地产品测试，但若直接沿用到比赛部署，客户端可修改 URL 或请求头切换 owner。现新增显式 `demo` 与 `fixed_user` 两种身份模式。competition profile 使用 `fixed_user`，BFF 忽略客户端同名请求头，只向 API 注入运维配置的单一用户；非法用户名或缺少固定用户时启动失败。

Docker HTTP 负面测试证明：以 Bob 请求头访问 fixed Alice 部署时，`/api/v1/web/session` 仍返回 Alice；Alice Run 列表返回 200，Bob Run 列表返回 403。

### 已修复 P1：固定身份部署的 UI 仍表现为可切换用户

只在代理层固定用户会造成界面和授权事实不一致。Web BFF 现提供同源 `/api/v1/web/session` read model，前端先读取服务端解析的用户与 `switchable` 状态。固定模式会把 `?user=bob` 归一化为 `?user=alice`，禁用选择器且只显示 Alice；demo 模式仍可显式切换 alice/bob，并让 URL、查询缓存和 owner 数据同步变化。

### 已修复 P1：固定模式曾隐式复用 demo 默认用户

固定模式初版从 demo 默认值推导固定用户，配置遗漏可能静默成为 Alice。现要求显式 `PILOT107_WEB_FIXED_USER`，base compose 将该变量透传，competition overlay 明确提供单用户默认值。一次真实容器复核暴露出 base compose 未透传变量、Web 因 fail-closed 退出；补齐透传后重新创建容器并通过健康与冒充测试。

### 已修复 P2：真人验收目标没有可重复、不可冒充自动化的证据契约

新增 `pilot107.novice-acceptance/v1` schema、统一 facilitator 协议、机读 evaluator 和 CLI。通过条件固定为至少 5 名无 Slurm 经验本科生、八项任务全部完成、基本流程未使用终端、首次成功中位时间不超过 600 秒；时长由带时区的 `started_at`/`first_success_at` 派生，不接受自报 duration。`automated` 必须为 false，证据来源必须是 `facilitated_human_study`。

当前仓库只提交诚实的 pending artifact。自动浏览器、API smoke、开发者自测或伪造记录都不能把门禁变成 passed；CLI 对当前记录返回 exit 2、0/5。

### 已修复 P2：验收 Evidence 引用最初未与声明的 Run ID 绑定

机读门禁现在要求 adopter Contract ID、成功 Run ID、失败 Run ID，并验证 Evidence URI 至少绑定成功 Run 的日志与输出、失败 Run 的日志。这样“找到日志、结果和失败原因”不再只是三个无来源布尔值。该约束提供可审计绑定，但 facilitator 身份和真人参与仍属于组织流程信任，不能只靠本地 JSON 完成强认证。

后续完成度审计又要求每位参与者使用唯一 Contract 和唯一成功 Run，且成功/失败 Run 不能相同，避免复制同一执行记录重复计数；预置失败 Run 可以在多场会话间复用。

### 已修复 P1：command-gateway 部署在错误的 UID/文件系统边界执行 workdir preflight

competition API 初版虽然通过网关提交 Slurm，却仍在只读 API 容器内使用 `LocalPathChecker`。API 进程 UID 10700 无法读取 Alice 的 `0700` home，于是把真实可用目录误报为 `WORKDIR_NOT_READABLE/WORKDIR_NOT_EXECUTABLE` 并返回 502。现由 `SimulatorPathChecker` 通过同一个 command gateway、以 Run owner 身份执行受控 `test -e/-d/-r/-x/-w`，API 与 Worker 的提交/重试路径均按目标 Slurm 用户检查。

网关同时补齐 Evidence 环境采集需要的 `pwd/whoami/date/python -V/which python`，并对 `env`、`python`、`test` 等新增命令执行精确 argv 约束，禁止借 `env sh` 或 `python -c` 扩大执行能力。真实 competition success/failure/cancel smoke 随后全部通过。

### 已修复 P1：缺失 stderr 的采集器元数据被诊断为用户程序错误

Slurm 默认可能把 stdout/stderr 合并到 `.out`。此时 `stderr.tail.json` 的 metadata 会包含 `stat: No such file or directory`，原诊断上下文直接匹配整份 JSON，错误触发 `RUNTIME.COMMAND_NOT_FOUND` 和 `USER_SHELL.VARIABLE_MISSING`。诊断构建器现在对 JSON 日志只消费实际 `tail`，不把 collector metadata 当用户症状。真实 exit 42 样本重新诊断后只保留有依据的 `RUNTIME.NONZERO_EXIT`。

### 已修复 P1：协议存在但研究环境没有可执行的失败任务样本

新增只读 `check_phase3d_novice_study_readiness.py`：研究开始前要求 fixed identity、已发布且 publication gate 通过的 Python CPU 模板、command backend 的 FAILED Run、完整日志/结果/accounting Evidence，以及至少一条引用 Evidence 的确定性诊断。当前 competition 环境已生成真实失败 Run `run_e99db9fb4c59482f8c10abd9c036680a`，exit `42:0`、collection/diagnosis succeeded，readiness 报告为 `ready`。该结果只证明环境可发放任务卡，不计入真人 5 人门禁。

## 已完成的契约

- 本地 apps profile 明确使用 `demo`，保留 alice/bob 产品测试能力；
- competition 与 competition app-node profile 使用 `fixed_user`，并要求显式安全用户名；
- fixed mode 不接受 URL、fetch header 或直接 HTTP header 改变身份；
- Web session read model 是 UI 的身份事实源，固定模式不会渲染虚假的切换能力；
- API 继续执行 owner scope，owner query/body 不能覆盖 BFF 注入身份；
- 真人研究协议、匿名记录 schema、pending artifact、报告生成和三态退出码均已落地；
- 生产身份决策明确区分比赛单用户 GO 与校园多用户 NO-GO，不把固定用户冒充 OIDC/RBAC。

## 验证证据

- 全量 Python：467 tests 通过；
- 身份、新手门禁、command gateway、API/Worker wiring 与诊断定向测试：57 tests 通过；
- `uv run ruff check src tests scripts`：通过；
- `uv run mypy src`：57 个源文件通过；
- `npm run typecheck`：通过；`npm test -- --run`：4 files、15 tests 通过；
- `npm run build`：1912 modules，主入口 256.57 kB（gzip 77.06 kB），最大 chunk 438.53 kB（gzip 144.34 kB）；
- `npm audit --omit=dev`：0 vulnerabilities；
- `check-app-images.sh`、`check-sim-core.sh`、`smoke-sim-apps-profile.sh`：通过；最终服务已恢复为默认 demo profile；
- `check-competition.sh`：真实 success `run_a706a559a10641d495a61e8f30057c43`、failure `run_e99db9fb4c59482f8c10abd9c036680a`、cancelled `run_bb78c0eae48142c1812a2e9fed4688ac` 均完成 Evidence 与 valid Capsule；
- live study readiness：`status=ready`、fixed Alice、release `release_777ed428220642d0bf0195a332597168`、failure Run `run_e99db9fb4c59482f8c10abd9c036680a`、0 issues，报告见 `artifacts/usability/phase3d_novice_study_readiness.local.json`；
- live failure diagnosis：重新诊断后只返回 `RUNTIME.NONZERO_EXIT`，已清除缺失 stderr metadata 引起的两条误报；
- fixed Docker HTTP：Bob header → session `fixed_user/alice/switchable=false`，Alice owner 200，Bob owner 403；
- fixed `pilot-browser`：`?user=bob` 自动归一化到 Alice，用户选择器 disabled 且仅有 Alice，页面 errors 为空；
- demo `pilot-browser`：Alice 可切换到 Bob，URL 更新为 `?user=bob`，列表从 3 条 Alice 记录变为 2 条 Bob 记录，页面 errors 为空；
- 当前新手 artifact：`status=pending`、`recorded_participants=0`、`required_participants=5`、CLI exit 2。

项目要求所有实际浏览器操作只通过 `pilot-browser`；本轮固定与 demo 两种 live 验收均遵守该边界。

## 残余风险与阶段门禁

1. 研究技术环境和任务样本已 ready，但尚未组织 5 名合格真人受试者；在有效匿名记录达到 passed 前，Phase 3D 新生目标未完成；
2. `fixed_user` 只适合单用户比赛部署，不是认证协议，也不能安全支持校园多用户；
3. 校园生产仍需 OIDC Authorization Code + PKCE、可信 auth proxy、subject → Slurm 映射、课程目录角色、session/CSRF、审计、短期 Worker credential 和完整负面测试；
4. 新手 JSON 的内容真实性仍依赖 facilitator 流程与审计，后续可由受控研究签名或只写采集服务增强，但不得以自动化替代真人；
5. 在真人门禁 passed 且结果经 review 前，不进入 Phase 3E RemediationSession；若要调整该路线，需要产品负责人显式修改阶段准入标准。
