# 控制面本地安全基线

日期：2026-07-18

本基线保护 Web BFF 到 API 的身份转发、HTTP 资源边界、浏览器同源写入和发布候选扫描。
它不是学校 OIDC、生产 RBAC、WAF 或多副本限流的替代品。

## 身份转发

浏览器仍只连接 Web BFF。BFF 解析 demo/fixed identity 后，使用共享密钥对以下字段共同执行
HMAC-SHA256：协议版本、时间戳、HTTP method、原始 path/query、用户、请求体 SHA256 和新生成的
request ID。API 在接受 `X-Pilot107-User` 前校验签名、30 秒 freshness 和 request ID 未重放。
方法、query、用户或请求体任一变化都会使签名失效。

`/healthz`、live/ready health 保持公开，`/metrics` 由 transport 直接提供；后两者必须只暴露给
可信本机/监控网络。Compose 应用 profile 为 API 和 Web 同时挂载
`/run/secrets/pilot107-proxy-hmac`，不在环境、日志或 manifest 中保存密钥值。密钥至少 32 bytes。

本地初始化：

```bash
bash scripts/init-local-secrets.sh
```

生成文件不进入 Git，权限为 `0640`。本地 Compose secrets 是保留宿主 ownership 的 bind mount，
因此非 root 容器通过补充组读取；宿主 GID 不是 `1000` 时设置 `PILOT107_SECRET_GID`。轮换时停止
API/Web、替换文件、保持同一 GID/权限，再重建两个服务。不同主机不得复用同一个值。

可选配置：

- `PILOT107_PROXY_HMAC_SECRET_FILE`：首选部署入口；
- `PILOT107_PROXY_HMAC_SECRET`：只用于测试，不能与 file 同时配置；
- `PILOT107_PROXY_SIGNATURE_MAX_AGE_SECONDS`：默认 30；
- `PILOT107_TRUSTED_USER_HEADER`：签名和身份解析使用同一名称。

重放缓存与 HMAC 密钥当前是单进程模型。增加 API 副本前必须迁移到共享 replay store 或在可信
L7 proxy 完成等价校验。

## HTTP 与浏览器边界

API 和 Web 默认请求上限 2 MiB、响应上限 8 MiB；stdlib、FastAPI、Web BFF 和 HTTPS reverse
proxy 均有有界读取。超限请求返回 413，API 过大响应 fail closed，BFF/HTTPS 上游过大返回 502。
chunked/其他 `Transfer-Encoding` 在当前 stdlib 链路明确拒绝，避免不一致解析。

API 默认每来源 IP 每分钟 600 次，Web 默认每来源 IP 每分钟 300 次，返回 429 和 `Retry-After`。
这是进程内 fixed-window 防护，不是生产分布式限流。Web 容器到 API 会共享一个内部来源 IP，
部署前按并发量调整 API 聚合阈值。

Web 的 POST/PATCH 只接受 `application/json`，拒绝 Cookie、cross-site/same-site Fetch Metadata 和
不匹配的 `Origin`。当前产品没有 Cookie session，因此这是 cookie-less、same-origin-only 策略；
未来引入 Cookie 时必须另行实现 CSRF token，而不是删除本门禁。生产设置
`PILOT107_WEB_PUBLIC_ORIGIN`；HTTPS profile 才设置 `PILOT107_WEB_ENABLE_HSTS=true`。

所有 Web 响应包含 CSP、`nosniff`、`DENY` frame policy、no-referrer、Permissions Policy 和
same-origin opener policy。CSP 只允许同源脚本/连接，样式暂保留 `unsafe-inline` 以兼容现有 React
组件；移除该例外是后续加固项。

## 发布候选扫描

本地无外部漏洞库时可执行：

```bash
uv run python scripts/scan-tracked-secrets.py
sh simulator/compose/scripts/check-compose-config.sh
uv run pip-audit --local --dry-run --progress-spinner off
npm audit --offline --omit=dev --audit-level=high
```

其中 `pip-audit --dry-run` 只证明依赖清单可收集，npm offline 只使用本机缓存，两者不能当作当前
在线漏洞结论。CI security job 执行在线 Python/Node audit、候选 secret scan、source/config scan、
镜像构建和 HIGH/CRITICAL image scan。Trivy Action 固定到 v0.36.0 的完整提交 SHA
`ed142fd0673e97e23eac54620cfb913e5ce36c25`；禁止改成可移动 tag，尤其考虑到 2026-03 的
[Trivy ecosystem supply-chain advisory](https://github.com/aquasecurity/trivy/security/advisories/GHSA-69fq-xp46-6x23)。

正式 RC 必须保存 CI run、漏洞数据库时间、镜像 digest 和 scan artifact；本机工具缺失或 offline
0 findings 不能替代这些证据。
