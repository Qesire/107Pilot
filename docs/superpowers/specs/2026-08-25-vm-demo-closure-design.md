# 107Pilot VM 演示闭环设计

- 日期：2026-08-25
- 状态：已确认
- 目标环境：`114.214.241.31` 上的单机 CPU VM
- 边界：正式演示环境；不是外部真实 107，也不是校园多用户生产环境

## 1. 目标

把 VM 内已经运行的 Slurm 控制器、计算节点和 accounting 服务作为
107Pilot 演示环境的正式 Slurm 对象，完成以下三条可验证闭环：

1. Agent Workspace 的 Python 校验在最终 API 镜像内通过 bubblewrap 隔离执行。
2. Agent 能创建受资源包络约束的 AgentTask，并实际产生 Slurm Run、Evidence、
   Capsule 和任务完成后的 Agent 唤醒事件。
3. 文件页允许在授权根内手工输入路径，并提供有扫描预算的文件名/相对路径搜索。

本设计不伪装成外部真实 107。页面与证据统一显示“VM 本机 Slurm（演示）”，
并继续明确 CPU-only、单用户 `alice`、无 GPU、非生产身份隔离。

## 2. VM Slurm 资源模型

VM 共 8 vCPU、约 15 GiB Linux 可见内存。Slurm 节点 `anode16` 对作业公布：

- CPU：6
- RealMemory：10240 MiB
- 分区：`CPU-RC`
- QoS：`qos_cpu_rc`
- GPU：0
- 最长运行时间：4 小时

剩余约 2 CPU、5 GiB 留给 MariaDB、slurmctld、slurmdbd、API、Worker、
pilot-agentd、Web 和反向代理。能力快照、静态 capability profile、模板兼容性、
Slurm 配置和测试断言必须使用同一组 6 CPU / 10 GiB 数值。

当前 VM 无法在 Docker/systemd 组合中为 Slurm 作业启用 cgroup/v2 任务隔离，
因此该配额是调度与准入边界，不宣称具备恶意作业级强隔离。Docker 继续限制
聚合计算节点容器；演示作业只允许由 Contract/AgentTask 产生的受限资源请求。

## 3. Sandbox 运行时闭环

### 3.1 镜像依赖

最终 API/Worker 共用应用镜像必须安装固定发行版来源的 `bubblewrap`，并继续以
UID/GID `10700:10700` 运行。不得因为演示方便而回退到宿主 Shell、`shell=True`
或关闭 Sandbox 的 fail-closed 检查。

### 3.2 容器安全与验证

Sandbox 保持：

- argv-only，首版只允许 `python`/`python3`；
- 无网络 namespace；
- 清空环境变量；
- 只读系统目录、可写 Workspace；
- CPU 时间、地址空间、进程数、文件大小和输出字节限制；
- Workspace/ChangeSet owner 与 snapshot 绑定。

验收必须在最终应用镜像、非 root 用户、compose 的 `cap_drop: ALL`、
`no-new-privileges` 和只读根文件系统条件下执行真实 bubblewrap 命令。
宿主机安装了 bubblewrap 不能替代该测试。

## 4. 文件路径栏

每个文件窗格的面包屑区域增加可切换的地址栏：

- 点击路径区域或使用键盘快捷键进入编辑状态；
- Enter 导航，Escape 恢复原路径；
- 支持绝对路径以及相对当前目录的路径；
- 规范化 `.`、`..` 和重复 `/`；
- 最终路径必须位于当前 owner 的授权根内；
- 越界路径在前端先拒绝，后端仍独立执行 owner/path 授权；
- 不存在、非目录、无权限和连接不可用显示不同错误；
- 成功导航进入既有 back/forward 历史。

地址栏不扩大授权边界。当前演示用户 `alice` 只能访问
`/public/home/alice`；未来切换连接 profile 时由 profile 提供 owner roots。

## 5. 受限文件搜索

沿用既有《文件发现、可靠传输与 Slurm 集群连接设计》的搜索语义，实现：

```http
GET /api/v1/files/search
  ?root=/public/home/alice
  &q=model
  &kind=file|directory|all
  &size_min=0
  &size_max=10737418240
  &mtime_from=<timestamp>
  &mtime_to=<timestamp>
  &limit=100
  &cursor=<opaque>
```

约束：

- 只匹配文件名和相对路径，不搜索文件内容；
- 不区分大小写；
- 不跟随符号链接，不返回特殊文件；
- 单页最多 100 条；
- 同时限制扫描项数和执行时间；
- 达到预算时返回 `incomplete=true` 和绑定 owner/root/query 的不透明游标；
- 每次请求重新授权 root；搜索结果用于后续操作时再次授权具体路径；
- 无权限目录只进入 `warnings`，不得泄漏子项。

