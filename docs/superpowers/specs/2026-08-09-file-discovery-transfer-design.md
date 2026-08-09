# 107Pilot 文件发现、可靠传输与 Slurm 集群连接设计

- 日期：2026-08-09
- 状态：设计已逐节确认，等待文档审阅
- 范围：文件名/路径搜索、批量下载与归档、5GB+ 大文件传输、集群文件连接、文件与作业提交一致性、本地同构模拟验收
- 验收环境：本地 Docker Slurm 模拟环境优先；远程 VM 不作为本阶段前提

## 1. 背景与目标

107Pilot 已合入在线文件系统前端、基础文件操作和可恢复上传后端，但比赛要求中的文件管理仍缺少完整纵向闭环：

1. 文件列表存在，但没有受预算约束的递归搜索。
2. 单文件读取存在，但没有面向批量选择的异步归档、断点下载和制品生命周期。
3. 当前上传实现包含自研 tus 协议处理，尚未把成熟的数据面实现与 107Pilot 的授权策略明确分离。
4. 5GB 以上模型权重会放大浏览器内存、32 位偏移、重复暂存、断线重传和磁盘配额问题。
5. 文件管理、上传落点和 Slurm 作业提交尚未由同一个集群连接与路径模型约束，可能出现“脚本提交成功但数据不在计算节点可见目录”的错误。
6. 当前本地模拟器具备共享 `/public` 和真实 Slurm 命令行为，但应用主要经 command-gateway 或共享卷访问，没有验证真实 SSH/SFTP 边界。

本设计完成以下闭环：

```text
受限文件搜索
  → 选择文件/目录
  → 直接下载或异步归档
  → HTTP Range 断点下载
  → TTL 与配额清理

浏览器 tus 上传
  → 107Pilot 暂存
  → SFTP 断点传入集群共享文件系统
  → 完整性校验与原子提交
  → 形成可复用 ClusterAsset
  → 冻结到 RunInputBinding
  → 提交 Slurm 作业
  → 有界读取日志与结果
```

## 2. 范围边界

### 2.1 本阶段必须覆盖

- 按文件名和相对路径进行递归搜索。
- 按文件类型、大小和修改时间过滤。
- 多选文件/目录后创建异步下载任务。
- 单文件直接下载；多文件默认生成 ZIP64；支持显式 `tar`/`tar.gz`。
- 标准 HTTP Range、ETag、If-Range、206 和 416 行为。
- 5GB+ 权重文件上传、传入集群、作业读取、传出集群和下载恢复。
- 比赛阶段将一个 107Pilot 门户学生一对一绑定到该学生自己的 POSIX/Slurm 账号，同时保留门户身份与集群身份两个显式字段。
- SSH 控制通道、SFTP 文件通道和 Evidence 通道共享同一个连接定义。
- 本地 Docker 环境先完成同构 SSH/SFTP/Slurm 测试。

### 2.2 明确不做

- 文件内容全文搜索或索引服务。
- 浏览器直接访问 Slurm、slurmrestd、SSH 或 SFTP。
- 把 WebDAV、SFTPGo、rclone 或 Nextcloud 整套引入并替换现有文件产品界面。
- 在集群侧安装 107Pilot Gateway 作为比赛前置条件。
- 把集群共享文件系统直接挂载进应用容器作为正式数据面。
- 把一个真实学生账号共享给多个门户身份，或把比赛账号当作跨学生服务账号。
- 依赖当前不可用的远程 VM 完成本阶段验收。

课程批改、四类领域模板内容和初学者通用问答继续按本轮约定暂缓；它们不属于本规格。

## 3. 成熟方案采纳决策

107Pilot 只保留身份、授权、路径策略、任务编排、审计和配额；传输协议与字节发送优先采用成熟实现。

