# Phase 3D Contract Studio canonical state 切片审查

日期：2026-07-16  
范围：Contract Studio 五投影、canonical state、Ajv/CodeMirror、服务端 validate/create、脚本 diff、终端协同、Docker 与真实浏览器验收。  
结论：本切片 P0/P1 已清零，可以进入 Market adoption → Contract preflight/submit 切片；**不代表整个 Phase 3D 已完成**。

## 完成的产品契约

- `/studio/new` 与 `/studio/:contract_id` 使用同一 canonical Contract object；
- 基础模式覆盖任务、workdir、command、常用资源和输出；
- 高级模式覆盖 conda/container/modules/environment、array、workflow retry/dependencies、policy 和 extensions；
- JSON/YAML 源码模式使用 CodeMirror 6，接入后端 JSON Schema 字段 completion、解析行位和 Ajv diagnostics；
- 源码草稿只有显式“应用源码”才替换 canonical state，表单更新不会静默覆盖脏源码；
- 脚本模式固定展示 Recipe version、Contract digest、entry→materialized diff、resolved script 和 server findings/risk；
- 终端协同展示基于 `contract.json` 的等价安全命令和 Contract ID，不依赖浏览器 session；
- 创建动作必须先通过客户端 schema 和服务端 validation，服务器仍是最终权威；
- 创建后的 immutable Contract 可直接深链，现有 Contract 可“另存为新 Contract”。

## Findings-first 结果

### 已修复 P1：Studio 静态导入使所有工作台用户下载 911 kB JS

Ajv、YAML 和 CodeMirror 初版被静态打入主 bundle，工作台 JS 从约 222 kB 增至 911 kB。现 Studio 按路由
lazy load，源码编辑器再按 tab 二次 lazy load。最终主入口 224.59 kB（gzip 69.09 kB），Studio 基础块
253.28 kB（gzip 78.84 kB），编辑器块 438.53 kB（gzip 144.34 kB），构建无大块警告。

### 已修复 P1：URL user 可污染可复制 shell 命令

`user` 来自 URL，若直接拼入终端协同命令会形成命令注入风险。产品壳现只接受 demo 身份 `alice`/`bob`，
其他 URL 值在渲染和 API 请求前归一化为 `alice` 并替换 URL；trusted-header 命令继续明确限定本地开发。

### 已修复 P1：创建后加载持久化 Contract 清空同 digest validation

创建成功会跳到 `/studio/:contract_id?tab=script`，初版 hydration 无条件 reset mutation，导致刚得到的
materialized script 和 diff 立即消失。现仅在 validation digest 与持久化 Contract digest 不一致时清空；
最终 Docker 流程创建后直接展示同 digest script/diff。

### 已修复 P1：镜像依赖安装触发无用 PEP 517 build isolation

`pip install .` 在无 setuptools/wheel 的 slim 基础镜像中每次源码变化都联网下载构建工具，而且应用实际通过
`PYTHONPATH` 运行源码。现 Dockerfile 用 Python `tomllib` 从唯一权威 `pyproject.toml` 读取 runtime
dependencies，在复制源码前安装并形成稳定缓存层；同时复制 known-errors 和 packaged recipes。镜像检查新增
PyYAML、known-error rules 和至少 3 个 Recipe 的断言，最终 UI 实际显示 4 个 Recipe。

### 已修复 P2：源码模式只有右侧错误，没有编辑器 completion/行位诊断

现 CodeMirror autocomplete 从服务端 schema 递归生成字段建议；JSON/YAML parser error 和 Ajv error 进入
lint range。真实浏览器中 `Ctrl+Space` 可见 schema 字段/路径列表，非法 JSON 产生 `.cm-lintRange-error`。

### 已修复 P2：表单和源码可能相互静默覆盖

源码编辑期间记录 dirty 状态；切回表单并修改 canonical 时只标记 conflict，不覆盖源码。用户必须明确选择
“应用源码并覆盖表单”或“放弃源码修改”。该行为已由 live browser 和 UI 回归契约覆盖。

### 已修复 P2：旧 Recipe 固定版本可能不在 latest 下拉中

Recipe summary API 只返回 latest。打开绑定旧版本的 Contract 时，现会注入“当前固定版本”选项，不会显示
空 select 或把旧版本静默替换为 latest。

### 已修复 P2：脚本 diff 对大输入使用平方级内存

小脚本使用 LCS 生成稳定行 diff；乘积超过 200,000 cells 时降级为 bounded remove/add diff，避免浏览器因
大型 sbatch 卡死。500×500 行回归覆盖降级路径。

### 已修复 P2：终端示例包含误入字符串的 `+` 前缀

多行 curl 的 continuation 初版带 patch 符号，复制后不可执行。真实浏览器复核现为合法反斜杠续行命令。

## 验证证据

- `npm run typecheck`：通过；
- `npm test`：2 files、9 tests 通过，覆盖未知 extensions、array、modules、conda、workflow、raw sbatch、JSON/YAML 往返、diff 上限、completion 与 diagnostics；
- `npm run build`：通过，1908 modules，主入口/Studio/编辑器三块均低于 500 kB；
- npm 每次安装审计：142 packages，0 vulnerabilities；
- UI 回归与浏览器 config 的 Node 语法检查：通过；
- 全量 Python：440 项通过；
- `ruff check src tests scripts`：通过；
- `mypy src`：56 个源模块通过；
- 模拟 Docker 健康门：应用/Slurm 服务 healthy，`sinfo` 正常，节点 idle，`Slurmctld(primary) ... UP`；
- `check-app-images.sh`：PyYAML、API/worker/web imports、Recipe catalog、known-error rules 通过；
- apps profile smoke：API、worker、web healthy，同源代理通过；
- `pilot-browser` 最终镜像验收：validate → create → deep-link → digest/script diff 通过，创建 Contract 为 `contract_852f11d9b14140d9b2ba13b4bd2b113b`；
- live Contract digest：`33203f3f71c539cb907bb8b8f4108df2ed3fe82b415d75590f3700002c762ffc`；
- 390px：document 390/390、Studio tabs 322/322，无横向溢出；控制台和页面错误为空；
- Studio 本机 Docker Web Vitals：TTFB 0.7ms、FCP 32ms、LCP 56ms、CLS 0（单次本地观测）。

项目规定所有实际浏览器操作只通过 `pilot-browser`。本轮未直接启动 Playwright 浏览器；浏览器回归文件作为
可复用契约通过语法检查，最终真实交互在最新 Docker 镜像上由 `pilot-browser` 完成。

## 残余风险与下一切片

1. wrapper/original submission artifacts 只有 Run prepare 后才存在；Studio 目前明确说明该边界，不伪造 wrapper；
2. 直接刷新已保存 Contract 的 script tab 需要再次点击服务端校验，避免把旧 materialization 当作持久事实；
3. CodeMirror 编辑器块虽已二次 lazy load，首次进入源码模式仍需下载约 144 kB gzip；生产需 CDN/cache 和真实网络预算；
4. Market release/diff/adoption 到 Studio 的 live 入口，以及 Contract preflight/Run submit 尚未接入，是下一切片；
5. 新生 5 人可用性验收尚未执行，需要真实受试者协调，不能由自动浏览器替代；
6. trusted-header 仍只适用于本地开发，生产身份/课程目录适配不在本切片内。
