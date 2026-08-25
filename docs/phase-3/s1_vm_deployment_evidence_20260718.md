# 107Pilot S1 VM 部署与验收证据 — 2026-07-18

- 日期: 2026-07-18
- 证据范围: S1 CPU VM (8C/16G, Docker Slurm simulator); NOT real 107, NOT campus multi-user production
- 目标 VM 规格: 8 vCPU Intel Xeon (Icelake), 15 GiB RAM + 4 GiB swap, 146 GB disk (131 GB free), Ubuntu 24.04.4 LTS, kernel 6.8.0-106-generic x86_64, Docker 29.1.3 (systemd cgroup v2, overlayfs/containerd snapshotter), Python 3.12.3, OpenSSL 3.0.13
- 公网访问 URL: `https://114.214.241.31:8443/` (自签证书，用户已显式接受用于测试)
- CPU-RC 修订: `9f0187e5ff38` (bundle `107pilot-cpu-rc-9f0187e5ff38-20260718T112947Z`)
- 交叉引用:
  - `docs/phase-3/revised_execution_plan_20260716.md` (G3 链定义)
  - `docs/phase-3/cpu_rc_release_review.md` (本记录在 S1 状态问题上取代其中 "尚未上传或部署 VM" 的结论)
  - `docs/phase-0/vm_test_readiness.md` (VM 进入条件)

> **环境边界声明**: 本验收在 Docker Slurm simulator + 单台 CPU VM + fixed_user=alice 边界内完成。不构成真实 107 平台验收,亦非校园多用户生产环境验证。所有并发测试使用单一身份 alice。

## 2026-08-25 演示部署收口

本节取代本文中“最新 Agent lifecycle 尚未部署”的后续旧状态，但不改变环境边界：演示对象是 VM 上实际运行的 `slurmctld/slurmd/slurmdbd` 和数字 Slurm Job，不是校园 107 集群。107Pilot 通过 `docker-compose-command` 调度后端把该 VM-local Slurm 当作本次演示的真实 Slurm 对象。

- 最终代码修订：`2322506af112e570896b7b1d3b2d6e9473b65942`。
- 最终 bundle：`107pilot-cpu-rc-2322506af112-20260825T070152Z`；SHA-256 `c1332f8b4f6ec64e76da67971f90648cffcffc2fc17141dbc4d59199e061659d`。
- systemd：`pilot107-cpu-rc.service` 已 enabled/active，WorkingDirectory 为 `/root/107pilot-cpu-rc-2322506af112-20260825T070152Z`。
- 运行态：11 个服务全部 running；带 healthcheck 的 7 个服务均 healthy；`anode16` 为 6 CPU、10240 MiB、`CPU-RC` 分区。
- 外部入口：`https://114.214.241.31:8443/` 与 `/healthz` 均 HTTP 200，health body 为 `{"status":"ok"}`。
- 模型固定为网关暴露的 `qwen3.8-reasoner`，空响应最多进行 3 次无副作用 provider 尝试。网关未提供参数规模元数据，因此不能把“27B”尺寸表述为已验证事实。
- Bubblewrap：API/worker 运行镜像包含 `bubblewrap`，API 服务提供可写、受限的 `/tmp` tmpfs；Workspace sandbox `sandbox-cc1f79856bdcaff9a8a4ddf4` 实际执行成功。

### AgentTask → Slurm → Evidence → Capsule

- Session：`session-11593d24-301f-45cb-9831-ff158ce786d5`。
- AgentTask：`task-581dab2d5b53e49b58bd45ec08e6a82fe077d6a163d1c5015bb80b5cb327ed8e`。
- Linked Run：`run_agent_6e430e5c5983a1d0704516cfe5a850fcd1e38bd8`，backend=`docker-compose-command`，Slurm Job `23`，state=`SUCCEEDED`。
- Evidence：19 个对象；Capsule `capsule_213787c26b3a4a11b79a5bf331691330` ready。
- Ready outbox 已唤醒后续 Turn：`turn-47c584c5-6c07-44bb-addc-c219497b45b2`。

### 运行、恢复与浏览器验收

