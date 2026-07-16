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

## 4. 可选人工确认动作

需要用户显式确认：

- REST submit smoke；
- cancel；
- 文件读取；
- Capsule 自动收集。

## 5. 禁止

- 不做无人值守 SSH command proxy；
- 不自动长期持有 JWT；
- 不假设应用节点挂载真实 `/public`；
- 不把真实 107 探测失败视为比赛系统失败。

