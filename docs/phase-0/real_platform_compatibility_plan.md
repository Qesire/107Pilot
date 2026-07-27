# Phase 0C：真实 107 非阻塞兼容探测

## 1. 定位

真实 107 平台用于：

```text
参考平台兼容目标
+ 可选只读探测
+ 少量真实作业验证
```

它不作为比赛系统运行所必需的生产依赖。

## 2. 已知资料事实

| 项 | 资料信息 |
|---|---|
| Slurm 版本 | Slurm 25.11 |
| 外部 REST 地址 | `http://107.ustc.edu.cn:6820` |
| 内部 REST 地址 | `tradmin-02:6820` |
| 示例 API 版本 | `v0.0.41` |
| 认证方式 | `Authorization: Bearer <token>` |
| Token 获取 | `scontrol token lifespan=86400` |
| 用户家目录 | `/public/home/<用户名>` |
| 共享存储 | `/public`，资料称也对应 `/home` |
| 节点本地目录 | `/tmp`、`/usr`、`/var`、`/opt` |
| 普通用户权限 | 不提供 sudo |
| 用户提交方式 | SCOW、SSH `sbatch`、REST API |

## 3. 初期只读探测

允许范围：

- ping；
- 查询当前用户作业；
- 查询分区；
- 查询单个 Job；
- 查询 accounting。

当用户提供测试专用 SSH alias 时，先使用已校验的探针包完成 CLI 快照，而不是直接运行
任意远程命令：

```bash
PILOT107_REAL107_SSH_TARGET=real107-login \
PILOT107_REAL107_PROBE_ARCHIVE=artifacts/probes/pilot107-real107-probe-<timestamp>.tar.gz \
bash scripts/probe-real107-ssh-cli.sh
```

该入口要求相邻的 SHA-256 文件匹配；远端只会在 `/tmp` 创建私有临时目录、解包探针并运行
固定的只读 CLI collector，随后拉回已脱敏的 `PlatformSnapshot`。它不会调用 `sbatch`、
`scancel` 或读取项目目录。默认会删除远端临时目录；仅当需要人工诊断时才设置
`PILOT107_REAL107_KEEP_REMOTE=1`。

## 4. 可选人工确认动作

需要用户显式确认：

- REST submit smoke；
- cancel；
- 文件读取；
- Capsule 自动收集。

当用户明确授权最小成功、失败与取消三类作业时，使用固定范围的 SSH 入口，而不是接受任意
远程脚本或命令：

```bash
PILOT107_REAL107_SSH_TARGET=pilot107-slurm \
PILOT107_REAL107_WORKDIR=<private-home>/pilot107-smoke-<label> \
bash scripts/smoke-real107-ssh-jobs.sh
```

该入口只会在提供的私有目录创建三份固定 sbatch 文件和它们的输出，使用
`stu/Students/qos_stu_default`、1 CPU、2 分钟时限。它验证 `COMPLETED/0:0`、
`FAILED/42:0`，并且只取消由自己记录的 sleep job；保留远端证据目录并复制到本地 artifact。

## 4.1 模拟器保真度环境清单

在用户明确授权 SSH 只读信息采集时，可运行固定环境清单：

```bash
PILOT107_REAL107_SSH_TARGET=pilot107-slurm \
bash scripts/probe-real107-ssh-environment.sh
```

它仅采集目录元数据与容量、挂载层级、选定的调度策略字段、QoS、程序可用性和进程资源
上限；不枚举目录、不读取项目文件、不读取环境变量，也不生成 JWT。采集结果用于更新
Docker 模拟器的资源几何、QoS、路径语义和调度器行为，SSH 仍不构成产品运行依赖。

## 5. 禁止

- 不做无人值守 SSH command proxy；
- 不自动长期持有 JWT；
- 不假设应用节点挂载真实 `/public`；
- 不把真实 107 探测失败视为比赛系统失败。