- 三态 smoke：成功 `run_b58e5628a4f14ac38442adf6ef0ad4a3:24`，失败 `run_c0dec4c6b1fe49ad9a02269352d8dba1:25`，取消 `run_0e66c4ac6950437aa5f39a318d2a665f:26`；三者 Capsule 均生成。
- 不删卷重启恢复：重启前 `run_fae5ebf9abc34980bbd146023a69bff5` 保留，重启后 `run_99698eabb1dd4d128166d4ef501ce8c6` 成功。
- 外部浏览器：Files 手工输入深层路径通过；按 `result.txt` 搜索返回文件并定位父目录；Agent 工程页可见最终 ChangeSet 与异步 Slurm 验证入口。
- 完整源门禁：1407 passed、24 skipped、36 subtests；179 Vitest；24 Playwright；Ruff、mypy、typecheck、build、static drift、Compose config、sync drift 全部通过。

机读摘要：`artifacts/acceptance/vm-demo/2322506af112/deployment-summary.json`。浏览器截图：`artifacts/qa/vm-final-files.png`、`artifacts/qa/vm-final-agent.png`。

残余边界：自签证书；fixed_user=alice；npm audit 仍报告 1 个 moderate、2 个 high；VM registry/DNS 与 NTP 状态会令预检产生环境告警；校园身份、校园 107 资源和多用户生产仍为 NO-GO。

## 部署环境

- VM: 8 vCPU Intel Xeon (Icelake), 15 GiB RAM + 4 GiB swap, 146 GB disk (131 GB free), Ubuntu 24.04.4 LTS, kernel 6.8.0-106-generic x86_64
- Docker: 29.1.3 (systemd cgroup v2, overlayfs/containerd snapshotter)
- Python: 3.12.3; OpenSSL: 3.0.13
- 网络: 内网 IP 192.168.246.3, 公网 NAT 114.214.241.31, SSH 端口 8000; 用户已建立端口转发 8080→8080 (HTTP) 与 8443→8443 (HTTPS)
- 校园网: github.com 不可达 (大文件下载超时); apt/pip 经 USTC 镜像可达; USTC LLM 端点可达
- 时钟: 首次启动时未同步, 通过 `timedatectl set-ntp true` 启用 NTP 后已同步
- 证书: 自签 (用户已显式接受用于测试)

## 部署步骤

1. VM 预检 (只读): 确认 8C/16G/Docker/Python3/openssl/cgroup2/firewall/ports。发现 `docker compose` v2 插件缺失 (Ubuntu docker.io 包不附带该插件), 且时钟未同步。
2. 安装 docker compose v2.29.2 二进制 (63 MB, 因 VM 无法从 github 下载而由本地经 SFTP 上传) 至 `/usr/libexec/docker/cli-plugins/docker-compose`。验证 `docker compose version` → v2.29.2。
3. 启用 NTP: `timedatectl set-ntp true`。
4. 经 SFTP 上传 287 MB 离线 bundle `107pilot-cpu-rc-9f0187e5ff38-20260718T112947Z.tar.gz` (103 MB/s, 2.8s)。在 VM 上验证 SHA256: `4816c886347bd9006b9d6ae4d22560fc17f3e7c6df32a6ba063f306387feabe9`, 与本地 sidecar 一致。
5. 解压 bundle 至 `/root/107pilot-cpu-rc-9f0187e5ff38-20260718T112947Z/`。
6. 通过 `scripts/import-cpu-rc-images.sh` 导入 4 个离线镜像: 全部 4 个加载成功, 摘要与 RELEASE_MANIFEST.json 一致 (slurm-sim sha256:8eb4ad3c1cd0..., api/worker/web 共享 sha256:2847eef1d132...)。
7. 预创建 `.env.cpu-rc`, 设置 `PILOT107_HTTP_PORT=8080` (匹配 NAT 转发) 并生成随机凭据 (root/slurm/jwt/gateway)。占位凭据拒绝闸门通过。
8. 以 `PILOT107_SKIP_BUILD=1 bash scripts/start-cpu-rc.sh` 启动 stack (关键: 使用已导入的离线镜像, 未重新构建 — 保全固定摘要)。stack 起来后 10/10 容器健康。

