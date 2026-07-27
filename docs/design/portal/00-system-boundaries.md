# 00. 系统边界与不变量

## 1. 组件边界

```text
┌──────────────────────── 用户控制的环境 ────────────────────────┐
│ 完整仓库、私有数据、本地 OpenCode/IDE、worktree、测试、热修复包 │
└───────────────┬───────────────────────────────────────────────┘
                │ 制品摘要、远端工作目录、可选受控代码片段
┌───────────────▼──────── 107Pilot 控制面 ───────────────────────┐
│ Web BFF / API / Worker / Postgres(or SQLite migration path)    │
│ Contract、Run、Evidence、Capsule、市场、Agent、策略、审计       │
│                 SSH Relay / Pilot Link protocol                │
└───────────────┬───────────────────────────────────────────────┘
                │ 受用户身份约束的固定 Slurm 操作
┌───────────────▼──────── Slurm 执行环境 ─────────────────────────┐
│ 高保真模拟 Slurm              或           真实 107 Slurm       │
│ sbatch / squeue / sacct / scancel / logs / 用户工作目录         │
└────────────────────────────────────────────────────────────────┘
```

控制面是门户和 Agent 的部署位置；它不是计算节点，也不是完整代码仓库的副本。真实 107 仅承担用户的 Slurm 作业和用户工作目录，不要求安装 107Pilot 组件或取得 sudo。

## 2. 模拟器不变量

模拟器是 `SlurmBackend` 的一种实现，不是产品功能分支。以下不变量必须由测试守住：

```text
同一 Contract
→ 相同的 API、Run 状态机、Evidence 任务、市场发布规则、Agent 动作协议
→ 仅 backend provenance 与连接配置不同
```

允许差异：调度耗时、资源规模、节点名称、由 capability profile 标明的已知行为差异。

不允许差异：

- 前端基于 `simulator` 显示另一套市场或简化工作流；
- 模拟成功跳过 Evidence、Capsule、owner、发布确认或审批；
- Agent 在模拟器中获得真实后端不会给予的任意 Shell 权限；
- 将模拟器结果表述为真实 107 已验证。

`backend_kind`、profile digest、snapshot source 是审计字段，不能成为业务状态判断的替代品。

## 3. 信任边界

| 对象 | 可以得到 | 不可以默认得到 |
|---|---|---|
| 浏览器用户 | 自己的 Contract、Run、Evidence read model、市场公开信息 | 其他用户的 Run、远端服务器路径、凭据 |
| 107Pilot Agent | 脱敏的 Run 事实、受限日志、明确授权的代码窗口 | 完整仓库、密钥、OTP、任意远程命令 |
| SSH Relay | 已批准、固定形状的 Slurm 操作 | LLM 原始提示、浏览器任意命令、跨用户 socket |
| 模拟/真实 Slurm | materialized script 与用户工作目录 | 107Pilot 数据库、LLM 密钥、其他用户 Evidence |
| 市场浏览者 | 发布者选择公开的元数据和摘要 | 发布者未选择公开的代码、数据、日志、路径 |

日志、脚本、代码注释和作业输出均视为**不可信数据**：它们可以被引用为 Evidence，但不能转化为 Agent 指令或 SSH 命令。

## 4. 身份与授权原则

1. Web identity、门户 owner、SSH session owner、Slurm user 必须显式绑定，不能由请求体中的 username 推断。
2. 真实平台 MVP 可以是一个经用户确认的单用户 session；这不是校园多用户生产认证。
3. SSH 密码、OTP、私钥和短期 token 不进入数据库、Evidence、Capsule、日志或 LLM context。
4. Session 失效时返回 `AUTH_REQUIRED`；不得由 Worker 自动发起交互认证，也不得把失败误判为作业失败。
5. 每个外部副作用都必须可关联到 `run_id`、`owner`、request id、批准决策和 backend provenance。

## 5. 非目标

当前设计明确不做：

- 在门户中实现完整远程 IDE、worktree 编码或通用 OpenCode；
- 全平台运维、节点温度、管理员告警或调度器 daemon 控制；
- 自动判断市场条目的代码、数据和私有依赖是否能被其他人运行；
- 以“管理员权限”绕过用户的真实 107 权限；
- 为普通市场分享增加课程审核、依赖打包或复现承诺。

## 6. 门户完成时的用户闭环

```text
浏览市场成功作业
→ 采用为自己的 Contract
→ 在 Studio 调整参数并预检
→ 提交到统一 SlurmBackend
→ Worker 对账、采证、诊断
→ 成功后由本人勾选发布，或失败后由 Agent 生成修复建议
→ 本地完成代码修复后再次提交
```

这个闭环在高保真模拟 Slurm 上完整验收；切换真实 107 时只替换连接、身份、能力画像和文件访问 transport。