首个实现使用当前 command-gateway 的固定远端 projection，使 API 不接收任意
Shell 或远端源码。文件页提供搜索框、基本过滤器、分页继续、结果路径和
“在当前/新窗格中打开”操作。

## 6. AgentTask 到 VM Slurm 的闭环

Agent 的 Project profile 继续通过 `validation_schedule` 创建 AgentTask；不新增
允许浏览器任意构造集群命令的通用接口。完整数据流为：

```text
Project + Workspace snapshot + approved resource envelope
  -> Agent turn
  -> validation_schedule capability tool
  -> durable AgentTask + outbox
  -> Worker prepare/submit Run
  -> VM Slurm sbatch/squeue/sacct
  -> Evidence + Capsule
  -> AgentTask terminal result
  -> ready outbox
  -> follow-up Agent turn
```

AgentTask 请求不得超过 6 CPU / 10240 MiB / 0 GPU / 4 小时；Workspace snapshot
变化、owner 不一致、缺少资源包络或 Sandbox 未通过时必须 fail closed。

演示页面至少展示任务 ID、状态、关联 Run、请求资源、Slurm job ID、Evidence、
失败原因和后续 Agent 事件。取消请求必须传播到关联 Run/Slurm job。

## 7. 发布与部署修复

CPU-RC 离线发布包必须包含 compose 实际需要的全部 11 个服务镜像，其中
`pilot-agentd` 使用同一 release revision 的固定标签并进入
`RELEASE_MANIFEST.json`、`images.txt`、导入脚本和运行镜像绑定检查。

systemd 安装器的临时文件清理不得引用已离开作用域的局部变量。安装器需以
退出码 0 完成，保留既有 `/etc/pilot107/cpu-rc.env`，并把 unit 工作目录绑定到
新 release 目录。

重部署继续保留数据库卷、Evidence/Capsule 卷、远端密钥配置和旧 release
目录。不得执行 `down -v` 或删除旧目录。

## 8. 错误处理

- bubblewrap 缺失或 namespace 不可用：返回稳定 Sandbox 错误码并禁止降级执行。
- 搜索预算耗尽：返回部分结果、`incomplete=true` 和下一游标，不返回 500。
- 搜索根越界：403；路径不存在：404；远端执行失败：502。
- AgentTask 调度失败：保留任务、Run 和 outbox 审计状态，按既有有界重试处理。
- Slurm 不可用：任务进入可解释失败或 `AUTH_REQUIRED`/连接错误状态，不伪造成功。
- 控制面资源不足：拒绝超过 capability/QoS 的新任务，不动态扩大 Slurm 节点。

## 9. 验收标准

### 9.1 自动化

1. 最终 API 镜像中 `bwrap` 存在，真实 Sandbox 成功、超时、无网络和环境清理测试通过。
2. Slurm 配置、capability profile、页面快照和 QoS 均报告 6 CPU / 10 GiB。
3. 地址栏覆盖绝对路径、相对路径、历史、越界、缺失目录和无权限错误。
4. 搜索覆盖匹配、过滤、游标、预算、符号链接、owner 越界和 warnings。
5. AgentTask 覆盖创建、幂等、调度、取消、失败、Run 关联、Evidence 和 Agent 唤醒。
6. 发布包检查确认 11 个服务齐全并逐个匹配 manifest digest。

### 9.2 VM 现场验证

1. `/files` 可手工输入授权路径并打开目录。
2. 创建嵌套测试文件后，文件名与相对路径搜索均能定位它。
3. Agent 创建 Workspace、应用 ChangeSet 并通过 bubblewrap Sandbox。
4. Agent 调度一个 1 CPU/512 MiB 的 validation task。
5. 任务获得真实 VM Slurm job ID，经历 Pending/Running/终态。
6. Evidence 和 Capsule ready，Agent 收到任务完成后的 follow-up turn。
7. 重启 stack 后上述任务、Run、Evidence 和搜索授权状态仍一致。
8. 11 个容器健康，外部 HTTPS 首页与 healthz 返回 200，systemd enabled/active。

## 10. 明确不做

- 不接入外部 107 SSH/MFA。
- 不把 VM 本机 Slurm 描述为校园真实 107。
- 不增加 GPU、跨学生身份、多租户生产隔离或任意远端 Shell。
- 不实现文件内容全文搜索、索引服务、SFTP 大文件迁移或批量下载传输任务。
- 不删除现有演示数据或旧发布目录。
