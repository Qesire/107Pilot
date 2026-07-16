# Phase 0B Competition 部署检查报告

日期：2026-07-11

## 1. 当前交付形态

本仓库新增本地 competition profile，用于模拟比赛 M1 的两层部署边界：

```text
browser
→ HTTPS reverse proxy
→ pilot107-web
→ pilot107-api / pilot107-worker
→ pilot107-command-gateway
→ Docker Slurm simulator
```

关键文件：

- `simulator/compose/compose.competition.yml`
- `simulator/compose/.env.competition.example`
- `simulator/compose/nginx/competition.conf`
- `simulator/compose/scripts/command-gateway.py`
- `scripts/start-competition.sh`
- `scripts/check-competition.sh`
- `scripts/stop-competition.sh`
- `scripts/smoke_competition_web.py`

## 2. 与设计树对齐

对照 `design_v1.4/13_测试验收与运维.md`：

| Phase 0B 条件 | 当前状态 | 说明 |
|---|---|---|
| 应用服务连接 Docker Slurm backend | 已验证 | `pilot107-api`、`pilot107-worker` 在 competition profile 下使用 `command-gateway`，不使用 `demo` |
| Web 使用 HTTPS | 已验证 | `pilot107-reverse-proxy` 在 8443 终止 TLS，8080 明文入口重定向到 HTTPS |
| DB 和 Evidence 持久化 | 已验证 | SQLite、Evidence、Capsule 均在 `pilot107-data` volume 下 |
| 一键部署/启动 | 已验证 | `scripts/start-competition.sh` |
| 一成一败一取消 | 已验证 | `scripts/smoke_competition_web.py` |
| Capsule 闭环 | 已验证 | `POST /api/v1/runs/{run_id}/capsule` |
| 学校应用节点 + Docker 宿主机两机验证 | 未验证 | 需要学校服务器地址、端口、防火墙、证书和重启策略 |

## 3. 安全边界

competition profile 不把 Docker socket 挂入 `pilot107-api` 或 `pilot107-worker`。

应用容器通过 `pilot107-command-gateway` 访问模拟 Slurm。gateway 的限制：

- 仅在 Compose 内网暴露；
- JSON API，不执行 shell；
- 命令首项白名单；
- 支持 bearer token；
- 文件写入和工作目录限制在 `PILOT107_ALLOWED_ROOTS`；
- 默认只允许 `/public/home/alice`。

当前 nginx HTTPS 使用本地自签名证书。比赛部署时应替换为学校提供或正式签发的证书。

## 4. 本地验证记录

已运行：

```bash
uv run --extra dev mypy src/pilot107
bash scripts/check_phase0_core.sh
npm run check:js
bash scripts/build-slurm-sim-image.sh
bash scripts/build-app-images.sh
bash scripts/check-app-images.sh
docker run --rm pilot107/slurm-sim:local python3 --version
docker compose --env-file simulator/compose/.env.competition.example -f simulator/compose/compose.yml -f simulator/compose/compose.competition.yml --profile competition config
bash scripts/start-competition.sh
bash scripts/check-competition.sh
curl -k -sS https://127.0.0.1:8443/healthz
curl -k -sS https://127.0.0.1:8443/
```

结果摘要：

```text
mypy -> Success: no issues found in 24 source files
unit -> Ran 107 tests, OK
js syntax -> ok
app images -> ok
slurm-sim python -> Python 3.12.3
competition compose config -> ok
start-competition -> competition profile is running: https://127.0.0.1:8443/
HTTPS health -> {"status":"ok"}
HTTPS page -> returned 107Pilot HTML
```

`check-competition.sh` 已完成两轮，其中一轮结果：

```text
competition web smoke ok
success=run_fa1e12d13b964a44a3a077a1b38c8839:105
failure=run_98185bf27f0b4a269aab7ca6f093cfca:106
cancelled=run_66b60ab59012481aa5623a62a315d820:107
capsules=capsule_973facfc430e4955a6bce2b1d99073e7,
         capsule_d7baf65702484e38b04d74b9250c97d4,
         capsule_a5f79803fa5e4af5965d2c18fc5bce71
```

该 smoke 强制检查：

- `submit_strategy == "command"`；
- `job_id` 不以 `demo-` 开头；
- 成功 Run 达到 `SUCCEEDED + collection_state=succeeded + capsule_state=ready`；
- 失败 Run 达到 `FAILED + collection_state=succeeded + capsule_state=ready`；
- 取消 Run 达到 `CANCELLED + collection_state=succeeded + capsule_state=ready`；
- Evidence 包含 submission、slurm、logs、environment、outputs、derived summary；
- Capsule 返回 `manifest_sha256`。

## 5. 限制说明

- 当前 profile 是本地 Compose 版 competition 模拟，不等同于学校两台服务器的最终验收。
- `slurmrestd` REST submit 仍是并行兼容专项；比赛主线使用受控 command gateway。
- 本地自签名证书只用于验证；正式演示需替换为学校或正式签发证书。
- 真实 107 平台仍只作为 M1-R 兼容探测，不作为比赛系统运行依赖。
- Docker simulator 使用的 Slurm 包版本可能低于真实 107 平台资料中的版本，不能把模拟结果外推为真实平台能力声明。