| 能力 | 选定方案 | 107Pilot 负责的部分 |
|---|---|---|
| 浏览器可恢复上传 | tus-js-client + 官方参考实现 tusd | pre-create 授权、目标映射、配额、完成事件、最终落盘 |
| 应用节点到集群文件传输 | OpenSSH SFTP；能力探测通过时可选 rsync | 任务状态、路径授权、摘要、重试、审计 |
| 本地下载数据面 | Nginx `X-Accel-Redirect` + HTTP Range | 用户授权、制品权限、短时下载引用 |
| 多文件归档 | libarchive/bsdtar 固定 argv | 选择清单、格式策略、配额和生命周期 |
| 下载文件名 | RFC 6266 `Content-Disposition` | 安全文件名生成 |
| 远程作业控制 | 当前为受限 SSH Slurm CLI；未来可替换为 slurmrestd | Run 状态机、幂等提交、身份和策略 |

当前自研 tus 服务作为迁移兼容路径保留，但设计目标是把 `/api/v1/files/tus` 反向代理到 tusd。长耗时远端复制不得在 tusd post-finish hook 中执行；hook 只发布完成事件，由 107Pilot Transfer Worker 接管。

WebDAV、SFTPGo 和 rclone 作为后续后端适配参考，不作为首版默认路径。直接引入它们会增加第二套认证和路径授权边界，而不会替代 107Pilot 的作业、资产和审计模型。

## 4. 文件搜索

### 4.1 API

```http
GET /api/v1/files/search
  ?root=/approved/root
  &q=model
  &kind=file|directory|all
  &size_min=0
  &size_max=10737418240
  &mtime_from=<timestamp>
  &mtime_to=<timestamp>
  &limit=100
  &cursor=<opaque>
```

### 4.2 语义与约束

- `q` 对文件名和相对路径执行不区分大小写的子串匹配。
- 不搜索文件内容。
- 不跟随符号链接，不返回特殊文件。
- `kind` 支持文件、目录或全部。
- 每页最多返回 100 条。
- 搜索设置扫描项数和执行时间预算；达到预算后返回 `incomplete=true` 与下一游标。
- 游标不暴露服务器路径遍历状态，并绑定 owner、连接、根目录和过滤条件。
- 无权限目录进入 `warnings`，不把越权路径内容返回给用户。
- 用户基于搜索结果创建传输任务时，服务必须重新解析并授权路径，不能信任旧结果。

本地文件由 `FileDiscoveryService` 扫描；远端集群文件由固定、不可由 API 替换源码的远端 projection 执行。两者输出相同的分页模型。

## 5. 下载任务与归档

### 5.1 API

```http
POST   /api/v1/files/transfers
GET    /api/v1/files/transfers
GET    /api/v1/files/transfers/{transfer_id}
GET    /api/v1/files/transfers/{transfer_id}/events
HEAD   /api/v1/files/transfers/{transfer_id}/download
GET    /api/v1/files/transfers/{transfer_id}/download
POST   /api/v1/files/transfers/{transfer_id}/cancel
DELETE /api/v1/files/transfers/{transfer_id}
```

创建请求：

```json
{
  "paths": ["cluster://connection/path-a", "cluster://connection/path-b"],
  "format": "auto",
  "request_key": "owner-provided-idempotency-key"
}
```

`format` 允许 `auto`、`direct`、`zip`、`tar` 和 `tar_gz`。

### 5.2 自动格式策略

- 单个普通文件：`direct`。
- 多文件或目录：默认 ZIP64。
- 已压缩类型占所选字节数至少 70%：ZIP STORE，避免无效重复压缩。
- 其他多选：ZIP DEFLATE。
- 用户显式选择 `tar` 或 `tar.gz` 时按请求执行。
- 所有归档命令使用固定 argv，不接受用户 Shell 字符串。

### 5.3 TransferTask

任务至少保存：

- `transfer_id`、owner、`connection_id`、版本和幂等键；
- 原始选择、冻结后的 resolved manifest；
- 文件数、目录数、逻辑总大小；
- 请求格式、实际格式和策略理由；
- 源 fingerprint；
- 制品路径、大小、摘要、ETag 和 Content-Type；
- 创建、开始、完成、过期时间；
- 错误码、可恢复性和事件序列。

状态机：

```text
queued → scanning → packing → ready → expired
             │          │
             ├──────────┴→ failed
             └───────────→ cancelled
```

集群跨机复制使用扩展状态：

```text
queued → preparing → transferring → verifying → committing → ready
                       │
                       └→ paused_auth
```

