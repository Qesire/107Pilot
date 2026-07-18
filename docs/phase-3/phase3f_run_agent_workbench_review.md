# Phase 3F Run / Agent / Terminal 工作台审查

日期：2026-07-18  
范围：Run keyset 列表、保存筛选、timeline/lineage/compare、对象级 cancel/retry/clone/submit、Agent 队列/diff/预算/接管、原生命令与配置化 terminal deep link。  
结论：本切片 P0/P1 已清零，R3 自动工作包完成，可以进入 R4 本机 Phase 3G 控制面；不代表 PostgreSQL、多实例、恢复或 VM 发布候选已完成。

## 完成的产品契约

- Run 列表使用后端 filter-bound keyset cursor，首屏 20 条，可显式加载后续页；筛选仍写入 URL；
- 每个 demo/fixed identity 用户可在浏览器本地保存、应用和清除一组 Run 状态/搜索筛选，损坏或越界存储值 fail closed；
- Run 详情新增 timeline、lineage nodes/edges、source/derived compare 和 Evidence 数量/终态/结果对比；
- `SUCCEEDED` 可 clone，`FAILED/SUBMIT_FAILED/COLLECTION_FAILED/CANCELLED` 可 retry；准备、提交和取消都要求对象级两次点击；
- Run read model 公开 owner-scoped `workdir`，不公开内部 Evidence store path；
- 原生命令仅由严格 Job ID 和 server-side workdir 生成，使用 shell single-quote，浏览器只复制、不执行；
- 全局 Terminal 不再是占位页：它绑定明确 Run/Job/workdir，展示 Queue/Detail/Accounting/Output tail/Cancel，并拒绝猜测未配置的平台地址；
- `PILOT107_WEB_TERMINAL_DEEP_LINK` 只接受无内嵌凭据的绝对 HTTP(S) URL，未配置时返回 `null`；不实现生产 PTY；
- Agent 队列显示 owner-scoped session，proposal patch 单独显示确定性 diff，派生 Run 可直接进入 source/derived compare；
- Agent 列表、详情、预算、取消、人工接管和终态状态保持与 R1/R2 API 事实一致。

## Findings-first 结果

### 已修复 P1：Agent 列表沿用被 R1 禁止的 `owner` 查询参数

R1 将 RemediationSession 身份完全绑定到可信认证头，并拒绝 `owner` query；前端仍发送 `?owner=alice`，导致详情深链可读但队列返回 `unsupported query parameters: owner`。前端现只发送 `state/cursor`，身份只在可信 header 中；live 队列从错误的 0 条恢复为 2 条，并新增传输契约回归。

### 已修复 P1：自动计划的 Phase 3F 退出条件未被首版实现覆盖

首版新增 timeline/compare/写操作后曾准备直接收口，但独立 review 发现 Run 页面仍只读取首 20 条、没有保存筛选，Terminal 仍显示 Phase 3D 占位文案，Run summary 还遗漏已有的 workdir。现已补齐 keyset 加载、owner-local 保存筛选、安全 Terminal 协同以及 workdir read model；阶段结论以修复后的全量门禁为准。

### 已修复 P2：成功 Run 无 clone 入口

首版只对失败/取消状态显示 retry，成功 Run 无法沿 lineage 克隆。现在终态成功 Run 显示“克隆 Run”，派生 reason 为 `manual_clone`；失败类状态仍使用 `manual_retry`。live 两次点击创建 `run_11825236f8b74affa7cc817a5f352e3b`，保持原 Contract 和 source lineage。

### 已修复 P2：安全命令缺少 workdir 与日志协同

RunRecord 已持久化 workdir，但公共 summary 未返回，命令区只能提供 Job 查询/取消。summary 现返回 owner-scoped workdir，并生成 quoted `tail -n 200 -- <workdir>/slurm-<job>.out`。Job ID 含 shell 语法或 workdir 含控制字符时不生成对应命令。

## 验证证据

- Python：508 tests 通过；
- 前端：10 files、64 tests 通过；
- `uv run ruff check src tests scripts`、`uv run mypy src`、`npm run typecheck`：通过；
- 生产构建：1914 modules；主入口 284.74 kB（gzip 83.71 kB），最大 chunk 438.53 kB（gzip 144.34 kB）；
- `check-sim-core.sh`：Slurm controller UP，Students 节点 idle；
- `check-app-images.sh`、`smoke-sim-apps-profile.sh`：API/Worker/Web 镜像和健康门通过；
- `smoke-sim-web-interactions.sh`：`run_d137893da7d04aa4bada3299d8bd7bd3` 为 SUCCEEDED、collection succeeded、20 个 Evidence objects；
- live clone：source `run_492c3b2ff1074a76b6498eb24a1b53ec` → `run_11825236f8b74affa7cc817a5f352e3b`，状态 VALIDATED，timeline/compare URL 和事实表正确；
- live pagination：20 条首屏显示“加载更多”，点击后显示 21 条；
- live saved filter：保存 FAILED，清空后应用恢复 `?state=FAILED` 和 2 条结果；
- live Terminal：Run/Job/workdir 与 5 条命令一致；未配置 deep link 时明确显示限制；
- `pilot-browser`：Agent 队列 2 条、Run compare、Terminal、390×844 均无页面错误；document `scrollWidth/innerWidth=390/390`。

## 残余风险与下一工作包

1. 保存筛选当前是 owner-keyed browser localStorage，不跨浏览器同步；这是明确的本地偏好，不是服务器资产；
2. Run events 视图固定读取前 100 条，极长时间线需要在后续控制面增加增量 cursor；
3. Terminal deep link 在本地 profile 未配置，只验证了 URL schema、空配置和 UI fail-closed；VM/真实平台地址仍属外部部署配置；
4. 当前 retained live Agent 数据覆盖 blocked/cancelled；审批/执行/评价由 HTTP/worker 回归和此前 live 会话覆盖，R4 故障套件应再生成完整终态样本；
5. PostgreSQL Repository parity、outbox、fencing、多 Worker、metrics/trace、备份恢复和安全基线仍属于 R4；
6. 生产 OIDC、真实 107、VM 上传和生产 PTY仍明确不在本切片范围内。

