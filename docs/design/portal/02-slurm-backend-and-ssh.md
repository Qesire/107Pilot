# 02. Slurm 后端与真实 SSH 接入

> 实现状态（2026-07-26）：受控 Relay、session metadata、SSH Slurm
> backend、SSH Evidence transport、API/Worker builder、连接 API 和前端状态
> 已实现；真实 107 四场景 live acceptance 尚未完成。当前事实见
> [08-m1-implemented-contract.md](08-m1-implemented-contract.md)。

## 1. 目标

真实 107 接入必须实现为 `SlurmBackend` 和 `EvidenceTransport` 的正式实现，而不是继续依赖人工 probe 脚本。API、Worker 和 Agent 只依赖抽象；高保真模拟器、真实 SSH 和未来正式 REST 共享同一 Run 生命周期。

当前 `SlurmBackend` 已有：

```python
submit(intent) -> SubmitReceipt
get_job(user, job_id) -> JobSnapshot
cancel(user, job_id) -> JobSnapshot
```

新实现命名为 `SshSlurmBackend`。它不需要改变 `RunService` 的核心状态机。

## 2. 分层

```text
RunService / Worker
        │
SshSlurmBackend + SshEvidenceTransport
        │
SshRelayClient                 # typed request / typed response
        │
SshRelay                       # owns a user-bound authenticated session
        │
SSH ControlMaster or Pilot Link
        │
真实 107 login node
```

LLM、Web API 和前端不得直接触碰 SSH socket。只有 Relay 有权执行 remote argv。

## 3. Session 模型

`SshSession` 是控制面元数据，不是凭据存储：

```text
session_id
portal_owner
slurm_user
target_id
state: active | auth_required | revoked | expired
authenticated_at / expires_at / last_checked_at
relay_endpoint_ref
known_hosts_fingerprint
```

禁止写入数据库的内容：密码、OTP、私钥、socket 内容、JWT、完整 SSH config。

### 3.1 两种部署模式

| 模式 | 用途 | 连接拥有者 |
|---|---|---|
| `control_plane_master` | 单用户比赛 MVP；用户在 107Pilot VM 上完成 MFA | 控制面 Relay |
| `pilot_link` | 长期多用户方案；用户设备或其私有常驻环境建立 SSH | 用户侧 Relay |

两种模式向 `SshRelayClient` 暴露相同协议。Session 过期时，一律返回 `AUTH_REQUIRED`；Worker 暂停需要远端调用的任务，保留 Run 状态和已采集 Evidence。

## 4. 受限操作协议

Relay 只接受已定义操作，不接受 shell 字符串：

| operation | 固定 remote argv 形状 | 调用者 |
|---|---|---|
| `platform_snapshot` | allowlisted `sinfo` / `scontrol show part` / `squeue` | snapshot worker |
| `prepare_run_dir` | `mkdir -p` 于 owner 的批准根 | RunService |
| `write_submission` | 上传 materialized script 到 `.107pilot/runs/<run_id>` | RunService |
| `submit_run` | `sbatch --parsable <approved-script>` | RunService |
| `get_job` | `squeue` 后回退 `sacct` 的固定 format | Worker |
| `cancel_run` | `scancel <validated-job-id>` | RunService |
| `read_log_tail` | 只读 owner/run 范围内的 stdout/stderr | Evidence worker |
| `inventory_outputs` | 有上限的文件清单 | Evidence worker |
| `read_source_window` | 仅错误位置附近、只读 | Code context service |

`entry.command` 只能出现在已 materialize、可审计的作业脚本中；它不能作为 Relay 命令拼接的一部分。

## 5. 远端目录策略

来源 Contract 的 `project.workdir` 仍是用户项目目录。107Pilot 自己生成的脚本、marker 和临时材料写在：

```text
<approved-workdir>/.107pilot/runs/<run_id>/
  submission.sbatch
  intent.json
  marker.json
```

路径必须经过 owner root policy、realpath containment 和 symlink 拒绝。每次 submit 都写入唯一 idempotency marker；网络超时后由 `squeue/sacct + marker` 进行提交对账，不能盲目二次 `sbatch`。

## 6. Job 查询与状态规范化

`SshSlurmBackend.get_job` 的逻辑：

1. 用固定格式的 `squeue -j <job_id>` 查询活跃作业；
2. 若不再活跃，以固定格式的 `sacct -X -j <job_id>` 查询终态；
3. 解析为现有 `JobSnapshot`，保留 raw state flags、exit code、reason、stdout/stderr path；
4. 无结果且会话有效时返回明确的 transport/not-found 语义，不把它映射为 `SUCCEEDED`；
5. 身份不符、路径越权、认证失效分别映射为 `SlurmAuthError`、policy error、`AUTH_REQUIRED`。

真实 107 中观察到的 QoS、分区和环境差异只能通过 `CapabilityProfile` 和 `PlatformSnapshot` 驱动，不能硬编码到 backend。

## 7. 真实 EvidenceTransport

新 `SshEvidenceTransport` 实现既有 `EvidenceTransport` protocol：

```text
probe
prepare_run_root
stat
read_text_tail
read_bytes_range
inventory
```

必须复用现有 `EvidencePolicy` 的最大文件数、最大读取字节数、排除模式、owner root 和 symlink 规则。真实后端不得将远端工作目录 mount 到应用容器后直接扫描。

第一阶段采集范围：

- materialized submission；
- `sacct` accounting 与状态；
- stdout/stderr tail；
- 合同中声明的结果文件 inventory；
- 受限环境摘要。

不要把“收集整个项目目录”作为 Evidence 功能。

## 8. API/Worker 配置接线

`ApiServiceConfig.backend` 与 `WorkerServiceConfig.backend` 增加 `real107-ssh`。两个 builder 必须由同一个 `SshRelayConfig` 构建，避免 API submit 与 Worker reconcile 使用不同用户或不同 target。

这是一条部署不变量：一次 Run 由 API 提交、由 Worker 查询状态与采集 Evidence，二者必须通过同一 transport 指向同一个 Slurm target。当前 Compose 仍分别传入 `PILOT107_API_BACKEND` 与 `PILOT107_WORKER_BACKEND`，是为了兼容不同进程的配置入口；任何 live smoke 或正式部署都必须显式把它们设为同一种后端，并共享同一 target/relay 配置（endpoint、target id 与认证引用也相同）。不能只切换其中一个，否则会制造“已提交作业在另一后端中不存在”的伪 `ORPHANED`，或因 Worker 缺少认证引用而把正常作业误记为 `AUTH_REQUIRED`。

建议配置名：

```text
PILOT107_BACKEND=real107-ssh
PILOT107_SSH_RELAY_URL=unix:///run/pilot107/ssh-relay.sock
PILOT107_SSH_TARGET_ID=real107
PILOT107_SSH_SESSION_STORE=<database-backed>
PILOT107_SSH_OWNER_ROOTS=/public/home/{user},/home/{user}
```

部署配置不写 password、OTP 或 key。`manage-code-context-ssh.sh` 是现有只读原型，不可直接升级为 API/Worker 的全局执行 socket；正式 Relay 必须按 session/owner 隔离。

## 9. 验收

同一 backend contract 测试应在：

1. in-memory（快速语义测试）；
2. 高保真模拟 Slurm（live 命令、日志、accounting、取消）；
3. 经明确授权的真实 107 私有目录（success、exit 42、cancel、auth expired）

真实验收只证明该 session 与当日平台事实，不反向修改模拟器的业务逻辑。差异写入 profile/probe artifact，再由模拟器行为测试决定是否需要模拟。
