# 107Pilot Web

Phase 3D 的 Web 源码位于 `apps/web/src`，使用 React 18、TypeScript strict、
TanStack Query 和 Vite。Python Web 入口仍负责提供构建产物及同源 `/api/*`
代理；它不是第二套前端实现。

当前可独立审查切片包括：

- 产品壳层、桌面侧栏和移动端底部导航；
- `/projects` 的近期 Run、平台快照和个人 Slurm 授权；
- `/runs` 的 URL 搜索/状态筛选及 `/runs/:run_id` 深链详情；
- `/cluster` 的 capability、动态事实 freshness 与授权边界；
- `/studio/new` 与 `/studio/:contract_id` 的 canonical Contract 五投影、Ajv/CodeMirror、服务端 validate/create、digest 与脚本 diff；
- `/market`、`/templates/:id`、`/studio/*`、`/agent`、`/terminal` 的显式后续切片路由；
- loading、empty、forbidden、error、fresh/stale 等语义状态。

Market、Template detail 和 Run Evidence 详情仍明确标注为后续
切片，不提供伪交互。

## 本地开发

```bash
npm ci
npm run typecheck
npm run dev
```

Vite 开发服务器会把 `/api` 代理到 `http://127.0.0.1:8070`。当前用户由 URL
中的 `?user=alice` / `?user=bob` 控制，并以 `X-Pilot107-User` 转发；这只适用于
本地 trusted-header 开发模式。

## 构建与部署入口

```bash
npm run build
PYTHONPATH=src python3 -m pilot107.web.server \
  --host 127.0.0.1 \
  --port 3000 \
  --api-base-url http://127.0.0.1:8070
```

Vite 固定输出到 `src/pilot107/web/static`，生成 `assets/app.js` 和
`assets/styles.css`。Python 服务器对产品路由回退到 SPA entrypoint，但不存在的
带扩展名资产仍返回 404，越界路径返回 403。

## 校验

```bash
npm run typecheck
npm test
npm run build
PYTHONPATH=src uv run --extra dev pytest -q tests/test_web_server.py
```

`tests/ui/visual.spec.js` 是 Phase 3D 的浏览器回归契约；项目中的实际浏览器操作
必须通过 `pilot-browser` 执行。
