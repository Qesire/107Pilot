# 04. 外置 Agent 与本地代码协作

## 1. Agent 的职责

107Pilot 的 Agent 是门户控制面的**算力运行 Agent**，不是完整代码仓库 Agent。

它擅长：

- 解释 Slurm、QoS、平台事实和 Contract；
- 根据 Evidence 诊断 OOM、超时、依赖、非零退出、资源浪费和调度问题；
- 形成有证据引用的恢复计划；
- 在策略与审批允许时创建派生 Contract/Run；
- 把代码问题转成发给本地代码工具的 Repair Ticket。

它不承担：完整 repo 搜索、任意文件修改、创建 worktree、安装任意依赖、执行任意 shell、保管私钥或 MFA。

## 2. 三层决策模型

```text
LLM reasoning
  → AgentActionPlan（建议，不能直接执行）
       → deterministic policy / approval / fence
            → typed backend action
                 → Run event + Evidence
```

`AgentActionPlan` 的最小字段：

```text
plan_id, run_id, owner, evidence_bundle_sha256,
actions[], risk, requires_approval, expires_at
```

允许的 action type：

```text
explain_only
contract_patch
retry_run
submit_derived_run
cancel_run
refresh_platform_snapshot
create_repair_ticket
```

`read_log_tail`、`query_job` 等是 Agent 的事实获取工具，不作为 LLM 任意命令入口。所有工具响应都进入 Evidence/trace，再供下一轮推理引用。

## 3. 与现有实现的衔接

现有 `AgentExplainService` 已将 LLM 输出绑定到 Evidence citation；`AgentAdviceService` 已有建议、审批和派生 Run 的 outbox/fencing。新实现应优先：

1. 将现有 explanation/advice 统一投影为 `AgentActionPlan` read model；
2. 将 action executor 改为调用 backend/relay，而不是新增浏览器到 shell 的通道；
3. 保留 deterministic diagnosis 和 policy 为最终执行守门人；
4. 不要求 LLM 配置才能运行规则诊断和普通门户。

当前受控 Terminal 只服务模拟器诊断命令。真实 SSH 接入后，Terminal 页面应改为 Relay 支持的只读 `query_platform/query_job/read_log_tail` 投影，而不是开放交互 shell。

## 4. 代码与制品的最小交换

### 4.1 ArtifactManifest

本地代码工具或用户可在提交前附加：

```json
{
  "revision": "git SHA or opaque revision",
  "dirty_diff_digest": "sha256:...",
  "bundle_digest": "sha256:...",
  "remote_workdir": "/approved/user/path",
  "local_test_summary": "tests passed locally",
  "disclosure": "metadata_only"
}
```

这不是上传完整仓库。107Pilot 只保存用户选择的制品标识和摘要；真实 Slurm 仍由用户既有工作目录运行代码。

### 4.2 Code context

现有只读错误位置窗口可以保留：它依据 traceback 从批准的 root 读取有限行数。它不得演化为全仓库遍历或远程写入。

代码窗口进入 LLM 前仍须：

- 明确开启；
- 限制文件、行数与字节数；
- 经过敏感值脱敏；
- 与 `run_id`、snapshot id 和 disclosure policy 关联。

### 4.3 RepairTicket

当诊断指向代码时，Agent 创建：

```text
RepairTicket
  source run / Contract / ArtifactManifest
  diagnoses + cited log snippets
  optional source windows
  requested change / resource facts / no-go constraints
  ticket state: open | resolved | abandoned
```

用户可把 ticket 交给本地 OpenCode/Codex/IDE。新的修复制品再以新的 ArtifactManifest 关联到派生 Run。门户验证的是运行结果，而不是替用户编辑代码。

## 5. LLM 与隐私策略

每一次 provider 调用必须带有数据分级：

| 级别 | 可进入外部高质量模型 | 默认 |
|---|---|---|
| 平台能力、资源事实、错误代码 | 可以 | 可用 |
| 脱敏日志摘要 | 可以，经用户/provider 策略 | 可用 |
| 受限代码窗口 | 仅 explicit opt-in | 默认关闭 |
| 完整仓库、原始数据、SSH 凭据 | 不可以 | 永不发送 |

本地小模型性能不足不能迫使用户把完整仓库送到门户。运行 Agent 的核心优势来自结构化事实、策略和平台接近性，而非在服务端复刻完整代码 Agent。

## 6. 验收场景

1. OOM：Evidence 指向资源与 batch 参数；Agent 提出受限 Contract patch，用户批准后派生 Run。
2. Python traceback：Agent 绑定日志和有限代码窗口，生成 RepairTicket；本地修复后新 Run 成功。
3. QoS/partition 失败：Agent 引用 capability profile，给出可用配置，不捏造平台事实。
4. Session 过期：Agent 显示 `AUTH_REQUIRED`，不重复提交、不假装任务失败。
5. 提示注入：日志中出现“执行此命令”文本时，Agent 将其视为 Evidence 而非指令。

