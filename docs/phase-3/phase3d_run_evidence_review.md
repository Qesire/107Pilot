# Phase 3D Run Evidence、Diagnosis 与 Raw Capsule 切片审查

日期：2026-07-16  
范围：Run Evidence read model、bounded object preview、日志/结果/诊断/Capsule 工作台、URL 深链、对象级授权、Docker 成功/失败/取消路径与真实浏览器验收。  
结论：本切片 P0/P1 已清零，可以进入新生可用性验收与生产身份残余风险决策；**不代表整个 Phase 3D 或 Phase 3F Run 工作台已完成**。

## 完成的产品契约

- `/runs/:run_id?tab=...&object=...` 提供摘要、日志、结果、诊断、Raw Capsule 和对象六个可分享视图；
- Evidence read model 返回 owner-scoped tasks、objects 和 tree，但不公开服务器 `store_path`；
- `/runs/:run_id/evidence/objects/:object_id` 只读取已登记对象，校验 run root confinement 与 store binding，文本预览固定上限为 128 KiB；
- 完整预览执行 sha256 校验；截断预览明确标为 `not_checked`；二进制对象只显示元数据与不可预览原因；
- 日志默认选择 stdout，并从采集器 JSON 中展示 bounded tail；选择对象后 object ID 写入 URL；
- 结果摘要只信任 `derived/result_summary.v1.json`，输出文件通过独立 Evidence object 读取；
- 诊断只展示确定性规则、Evidence refs、置信度和建议 patch；patch 不会自动执行；
- Raw Capsule 只复制 Evidence manifest 已登记对象，GET 返回 manifest、digest、文件数和 checksum 验证结果，不返回服务器目录；
- collection 未完成、空对象、预览不可用、权限拒绝、诊断为空、Capsule 未构建/失败/ready 均有明确状态；
- Run 详情从窄侧栏扩为可滚动工作台，390 px 下 tab 可横向滚动且 document 无横向溢出。

## Findings-first 结果

### 已修复 P1：Evidence API 暴露服务器绝对 store path

原 Evidence object payload 直接包含内部 `store_path`，浏览器不需要该字段，并会泄露容器目录结构。公共 tree/object
payload 现只提供 logical path、source URI、digest、size、MIME 和 collection metadata。对象内容接口重新从登记记录派生
安全路径，并要求登记的 store binding 与派生路径完全一致；跨用户读取返回 403。

### 已修复 P1：成功 Run 被误诊为 workdir 不共享

`SLURM.WORKDIR_NOT_SHARED` 初版症状包含裸 `/tmp` 和通用 `workdirpreflight`。成功 Run 的环境元数据包含 tmpdir，且
runtime probe 明确写有“shared path status is established by WorkDirPreflight, not this runtime probe”，两者都被子串
匹配器误当成错误。规则现只保留明确失败代码/语句，并新增与真实环境 Evidence 同形的回归。live Run 重新诊断后从错误
结论变为 `skipped`、0 条诊断。

### 已修复 P1：apps profile 的 Capsule root 落在只读镜像中

基础 compose 未设置 `PILOT107_CAPSULE_ROOT`，服务默认写入 `/opt/pilot107/data/phase0/capsules`，在只读容器中构建
导致连接提前关闭并把 Run 标为 failed。compose 现把 Capsule 固化到 `/var/lib/pilot107/capsules` 的持久卷；同一失败
Run 状态可安全重试，最终生成 ready Capsule。

### 已修复 P1：Capsule 文件系统异常越过 HTTP 错误边界

`RawCapsuleService` 原先记录 failed 后原样抛出 `OSError`，HTTP 只处理 `CapsuleError`，因此客户端收到 upstream
connection closed。服务现保留已知 Capsule 错误，对其他底层异常返回脱敏 `raw Capsule build failed`，同时维持
`capsule.failed` 状态；新增“capsule root 是普通文件”的回归，防止异常栈再次逃逸。

### 已修复 P2：Capsule POST 响应被 UI 误显示为校验失败

