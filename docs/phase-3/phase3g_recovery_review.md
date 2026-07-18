# Phase 3G 控制面恢复审查

日期：2026-07-18
范围：本机 SQLite、Evidence、Capsule 与可选 PostgreSQL 的冷备、完整性验证、空目录恢复及凭据边界。
结论：本恢复切片 P0/P1 已清零，冷备与隔离恢复演练通过；R4-3 仍需完成 PostgreSQL 业务接线、长期可观测性和其余本地安全基线。

## 固定的恢复契约

- create/restore 必须显式传入 `quiesced=True`；CLI 必须给出 `--quiesced`，表示 API/Worker 写入者已停止；
- SQLite 使用 online backup API 生成包含已提交 WAL 状态的独立快照，再执行 `quick_check` 与 `foreign_key_check`；
- Evidence/Capsule 只接受真实目录和普通文件，显式路径缺失、符号链接或特殊文件均 fail closed；
- manifest 记录 schema、backup ID、组件、逐文件大小与 SHA-256；verify 要求 payload 精确匹配 inventory；
- restore 先完整 verify，只允许不存在或空目录目标，不做原地覆盖，并通过同父目录 staging + rename 发布；
- PostgreSQL 使用 custom dump、`--no-owner`、`--no-privileges`；restore 使用 `--clean --if-exists`，因此核心 API 与 CLI 都要求显式 reset 确认；
- PostgreSQL DSN 不写 manifest、不进入子进程 argv；仅通过子进程 `PGDATABASE` 环境传递，工具错误中的原 DSN 被脱敏。

## Findings-first 结果

### 已修复 P1：跨 SQLite 与文件树的热备可能产生不一致时间点

SQLite snapshot 与 Evidence/Capsule copy 无法在活跃 Writer 下形成跨组件事务。create/restore 现在都要求调用方明确声明控制面已 quiesce；本机演练实际停止 API/Worker，完成后再启动并确认 healthy。

### 已修复 P1：显式组件缺失会被静默省略

早期草案把“没有配置组件”和“配置路径不存在”都解释为跳过，可能生成缺 Evidence/Capsule 的成功备份。现在只有 `None` 表示不包含组件；显式路径缺失立即失败。

### 已修复 P1：路径、链接与覆盖边界不足

verify 现在拒绝 `..`/绝对 manifest path、重复项、未列入 inventory 的 payload、digest/size 不符、symlink 和特殊文件。create 拒绝位于 Evidence/Capsule 源树内的递归目标；create/restore 拒绝目标 symlink；restore 拒绝 backup 子树与非空目录，失败不改变原目标。

### 已修复 P1：PostgreSQL 凭据和破坏性恢复确认只在 CLI 层

早期草案把 DSN 放在 `pg_dump`/`pg_restore` argv，且直接调用库函数可绕过 CLI reset 确认。现在 DSN 仅进入子进程环境，错误会替换原 DSN；`postgres_allow_reset=True` 已成为核心 restore 的强制条件。

## 自动验证

- 恢复专项：15 passed；覆盖 SQLite/Evidence/Capsule 往返、WAL、payload 篡改、路径穿越/退化路径、payload root symlink、非空目标、显式缺失组件、quiesce、递归目标、PostgreSQL reset/DSN、真实 CLI 往返；
- PostgreSQL adapter 负面测试证明 DSN 不在 argv，失败文本不含用户名/密码；
- 全量 Python 门禁：565 passed、10 PostgreSQL integration skipped、2 subtests passed；
- `ruff check .` 与 strict `mypy src` 通过，67 个 source files 无类型错误；
- `git diff --check` 通过。

## 本机冷备恢复演练

- 停止 `pilot107-sim-pilot107-api-1` 与 `pilot107-sim-pilot107-worker-1` 后，以只读方式挂载 `pilot107-sim_pilot107-data`；
- create 生成 `backup_032c48ee033d43a7ae658edc96e96cf8`：552 files、2,464,367 bytes，manifest SHA-256 `69789bdef2c98c64839b867a188dce01a760a2c2dbe6a4c7e4d0bec0b8dd2de1`；
- 独立 verify 返回 `verified=true`，随后 restore 到隔离临时目录；
- 源与恢复 SQLite 的 28 张业务表、1,209 行计数完全一致；394 个 Evidence 文件与 155 个 Capsule 文件逐文件 SHA-256 一致；
- API/Worker 重启后均恢复 healthy；演练副本随后从 `/tmp` 删除，运行数据卷未写入；
- 演练只使用本机 simulator，未连接真实 107，未上传或部署 VM。

## 运维入口

```bash
PYTHONPATH=src python scripts/control-plane-recovery.py create \
  --destination /safe/offline/backup \
  --sqlite-db /var/lib/pilot107/pilot107.db \
  --evidence-root /var/lib/pilot107/evidence \
  --capsule-root /var/lib/pilot107/capsules \
  --quiesced

PYTHONPATH=src python scripts/control-plane-recovery.py verify \
  --backup-root /safe/offline/backup

PYTHONPATH=src python scripts/control-plane-recovery.py restore \
  --backup-root /safe/offline/backup \
  --destination /new/empty/runtime-root \
  --quiesced
```

PostgreSQL restore 还必须同时给出 `--postgres-dsn` 与 `--postgres-allow-reset`。

## 残余风险与 R4-3 输入

1. manifest 本身未签名；create 返回的 manifest SHA-256 必须保存在备份目录之外，才能防御 payload 与 manifest 同时被改写，而不只是偶发损坏；
2. 本机未对真实 PostgreSQL 数据执行 pg tools 恢复，本切片只验证 fake adapter 与命令/凭据契约；完整业务 PostgreSQL Store parity 仍未完成；
3. quiesce 是显式运维确认而非自动分布式锁；R5 发布包必须把停止 Writer、验证健康与回滚写入 runbook；
4. restore 有意只支持新建/空目录，不提供原地覆盖；切换、旧目录保留和回退属于部署 runbook；
5. 长期 metrics/trace、告警、安全负面矩阵及 8C/16G 资源门禁仍属于后续 R4-3/R5。
