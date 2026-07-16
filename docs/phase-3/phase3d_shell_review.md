# Phase 3D 产品壳与 live read model 首切片审查

日期：2026-07-16  
范围：React/TypeScript 产品壳、信息架构、Run/平台/授权 live read model、Python Web SPA/代理入口、应用镜像与真实浏览器验收。  
结论：本切片 P0/P1 已清零，可以进入 Contract Studio canonical state 切片；**不代表整个 Phase 3D 已完成**。

## 交付边界

本切片完成：

- React 18、TypeScript strict、Vite 生产构建和 TanStack Query server state；
- `/projects`、`/runs`、`/runs/:run_id`、`/cluster` 的 live API 页面；
- `/market`、`/templates/:id`、`/studio/*`、`/agent`、`/terminal` 的显式后续切片路由；
- URL 中的用户、Run 搜索和状态筛选；
- loading、empty、forbidden、error、fresh/stale/degraded 等事实状态；
- 桌面侧栏、390px 移动端底部导航、skip link、语义 heading/region/label；
- Python Web 服务器的 SPA 深链回退、同源 API 代理和 PATCH 转发。

本切片未实现且在 UI 中明确标注：Template Market 交互、Contract Studio 五投影、Run Evidence 详情、Agent 和 Terminal。禁用或后续切片页面不会伪装成可用功能。

## Findings-first 结果

### 已修复 P1：浏览器回归仍绑定旧版单表单 UI

原 `tests/ui/visual.spec.js` 依赖已删除的 DOM ID 和 submit 表单，测试服务器又使用
`python -m http.server`，无法验证产品路由深链及同源代理。现已重写为 Phase 3D 契约，覆盖工作台、URL
筛选、Run 深链、用户隔离、stale/degraded、403、缺失字段与移动端溢出；测试服务器改用真实
`pilot107.web.server`。

### 已修复 P1：应用镜像未安装声明的运行时依赖

全量宿主测试通过后，Docker API 仍因 `ModuleNotFoundError: yaml` 无法启动。根因是 Dockerfile 只复制
源码而未安装 `pyproject.toml` 依赖。现镜像执行 `pip install .`，并携带 known-error YAML；
`check-app-images.sh` 明确导入 PyYAML、API/worker/web 模块并加载 known-error rules，防止再次被宿主环境
掩盖。

### 已修复 P1：390px 页面产生横向越界

真实浏览器初测为 `scrollWidth=396`、`clientWidth=390`。根因包含 sr-only 元素的静态绝对定位和移动端
Run 表格保留过多列。现 sr-only 固定到安全坐标，移动端只保留 Run/状态并允许长 ID 换行；复测页面与
表格均为 390/322px 精确贴合，无横向滚动。

### 已修复 P2：信息架构漏掉 Agent 与 Terminal

规划要求 `/agent` 和 `/terminal`，初版壳层会把直接访问误判为 404。现两者均进入导航和已知路由，
并以清晰的“下一切片”页面声明证据/权限边界。

### 已修复 P2：未实现 Evidence 按钮会产生假交互

初版按钮只把 `tab=evidence` 写入 URL，却仍显示同一详情。现入口禁用并标明“下一切片”，直到真实
Evidence read model 接入。

### 已修复 P2：动态快照 404 被表现成普通错误

首次部署尚未采集平台或授权快照是合法空状态。现在 404 分别显示“尚无平台快照”和“尚无授权快照”，
同时声明不会用静态 capability 推断动态事实或个人授权；其他错误仍保留 alert。

### 已修复 P2：Web 代理上游失败响应可能不是合法 JSON

原实现用 Python `repr()` 拼接 JSON message，单引号及特殊字符可能导致前端解析失败。现统一使用
`json.dumps(..., ensure_ascii=False)` 生成 `WEB.UPSTREAM_UNAVAILABLE` 响应。

### 已修复 P2：前端 Run 类型强制要求后端未公开的 workdir

live API 的 Run read model 不包含 `workdir`，初版详情留下空白。现字段为 optional/null，缺失时明确显示
“服务器 read model 未公开”；mock 回归同时覆盖字段存在时的长路径换行和字段缺失时的诚实降级。

## 验证证据

- `npm run typecheck`：通过；
- `npm run build`：通过，Vite 6.4.3，1628 modules，JS 221.79 kB（gzip 68.08 kB），CSS 16.54 kB（gzip 4.21 kB）；
- `npm audit --audit-level=moderate`：0 vulnerabilities；
- UI 回归与 Playwright config 的 Node 语法检查：通过；
- 全量 Python 测试：440 项通过；
- `ruff check src tests scripts`：通过；
- `mypy src`：56 个源模块通过；
- 模拟器健康门：核心 Compose 服务运行，MariaDB healthy，`sinfo` 正常，`Slurmctld(primary) ... UP`；
- 应用镜像检查：API/worker/web import、PyYAML、Recipe catalog 与 known-error rules 均通过；
- apps profile smoke：API、worker、web 均 healthy，同源 `/api` 代理通过；
- Web interaction smoke：最终 Run `run_8d185d654ee1450d917714c34d612a84` 为 SUCCEEDED、collection succeeded、Evidence 20 objects；
- `pilot-browser` live QA：工作台、直接 Run 深链、URL 筛选、alice→bob 切换、Agent 后续路由、390×844 与 1440×900 均通过；控制台和页面错误为空；
- 移动端最终 `scrollWidth=clientWidth=390`，table `scrollWidth=clientWidth=322`；
- 本机 Docker 路径 Web Vitals：TTFB 0.6ms、FCP/LCP 52ms、CLS 0（单次本地观测，不外推生产）。

项目要求所有实际浏览器操作只通过 `pilot-browser`。因此本轮未直接启动 Playwright 浏览器；其回归文件
作为可复用契约做了语法检查，实际验收由 `pilot-browser` 在最新 Docker 构建产物上完成。

## 残余风险与下一切片

1. Contract Studio canonical state、JSON Schema/Ajv、基础/高级/YAML 往返尚未实现，是下一切片主目标；
2. Market/Template/Agent/Terminal/Evidence 当前只有诚实占位路由，不应计为功能完成；
3. Run API 尚未公开 workdir 和 recipe version，页面只能显示缺省说明；若产品需要，应扩展后端 read model 并做授权审查；
4. trusted-header 用户切换仅适用于本地开发，生产仍需学校身份/课程目录适配；
5. 当前 Dockerfile 每次源码变化都会重新构建 Python wheel并下载依赖，正确但构建缓存效率可在后续工程化切片优化；
6. Web Vitals 来自本机单次样本；生产资源体积、网络和长列表性能仍需独立预算与观测。