Worker 在开始打包或复制前冻结 manifest；每个源在实际读取前重新 `stat`。源发生变化时失败为 `SOURCE_CHANGED`，不能生成混合时间点的成功制品。

### 5.4 下载制品

- 本地单文件直接授权后通过受保护的 Nginx internal location 发送，不创建副本。
- 远端集群文件先下载成不可变本地制品，再由 Nginx 发送。
- 多文件在完整归档结束后才开放下载，以支持任意 Range 恢复。
- 制品摘要作为强 ETag；`If-Range` 失配时回退完整响应。
- 默认 TTL 为完成后 24 小时，部署可配置。
- 清理任务获取租约；存在活跃下载时不得删除制品。
- 删除 TransferTask 只删除平台管理的制品和元数据，不删除用户源文件。

## 6. 5GB+ 大文件硬约束

### 6.1 通用约束

- 浏览器不得使用 `fetch → Blob → save` 下载大文件，必须使用原生下载或外部下载工具。
- 不允许 base64、整文件 JSON、整文件内存缓冲或分片合并后的第二份完整暂存。
- 文件大小、Range、SFTP offset、累计字节和数据库字段使用 64 位整数；Python 内部使用无窄化转换的 `int`。
- 所有关键测试覆盖 `2^32-1`、`2^32` 和 `2^32+1`。
- 单文件大小、用户暂存配额和制品配额全部配置化。

### 6.2 浏览器上传

```text
browser tus-js-client
  → Nginx /api/v1/files/tus
  → tusd local filestore
  → 107Pilot finalize task
```

- tusd 负责协议、锁、offset 和存储。
- 107Pilot pre-create hook 校验 owner、目标、单文件限制和暂存配额。
- post-finish hook 只产生事件，不执行远端复制。
- 同文件系统落盘使用原子 rename。
- 大于等于 1GiB 的上传建议 64MiB 分片、`parallelUploads=1`。
- 小文件可使用 8–16MiB 分片。
- 本地 filestore 不使用会产生最终拼接副本的并行 concatenation。

### 6.3 集群间传输

- SFTP 基线使用唯一 `<transfer_id>.part` 文件，并支持 `reput`/`reget`。
- rsync 只有在能力探测确认远端版本可用时才能成为优化路径，不能作为硬依赖。
- 恢复前确认 `.part` 属于同一任务、长度不超过预期，并读取比较最后一个已完成数据块。
- 完成后在远端执行固定摘要程序计算 SHA-256；摘要一致后原子 rename。
- SSH/MFA 失效时保留本地暂存和远端 `.part`，任务进入 `paused_auth`。
- 恢复认证后从已验证 offset 继续，禁止后台尝试密码、OTP 或无限重连。

### 6.4 大文件归档与空间

- ZIP 输出始终启用 ZIP64。
- 归档输出过程中同步计算摘要，不为制品再执行一次本地全量读取。
- 归档前检查用户制品配额与磁盘余量。
- 本地直接下载不占归档暂存配额。
- 已冻结的远端单文件只暂存一次；浏览器 Range 直接读取该不可变缓存。

## 7. 107Pilot 与 Slurm 集群的连接

### 7.1 身份模式

选定“比赛单学生一对一绑定、未来扩展为每名学生各自绑定”的兼容模式：

- `portal_owner` 是当前参赛学生的 107Pilot 身份。
- `cluster_user` 是同一名学生自己的 POSIX/Slurm 用户名。
- 比赛配置中两者是一对一关系，但必须保留为独立字段，以便显式审计身份映射并适配名称不一致的环境。
- 一个真实学生账号不得同时绑定给多个门户身份。
- 同一学生通过终端等方式提交的非 107Pilot 作业仍属于该学生，但必须标记为外部作业，不能伪造 Run 关联。
- 已创建 Run 和 TransferTask 固定其 `connection_id`，不能随默认集群配置漂移。
- 未来扩展多学生时，每个门户身份都必须拥有独立的集群身份映射和认证引用，不改变 Run、Asset 或 Transfer 状态机。

### 7.2 拓扑方案比较

