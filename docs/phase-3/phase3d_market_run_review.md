# Phase 3D Template Market 与 Contract → Run 切片审查

日期：2026-07-16  
范围：Template Market live 查询与筛选、immutable release 详情/diff、采用 lineage、Contract dynamic preflight、Run prepare/对象级确认/submit、Docker 与真实浏览器验收。  
结论：本切片 P0/P1 已清零，可以进入 Run Evidence/结果与失败可解释性切片；**不代表整个 Phase 3D 已完成**。

## 完成的产品契约

- `/market` 使用真实 Template Market read model，搜索、visibility、CPU/GPU、verification 与 environment 筛选全部写入 URL；
- `/templates/:template_id?version=...` 展示 immutable release digest、发布信息、compatibility、canonical payload 和服务端 release diff；
- 采用动作持有并复用 request key，服务器原子创建用户 private draft、canonical Contract 和 adoption lineage，再深链到 Studio；
- withdrawn release 不出现在 Market，可通过历史深链查看，但不能重新采用；
- 已持久化 Contract 才能进入 dynamic preflight → prepare → submit；prepare 只生成具体 Run 和 submitted-script preview；
- submit 前必须勾选包含精确 Run ID 的对象级确认，成功后进入 `/runs/:run_id` live read model；
- Contract 本地有未保存修改时，preflight/prepare 和旧 Prepared Run 的确认/submit 全部锁定，避免视觉状态与服务器对象错配；
- Market、Studio 和 Run 主流程没有使用 mock；UI 回归 mock 仅保留为可复用契约测试数据。

## Findings-first 结果

### 已修复 P1：dev server 重建配置时静默丢弃环境能力

`pilot107.api.dev_server` 原先从环境读取完整 `ApiServiceConfig` 后，又手工构造只包含旧字段的新对象，导致
template reviewers、verification environment、Contract profile、REST auth、Slurm username、LLM 等环境配置在
容器内虽然存在却不生效。真实发布审批因此返回 `TEMPLATE.REVIEW_FORBIDDEN`。现启动入口使用
`dataclasses.replace` 只覆盖 CLI 字段，保留全部环境能力，并新增回归测试固定 reviewer、verification、profile 和
Slurm username。

### 已修复 P1：Docker demo 允许 bob 采用，却不允许 bob workdir 提交

Web demo 明确提供 alice/bob 两个用户，但 API backend 默认 allowed root 只有 `/public/home/alice`。bob 的 adopted
Contract 可以通过 Contract preflight 和 prepare，submit 却返回 `WORKDIR_NOT_ALLOWED`。compose 现显式配置
`/public/home/alice,/public/home/bob`；同一 Prepared Run 重试后提交成功并完成 Evidence 收集。

### 已修复 P1：本地修改后仍可提交旧 Prepared Run

初版在 prepare 后继续编辑 canonical 表单时，旧 Run 的提交确认仍可操作。现 dirty 状态立即清空确认勾选，并禁用
checkbox、preflight、prepare 和 submit，同时明确提示旧 Prepared Run 已锁定。真实浏览器覆盖“先确认、再编辑”路径。

### 已修复 P1：withdrawn 历史深链的版本选择器显示错误 release

Market 按契约排除 withdrawn release，直接打开已撤回的 1.0.0 时初版内容和 URL 为 1.0.0，版本 select 却只剩
1.1.0，diff 语义也难以判断。现把显式请求的历史 version 注入详情页版本集合；回归测试覆盖 withdrawn version
缺失于 Market 结果的情况。最终 1.0.0 内容、select、1.1.0 对比方向和禁用采用状态一致。

### 已修复 P2：模拟 compose 缺少模板治理角色与验证环境

apps profile 现显式提供本地 reviewer 和 Docker verification environment，使真实 HTTP 审批/发布/验证能力与
Phase 3C 后端契约一致。reviewer 仍不是终端用户下拉项，避免把治理身份伪装成普通产品身份。

## 验证证据

- `npm run typecheck`：通过；
- `npm test`：3 files、11 tests 通过，新增 withdrawn direct-version 排序/隔离回归；
- `npm run build`：通过，1910 modules；主入口 239.21 kB（gzip 72.45 kB）、Studio 254.17 kB（gzip 78.68 kB）、编辑器 438.53 kB（gzip 144.34 kB），均低于 500 kB；
- `npm audit --audit-level=high`：0 vulnerabilities；
- UI 回归契约 Node 语法检查：通过；
- 全量 Python：440 tests 通过；首次沙箱运行的 6 项回环 socket 用例受权限限制，按权限流程在沙箱外完整重跑后通过；
- `ruff check src tests scripts`：通过；
- `mypy src tests/test_api_dev_server.py`：57 个源文件通过；
- `smoke-sim-phase3c.sh`：发布、采用、Contract、Run、Capsule、verification 纵向闭环通过，Run `run_ec20540827ef4591b5fb5124d1b9d1cb`；
- `smoke-sim-web-interactions.sh`：Contract `contract_5e80910689b74bbc8fe855f88be69f28`、Run `run_ad4872622b8d48f28ec658cb2a292aec`，最终 SUCCEEDED、20 个 Evidence objects；
- Docker 健康门：API/worker/web healthy，Slurm controller UP，Students 节点 idle；`check-app-images.sh` 通过；
- 真实发布模板：`template_4dc24293ae204d7f913aabaf4585e9a6`，1.0.0/1.1.0 审批发布、title diff 和 1.0.0 withdrawal 均由 live API 完成；
- 真实采用：`adoption_5090f1a8a700c786d7fbd6532c9849b5` → `contract_adopted_5090f1a8a700c786d7fbd6532c9849b5`，Studio 展示 `template_adoption` lineage；
- 真实提交：`run_0a15e25b7c694173adc5d9840ea711aa`，最终 SUCCEEDED、Evidence succeeded、Exit `0:0`；
- 390px：Market/detail/Studio document 均 390/390，Studio tabs 322/322，无横向溢出；桌面与移动截图人工检查通过；
- 最终浏览器控制台和页面错误为空。

项目规定所有实际浏览器操作只通过 `pilot-browser`。本轮未直接启动 Playwright 浏览器；浏览器回归文件通过语法
检查，最终筛选、diff、adoption、dirty lock、preflight、prepare、confirm、submit、withdrawn 和移动端交互均在
最新 Docker 镜像上由 `pilot-browser` 完成。

## 残余风险与下一切片

1. Run 列表已显示 live state/Evidence summary，但 Evidence 日志、结果文件、诊断和 Capsule 详情仍是诚实的“下一切片”；
2. prepare 后放弃的 DRAFT Run 会保留在历史中；需要在后续决定显式取消/清理 UX，而不是静默删除；
3. 模板 authoring/review/publish 尚无产品 UI，本切片只交付消费侧 Market；治理仍通过 API/CLI；
4. Run read model 未公开 workdir，详情页已明确显示“服务器 read model 未公开”，不从 Contract 猜测；
5. 课程目录和生产 OIDC/trusted identity 适配仍未完成；本地 demo reviewer 只用于模拟治理门；
6. 新生 5 人、首次成功作业中位时间不超过 10 分钟的可用性验收尚需真实受试者协调，不能由自动浏览器替代。