## G3 验收链结果

### 全链路冒烟 (`scripts/check-cpu-rc.sh`, rc=0)

success/fail/cancel + Evidence/Capsule 闭环全部通过 (capsule 由显式 `POST /runs/{id}/capsule` 触发生成,非作业结束后自动生成;见下文 Phase A 边界):

- success: run_09c4a41645de4093a108a07474306e17:1
- failure: run_f984a63a06964de1875f27cabca4f4bc:2
- cancelled: run_38b88311dd824a3dbec05f13f4d87cc0:3
- capsules: capsule_2002b6c4..., capsule_00ac09d4..., capsule_e3d1cdba...

### 外部可达性

- `https://114.214.241.31:8443/` → HTTP 200, 返回 107Pilot workbench HTML
- `http://114.214.241.31:8080/` → 响应 (重定向代理)
- 自签证书经 NAT 可用

### 并发 20 (`load_competition.py --scenario all --concurrency 20`, CPU-RC partition)

- read 20/20 (15.5 rps)
- validate 20/20 (18.3 rps)
- prepare 20/20 (11.9 rps)
- 错误: 0

### 并发 50 (`--concurrency 50`)

- read 50/50 (21.6 rps)
- validate 50/50 (24.0 rps)
- prepare 50/50 (9.5 rps)
- 错误: 0
- 资源余量: mem 1.1 GiB/15 GiB, load avg 0.28, 最大容器 CPU 25%

### 4 路并发端到端工作流 (`--scenario workflow --concurrency 4`)

- ok=4/4
- errors=0
- elapsed 9.4s
- p50 latency 6.1s
- 证明单一用户 (alice) 的并发请求处理与 capsule 完成（非多用户身份隔离或越权测试）

### 重启 + 卷恢复

- 停止 stack (`docker compose down`, 0 容器), 重启 (`start-cpu-rc.sh`), 再次 10/10 健康
- 重启前 run `run_2798bf048f834ac7a9342e44c9fc7966` 存活: state=SUCCEEDED, collection_state=succeeded, capsule_state=ready
- 重启后新工作流成功 (ok=1/1, 3.4s)
- healthz=200

## 在线服务就绪补充

(超出 G3, 用于竞赛使用)

- 固定重启策略: app 容器 (api/web/worker) 在 compose.yml 中原为 `restart=no` (仅 Slurm 服务通过 anchor 设有 unless-stopped)。对全部 10 个容器执行 `docker update --restart=unless-stopped`, 现已统一。
- 创建 systemd 单元 `/etc/systemd/system/pilot107-cpu-rc.service` (oneshot, RemainAfterExit, ExecStart=start-cpu-rc.sh, ExecStop=stop-cpu-rc.sh, After=docker.service)。已 enable。VM 重启后 stack 自动启动。
- 将 systemd 默认 target 设为 `multi-user.target` (原为 `graphical.target`)。

## 工程缺口修复

(本地修复, 非 VM 上)

- 编写 `scripts/preflight-cpu-rc-vm.sh` (CPU-RC 专用 VM 预检, 由 preflight-competition-vm.sh 改编, 检查 4 个 cpu-rc-9f0187e5ff38 镜像标签 + compose.cpu-rc.yml + CPU-only 断言 + 时钟同步告警)。已通过语法检查, 可执行。
- 编写 `artifacts/deployment/LATEST_CPU_RC.txt`, 指向 `107pilot-cpu-rc-9f0187e5ff38-20260718T112947Z` (现有 LATEST.txt 未触碰 — 仍指向较早的 competition bundle)。

## 结论与边界

S1 已部署, G3 功能链通过 (smoke/外部可达/并发 20/并发 50/4 路工作流/重启卷恢复 全部通过, rc=0)。

边界:

- 非 real 107 (Slurm 为模拟器)
- 非校园多用户生产 (单租户 fixed_user=alice)
- G4 供应链 CI 扫描仍不满足
- 自签证书 (用户已显式接受用于测试, 非生产信任链)
- 单一 fixed_user=alice 身份