#### 方案 A：SSH 双通道（选定）

- 控制通道：受限 SSH Slurm CLI。
- 文件通道：SFTP，能力允许时可选 rsync。
- 两者复用同一个 OpenSSH ControlMaster、目标、集群账号和认证状态。
- 不需要集群管理员部署新服务，符合当前项目实现和比赛约束。

#### 方案 B：slurmrestd + SFTP（后续控制通道）

- 作业控制使用 Slurm REST，文件仍使用 SFTP。
- 需要集群管理员提供 slurmrestd、JWT 或认证代理，并建立可信网络与 TLS 边界。
- 作为未来 `ControlTransport` 替换目标，不阻塞首版。

#### 方案 C：集群侧 Pilot Gateway（长期候选）

- 在登录节点附近部署受控服务，本地访问 Slurm 与共享存储，对外提供 mTLS API。
- 性能和治理更强，但要求集群侧部署权限，不是比赛前置条件。

共享文件系统跨边界直挂不作为第四种正式方案，因为它会扩大应用节点权限、耦合部署并绕开传输审计。

### 7.3 ClusterConnector

```text
107Pilot API / Worker / Transfer Worker
                 │
                 ▼
          ClusterConnector
        ┌────────┼───────────┐
        ▼        ▼           ▼
 ControlTransport FileTransport EvidenceTransport
  SSH CLI         SFTP        bounded projection
        └────────┬───────────┘
                 ▼
          Slurm login endpoint
                 │
                 ▼
        shared cluster filesystem
                 │
                 ▼
            compute nodes
```

`ClusterConnectionProfile` 至少包含：

```text
connection_id
cluster_id
auth_mode
auth_session_ref
portal_owner
cluster_user
workspace_root_template
shared_roots
control_transport
file_transport
target_id
capability_snapshot
```

API、Worker、Transfer Worker 和 Evidence Worker 必须解析到同一个 profile 版本。配置不保存密码、OTP、私钥或完整 SSH socket 内容。

### 7.4 指令通道

浏览器只发送结构化操作，不能发送远端 Shell。固定操作映射为：

| 平台操作 | 远端动作 |
|---|---|
| 准备工作目录 | 根目录约束下的 `mkdir`/`stat` |
| 写入提交文件 | SFTP 临时文件 + 原子 rename |
| 提交作业 | `sbatch --parsable <approved-script>` |
| 查询状态 | 固定格式 `squeue`，终态回退 `sacct` |
| 取消作业 | `scancel <validated-job-id>` |
| 日志读取 | 指定 owner/run 下的有界字节范围 |
| 文件搜索 | 固定远端 projection，带扫描预算 |

用户的命令、环境和参数只能进入已 materialize、可审计的 `submission.sbatch`，不能参与 SSH remote argv 拼接。

### 7.5 文件和数据通道

上传：

```text
browser → tusd staging → SFTP remote .part → verify → atomic rename
```

下载：

```text
remote shared file → SFTP local .part → immutable artifact → Nginx Range
```

正式输入、脚本、权重和输出必须落到登录节点与计算节点都可见的共享根，不能使用应用节点本地目录或登录节点 `/tmp`。每次提交显式设置已验证的 `--chdir`。

### 7.6 Evidence 通道

- 作业状态来自 `squeue`/`sacct`，不从日志文本猜测。
- stdout/stderr 允许实时有界读取，并明确标记为仍可能增长。
- 普通结果下载必须先冻结为不可变制品。
- Evidence inventory 有文件数、总字节、单文件读取和执行时间上限。
- 不收集整个项目目录，不跟随符号链接。

## 8. 文件资产与作业提交一致性

### 8.1 ClusterAsset

文件完成集群落盘后形成资产记录：

```text
asset_id
portal_owner
connection_id
cluster_user
remote_path
kind: file | directory | weight | dataset
size
sha256
state
created_at
verified_at
```

5GB+ 权重按摘要保存：

```text
<shared-root>/.107pilot/users/<portal-owner>/weights/<sha256>/<filename>
```

同一门户用户、同一连接、同一摘要可复用资产，不为作业克隆重复上传。Contract 和 Run 引用 `asset_id`，只有提交服务能在最后阶段把它解析为集群路径。

