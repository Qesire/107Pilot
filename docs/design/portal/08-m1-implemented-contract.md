# 08. M1 已实现契约与 live 验收边界

状态：2026-07-26 本地实现与 contract 验证完成；真实 107 的授权会话 live
矩阵待执行。本文只描述代码和已取得的证据，不把人工 probe 当成正式 backend
验收。

## 1. 已实现的控制边界

`SubprocessSshRelayClient` 只复用部署配置中的既有 OpenSSH
ControlMaster。每次调用先执行 BatchMode 的 `-O check`，不会在 API/Worker
后台触发密码或 MFA prompt。

Relay 请求是 structured argv。允许的 operation 仅覆盖：

- `sbatch`、`squeue`、`sacct`、`scontrol`、`scancel`；
- owner root 下的 `realpath`、`mkdir`、`tee`、`chmod 600`；
- baseline 与受限环境采集所需的固定只读命令；
- 由应用内置、不可被 API 替换源码的 Evidence Python projection。

任意 `bash -c`、未定义命令、owner 不匹配、路径越界、NUL、换行和父目录穿越
均在发起 SSH 前拒绝。

数据库表 `ssh_connection_sessions` 只保存：

```text
connection_id / portal_owner / slurm_user / target_id
state / status_code / checked_at / authenticated_at / expires_at / revision
```

它不保存 hostname、ControlPath、password、OTP、私钥、agent socket 或 SSH
config。公开 API 也不返回这些字段。

## 2. Slurm backend

`SshSlurmBackend` 实现现有 `SlurmBackend`，没有复制 Run 状态机。

materialized 文件位于：

```text
<approved-workdir>/.107pilot/runs/<run-id>/
  submission.sbatch
  intent.json
```

`intent.json` 仅包含 owner、job name、idempotency key 和 script SHA-256。
提交使用固定 `sbatch --parsable` 形状。超时恢复通过 owner、JobName 和提交时间
窗口查询 `sacct`；一个候选绑定，零个候选按现有 outbox policy 处理，多个候选
进入 `SUBMISSION_UNCERTAIN`。reconcile receipt 保留 `command` strategy 和
`real107-ssh` provenance，不再错误标记为 REST。

## 3. Evidence transport

`SshEvidenceTransport` 完整实现现有 protocol：

```text
probe / prepare_run_root / stat / read_text_tail /
read_bytes_range / inventory
```

远端 projection 会再次执行 realpath containment、symlink/special-file
拒绝、最大深度、最大文件数、单文件读取上限、总 inventory 字节数和排除规则。
Worker 不 mount 远端目录，也不扫描 owner root 之外的内容。

## 4. API、Worker 与 Web

共同配置前缀：

```text
PILOT107_SSH_CONNECTION_ID
PILOT107_SSH_TARGET_ID
PILOT107_SSH_TARGET
PILOT107_SSH_CONTROL_PATH
PILOT107_SSH_KNOWN_HOSTS_FILE
PILOT107_SSH_PORT
PILOT107_SSH_PORTAL_OWNER
PILOT107_SSH_SLURM_USER
PILOT107_SSH_OWNER_ROOTS
```

`PILOT107_BACKEND=real107-ssh` 可同时作为 API/Worker 默认值；显式的
process-specific backend 仍优先。正式 Compose overlay 会同时把 API 和 Worker
切换到同一 target/session/root 配置。

HTTP：

```text
GET  /api/v1/platform/connections
POST /api/v1/platform/connections/{connection_id}/check
```

前端在 topbar、所有页面共用的 action banner 与 Cluster 页面消费同一 query。
因此 Run 和 Agent 页面不会出现互相矛盾的认证提示。Run read model 还返回：

```json
{
  "backend": {
    "kind": "real107-ssh",
    "target_id": "real107"
  }
}
```

## 5. 已验证证据

- SSH Relay owner、argv、path、auth-required contract；
- 私有 run directory materialization 与 JobName reconciliation；
- SSH Evidence tail、range、inventory、排除规则和 symlink escape；
- connection metadata owner scope 与 API 安全 payload；
- API/Worker 共用环境解析；
- Python ruff、mypy、相关 pytest；
- TypeScript typecheck、Vitest 与 production build。

## 6. 尚不能宣称完成的 live acceptance

下列四项必须通过既有 MFA-authenticated session，在明确批准的个人目录中执行：

1. success Run；
2. exit 42 Run；
3. running Run cancel；
4. ControlMaster 缺失时的 `auth_required`。

执行结果还必须证明 API submit 与 Worker reconcile/Evidence 使用同一个 target。
在此之前，M1 状态保持“代码完成、live acceptance 待完成”，不得把旧
`smoke-real107-ssh-jobs.sh` 的人工路径当作新 backend 的替代证明。