构建响应只承诺 capsule ID、digest 和 copied count，不包含 GET 才计算的 `valid`。初版把 POST payload 直接写入查询
缓存，`valid=undefined` 被画成失败。构建成功后现失效并重取 GET read model，只有服务端 checksum 验证结果会驱动
“通过/失败”视觉状态。

### 已修复 P2：对象选择在 tab 间继承不兼容状态

Evidence 选择逻辑现为纯函数：日志视图确定性默认 stdout，结果视图只接受 output 对象，overview/diagnosis/capsule 不携带
旧 object。4 项前端单测固定默认选择、跨 tab 清理、日志 tail 提取和 byte size 显示。

## 验证证据

- 全量 Python：447 tests 通过；回环 socket 用例按沙箱权限流程完整运行；
- `npm test -- --run`：4 files、15 tests 通过；
- `npm run typecheck`、`uv run ruff check src tests scripts`、`uv run mypy src`：全部通过；
- `npm run build`：1912 modules；主入口 255.74 kB（gzip 76.83 kB），最大 chunk 438.53 kB（gzip 144.34 kB）；
- `npm audit --omit=dev`：0 vulnerabilities；
- `check-app-images.sh` 与 `smoke-sim-apps-profile.sh`：最新 API/worker/web 镜像和健康门通过；
- live Web Run `run_0a15e25b7c694173adc5d9840ea711aa`：owner bob、SUCCEEDED、Exit `0:0`、collection succeeded、20 个 Evidence objects；
- live object preview：stdout 555 B、digest verified、未截断；alice 跨用户访问同一对象返回 403；
- live diagnosis：重新运行后 `skipped`、0 items，已清除 `SLURM.WORKDIR_NOT_SHARED` 误报；
- live Raw Capsule `capsule_b4bc6a0ad8064eadbfb88d219c1e31a7`：19 copied、22 checked、`valid=true`、无 warnings/errors；
- 真实 Docker Slurm transition smoke：失败 Run `run_f53db31272a04444a8eef1046140c663` 为 FAILED/`42:0` 且 collection succeeded；取消 Run `run_c4a3bdd024bd4d859724ce2531a55791` 为 CANCELLED 且 collection succeeded；
- `pilot-browser` live 验收：日志默认 stdout、结果 object 深链、Capsule checksum、空 Evidence、跨用户 403 均通过；页面 errors 为空；
- 390 px：document `scrollWidth/clientWidth=390/390`，无横向溢出，移动端截图人工检查通过。

项目规定所有实际浏览器操作只通过 `pilot-browser`。本轮没有直接启动 Playwright 浏览器；`tests/ui/visual.spec.js`
已更新成功/失败/空/Capsule mock 契约并通过 Node 语法检查，live Web 验收与真实 Slurm 失败 smoke 的证据来源在上面分别列出。

## 残余风险与下一切片

1. Evidence object/tree 与 Capsule manifest 当前一次性返回；单 Run 极大对象数量需要服务端分页和响应体预算；
2. 文本预览固定 128 KiB，尚无 range/tail cursor、实时流或大日志搜索；当前状态是明确 bounded preview，不冒充完整日志系统；
3. 已知错误库仍使用文本子串/regex 匹配，虽然本轮清除 workdir 误报，长期应优先消费结构化 finding code 与 terminal facts；
4. live Web apps profile 的 demo backend 只生成成功 Run；失败/取消由同一模拟 Slurm 的独立真实 transition smoke 验证，尚未形成同一持久 Web 卷的失败浏览器记录；
5. Phase 3F 完整 Run 工作台仍缺 timeline/DAG、raw-normalized Slurm 对照、retry/clone/compare/cancel 操作和保存筛选器；
6. 新生 5 人、首次成功作业中位时间不超过 10 分钟的可用性验收仍需真实受试者，不能由自动浏览器代替；
7. 生产 OIDC、课程目录、PostgreSQL、多 API/Worker lease 和 secret scanning 仍是后续生产控制面工作。
