# Phase 1 REST 专项收敛报告

> 状态：complete
> 日期：2026-07-13
> 上一批次：2026-07-12 第五十七批
> 前置计划：`docs/phase-1/rest_convergence_plan.md`

> 2026-07-15 补充：本报告记录的是 Ubuntu Slurm 23.11 fallback 阶段的
> REST 收敛。当前默认 Docker simulator 已切换到 source-built
> `pilot107/slurm-sim:25.11-real107`，REST API 默认 `v0.0.41`，JWT auth
> probe 已在 Slurm `25.11.2` target image 上验证为 supported。

## 1. 收敛目标与路径回顾

REST 专项收敛的目标是让 `RestNativeSlurmBackend` 适配层在 simulator 上 live 跑通 submit / cancel / read，并作为 env-gated 可选后端接入 competition profile，同时固化 real107 只读 REST smoke 与 OpenAPI digest 自动刷新。

收敛路径选定 **A — 全力打通 simulator REST live**。关键决策是 **源码构建 `auth_jwt.so` 而非 SchedMD apt 仓库预装**，原因如下：

- SchedMD 官方 apt 仓库不直接提供（只有源码 tarball），`download.slurm.sh` 在当前网络下 NXDOMAIN；
- Ubuntu 自带 `slurm-wlm-basic-plugins` 包不含 `auth_jwt.so`，仅含 `auth_munge.so` / `auth_none.so` / `auth_slurm.so` 与 `rest_auth_jwt.so`；
- `rest_auth/local` 仅支持 UNIX socket，不适用应用节点 → Docker 宿主机的 TCP 网络拓扑，廉价路径放弃；
- 因此直接 `apt-get source slurm-wlm` 拉取 Ubuntu 源码包，`--with-jwt` + `libjwt-dev` 编译出 `auth_jwt.so`，仅 COPY 单个 `.so` 到最终镜像，避免整包替换带来的 ABI 风险与镜像膨胀。

## 2. 完成项（按 lane）

### Lane 1 — Slurm simulator JWT auth

- 从 Ubuntu `slurm-wlm` 源码构建 `auth_jwt.so`（`apt-get source` + `--with-jwt` + `libjwt-dev`），仅编译 `src/plugins/auth/jwt`，单个 `.so` COPY 到 `/usr/lib/x86_64-linux-gnu/slurm-wlm/auth_jwt.so`。
- `slurm.conf` + `slurmdbd.conf` 增 `AuthAltTypes=auth/jwt` + `AuthAltParameters=jwt_key=/etc/slurm/jwt_hs256.key`；munge 保持主认证。
- `jwt_hs256.key`（32 随机字节，`slurm:slurm` 0400）烤入镜像，弃用 bind-mount（解决 host/container UID 不一致导致 key 不可读）。
- slurmrestd 保持 `-a rest_auth/jwt` + `0.0.0.0:6820`，新增 `SLURM_JWT=daemon` 环境变量。
- 清理 `compose.yml`（5 处）与 `compose.competition.yml`（1 处）遗留的 `jwt_hs256.key` bind-mount。
- Live 验证：`scontrol token lifespan=3600` 签发 JWT；`curl GET /slurm/v0.0.40/nodes` 带 `X-SLURM-USER-NAME` + `X-SLURM-USER-TOKEN` 返回 HTTP 200（2 节点：anode16、anode17，Slurm 23.11.4）。

### Lane 2 — REST 适配器契约测试

- 新增 `tests/test_rest_native_backend.py`：27 个契约测试，覆盖 6 矩阵 + 3 submit smoke + read / cancel / auth / 语义分类。
- 混合策略：in-process `ScriptedTransport` 跑矩阵；real-socket `FakeSlurmRestServer` 跑 `SLURM_HEADERS` 线缆级断言。
- 确认 `RestNativeSlurmBackend`、`UrllibHttpTransport`、`RestAuthStyle.SLURM_HEADERS`、`check_slurm_rest_semantics` 已正确，**无需硬化**。
- 确认 `SLURM_HEADERS` 同时发送 `X-SLURM-USER-NAME` + `X-SLURM-USER-TOKEN`，无 `Authorization: Bearer`。
- 暴露三项 gap 转交 Lane 3 / 4b：适配器不做 idempotency_key 去重；不强制 WorkDirPreflight；cancel 不预检终态。
- 对既有 lint 做机械 ruff 修正（无逻辑变更）。

### Lane 3 — Token mint + probe 重做 + live 矩阵

