# Phase 1 REST 专项收敛计划

> 状态：planned
> 日期：2026-07-13
> 上一批次：2026-07-12 第五十七批
> 收敛路径：A — 全力打通 simulator REST live
> 前置：`RestNativeSlurmBackend` 适配层已实现；simulator REST auth 因镜像缺 `auth_jwt.so` 阻塞

## 1. 背景与阻塞

`RestNativeSlurmBackend`（submit/get_job/cancel）、`UrllibHttpTransport`、`RestAuthStyle`（bearer/slurm_headers）、`check_slurm_rest_semantics` 均已实现。当前阻塞是基础镜像能力问题，不是适配层问题：

```text
slurmrestd -a rest_auth/jwt 0.0.0.0:6820
镜像有 rest_auth_jwt.so（REST 侧），缺 auth_jwt.so（控制面侧）
→ scontrol token 不可用
→ no-token / Bearer dev / Slurm header dev 均 401
→ probe_sim_rest_submit.py 自动 skipped
→ 比赛主线退回 CommandSubmitBackend / DockerSimulatorCommandBackend
```

可用 auth 插件：`auth_munge.so`、`auth_none.so`、`auth_slurm.so`、`rest_auth_jwt.so`、`rest_auth_local.so`（无 `auth_jwt.so`）。

## 2. 路径选择

| 路径 | 内容 | 风险 |
|---|---|---|
| A 全力打通 simulator REST live | 构建/替换含 `auth_jwt.so` 的镜像，跑通 live 矩阵 | 高（镜像构建、libjwt ABI） |
| B 契约级收敛 + real107 只读 | fake REST server 跑矩阵，real107 只读 smoke | 低 |
| C 时间盒混合 | 廉价 auth 尝试，不通落 B | 中 |

**选定：A**。目标让 simulator REST live submit/cancel/read 全通，已实现的 `RestNativeSlurmBackend` 不浪费。Lane 1 第一动作是调研，以降低构建风险；若镜像构建超预算，再评估是否降级。

## 3. 目标

- simulator REST live submit / cancel / read 全通；
- 覆盖 `docker_mainline_plan.md` §5 的 6 矩阵场景与 `submission_strategy.md` §3 的 9 smoke（6 只读 + 3 提交）；
- `RestNativeSlurmBackend` 作为 env-gated 可选后端接入 competition profile（默认仍 command backend）；
- real107 只读 REST smoke 脚本化固化；
- OpenAPI digest 自动刷新任务接入；
- 边界文档明确 REST-native 与 command-backend 主线边界。

## 4. 任务拆分

### Lane 1 — Slurm 镜像 JWT auth 打通（关键路径）

> 调研结论（lib-1，2026-07-13）已纳入：
> - `rest_auth/local` 仅支持 UNIX socket，**不适用** TCP 网络拓扑（应用节点 → Docker 宿主机），廉价路径放弃；
> - `auth_jwt.so` 在 SchedMD 官方 apt 仓库包中**预装**，当前镜像用的是 Ubuntu 自带 `slurm-wlm-basic-plugins`（不含该插件）；
> - Slurm 23.11 REST JWT 客户端必须用 `X-SLURM-USER-NAME` + `X-SLURM-USER-TOKEN` 头（即 `RestAuthStyle.SLURM_HEADERS`），**不是** `Authorization: Bearer`；
> - token 必须由 `scontrol token lifespan=3600` 签发，运行时需 mint + 刷新。

- 1.1 ~~调研~~（已完成，结论见上）；
- 1.2 spike：**优先尝试切换 apt 源到 SchedMD 官方仓库**安装含 `auth_jwt.so` 的包（`slurm-slurmrestd` / 插件包），避免源码构建；若 apt 路径不通再回退源码构建（`--with-jwt` + `libjwt-dev`）；
- 1.3 镜像配置：
  - `slurm.conf` + `slurmdbd.conf` 增 `AuthAltTypes=auth/jwt` + `AuthAltParameters=jwt_key=<StateSaveLocation>/jwt_hs256.key`；
  - 生成 `jwt_hs256.key`（`dd if=/dev/urandom bs=32 count=1`，`slurm:slurm` 0400）；
  - slurmrestd 启动增 `SLURM_JWT=daemon` 环境变量；
  - compose 保持 `-a rest_auth/jwt` + TCP `0.0.0.0:6820`；
- 1.4 验证：`bash scripts/probe-sim-rest-auth.sh` 报 supported；容器内 `scontrol token lifespan=3600` 返回 JWT；手动 `GET /slurm/v0.0.41/nodes` 带 `X-SLURM-USER-NAME`/`X-SLURM-USER-TOKEN` 返回 200。

门：auth matrix supported + `scontrol token` 可用 + 一个 REST GET 成功。不通则评估降级路径 B。

### Lane 2 — REST 适配器契约收敛（与 Lane 1 并行，写域不冲突）