### 8.2 提交流程

```text
TransferTask ready
  → ClusterAsset ready
  → 冻结 RunInputBinding
  → materialize submission.sbatch
  → SFTP 写入脚本临时文件
  → 原子 rename
  → 重新 stat/verify 所有输入
  → sbatch
  → 保存 job_id 与 idempotency marker
```

约束：

- 任一输入资产未 `ready` 时不得 `sbatch`。
- 提交脚本只引用本次冻结的 `RunInputBinding`。
- 已被非终态 Run 引用的资产不能物理删除。
- 资产删除使用引用计数和延迟清理。
- `sbatch` 响应不确定时通过 marker 与 Slurm 查询对账，禁止盲目重复提交。
- 源文件传输中变化时返回 `SOURCE_CHANGED`。

### 8.3 实时日志例外

实时 stdout/stderr 可按范围读取，不要求先生成完整制品。用户请求“下载完整日志”时仍走冻结制品流程。

## 9. 单学生账号与未来多用户边界

比赛模式只允许一个门户学生绑定其自己的 POSIX/Slurm 账号。该学生的文件、Slurm association、配额、107Pilot Run 和外部作业处于同一个真实账号边界内；107Pilot 不创建跨学生共享服务身份。

身份规则：

- 连接、文件、Run、Evidence 和 TransferTask 都绑定同一 `portal_owner ↔ cluster_user ↔ connection_id` 映射。
- 只有该门户 owner 能使用连接；演示用用户切换不得改变真实连接 owner。
- 107Pilot 管理的作业通过 `connection_id + job_id` 关联 Run。
- 同一学生账号下无法关联 Run 的作业标记为“外部作业”，可以计入该学生资源态势，但不能获得伪造的 Contract、Evidence 或 lineage。

正式扩展到多名互不信任的学生时，必须满足其一：

1. 每个门户用户映射到独立 POSIX/Slurm 身份；或
2. 集群提供具有独立文件权限边界的容器或执行身份。

不得通过把多个学生映射到同一 Unix UID，再依赖随机目录名、API 检查或同 UID 下的 `0700` 目录来冒充隔离。

## 10. 本地同构模拟

### 10.1 当前缺口

当前 `login-node-sim` 不运行 SSHD/SFTP，应用主路径经 command-gateway 或直接共享卷访问。该形态可验证 Slurm 行为，但不能验证批准的真实连接边界。

### 10.2 新的可选 Compose 覆盖层

```text
pilot107-api / worker / transfer-worker
             │ shared ControlMaster socket
             ▼
       ssh-session-sim
             │ SSH exec + SFTP
             ▼
      cluster-access-sim
       ├── sshd/SFTP
       ├── Slurm CLI
       ├── alice student user
       └── shared /public volume
                   │
          worker-1 / worker-2
```

- `cluster-access-sim` 使用与 Slurm 登录环境一致的镜像和共享卷。
- 模拟密钥与固定 known_hosts 只能用于本地测试。
- `ssh-session-sim` 以应用 UID 建立和持有 ControlMaster。
- API、Worker 和 Transfer Worker 只看到 socket 引用。
- 同构 profile 下应用容器不挂载 `/public`。
- `alice` 作为一对一比赛载体身份，同时是门户 owner 和模拟 Slurm 学生账号。
- `bob` 保留为独立学生与负面权限/QoS fixture，不得复用 Alice 的连接或目录。
- 终止 ControlMaster 用于模拟 MFA/会话失效；重建后任务必须恢复。

## 11. 测试与验收

### 11.1 分层矩阵

| 层级 | 内容 | 普通 CI |
|---|---|---|
| 单元测试 | 路径、身份、状态机、幂等、64 位 offset | 是 |
| Connector 契约 | Fake 与 SSH/SFTP 实现运行同一套契约 | 是 |
| Docker 闭环 | 上传、远端校验、Slurm 作业、结果和下载 | 是 |
| 5GB+ 实传 | 中断恢复、摘要、内存和磁盘 | 发布前专项 |

### 11.2 普通 CI 大文件边界