- 新增 `src/pilot107/adapters/rest_token.py`：`SimulatorRestTokenProvider`（`scontrol token` 签发、按用户内存缓存、60s 刷新阈值、`threading.Lock` 线程安全、token 不入日志）、`StaticTokenProvider`、`RestTokenProvider` Protocol、`_parse_slurm_jwt`。
- 新增 `tests/test_rest_token.py`：16 个测试（parse、缓存、过期、按用户、失败、并发、token 不入 repr）。
- 新增 `scripts/_sim_rest_helpers.py`：`detect_sim_rest_url()`（基于 `docker port`）、`mint_sim_token()`、`DEFAULT_API_VERSION=v0.0.40`。
- 重做 `scripts/probe_sim_rest_auth.py`：真实 `scontrol token`、v0.0.40、no_token(401) + real_token(200)，报 supported。
- 解 skip `scripts/probe_sim_rest_submit.py`：通过 `RestNativeSlurmBackend` 走真实 submit / get / cancel。
- 新增 `scripts/smoke_sim_rest_live.sh` + `.py`：11 场景 live 矩阵（read jobs/nodes/partitions/accounting、submit shared、get by id、cancel-terminal、invalid workdir、unwritable output、idempotency-no-dedupe）。
- 适配器 v0.0.41 默认保留；simulator 路径显式传 v0.0.40。
- Live 确认：适配器不对 idempotency_key 去重（同 key 两次提交 → 两个不同 job_id）。
- Accounting 端点确认为 `/slurmdb/v0.0.40/jobs`（`/slurm/v0.0.40/accounting` 返回 404）。

### Lane 4a — real107 smoke + digest 刷新

- 新增 `scripts/probe_real107_rest_readonly.py` + `.sh`：对真实 107 的只读 GET probe，无 token 时安全跳过（非阻塞），计算 openapi_digest，token 不入输出。
- `src/pilot107/core/platform.py` 新增 4 个函数：`compute_openapi_digest`、`refresh_openapi_digest`、`refresh_configuration_snapshot_digest`、`refresh_rest_capability_digest`；token 不入 digest / 错误。
- 新增 `tests/test_openapi_digest.py`：13 个测试。

### Lane 4b-i — WorkDirPreflight

- 新增 `src/pilot107/core/preflight.py`（约 640 行）：`PathChecker` Protocol、`LocalPathChecker`、`preflight_workdir_paths`（纯函数）、`preflight_workdir_fs`（注入 FS），返回 `list[PreflightFinding]`，使用 `WORKDIR_*` 代码。
- 新增 `tests/test_preflight.py`：26 个测试。`/tmp` → BLOCK、local-only → BLOCK、path-escape → BLOCK。

### Lane 4b-ii — 服务接入

- 新增 `src/pilot107/adapters/rest_token_backend.py`：`TokenMintingRestBackend` 包装器（每次 submit / get_job / cancel 前按用户 mint token，set 到内层 backend，不入 receipt / 日志）、`find_jobs_by_marker` 用于对账。
- 新增 `src/pilot107/core/submission_reconcile.py`：`reconcile_submission` → `ReconcileResult`（bound / not_found / uncertain），通过 marker + 时间窗口查询。
- `src/pilot107/core/run_service.py`：WorkDirPreflight 接入（在 `backend.submit` 之前，与 resource-plan findings 聚合，BLOCK → `WorkDirPreflightError`）；idempotency 对账（超时 → reconcile → bound / not_found / uncertain）；新增错误类 `WorkDirPreflightError` / `SubmissionUncertainError`。
- `src/pilot107/api/service.py` + `src/pilot107/worker/service.py`：新增配置标志 `rest_token_provider_enabled`、`workdir_preflight_enabled`、`idempotency_reconcile_enabled`；REST 接入 token provider 包装。
- `simulator/compose/.env.example` + `.env.competition` + `.env.competition.example`：`PILOT107_REST_AUTH_STYLE=slurm_headers`、`PILOT107_SLURM_API_VERSION=v0.0.40`、`PILOT107_REST_TOKEN_PROVIDER=1`。
- 新增 `tests/test_service_rest_wiring.py`（11 个测试）、`tests/test_submission_reconcile.py`（5 个测试）。

## 3. 验证结果

收尾验证全部通过（orchestrator 验证门确认）：