本记录在 S1 状态问题上取代 `docs/phase-3/cpu_rc_release_review.md:5` 中 "尚未上传或部署 VM" 的结论。`current_status_index.md` 的 S1 行应从 "当前未部署" 更新为 "已部署，G3 功能链通过"。

## Phase A 重部补录 (2026-07-18T21:46Z)

Phase A 四缺口修复后重部到 VM (revision `a91b9765def1`)，让设计中的 12 步演示闭环在 VM 上真正跑通。

### 修复内容

| 缺口 | 实现 | VM 验证结果 |
|---|---|---|
| A-1 Slurm 实时事实 | 新增 `slurmrest_snapshot.py` 采集器 + `service.py` 启动时采集 + 5min 后台刷新；owner 用 `config.slurm_username` (alice)；collector 接受 `slurm_token` (JWT)；compose.yml 传 `PILOT107_SLURM_TOKEN` 到 api 容器 | `/api/v1/platform/capabilities` 的 `latest_snapshot` 非 null ✓; `counts: {partitions:1, nodes:1}` (CPU-RC 分区 + anode16 节点 4CPU) ✓; `source_type:rest`, `freshness:fresh` ✓ |
| A-2 模板市场 seed | 新增 `template_market_seed.py` 完整发布流 (create_draft→submit_review→decide_review→publish), 幂等 (skip 已发布 + resume editable stale draft + refresh stale payload), 容错 (gate-blocked 记录不中断); 系统 bootstrap reviewer 注入 | 启动日志 `published=5 gate_blocked=0` ✓; `GET /api/v1/templates` 返回 5 个模板 (学生 CPU/结构化 Preflight/健壮 Slurm/Python CPU/Fail-closed 合并) ✓ |
| A-3 LLM 接入 | `.env.cpu-rc.example` 加 USTC glm-5.2-107 模板; VM `.env.cpu-rc` 注入真实 apiKey（不在证据中记录）; `api.ts` `advanceRemediationSession` 默认发 `provider=local`; `AgentPage.tsx` 加 provider 选择器 | env 配置 ✓; LLM endpoint 可达 (status 200, 返回模型列表) ✓; UI provider 选择器已部署 (web 测试 74 pass) |
| A-4 workspace 绑 job | 新增 `RunPicker.tsx` 纯组件; `AgentPage.tsx` 空状态改内联 RunPicker (filter FAILED); `pages.tsx` `TerminalCollaborationPage` 空状态改内联 RunPicker; `QueryBoundary.emptyDetail` 放宽为 ReactNode | 代码完成 (web 测试 74 pass); 浏览器视觉验证待人工 |

### 测试基线

- Python: 628 passed, 13 skipped, 5 subtests passed
- Web (vitest): 74 passed, 0 failed
- TypeScript: `tsc --noEmit` 无错误

### 部署过程关键修复 (4 轮重建)

1. 首次重建: Docker 层缓存导致 Phase A 文件未入镜像 → 改用 `--no-cache` 重建
2. `.env.cpu-rc` image refs 指向旧 revision → sed 更新到新 revision
3. seed 因 `config.template_reviewers` 默认不含系统 reviewer → seed 内部构造 seed-scoped role_directory
4. 持久 DB 卷残留 stale editable drafts (qos='normal') → resume 时 `update_draft` 刷新 payload

### Phase A 不做 (边界)

- 真实 107 (Slurm 仍为模拟器)
- 校园多用户生产 (单租户 fixed_user=alice)
- 扩展闭环新功能 (代码上传 / LLM 生成作业 / 自动 capsule / agent 热修 / 下载上传重试 / 分享) — Phase B
- `RemediationPlanV1` 结构化提案接入 live `_plan_turn` — Phase B

### Follow-up (不阻塞演示)

- A-4 浏览器视觉验证: 用户手动打开 `https://114.214.241.31:8443/agent` 和 `/terminal` 确认 RunPicker 空状态
- VM root 密码更换: 密码在此文档前已暴露, 建议换 SSH key 或新密码
- slurmrestd JWT token TTL: 当前 minted 7 天 (604800s), 过期后需重新 mint + 注入 `PILOT107_SLURM_TOKEN`; 长期方案是让 api 容器通过 docker socket 或共享机制自动 mint