- 创建逻辑大小 6GiB 的稀疏文件。
- 在 `2^32-1`、`2^32`、`2^32+1` 附近写入标记。
- 经 SFTP 读取和写入这些区间。
- 验证 API、数据库、事件和进度不存在窄化。
- 验证 HTTP `HEAD`、跨 4GiB Range、206、416 和 If-Range 失配。
- 生成包含大于 4GiB 条目的 ZIP64。
- 提交 Slurm 作业从计算节点读取尾部标记，证明输入位于共享存储。

### 11.3 发布前 5GB+ 专项

1. 生成确定性内容的 5.5GiB 文件。
2. 通过 tus 上传到应用暂存。
3. SFTP 传输约 1GiB 后终止 ControlMaster。
4. 验证任务进入 `paused_auth`。
5. 重建模拟会话并从已验证 offset 恢复。
6. 验证远端 SHA-256。
7. 提交 Slurm 作业读取该权重并产生结果。
8. 将结果和权重测试制品传回应用节点。
9. 通过 HTTP Range 恢复下载并验证摘要。
10. 验证暂存、`.part`、租约和 TTL 清理。

专项测试开始前要求至少 15GiB 可用临时空间。环境不足必须明确报告未执行，不能记录为通过。

### 11.4 故障矩阵

- SSH 在上传或下载中断。
- ControlMaster 失效、认证恢复和会话撤销。
- API、Worker 或 Transfer Worker 重启。
- 同一请求重复投递。
- `.part` 属于其他任务或已被修改。
- 源文件在传输中变化。
- 上传成功但 `sbatch` 失败。
- `sbatch` 响应超时但作业实际创建。
- 作业运行时用户删除资产逻辑引用。
- owner 越权访问。
- 本地或远端磁盘不足。
- 取消、下载租约和 TTL 清理竞争。

### 11.5 固定验收顺序

```text
单元/契约
→ 本地 SSH/SFTP 模拟
→ 本地 Slurm 完整闭环
→ 本地 5GB+ 专项
→ 远程 VM 兼容验证（环境恢复后）
```

远程 VM 不可用不阻塞前四个阶段的开发与正式验收。

## 12. 迁移顺序

该规格应拆成实现计划中的独立增量，推荐顺序如下：

1. 定义 `ClusterConnectionProfile`、`ClusterConnector` 和统一路径引用。
2. 建立本地 `cluster-access-sim` 与 SSH/SFTP 契约测试。
3. 增加异步 TransferTask、不可变制品和 Nginx Range 下载。
4. 把上传数据面迁移到 tusd，并保留短期兼容路由。
5. 实现 SFTP 集群传输、恢复、摘要和原子提交。
6. 增加 ClusterAsset、RunInputBinding 和提交依赖门禁。
7. 完成文件搜索、批量归档和前端任务展示。
8. 执行普通 CI、Docker 闭环和 5GB+ 发布专项。

每个增量都必须保持现有 command-gateway 与本地文件 API 可回归，直至同构 SSH/SFTP 主路径完成替换。

## 13. 参考标准与实现

- [RFC 9110：HTTP Semantics 与 Range Requests](https://www.rfc-editor.org/rfc/rfc9110.html)
- [RFC 6266：Content-Disposition](https://www.rfc-editor.org/info/rfc6266/)
- [tus resumable upload protocol](https://tus.io/protocols/resumable-upload)
- [tusd 官方参考实现](https://github.com/tus/tusd)
- [tusd hooks](https://tus.github.io/tusd/advanced-topics/hooks/)
- [OpenSSH sftp：reget、reput 与 fsync](https://man.openbsd.org/OpenBSD-7.5/sftp.1)
- [Nginx internal location](https://nginx.org/en/docs/http/ngx_http_core_module.html)
- [libarchive](https://www.libarchive.org/)
- [Python ZIP64 支持](https://docs.python.org/3/library/zipfile.html)
- [Slurm REST API 安全与认证](https://slurm.schedmd.com/rest.html)
- [Slurm API 选择建议](https://slurm.schedmd.com/api.html)
- [Slurm sbatch 与工作目录](https://slurm.schedmd.com/sbatch.html)
