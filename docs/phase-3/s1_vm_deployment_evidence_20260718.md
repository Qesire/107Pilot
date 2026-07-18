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

success/fail/cancel + Evidence/Capsule 闭环全部通过:

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
- 证明多用户并发实际作业提交 + capsule 完成

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
| A-1 Slurm 实时事实 | 新增 `slurmrest_snapshot.py` 采集器 + `service.py` 启动时采集 + 5min 后台刷新；owner 用 `config.slurm_username` (alice) | `/api/v1/platform/capabilities` 的 `latest_snapshot` 非 null ✓；partitions/nodes 数据为空 (slurmrestd 需 JWT, api 容器无 docker socket 不能 `scontrol token`) — 静态 profile 仍显示 CPU-RC 分区/QoS, 可演示; JWT 接入列为 follow-up |
| A-2 模板市场 seed | 新增 `template_market_seed.py` 完整发布流 (create_draft→submit_review→decide_review→publish), 幂等 (skip 已发布 + resume editable stale draft + refresh stale payload), 容错 (gate-blocked 记录不中断); 系统 bootstrap reviewer 注入 | 启动日志 `published=5 gate_blocked=0` ✓; `GET /api/v1/templates` 返回 5 个模板 (学生 CPU/结构化 Preflight/健壮 Slurm/Python CPU/Fail-closed 合并) ✓ |
| A-3 LLM 接入 | `.env.cpu-rc.example` 加 USTC glm-5.2-107 模板; VM `.env.cpu-rc` 注入真实 apiKey (sk-4J_...); `api.ts` `advanceRemediationSession` 默认发 `provider=local`; `AgentPage.tsx` 加 provider 选择器 | env 配置 ✓; LLM endpoint 可达 (status 200, 返回模型列表) ✓; UI provider 选择器已部署 (web 测试 74 pass) |
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

- A-1 slurmrestd JWT auth: api 容器需 docker socket mount 或与 worker/slurm 容器共享 token 机制, 才能让 REST 采集读到真实 partitions/nodes
- A-4 浏览器视觉验证: 用户手动打开 `https://114.214.241.31:8443/agent` 和 `/terminal` 确认 RunPicker 空状态
- VM root 密码更换: 密码在此文档前已暴露, 建议换 SSH key 或新密码
