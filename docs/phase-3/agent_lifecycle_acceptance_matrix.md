# Agent 生命周期发布候选验收矩阵

日期：2026-08-25

适用范围：统一 Project / Session / Turn / Task / Run / Evidence / Runtime Watch 生命周期候选

## 判定规则

四个入口生成 `pilot107.agent-lifecycle-acceptance/v1` JSON，默认写入
`artifacts/acceptance/agent-lifecycle/<git-sha>/`。`release_revision` 必须等于执行时的
`git rev-parse HEAD`；D0 和 D1 只有在同一 SHA 上均为 `PASS` 才构成本机候选证据。
任一必需 case 没有执行步骤时记为 `MISSING`，任一支撑步骤失败时记为 `FAIL`，均不得聚合为
`PASS`。

| 环境 | 入口 | 必需事实 | 当前准入含义 |
| --- | --- | --- | --- |
| D0 source | `bash scripts/accept-agent-lifecycle-source.sh` | Python lint/type/unit/schema；Agentd type/unit/build；Web type/unit/build/browser；Compose；sim-core | 与 D1 同 SHA 通过后允许形成本机候选 |
| D1 runtime | `bash scripts/accept-agent-lifecycle-runtime.sh` | 12 个生命周期场景；100 idle Sessions；10 concurrent Turns；100 active Watches；每 Turn 32 commands / 1 MiB、每 result 64 KiB 预算 | 只证明干净 Docker Slurm 25.11.2 simulator 行为 |
| S1 VM | `bash scripts/accept-agent-lifecycle-s1.sh` | 同 revision bundle；确认的 8C/16G 主机；worker 8 CPU / 15 GiB ceiling；部署与重启恢复 | 缺少主机、bundle 或公开 URL 时为 `not_run` |
| R1 real 107 | `bash scripts/accept-agent-lifecycle-r1.sh ...` | success、exit 42、cancel、auth expired、Evidence、Watch、资源可用性、模型不可用降级 | 缺少明确授权或活动 ControlMaster 时为 `not_run`；模拟器目标一律 `refused` |

## D1 必需场景

- blank Project 金路径；失败 Run 的 code repair；长时间 pending Turn 释放；
- Runtime Watch 重放和 terminal drain；资源缺失语义和摘要；publish conflict；
- Worker / Agentd / browser restart；双 owner 隔离；Market application/publication；
- 模型不可用时 Run / Evidence / Watch 仍确定性可读，只有对应生成式 Project 进入 `blocked`；
- 5 GiB 大文件只记录 metadata，不复制内容；artifact-aware array recovery；
- 100 idle Sessions、10 concurrent Turns、100 active Watches 和连接 command/byte budgets。

D1 每次先清理 stack，再用 `lifecycle-<short-sha>` tag 构建并记录四个应用镜像 ID，最后清理
volume/orphan。旧容器、ambient stack 或不同 revision 镜像不能作为通过证据。

## S1 执行合同

S1 只在目标 VM 本机执行，并同时要求：

```bash
PILOT107_S1_CONFIRMED=1 \
PILOT107_S1_BUNDLE_DIR=/absolute/path/to/extracted-bundle \
PILOT107_PUBLIC_URL=https://staging.example.edu \
bash scripts/accept-agent-lifecycle-s1.sh
```

入口校验 bundle manifest revision 与当前 SHA 一致、宿主至少 8 CPU 且具有 16 GiB 标称主机的
可用内存包络，并检查 CPU-RC worker ceiling。随后以 seal mode 执行既有 offline runtime bundle
验收，覆盖部署、镜像绑定和 restart recovery。

## R1 授权合同

R1 不建立新认证，也不从环境、历史探测或 D1 结果推断批准。调用者必须显式给出：

```bash
PILOT107_R1_CONTROL_PATH=/absolute/path/to/active-control-socket \
bash scripts/accept-agent-lifecycle-r1.sh \
  --target <real-107-ssh-alias> \
  --owner <owner> \
  --approved-root /public/home/<owner>/pilot107-smoke-<label> \
  --authorization-id <approval-id> \
  --confirm-real-107
```

批准目录必须是新的、owner-scoped `pilot107-smoke-*` 路径。固定作业只申请 Students/stu、1 CPU、
2 分钟，提交 success、exit 42 和 cancel 三个作业；所有 SSH/SCP 命令复用给定 ControlMaster。
过期认证必须 fail closed。任何 localhost、Docker、simulator 或 `.local` 目标都在网络访问前拒绝。

## 发布决定

- D0=`PASS` 且 D1=`PASS`、revision 相同：本机 agent lifecycle candidate GO。
- S1 或 R1=`not_run`：不推翻 D0/D1，但相应环境仍未验证。
- S1、R1 或校园身份/运维批准任一未通过：校园多用户生产 **NO-GO**。
- manifest 的 `status=not_run` 只是待执行合同，不是验收结果；发布判断只读取 acceptance report。
