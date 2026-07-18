# Phase 3G 控制面安全与供应链基线审查

日期：2026-07-18
范围：Web→API 身份信任、重放、HTTP 限额/限流、CSRF/CSP、安全响应头、Compose secret 和供应链门禁。
结论：本地单机安全基线的 P0/P1 findings 已清零，真实 stdlib Compose 链路通过；这不是生产身份、
共享限流或已执行的在线镜像漏洞审计，R4-3 仍需 trace/LLM/SSE/PostgreSQL 业务接线。

## 固定契约

- API 配置共享密钥后，不再单独信任客户端 `X-Pilot107-User`；BFF 签名绑定 method、完整 target、
  user、body digest、timestamp 和新 request ID；
- API 使用 constant-time compare、30 秒 freshness 和进程内 replay cache；篡改、过期和同 request
  ID 重放统一返回 `AUTH.PROXY_SIGNATURE_INVALID`，不泄露失败细节；
- secret value 不进入 Compose environment、Git、日志或 manifest；本地生成文件 ignored，API/Web
  以非 root + supplemental host group 只读挂载；
- API/FastAPI/Web/HTTPS proxy 请求和响应均有上限，拒绝 ambiguous Transfer-Encoding；API/Web 有
  进程内 fixed-window rate limit；
- browser write 只接受 JSON、same-origin、无 Cookie；所有 Web 响应有 CSP/frame/nosniff/referrer/
  permissions/opener headers，HSTS 仅在 HTTPS profile 显式打开；
- CI security job 执行 Python/Node dependency audit、secret/config/source scan 和 application image
  HIGH/CRITICAL scan；Trivy Action 使用完整 SHA 固定。

## Findings-first 结果

### 已修复 P0：可信身份头可由直连客户端伪造

原 API 的 `auth_required` 只验证头存在和用户名格式，暴露的 8080 端口允许客户端直接自报任意安全
格式用户名。现在 Compose 双端强制共享密钥文件；live 直连伪造返回 403，经 BFF 同一用户请求返回
200。健康检查保持独立，领域 API 不再把格式校验误当来源认证。

### 已修复 P1：签名 freshness 不能阻止窗口内重放

初始设计只有时间戳。现加入 BFF 生成 request ID，并在 API freshness 窗口内原子登记；同 ID 第二次
使用失败。缓存为进程内，明确不声称多副本全局 replay protection。

### 已修复 P1：四层请求/响应存在无界读取和解析差异

stdlib API、FastAPI `request.body()`、Web BFF 和 HTTPS proxy 原先均可能无界读取，且非法
Content-Length 会直接抛异常。现所有入口有一致限额、非法/负长度错误、stream 累计检查和有界上游
读取；过大响应不截断成看似成功的 JSON。

### 已修复 P1：fixed identity 下 simple cross-site POST 可由 BFF 代签

fixed mode 不需要浏览器自报用户，跨站 form POST 可能由 BFF 注入固定用户。现在 mutating API 强制
JSON、拒绝 Cookie、检查 Fetch Metadata/Origin，OPTIONS 不提供 CORS 放行；cookie-less CLI JSON
仍可在无 Origin 时使用。

### 已修复 P1：Docker Compose secret 对非 root 容器不可读

首次 live 启动证明本地 Compose secret 是保留宿主 `0600` ownership 的 bind mount，UID 10700 的
API/Web 均 fail closed 退出。修复为 host owner + group `0640`，容器仅加入可配置 secret GID；重建
后三个应用服务全部 healthy。

### 已修复 P1：供应链 action 使用 tag 会暴露于上游标签篡改

官方 2026-03 advisory 记录 Trivy 生态 tag 被强制移动。CI 采用当前已签名 v0.36.0 的完整 commit
SHA，不使用 `latest` 或版本 tag；本地 secret scanner 输出位置/类别而不回显匹配凭据。

## 验证证据

- HMAC unit/contract：字段绑定、篡改、过期、重放、双 secret source/弱 secret 拒绝；
- transport contract：ASGI 请求/响应/限流、health bypass、Retry-After、安全头；
- 全量 Python：587 passed、11 PostgreSQL integration skipped、2 subtests passed；
- Web：typecheck、10 files/64 tests、production build 通过；
- Ruff 全树、strict mypy 72 source files、四种 Compose config、candidate secret scan、diff check 通过；
- 本机离线清单：pip-audit dry-run 可收集 60 packages；npm cached audit 报告 0 findings；没有把这两项
  记为当前在线漏洞库结论；
- Docker `security-live`：API/Worker/Web 均 healthy；直连伪造 identity 403，经 BFF GET 200；
  text/plain write 403，合法 JSON 到达领域层并返回预期 422；
- Web 纵向 smoke：Contract `contract_f657b1e8fdd945a8978fd3246ddac777`、Run
  `run_483cc707ed0c476c8362589711488fa1`、demo Job `demo-5e8c0ca07a5b` 最终 SUCCEEDED，20 个
  Evidence objects 收集成功；
- `pilot-browser` 在启用 CSP 的 live Web 加载完整工作台，JS/CSS/session/runs/capabilities 请求成功，
  accessibility tree 可交互且 console/page errors 为空；
- 本机未安装/执行 Trivy image scanner，在线 npm audit 因依赖清单外发审批被拒；CI 门禁已配置但
  本地尚无 run，因此不声称 image CVE=0；
- 未连接真实 107，未上传或部署 VM。

## 残余风险与后续输入

1. demo/fixed identity 不是学校 OIDC，HMAC 只证明请求来自持有共享密钥的 BFF；
2. replay cache 与 rate limiter 为进程内；多 API/Web 副本前需共享存储或可信 L7 实现；
3. `/metrics` 无用户鉴权，base simulator 仍暴露 API 端口；RC 必须内网化或配置监控 allowlist；
4. CSP `style-src` 仍含 `unsafe-inline`；当前无 Cookie session，未来 Cookie auth 必须新增 CSRF token；
5. secret rotation 需要短暂停 API/Web，尚无双密钥无损轮换；宿主 secret GID 需安装器显式探测；
6. online dependency、source/config 和 image scan 只有 CI 配置，必须取得真实通过 run 才能关闭供应链
   证据缺口；
7. LLM/SSE 专项 metrics、持久 request-domain trace 与 PostgreSQL business Store parity 未包含在本切片。
