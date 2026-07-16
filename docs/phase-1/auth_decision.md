# Auth Decision

> 状态：superseded in part by [`phase3d_identity_readiness_decision.md`](../phase-3/phase3d_identity_readiness_decision.md)  
> 当前建议：Docker 主线使用 `docker_root_demo` 和模拟多用户身份；真实 107 的 `single_user_jwt` 降级为 M1-R 非阻塞兼容探测候选。

## 1. Candidate Modes

| 模式 | 适用 | 当前状态 |
|---|---|---|
| `docker_root_demo` | 本地 M0 | 可用于 Docker |
| `single_user_jwt` | 真实 107 M1-R 只读探测或人工 smoke | 资料给出 token 获取方式，仍需实测 |
| `trusted_auth_proxy` | M2 校园生产化 | 待校方支持 |

## 2. Selected Mode

```yaml
selected_identity_mode_for_competition: docker_root_demo_with_simulated_users
selected_identity_mode_for_real_compat: single_user_jwt
confidence: docker_mainline_ready
blocking_unknowns:
  - worker_credential_refresh
```

## 3. Credential Rules

- token 不写源码；
- token 不写普通配置文件；
- token 不写日志；
- token 不写 DB 明文字段；
- token 不进入 Evidence；
- token 不进入 Capsule；
- token 不进入 Agent Context；
- 401 时进入 `AUTH_REQUIRED`，不误判作业失败；
- token 过期不触发重复提交。

## 4. Worker Recovery

待确认策略：

```text
Option A：Worker 可通过 credential_ref 获取短期凭据
Option B：Worker 在 AUTH_REQUIRED 停止自动重试，用户刷新 token 后恢复
```

当前默认：Docker 主线不依赖真实 Worker 凭据；真实 107 探测使用 Option B，直到确认安全的凭据存储。

## 5. Forbidden Design

禁止：

```text
前端提交 username
→ 后端信任 username
→ 后端以该用户身份提交/取消作业
```

除非后端是校方管理的 trusted auth proxy。
