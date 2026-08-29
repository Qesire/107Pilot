# Phase 0B：分布式比赛部署计划

## 1. 目标形态

```text
校园网应用部署节点
├── reverse-proxy
├── pilot107-web
├── pilot107-api
├── pilot107-worker
└── metadata DB

校园网内部连接

Docker 模拟宿主机
├── slurmctld
├── slurmrestd
├── slurmdbd
├── mariadb
├── worker-1
├── worker-2
└── simulated shared storage
```

## 2. 部署原则

- 107Pilot 不部署到真实 107 管理节点；
- 不修改真实 107 平台；
- 应用节点只访问 Docker 宿主机必要接口；
- Web/API/Worker 与 Slurm 控制面分离；
- 浏览器访问应用节点必须使用 HTTPS；
- HTTPS 在应用节点 reverse proxy 终止，`pilot107-api` 内部只监听 localhost 或私网 HTTP；
- DB、Evidence、Capsule 持久化；
- 演示前准备离线镜像和备份。

## 3. 网络要求

待确认：

- 应用节点固定 IP 或域名；
- Docker 宿主机固定 IP 或域名；
- 应用节点到 Docker 宿主机的端口访问；
- 是否允许并提供 HTTPS/TLS 证书或校内证书签发方式；
- 防火墙是否只允许应用节点访问 Docker 宿主机；
- 两机时间同步。

## 3.1 HTTPS 决策

```text
browser
→ HTTPS
→ application-node reverse-proxy
→ private HTTP
→ pilot107-web / pilot107-api
```

比赛演示环境不应让浏览器直接访问明文 API。若应用节点和 Docker 宿主机之间跨机器传输 Evidence bundle 或 API 数据，优先使用 TLS；若只能在封闭校园网内使用私网 HTTP，则必须由防火墙限制为应用节点到 Docker 宿主机的最小端口集合。

## 4. 暴露接口

Docker 宿主机建议只暴露：

```text
slurmrestd
EvidenceTransport API 或 bundle endpoint
必要健康检查
```

不暴露：

```text
Docker daemon socket
MariaDB
任意 shell
宿主机文件系统
```

## 5. 验收

- 应用节点可访问 Docker slurmrestd；
- 应用节点可读取或接收模拟 Evidence；
- 一成一败一取消一重试可演示；
- Worker 重启可恢复；
- Docker 宿主机重启后 volume 保留；
- 应用节点重启后 DB/Evidence 保留。

## 6. 当前脚本化形态

当前保留单机 competition profile：

```bash
PILOT107_SKIP_BUILD=1 bash scripts/start-competition.sh
bash scripts/check-competition.sh
```

同时新增两机脚本化入口：

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

新增 compose override：

- `simulator/compose/compose.competition-slurm-host.yml`
- `simulator/compose/compose.competition-app-node.yml`

网络边界：

- 浏览器只访问应用节点 HTTPS；
- 应用节点访问 Slurm 宿主机 `pilot107-command-gateway`；
- Slurm 宿主机无需暴露 Docker daemon、MariaDB 或宿主机文件系统。

## 7. Phase-aware Builder 演示配置

CPU-RC 演示使用 `PILOT107_PHASE_AWARE_BUILDER=1`，并把该值同时传给
`pilot-agentd`、`pilot107-api` 和 `pilot107-worker`。Builder 模型只调用
`builder_context_get` 与 `builder_build_submit`；Sandbox、资源推导和
`vm-slurm` validation 调度由服务端确定性执行。

少轮数是观测目标，不是小预算硬限制。有效修复可继续进行；20 个 Pi step
和 32 次 gateway invocation 仅作为异常循环保险。部署及回滚流程、健康检查
和科学计算验收见 `docs/operations/phase-aware-builder.md`。
