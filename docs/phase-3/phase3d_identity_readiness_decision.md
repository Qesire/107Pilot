# Phase 3D 身份准入决策

日期：2026-07-16  
状态：accepted for Docker/competition scope；production NO-GO  
决策对象：Web → API 用户身份、比赛单用户部署、真实 107 兼容探测与校园生产身份。

## 决策

身份能力按部署范围显式分层，不允许从本地演示行为推导生产能力：

| 范围 | Web identity mode | 身份来源 | 准入结论 |
|---|---|---|---|
| 本地 apps profile | `demo` | 浏览器 alice/bob 选择器 | 仅开发与演示 |
| competition profile | `fixed_user` | 运维显式配置的单一 `PILOT107_WEB_FIXED_USER` | 可用于单用户比赛部署 |
| 真实 107 只读/人工 smoke | single-user JWT、人工刷新 | 当前操作者 | 仅兼容探测 |
| 校园多用户生产 | 学校 OIDC/SSO + trusted auth proxy | 已验证 OIDC subject | **NO-GO，尚未实现** |

`fixed_user` 模式要求显式设置 `PILOT107_WEB_FIXED_USER`，并忽略客户端发送的 `X-Pilot107-User`。因此攻击者即使修改 URL、fetch header 或 HTTPS 请求，也不能
切换 BFF 注入 API 的用户。competition 的 API/Web 直连端口继续绑定 loopback，外部入口只暴露 TLS reverse proxy。

此模式只证明“单用户固定身份不接受客户端冒充”，不提供多用户认证，不应称为 OIDC、RBAC 或校园生产身份。

## 已落实的保护

- `WebIdentityMode.DEMO` 与 `FIXED_USER` 是显式配置，未知值启动失败；
- fixed user 必须通过安全 username 校验；
- competition 单机 overlay 与 app-node compose 都强制 `fixed_user`；
- local apps profile 显式保留 `demo`，避免无意改变开发双用户测试；
- API 继续 `auth_required=true`，owner query/body 不能覆盖 BFF 注入身份；
- 跨 owner Run/Contract/Evidence/Diagnosis/Capsule 读取保持 403；
- token 不进入浏览器、普通日志、DB 明文字段、Evidence、Capsule 或 Agent context。

## 生产 NO-GO 条件

以下条件全部完成前，不允许把系统部署为校园多用户生产服务：

1. 学校 OIDC/SSO 使用 Authorization Code + PKCE，验证 issuer、audience、nonce、state、签名和时钟偏差；
2. auth proxy 必须删除外部同名身份头，再由已验证 session 注入内部 header；
3. subject → Slurm username 映射由校方目录或受控映射库提供，不能使用前端 username；
4. course member/TA/instructor/admin 与模板治理角色有明确目录来源和 deny-by-default 规则；
5. session 具备 HttpOnly/Secure/SameSite cookie、CSRF、防重放、空闲/绝对超时、注销和撤销；
6. API/Web 只接受来自 auth proxy 的内部网络流量，并验证 proxy trust boundary；
7. Worker 使用短期 credential reference 或在 `AUTH_REQUIRED` 停止，token 过期不能触发重复提交；
8. 身份切换、授权拒绝、提交、取消、审批和凭据刷新产生可归档审计事件；
9. 完成跨用户、header spoof、session fixation、CSRF、过期/撤销和目录角色负面测试；
10. 安全负责人确认 threat model、密钥托管、轮换和事故响应。

## 被拒绝的替代方案

- 浏览器提交 username，API 直接信任：客户端可冒充，拒绝；
- reverse proxy 原样转发外部 `X-Pilot107-User`：没有认证含义，拒绝；
- competition 使用 demo 多用户并称为生产 RBAC：能力声明不实，拒绝；
- 在 Worker/DB 中长期保存真实 107 token：扩大泄露与恢复风险，拒绝；
- OIDC 不可用时静默降级到 demo identity：会把认证故障变成越权，拒绝。

## 验证要求

- 单元测试固定 demo 接受安全测试用户、fixed user 忽略伪造 header、非法 fixed user 启动失败；
- competition compose 的最终解析配置必须显示 `PILOT107_WEB_IDENTITY_MODE=fixed_user`；
- 最新镜像中 `check-app-images.sh` 验证身份枚举和环境解析；
- Docker HTTP 负面测试必须证明：fixed user 下发送 bob header，API 仍只看到配置的 alice；
- Phase 3G 实现 OIDC 后必须新建生产身份 review，本决策不能自动升级为 production GO。

本决策取代 [`auth_decision.md`](../phase-1/auth_decision.md) 中把 competition demo 多用户视为最终身份模式的部分；
真实 107 single-user JWT 的 Option B（凭据失效进入 `AUTH_REQUIRED`）继续有效。