| 验证门 | 结果 | 备注 |
|---|---|---|
| `pytest` 全量 | **269 passed in 6.79s** | 收敛前 167，新增 102 |
| `mypy src/pilot107` | **Success, no issues, 32 source files** | 收敛前 24-25 |
| `ruff check src/pilot107 tests` | **all checks passed** | `scripts/` 既有 42 处 ruff baseline 不在本批次范围 |
| `bash scripts/check-competition.sh` | competition web smoke ok | success/failure/cancelled + capsules |
| `bash scripts/probe-sim-rest-auth.sh` | status = **supported**（原 blocked） | 真实 `scontrol token` + v0.0.40 |
| `bash scripts/probe-sim-rest-submit.sh` | status = **submitted**（原 skipped） | 真实 submit / get / cancel |
| `bash scripts/smoke_sim_rest_live.sh` | **11/11 scenarios pass** | 含 read / submit / cancel / idempotency |

`scripts/` 既有 ruff 42 errors 为预存 smoke-script lint baseline，**不属于本批次范围**，已在收尾时与 orchestrator 验证门明确区分。

## 4. 关键技术决策

- **API 版本 v0.0.40 而非 v0.0.41**：simulator 镜像为 Slurm 23.11.4，最大 REST API 版本为 `v0.0.40`；`v0.0.41` 属 Slurm 24.05+，在 simulator 上访问 `v0.0.41` 路径返回 404。适配器默认保留 v0.0.41（面向真实 107 Slurm 25.11.2），simulator 路径显式传 v0.0.40。
- **`SLURM_HEADERS` 而非 Bearer**：Slurm 23.11 REST JWT 客户端必须用 `X-SLURM-USER-NAME` + `X-SLURM-USER-TOKEN` 头（即 `RestAuthStyle.SLURM_HEADERS`），`Authorization: Bearer` 不可用。真实 107 仍以 Bearer 形态保留。
- **`auth_jwt.so` 源码构建**：SchedMD 无 apt 仓库、`download.slurm.sh` NXDOMAIN，直接 `apt-get source slurm-wlm` + `--with-jwt` + `libjwt-dev`，仅 COPY 单个 `.so`，规避整包替换。
- **`jwt_hs256.key` 烤入镜像**：host/container UID 不一致使 bind-mount 的 key 在容器内不可读，改为镜像内 baked-in（32 随机字节，`slurm:slurm` 0400），并清理 compose 中所有遗留 bind-mount。
- **bad token 返回 500 而非 401**：Slurm 23.11 对无效 / 损坏 token 返回 HTTP 500（`error_number 7000`），只有完全不带 token 才返回 401。语义分类已据此校准。
- **Accounting 走 `/slurmdb/` 而非 `/slurm/`**：accounting 端点为 `/slurmdb/v0.0.40/jobs`，`/slurm/v0.0.40/accounting` 在 simulator 上 404。

## 5. 已知限制与后续

1. **唯一 job name marker 未实现**：当前 marker 硬编码为 `"pilot107-run"`（adapter 在 `_job_payload` 内拥有）。per-run 唯一 marker 需 `SubmitIntent.job_name` 字段或适配器改造，暂缓；同一时间窗口内同用户两次并发提交 → 对账不确定。
2. **Command-gateway FS checker 待补**：`LocalPathChecker` 用于所有需 FS 检查的后端；`command-gateway` 下 workdir 在容器内部，gateway-backed `PathChecker` 暂缓。
3. **Live REST smoke 仍走适配器直连**：`smoke_sim_rest_live.sh` 直连适配器测试；通过 wired `rest-native` 服务后端（经 api / worker 容器）的端到端 live 验证仅以 fake 单测覆盖，未 live 跑（需 compose up 携带 rest-native env）。
4. **对账重试策略简陋**：当前对 `not_found` 仅重试 submit 一次；可配置 max-retry + backoff 为后续。
5. **真实 107 submit / cancel / file read 未 probe**：仍保持 M1-R 非阻塞，目前仅 read-only REST smoke 脚本就绪。
6. **LLM campus provider 未实测**：仍只在 smoke 中 skipped，未用真实 key 校验。

## 6. 主线边界

- **command backend 仍是默认**：`RestNativeSlurmBackend` 仅作为 env-gated 可选后端接入 competition profile，竞赛演示不依赖 REST live。
- **REST 接入受 env 控制**：通过 `PILOT107_REST_AUTH_STYLE`、`PILOT107_SLURM_API_VERSION`、`PILOT107_REST_TOKEN_PROVIDER`、`rest_token_provider_enabled`、`workdir_preflight_enabled`、`idempotency_reconcile_enabled` 控制，未设置时落回 command 主线。
- **竞赛演示无回归**：`check-competition.sh` 仍走 success/failure/cancelled + capsules 主线，REST 收敛未引入主线 flakiness。

## 7. 下一步

```text
REST 专项收敛（本批次完成）
→ M1 HTTPS/reverse proxy 与两机部署脚本
→ 前端设计包接入前的后端交互回归固化
```