- fake slurmrestd test server，覆盖 6 矩阵 + 3 submit smoke（契约级）：
  - 合法共享 workdir 成功；
  - 不存在 workdir 结构化失败；
  - 无权限 workdir 结构化失败；
  - 本地 `/tmp` 输出警告或拒绝；
  - submit 超时但实际成功 → 对账不重复提交（idempotency）；
  - REST 不可用 → 切换模拟 command backend；
- 加固 `check_slurm_rest_semantics` 错误分类、`RestAuthStyle` 切换、token 处理。

门：新增 pytest 模块 pass + `mypy src/pilot107` strict + `ruff`。

### Lane 3 — Live REST 矩阵 + token mint（Lane 1 通过后）

- **token mint/refresh**（新代码点）：Worker/API 在 REST 调用前通过 command executor 运行 `scontrol token lifespan=3600` 签发 JWT，缓存并按 `exp` 刷新；`RestAuthStyle` 固定为 `SLURM_HEADERS`；
- `scripts/probe_sim_rest_submit.py` 不再 skipped；
- 只读 smoke：`/jobs`、`/job/{id}`、`/nodes`、`/partitions`、accounting、cancel 已终态作业语义；
- submit smoke：shared workdir success、invalid workdir failure、unwritable output failure；
- 超时对账 idempotency（marker + 时间窗口查询）。

门：smoke 全绿。

### Lane 4 — 后端接入 + real107 + digest（独立，可与 Lane 1/2 并行）

- `RestNativeSlurmBackend` 作为 `PILOT107_REST_*` env-gated 可选后端接入 competition profile（默认 command backend）；
- real107 只读 REST smoke 脚本化（jobs/nodes/partitions/openapi），沿用已 probe 的 job 21039 口径；
- OpenAPI digest 自动刷新任务接入（当前为一次性生成）。

门：profile 可切换 + real107 smoke 绿或 partial 有记录。

### Lane 5 — 文档与状态（收尾）

- 新增 `docs/phase-1/rest_convergence_report.md`（simulator auth 结果、主线边界、real107 状态、submit/cancel 延后至 M1-R）；
- 更新 `docs/phase-1/interface_hardening_status.md`、`docs/phase-0/current_gap_assessment.md`；
- 追加 `docs/phase-0/development_log.md` 第五十八批。

门：文档完成（无 git 仓库，文档是唯一进度留存）。

## 5. 阶段门

Phase 1 REST 专项收敛通过条件：

- [ ] simulator REST live submit 一个成功 Run；
- [ ] simulator REST live cancel 一个取消 Run；
- [ ] 6 矩阵场景结构化通过；
- [ ] `RestNativeSlurmBackend` env-gated 可选；
- [ ] real107 只读 smoke 有记录；
- [ ] 全量 `pytest` / `mypy src/pilot107` / `ruff` 无回归；
- [ ] `bash scripts/check-competition.sh` 无回归。

## 6. 风险与缓解

- **`auth_jwt.so` 构建踩坑**（libjwt 版本 / Slurm ABI）：Lane 1.2 优先 SchedMD apt 仓库预装包，规避源码构建；若 apt 路径不通再回退源码构建（`--with-jwt` + `libjwt-dev`）。若 1.2 全超预算，降级路径 B（契约级 + real107 只读），已实现适配器不浪费。
- **`scontrol token` 运行时依赖**：token mint 需要 slurmctld 可达 + key 可读；Worker/API 需新增 token 缓存与过期刷新逻辑，token 不得写入日志/DB/Capsule（沿用 auth_decision.md 凭证规则）。
- **`RestAuthStyle.BEARER` 不可用于 Slurm JWT**：默认值需切到 `SLURM_HEADERS`，或 REST 后端硬绑 SLURM_HEADERS。
- **镜像变大影响 bundle**：评估 `vm_test_readiness.md` 的 bundle 体积，必要时分层或剥离构建缓存。
- **REST live 引入 flakiness**：command backend 保持默认，REST 仅 env-gated 可选，竞赛演示不强依赖 REST live。

## 7. 并行性与派工

- Lane 1（compose / probe / 镜像）与 Lane 2（adapters / tests）写域不冲突 → 可同时派两个 @fixer；
- Lane 4 独立 → 可并行；
- Lane 3 依赖 Lane 1 结果；
- Lane 5 + 验证门收尾。

## 8. 验证命令（收尾）

```bash
PYTHONPATH=src uv run --extra dev pytest
uv run --extra dev mypy src/pilot107
uv run --extra dev ruff check
bash scripts/probe-sim-rest-auth.sh
bash scripts/probe-sim-rest-submit.sh   # 不再 skipped
bash scripts/check-competition.sh
```

## 9. 下一步

```text
REST 专项收敛（本计划）
→ M1 HTTPS/reverse proxy 与两机部署脚本
→ 前端设计包接入前的后端交互回归固化
```
