# VM 实测前就绪说明

日期：2026-07-12

## 1. 当前目标

本阶段做到“使用学校虚拟机实际测试之前的一步”：

```text
本机生成可迁移 competition 部署包
→ 部署包内含 compose、脚本、源码、文档和离线镜像
→ 本机可校验脚本、compose 和 100 并发承载力
→ 下一步才把包传到学校 VM 执行实测
```

本文件不声明学校 VM 已通过；学校 VM 的网络、证书、防火墙、磁盘、Docker 权限和重启恢复仍需现场验证。

## 2. 新增交付件

脚本：

- `scripts/export-competition-bundle.sh`
- `scripts/import-competition-images.sh`
- `scripts/preflight-competition-vm.sh`
- `scripts/load_competition.py`
- `scripts/start-competition-slurm-host.sh`
- `scripts/start-competition-app-node.sh`
- `scripts/stop-competition-slurm-host.sh`
- `scripts/stop-competition-app-node.sh`

报告：

- `docs/phase-0/competition_deployment_check_report.md`
- `docs/phase-0/load_capacity_report.md`
- `docs/phase-0/vm_test_readiness.md`

## 3. 本机生成部署包

```bash
bash scripts/export-competition-bundle.sh
```

默认会：

- 构建 `pilot107/slurm-sim:local`；
- 构建 `pilot107/api:local`；
- 构建 `pilot107/worker:local`；
- 构建 `pilot107/web:local`；
- 导出 `images/pilot107-images.tar.gz`；
- 复制 competition compose、脚本、源码和文档；
- 生成 `SHA256SUMS`；
- 生成最终压缩包和 `.sha256`。

如只想测试打包逻辑，不导出大镜像：

```bash
PILOT107_SKIP_BUILD=1 PILOT107_EXPORT_IMAGES=0 bash scripts/export-competition-bundle.sh
```

本轮已生成的最终包：

```text
archive: /home/knowingthesea/107pilot/artifacts/deployment/107pilot-competition-bundle-20260711T163304Z.tar.gz
sha256:  4ce6ba12509660b2da05d3a8ef885313a2508a7903620b11d59001d245616c89
latest:  /home/knowingthesea/107pilot/artifacts/deployment/LATEST.txt
size:    153M
```

已验证：

```text
外层 sha256sum -c 通过
包内 SHA256SUMS 通过
包内 import-competition-images.sh 可运行
包内 preflight-competition-vm.sh --require-images 可运行
包内不包含本机 .env.competition
包内不包含本机 certs/tls.key 或 certs/tls.crt
```

## 4. VM 上的预期执行顺序

把生成的归档包传到 VM 后：

```bash
tar -xzf 107pilot-competition-bundle-*.tar.gz
cd 107pilot-competition-bundle-*

sha256sum -c SHA256SUMS
bash scripts/import-competition-images.sh
bash scripts/preflight-competition-vm.sh --require-images

cp simulator/compose/.env.competition.example simulator/compose/.env.competition
```

根据 VM 情况编辑：

```text
simulator/compose/.env.competition
```

至少确认：

- `PILOT107_HTTP_PORT`
- `PILOT107_HTTPS_PORT`
- `SLURMRESTD_PORT`
- `PILOT107_API_PORT`
- `PILOT107_WEB_PORT`
- `PILOT107_COMMAND_GATEWAY_TOKEN`
- `PILOT107_ALLOWED_ROOTS`

启动：

```bash
PILOT107_SKIP_BUILD=1 bash scripts/start-competition.sh
```

如使用两台 VM：

Slurm 宿主机：

```bash
PILOT107_SKIP_BUILD=1 bash scripts/start-competition-slurm-host.sh
```

应用节点：

```bash
cp simulator/compose/.env.competition.example simulator/compose/.env.competition
```

编辑：

```text
PILOT107_REMOTE_COMMAND_GATEWAY_URL=http://<slurm-host-ip>:18090
```

启动：

```bash
PILOT107_SKIP_BUILD=1 bash scripts/start-competition-app-node.sh
```

功能 smoke：

```bash
bash scripts/check-competition.sh
```

承载力 smoke：

```bash
python3 scripts/load_competition.py --concurrency 100 --scenario all
python3 scripts/load_competition.py --concurrency 100 --scenario workflow --workflow-timeout 360
```

## 5. VM 预检项

`scripts/preflight-competition-vm.sh --require-images` 会检查：

- Docker CLI；
- Docker daemon 权限；
- Docker Compose；
- Python 3；
- OpenSSL；
- competition compose config；
- 至少 20GB 可用空间；
- 关键端口是否可绑定；
- 四个 `pilot107/*:local` 镜像是否已导入。

## 6. 当前本机已验证事实

本机 competition profile 已验证：

- HTTPS 入口可访问；
- API/Worker 连接 `command-gateway`，不是 demo backend；
- 成功、失败、取消 Run 均能完成 Evidence；
- Capsule API 可生成 raw capsule；
- 100 并发轻量请求通过；
- 100 并发完整 `submit → Evidence → Capsule` 工作流通过。

详见：

- `docs/phase-0/competition_deployment_check_report.md`
- `docs/phase-0/load_capacity_report.md`

## 7. 进入 VM 实测前仍需确认

学校或比赛组织方需要提供：

- 应用节点和 Docker 宿主机的实际 IP/域名；
- Docker 权限或 root/sudo；
- 可开放端口；
- 防火墙策略；
- 正式 HTTPS 证书或校内证书；
- 是否允许自签证书演示；
- VM 磁盘容量；
- 重启策略；
- 是否需要离线导入镜像。

## 8. 限制

- 当前部署包支持单机 profile 和两机脚本化 profile；真正“两台 VM 分布式部署”仍需要现场网络、防火墙、证书和远端 command-gateway 连通性验证。
- 本地自签名证书仅用于测试；正式比赛应替换证书。
- 当前完整 100 并发 workflow 可通过，但 p95 约 164 秒，体验优化仍建议增加多 Worker 和指标。
- 真实 107 平台仍属于 M1-R 兼容探测，不是比赛主系统依赖。
